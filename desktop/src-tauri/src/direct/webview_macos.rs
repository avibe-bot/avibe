//! macOS: the rendezvous request made by the instance's own WKWebView, and
//! the route that sends only that instance's host to the direct listener.
//!
//! Library functions only: which WebView, when, and for which instance is the
//! shell's decision. Everything here runs on the main thread, as WebKit
//! requires. Nothing is logged: a caller that logs keeps to families and
//! counts.

use std::{
    cell::RefCell,
    ffi::{c_char, c_void, CStr, CString},
    net::SocketAddrV4,
};

use block2::RcBlock;
use objc2::{
    msg_send,
    rc::Retained,
    runtime::{AnyObject, NSObject, NSObjectProtocol},
    sel, MainThreadMarker, Message,
};
use objc2_foundation::{NSArray, NSDictionary, NSError, NSString};
use objc2_web_kit::{WKContentWorld, WKWebView};

use super::rendezvous::{origin_of, script, Reply, Request, WORLD};

/// Asks the instance how to dial it, as a same-origin request of the
/// document `webview` shows, and gives `done` the outcome. `done` runs once,
/// on the main thread.
///
/// The request runs as a fixed script in a named `WKContentWorld` of its
/// own. That world shares the document and its cookies with the page, not
/// the page's JavaScript objects: page script can neither replace what the
/// script calls nor see what it reads, and the answer comes back through
/// WebKit's completion handler, never through a message handler a page
/// could call. The request therefore carries this WebView's signed-in
/// session and meets the instance's usual Origin and CSRF checks.
///
/// `current` is the shell's own word that the selection this request was
/// started for is still the one on show: the same session and the same
/// instance view. It is asked, with the document's exact origin, before the
/// request and again when the answer arrives. A selection that is no longer
/// current answers `SelectionChanged`, whatever the instance said. A
/// document that is not the instance's (the view is loading or reloading in
/// the same selection, or a navigation committed meanwhile) answers
/// `NotReady` before the request, and has a ready answer dropped after it.
pub fn fetch_rendezvous(
    webview: &WKWebView,
    mtm: MainThreadMarker,
    request: &Request,
    current: impl Fn() -> bool + 'static,
    done: impl FnOnce(Reply) + 'static,
) {
    // Whatever is delivered, and from whichever path, is delivered for the
    // selection as it is at that moment.
    let done = move |reply: Reply, current: &dyn Fn() -> bool| done(reply.for_selection(current()));
    if !current() || !shows(webview, request.origin()) {
        done(Reply::NotReady, &current);
        return;
    }
    let arguments = request.arguments();
    let keys: Vec<Retained<NSString>> = arguments.iter().map(|(key, _)| NSString::from_str(key)).collect();
    let values: Vec<Retained<AnyObject>> = arguments
        .iter()
        .map(|(_, value)| Retained::into_super(Retained::into_super(NSString::from_str(value))))
        .collect();
    let key_refs: Vec<&NSString> = keys.iter().map(|key| &**key).collect();
    let dictionary = NSDictionary::from_retained_objects(&key_refs, &values);

    let origin = request.origin().to_owned();
    let view = webview.retain();
    let done = RefCell::new(Some(done));
    let block = RcBlock::new(move |result: *mut AnyObject, error: *mut NSError| {
        let Some(done) = done.borrow_mut().take() else { return };
        let text = if error.is_null() && !result.is_null() {
            // SAFETY: WebKit passes a live object for the duration of the call.
            unsafe { &*result }
                .downcast_ref::<NSString>()
                .map(|value| value.to_string())
        } else {
            None
        };
        let reply = Reply::read(text.as_deref());
        // The selection may have changed while the request ran: nothing
        // the instance said is then this selection's any more (`done`). A
        // navigation may have committed meanwhile: a ready answer is then
        // not this document's to give.
        let reply = match reply {
            Reply::Ready(_) if !shows(&view, &origin) => Reply::Failed,
            other => other,
        };
        done(reply, &current);
    });
    // SAFETY: Main thread; every argument is a plain NSString, the frame is
    // the main frame, and the world is this adapter's own.
    unsafe {
        let world = WKContentWorld::worldWithName(&NSString::from_str(WORLD), mtm);
        webview.callAsyncJavaScript_arguments_inFrame_inContentWorld_completionHandler(
            &NSString::from_str(&script()),
            Some(&dictionary),
            None,
            &world,
            Some(&block),
        );
    }
}

/// Whether the top document `webview` shows has exactly `origin`.
fn shows(webview: &WKWebView, origin: &str) -> bool {
    // SAFETY: Main thread; reading the web view's current URL.
    unsafe { webview.URL() }
        .and_then(|url| url.absoluteString())
        .and_then(|url| origin_of(&url.to_string()))
        .is_some_and(|shown| shown == origin)
}

/// Sends the connections of `webview`'s data store to `host`, and only
/// those, to the listener at `proxy`, with the listener's own credential.
/// `false` when the system cannot (before macOS 14) or refuses: nothing is
/// then changed, and the instance stays on the tunnel.
///
/// The route lives in the data store's memory: it is not written to a URL,
/// an argument, a file or a preference, and WebKit gives a page no way to
/// read a proxy or its credential. The system matches `host` and the hosts
/// below it; the listener accepts `host` alone and refuses the rest, so a
/// child host does not load through it (Known-by-design, as on iPhone).
/// Failover stays allowed: with no listener the store connects as before.
///
/// This replaces whatever proxy configurations the store had. The shell
/// installs no direct route for an instance that has a configured proxy.
///
/// `true` means the store took the configuration, not that any connection
/// uses it. What WebKit was measured to do (`docs/plans/remote-direct-ios.md`
/// section 2, on iOS; not measured on macOS either way):
/// - a route written while the store has a live or pooled connection to the
///   host is not used by that connection, nor by requests pooled on it, a
///   reload included; only a connection opened later takes it;
/// - so a window is routed from its first request only when the route is
///   installed before the instance's first load.
pub fn install_route(
    webview: &WKWebView,
    _mtm: MainThreadMarker,
    host: &str,
    proxy: SocketAddrV4,
    username: &str,
    password: &str,
) -> bool {
    let Some(network) = Network::load() else { return false };
    let (Ok(host), Ok(address), Ok(port), Ok(username), Ok(password)) = (
        CString::new(host),
        CString::new(proxy.ip().to_string()),
        CString::new(proxy.port().to_string()),
        CString::new(username),
        CString::new(password),
    ) else {
        return false;
    };
    // SAFETY: The functions were found in this process under their own
    // names and have the signatures of <Network/proxy_config.h>; every
    // string is NUL-terminated and outlives its call; each object returned
    // is owned here and released when its `Retained` is dropped.
    let config = unsafe {
        let Some(endpoint) = Retained::from_raw((network.endpoint_create_host)(address.as_ptr(), port.as_ptr())) else {
            return false;
        };
        let Some(config) = Retained::from_raw((network.create_http_connect)(
            Retained::as_ptr(&endpoint).cast_mut(),
            std::ptr::null_mut(),
        )) else {
            return false;
        };
        let raw = Retained::as_ptr(&config).cast_mut();
        (network.add_match_domain)(raw, host.as_ptr());
        (network.set_username_and_password)(raw, username.as_ptr(), password.as_ptr());
        (network.set_failover_allowed)(raw, true);
        config
    };
    set_proxy_configurations(webview, &NSArray::from_retained_slice(&[config]))
}

/// Takes the route away again, for connections the store opens from now on.
/// Called before the direct client that owns the listener is dropped.
///
/// `true` means the store took the empty configuration. The same
/// measurements set its limits:
/// - emptying the route does not stop a proxy connection that is alive: the
///   store keeps using it until it closes, so dropping the client is what
///   ends those connections;
/// - after a NetworkProcess restart the emptied route came back. By then
///   the client is dropped and its port closed, and the route allows
///   failover, so the store connects on the tunnel.
pub fn remove_route(webview: &WKWebView, _mtm: MainThreadMarker) -> bool {
    set_proxy_configurations(webview, &NSArray::new())
}

fn set_proxy_configurations(webview: &WKWebView, configurations: &NSArray<NSObject>) -> bool {
    // SAFETY: Main thread. `proxyConfigurations` is an array of
    // `nw_proxy_config_t`, which are Objective-C objects; the setter is
    // sent only to a store that has it (macOS 14).
    unsafe {
        let store = webview.configuration().websiteDataStore();
        if !store.respondsToSelector(sel!(setProxyConfigurations:)) {
            return false;
        }
        let _: () = msg_send![&*store, setProxyConfigurations: configurations];
    }
    true
}

type EndpointCreateHost = unsafe extern "C" fn(*const c_char, *const c_char) -> *mut NSObject;
type CreateHttpConnect = unsafe extern "C" fn(*mut NSObject, *mut NSObject) -> *mut NSObject;
type AddMatchDomain = unsafe extern "C" fn(*mut NSObject, *const c_char);
type SetUsernameAndPassword = unsafe extern "C" fn(*mut NSObject, *const c_char, *const c_char);
type SetFailoverAllowed = unsafe extern "C" fn(*mut NSObject, bool);

/// The proxy-configuration functions of Network.framework, which exist from
/// macOS 14. They are looked up by name when a route is installed, so the
/// shell still starts on a system without them.
struct Network {
    endpoint_create_host: EndpointCreateHost,
    create_http_connect: CreateHttpConnect,
    add_match_domain: AddMatchDomain,
    set_username_and_password: SetUsernameAndPassword,
    set_failover_allowed: SetFailoverAllowed,
}

impl Network {
    fn load() -> Option<Self> {
        let endpoint_create_host = symbol(c"nw_endpoint_create_host")?;
        let create_http_connect = symbol(c"nw_proxy_config_create_http_connect")?;
        let add_match_domain = symbol(c"nw_proxy_config_add_match_domain")?;
        let set_username_and_password = symbol(c"nw_proxy_config_set_username_and_password")?;
        let set_failover_allowed = symbol(c"nw_proxy_config_set_failover_allowed")?;
        // SAFETY: Each symbol is the C function of that name in
        // <Network/endpoint.h> or <Network/proxy_config.h>, whose signature
        // is the type it is given here.
        unsafe {
            Some(Self {
                endpoint_create_host: std::mem::transmute::<*mut c_void, EndpointCreateHost>(endpoint_create_host),
                create_http_connect: std::mem::transmute::<*mut c_void, CreateHttpConnect>(create_http_connect),
                add_match_domain: std::mem::transmute::<*mut c_void, AddMatchDomain>(add_match_domain),
                set_username_and_password: std::mem::transmute::<*mut c_void, SetUsernameAndPassword>(
                    set_username_and_password,
                ),
                set_failover_allowed: std::mem::transmute::<*mut c_void, SetFailoverAllowed>(set_failover_allowed),
            })
        }
    }
}

fn symbol(name: &CStr) -> Option<*mut c_void> {
    extern "C" {
        fn dlsym(handle: *mut c_void, symbol: *const c_char) -> *mut c_void;
    }
    // `RTLD_DEFAULT`, from <dlfcn.h>: every image loaded in this process.
    // WebKit links Network.framework, so a process with a WKWebView has it.
    let default_handle = -2isize as *mut c_void;
    // SAFETY: `name` is NUL-terminated; a missing symbol is a null pointer.
    let found = unsafe { dlsym(default_handle, name.as_ptr()) };
    (!found.is_null()).then_some(found)
}
