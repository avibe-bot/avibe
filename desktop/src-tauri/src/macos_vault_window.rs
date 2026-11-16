//! Shows the Vault sandbox's own window inside the app.
//!
//! A protected Vault step ends in a top-level window of the sandbox
//! (`https://sandbox.avibe.bot`): the passkey is created there, and every
//! approval, signature and reveal is confirmed there. The window finds the
//! request through `window.opener`, so it has to be a real script-opened window
//! of the Workbench's webview. The shell otherwise sends every new window to
//! the system browser, where that link does not exist.
//!
//! For exactly this case the shell answers WebKit's request for a new webview
//! itself, before wry does, and builds the child by hand. wry's own child
//! shares the opener's `WKUserContentController` (the shell's init scripts and
//! the `ipc` message handler would run in it), has no navigation rule, and does
//! not close on `window.close()`.
//!
//! What holds for a child:
//!
//! - **Only the sandbox origin.** It is created for a request to the sandbox
//!   origin, or for `about:blank` (the Workbench reserves the window in the
//!   approval click and points it at the sandbox once the pinned release is
//!   verified). Every navigation in it, by the opener, the page, a redirect or
//!   a nested request for a window, is refused unless it stays on the sandbox
//!   origin. Every other request for a window still goes to wry, and so to the
//!   browser. The shell enforces the origin, not which sandbox release loads;
//!   pinning the release is the Workbench's check, as in a browser.
//! - **Never the opener's pixels.** The Workbench origin also serves
//!   Agent-authored Show Pages, and an opener can draw in the `about:blank` it
//!   reserved. The native window is created ordered out. When a sandbox
//!   document commits it comes on screen empty: its webview is transparent and
//!   it takes neither clicks nor keys. WebKit goes on displaying the previous
//!   document's layers until the new one has something to show, so the
//!   webview is revealed only by the first presentation update WebKit delivers
//!   after that commit, which carries the sandbox document's layers. From then
//!   on the document is cross-origin to the opener and can only be replaced by
//!   another sandbox document. A child that commits anything else is closed;
//!   one that has displayed nothing within [`RESERVE_DEADLINE`] is closed too.
//! - **Not without the user's attention.** WebKit on macOS lets scripts open
//!   windows without a user gesture, which a browser would require, so a page
//!   of the Workbench origin can ask for the sandbox's real window at any
//!   moment. The shell bounds that: a child comes on screen only while this app
//!   is active and the opener's window is on screen, only one at a time, and
//!   not within [`QUIET_AFTER_CLOSE`] of another leaving the screen; one that
//!   may not appear is closed, never kept for later. Its size is clamped. The
//!   title names the allowed host and ignores the page's own title, but it is
//!   not proof of identity: other native windows of this app can carry any
//!   title.
//! - **Nothing of the shell inside it.** It gets a `WKUserContentController` of
//!   its own with no scripts and no message handler, so nothing wry registered
//!   reaches it and closing it removes nothing from the opener, and default
//!   `WKPreferences` of its own with element fullscreen, picture in picture and
//!   video presentation modes off. WebKit also copies the opener's custom URL
//!   schemes (`ipc`, `tauri`) into the child's configuration and offers no way
//!   to remove them; wry's handler for them reads fields that only its own
//!   webview class has. Every such handler is therefore guarded: a request
//!   from a child fails before the handler runs. Tauri never learns the child
//!   exists, so it has no label, no capability and no page Tauri could
//!   classify.
//! - **Opener and lifetime.** The child is created from the configuration WebKit
//!   hands over, which carries the opener link and the opener's website data
//!   store, persistent or not. `window.close()` closes the native window;
//!   closing the native window destroys the page, which the opener sees as a
//!   closed popup. A committed navigation of the opener, or its window being
//!   closed or destroyed, closes its children.
//!
//! Three of these rest on WebKit methods outside its public API: the
//! presentation update (`_doAfterNextPresentationUpdate:`), destroying the page
//! (`_close`), and the feature list of `WKPreferences`. Each is checked to
//! exist before anything is installed; without one there is no child and no
//! marker.
//!
//! The Workbench learns that this works from
//! `window.__AVIBE_DESKTOP_VAULT_PASSKEY__`, defined only when the hooks above
//! are installed on this webview, the system is macOS 15 or later (the first
//! with passkey PRF), and the running app's own code signature associates it
//! with the sandbox's passkeys. Without all three nothing here is installed and
//! every window request is handled as before.

use std::cell::{Cell, RefCell};
use std::ffi::{c_char, c_void};
use std::ptr::NonNull;
use std::rc::Rc;
use std::sync::{Mutex, OnceLock};
use std::time::{Duration, Instant};

use block2::{DynBlock, RcBlock};
use objc2::rc::{Retained, Weak};
use objc2::runtime::{AnyClass, AnyObject, Bool, Imp, NSObject, ProtocolObject, Sel};
use objc2::{define_class, msg_send, sel, ClassType, DefinedClass, MainThreadMarker, MainThreadOnly};
use objc2_app_kit::{
    NSApplication, NSAutoresizingMaskOptions, NSBackingStoreType, NSView, NSWindow, NSWindowDelegate,
    NSWindowOrderingMode, NSWindowStyleMask,
};
use objc2_core_foundation::{CFRetained, CFString};
use objc2_foundation::{
    ns_string, NSArray, NSError, NSNotification, NSObjectProtocol, NSPoint, NSProcessInfo, NSRect, NSSize, NSString,
    NSURL,
};
use objc2_security::SecTask;
use objc2_web_kit::{
    WKFrameInfo, WKMediaCaptureType, WKNavigation, WKNavigationAction, WKNavigationActionPolicy, WKNavigationDelegate,
    WKNavigationResponse, WKNavigationResponsePolicy, WKPermissionDecision, WKPreferences, WKSecurityOrigin,
    WKUIDelegate, WKURLSchemeTask, WKUserContentController, WKUserScript, WKUserScriptInjectionTime, WKWebView,
    WKWebViewConfiguration, WKWindowFeatures,
};
use tauri::{AppHandle, Manager, WebviewWindow, WindowEvent};
use url::Url;

/// Tells the Workbench, before any of its scripts run, that protected Vault
/// passkey steps work inside this app. Top-level document only.
const DESKTOP_VAULT_PASSKEY_MARKER: &str =
    "if (window.self === window.top) Object.defineProperty(window, '__AVIBE_DESKTOP_VAULT_PASSKEY__', { value: true });";

/// The entitlement that lets WebKit ask the system for a site's passkeys.
const ASSOCIATED_DOMAINS: &str = "com.apple.developer.associated-domains";
const SANDBOX_CREDENTIALS: &str = "webcredentials:sandbox.avibe.bot";
/// Passkey PRF, which the protected tier derives its key from, starts here.
const FIRST_MACOS_WITH_PASSKEY_PRF: isize = 15;

/// How long a child may exist without displaying a sandbox document. The
/// Workbench verifies the pinned release (15 s at most) before it navigates a
/// reserved window, and the sandbox gives up pairing after 30 s.
const RESERVE_DEADLINE: Duration = Duration::from_secs(60);
/// The flows open one window at a time; this bounds what the main webview's
/// pages can hold, hidden ones included.
const MAX_CHILDREN: usize = 4;
/// After a child leaves the screen, no other appears for this long. Longer than
/// the default double-click interval (half a second), so a click meant for the
/// window that just closed never meets a new one in its place; shorter than a
/// person needs to go back to the Workbench, approve another request and have
/// its sandbox release verified and loaded.
const QUIET_AFTER_CLOSE: Duration = Duration::from_millis(750);
/// The size of a child's content, whatever the opener asks for.
const DEFAULT_SIZE: (f64, f64) = (520.0, 760.0);
const LEAST_SIZE: (f64, f64) = (360.0, 400.0);
const MOST_SIZE: (f64, f64) = (900.0, 1000.0);

/// The one origin a child may show.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct Origin {
    pub(crate) scheme: String,
    pub(crate) host: String,
    /// `None` is the scheme's default port.
    pub(crate) port: Option<u16>,
}

impl Origin {
    fn sandbox() -> Self {
        Self {
            scheme: "https".to_owned(),
            host: "sandbox.avibe.bot".to_owned(),
            port: None,
        }
    }

    fn holds(&self, url: &Url) -> bool {
        url.scheme() == self.scheme
            && url.host_str() == Some(self.host.as_str())
            && url.port() == self.port
            && url.username().is_empty()
            && url.password().is_none()
    }

    /// What the window's title bar says: where its content comes from.
    fn title(&self) -> String {
        match self.port {
            Some(port) => format!("{}:{port}", self.host),
            None => self.host.clone(),
        }
    }
}

#[derive(Clone)]
pub(crate) struct Policy {
    pub(crate) origin: Origin,
    /// How long a child may exist without displaying a document of `origin`.
    pub(crate) reserve_deadline: Duration,
}

/// What the opener asked a new window for.
#[derive(Debug, PartialEq, Eq)]
enum Request {
    /// A sandbox document.
    Sandbox,
    /// The blank window reserved ahead of one.
    Reserve,
    /// Anything else stays with wry, which hands web links to the browser.
    Other,
}

fn classify(url: Option<&Url>, origin: &Origin) -> Request {
    match url {
        Some(url) if origin.holds(url) => Request::Sandbox,
        Some(url) if is_blank(url) => Request::Reserve,
        _ => Request::Other,
    }
}

fn is_blank(url: &Url) -> bool {
    url.as_str() == "about:blank"
}

/// Whether a child may start a navigation. `committed` is whether it has shown
/// a sandbox document; before that its main frame may still hold the reserved
/// blank page.
fn child_may_navigate(
    origin: &Origin,
    url: Option<&Url>,
    new_context: bool,
    download: bool,
    main_frame: bool,
    committed: bool,
) -> bool {
    if new_context || download {
        return false;
    }
    url.is_some_and(|url| origin.holds(url) || (main_frame && !committed && is_blank(url)))
}

#[derive(Debug, PartialEq, Eq)]
enum Commit {
    /// A sandbox document: the window may be on screen.
    Show,
    /// Still the reserved blank page: the window stays hidden.
    Wait,
    /// Anything else must never be displayed.
    Close,
}

fn commit_outcome(origin: &Origin, url: Option<&Url>, committed: bool) -> Commit {
    match url {
        Some(url) if origin.holds(url) => Commit::Show,
        Some(url) if !committed && is_blank(url) => Commit::Wait,
        _ => Commit::Close,
    }
}

/// What decides whether a child whose sandbox document just committed may come
/// on screen. WebKit lets scripts open windows without a user gesture, so these
/// stand in for the attention a browser would demand.
#[derive(Clone, Copy, Debug)]
struct Attention {
    /// The user is in this app.
    app_active: bool,
    /// The opener's window is on screen, not hidden or minimized.
    opener_on_screen: bool,
    /// Another child of this opener is on screen.
    sibling_on_screen: bool,
    /// How long ago a child of this opener last left the screen.
    since_sibling_left: Option<Duration>,
}

/// A child that may not appear is closed, never kept to appear later.
fn may_appear(attention: Attention) -> bool {
    attention.app_active
        && attention.opener_on_screen
        && !attention.sibling_on_screen
        && attention
            .since_sibling_left
            .is_none_or(|since| since >= QUIET_AFTER_CLOSE)
}

/// The content size of a child for the size its opener asked for.
fn child_size(width: Option<f64>, height: Option<f64>) -> (f64, f64) {
    let clamp = |asked: Option<f64>, fallback: f64, least: f64, most: f64| match asked {
        Some(asked) if asked.is_finite() => asked.clamp(least, most),
        _ => fallback,
    };
    (
        clamp(width, DEFAULT_SIZE.0, LEAST_SIZE.0, MOST_SIZE.0),
        clamp(height, DEFAULT_SIZE.1, LEAST_SIZE.1, MOST_SIZE.1),
    )
}

/// Whether this build may tell the Workbench that Vault passkeys work in-app.
fn advertises_passkeys(macos_major: isize, associated_domains: Option<&[String]>) -> bool {
    macos_major >= FIRST_MACOS_WITH_PASSKEY_PRF
        && associated_domains.is_some_and(|domains| domains.iter().any(|domain| domain == SANDBOX_CREDENTIALS))
}

/// The associated domains in the running process's own code signature, as the
/// kernel holds them. A build flag would also be true for an ad-hoc or
/// development build, where WebKit is refused the passkey.
fn signed_associated_domains() -> Option<Vec<String>> {
    // SAFETY: Both calls take the current task and a constant name; the error
    // out-parameter is optional.
    let value = unsafe {
        let task = SecTask::from_self(None)?;
        task.value_for_entitlement(&CFString::from_static_str(ASSOCIATED_DOMAINS), std::ptr::null_mut())?
    };
    // SAFETY: A Core Foundation object is an Objective-C object, and the
    // ownership `CFRetained` held moves to `Retained`. Its type is checked next.
    let value: Retained<AnyObject> = unsafe { Retained::from_raw(CFRetained::into_raw(value).as_ptr().cast())? };
    let domains = value.downcast::<NSArray>().ok()?;
    Some(
        domains
            .iter()
            .filter_map(|domain| domain.downcast_ref::<NSString>().map(ToString::to_string))
            .collect(),
    )
}

/// Makes the main window able to show the sandbox's windows, and says so to the
/// Workbench, when this system and this build can complete a passkey there.
pub fn install(window: &WebviewWindow) {
    let macos_major = NSProcessInfo::processInfo().operatingSystemVersion().majorVersion;
    if !advertises_passkeys(macos_major, signed_associated_domains().as_deref()) {
        return;
    }
    install_with(
        window,
        Policy {
            origin: Origin::sandbox(),
            reserve_deadline: RESERVE_DEADLINE,
        },
    );
}

/// [`install`] without its admission, for an origin the caller names. The
/// shell's only caller is `install`. The hermetic WebView probe compiles this
/// file into itself and enters here with a loopback origin, because it can
/// neither serve `https://sandbox.avibe.bot` nor carry the entitlement.
pub(crate) fn install_with(window: &WebviewWindow, policy: Policy) {
    let app = window.app_handle().clone();
    let events = window.clone();
    let _ = window.with_webview(move |platform| {
        let Some(mtm) = MainThreadMarker::new() else {
            return;
        };
        // SAFETY: Tauri hands out the live WKWebView, on the main thread, for
        // the duration of this closure.
        let Some(webview) = (unsafe { Retained::retain(platform.inner().cast::<WKWebView>()) }) else {
            return;
        };
        let key = Retained::as_ptr(&webview) as usize;
        let installed = STATE.with_borrow(|state| {
            state
                .openers
                .iter()
                .any(|opener| opener.key == key && opener.webview.load().is_some())
        });
        if installed {
            return;
        }
        if !adopt(&webview) {
            eprintln!("the webview changed shape; Vault sandbox windows stay in the system browser");
            return;
        }
        STATE.with_borrow_mut(|state| {
            state.policy.get_or_insert(policy);
            state
                .openers
                .retain(|opener| opener.key != key && opener.webview.load().is_some());
            state.openers.push(Opener {
                key,
                webview: Weak::from_retained(&webview),
                app: app.clone(),
                child_left_screen: None,
            });
        });
        // Only now is the claim true for this webview. Its controller is the
        // one wry registered the other markers on.
        // SAFETY: Main thread; the script is a constant.
        unsafe {
            let marker = WKUserScript::initWithSource_injectionTime_forMainFrameOnly(
                mtm.alloc(),
                &NSString::from_str(DESKTOP_VAULT_PASSKEY_MARKER),
                WKUserScriptInjectionTime::AtDocumentStart,
                true,
            );
            webview.configuration().userContentController().addUserScript(&marker);
        }
        events.on_window_event(move |event| {
            // The shell hides its window when the user closes it; either way
            // the children go with it.
            let gone = match event {
                WindowEvent::CloseRequested { .. } => false,
                WindowEvent::Destroyed => true,
                _ => return,
            };
            if MainThreadMarker::new().is_some() {
                opener_closed(key, gone);
            } else {
                let _ = app.run_on_main_thread(move || opener_closed(key, gone));
            }
        });
    });
}

/// The opener committed a new document: the one that opened its children is
/// gone, and so is the request they were answering.
pub fn opener_navigated(window: &WebviewWindow) {
    let _ = window.with_webview(|platform| {
        if MainThreadMarker::new().is_some() {
            close_children_of(platform.inner() as usize);
        }
    });
}

/// The opener's window was closed by the user (`gone` is false: the shell
/// keeps it, hidden) or destroyed.
fn opener_closed(key: usize, gone: bool) {
    close_children_of(key);
    if gone {
        STATE.with_borrow_mut(|state| state.openers.retain(|opener| opener.key != key));
    }
}

fn close_children_of(opener: usize) {
    let children: Vec<u64> = STATE.with_borrow(|state| {
        state
            .children
            .iter()
            .filter(|child| child.opener == opener)
            .map(|child| child.id)
            .collect()
    });
    for id in children {
        close(id, false);
    }
}

struct Opener {
    /// The webview's address, which is how WebKit names it in callbacks.
    key: usize,
    /// Proves the address still belongs to the webview that was installed.
    webview: Weak<WKWebView>,
    app: AppHandle,
    /// When a child of this opener last left the screen.
    child_left_screen: Option<Instant>,
}

struct Child {
    id: u64,
    opener: usize,
    window: Retained<SandboxWindow>,
    webview: Retained<SandboxWebView>,
    delegate: Retained<ChildDelegate>,
    /// A sandbox document has committed. Before that the main frame may hold
    /// the reserved blank page; after it, only sandbox documents.
    committed: bool,
    /// The native window is on screen. Its webview stays transparent, and the
    /// window takes neither clicks nor keys, until the sandbox document has
    /// been presented.
    on_screen: bool,
}

#[derive(Default)]
struct State {
    policy: Option<Policy>,
    openers: Vec<Opener>,
    children: Vec<Child>,
    next_id: u64,
}

thread_local! {
    /// Main thread only: every entry point is a WebKit or AppKit callback.
    static STATE: RefCell<State> = RefCell::new(State::default());
}

define_class!(
    /// A child's webview. The class is what the URL scheme guard recognizes.
    #[unsafe(super(WKWebView))]
    #[thread_kind = MainThreadOnly]
    #[name = "AvibeVaultSandboxWebView"]
    struct SandboxWebView;
);

struct WindowFields {
    /// The sandbox document has been presented; see `appear` and `reveal`.
    revealed: Cell<bool>,
}

define_class!(
    /// A child's window. Until its page is displayed it cannot take the
    /// keyboard, so no key can reach a page the user cannot see.
    #[unsafe(super(NSWindow))]
    #[thread_kind = MainThreadOnly]
    #[name = "AvibeVaultSandboxWindow"]
    #[ivars = WindowFields]
    struct SandboxWindow;

    impl SandboxWindow {
        #[unsafe(method(canBecomeKeyWindow))]
        fn can_become_key(&self) -> bool {
            self.ivars().revealed.get()
        }

        #[unsafe(method(canBecomeMainWindow))]
        fn can_become_main(&self) -> bool {
            self.ivars().revealed.get()
        }
    }
);

struct ChildFields {
    id: u64,
}

define_class!(
    #[unsafe(super(NSObject))]
    #[thread_kind = MainThreadOnly]
    #[name = "AvibeVaultSandboxWindowDelegate"]
    #[ivars = ChildFields]
    struct ChildDelegate;

    unsafe impl NSObjectProtocol for ChildDelegate {}

    unsafe impl WKNavigationDelegate for ChildDelegate {
        #[unsafe(method(webView:decidePolicyForNavigationAction:decisionHandler:))]
        fn decide_action(
            &self,
            _webview: &WKWebView,
            action: &WKNavigationAction,
            handler: &DynBlock<dyn Fn(WKNavigationActionPolicy)>,
        ) {
            // SAFETY: WebKit calls this on the main thread with a live action.
            let (url, target_is_main, download) = unsafe {
                let download = action.respondsToSelector(sel!(shouldPerformDownload)) && action.shouldPerformDownload();
                let target_is_main = action.targetFrame().map(|frame| frame.isMainFrame());
                (nsurl(action.request().URL()), target_is_main, download)
            };
            let allowed = self.child_state().is_some_and(|(origin, committed)| {
                child_may_navigate(
                    &origin,
                    url.as_ref(),
                    target_is_main.is_none(),
                    download,
                    target_is_main == Some(true),
                    committed,
                )
            });
            handler.call((if allowed {
                WKNavigationActionPolicy::Allow
            } else {
                WKNavigationActionPolicy::Cancel
            },));
        }

        #[unsafe(method(webView:decidePolicyForNavigationResponse:decisionHandler:))]
        fn decide_response(
            &self,
            _webview: &WKWebView,
            response: &WKNavigationResponse,
            handler: &DynBlock<dyn Fn(WKNavigationResponsePolicy)>,
        ) {
            // The response is where a redirect ended up. Nothing is downloaded.
            // SAFETY: WebKit calls this on the main thread with a live response.
            let (url, showable) = unsafe { (nsurl(response.response().URL()), response.canShowMIMEType()) };
            let allowed = showable
                && self
                    .child_state()
                    .is_some_and(|(origin, _)| url.is_some_and(|url| origin.holds(&url)));
            handler.call((if allowed {
                WKNavigationResponsePolicy::Allow
            } else {
                WKNavigationResponsePolicy::Cancel
            },));
        }

        #[unsafe(method(webView:didCommitNavigation:))]
        fn did_commit(&self, webview: &WKWebView, _navigation: Option<&WKNavigation>) {
            // SAFETY: Main thread; the webview is live for this call.
            let url = unsafe { nsurl(webview.URL()) };
            committed(self.ivars().id, url.as_ref());
        }

        #[unsafe(method(webViewWebContentProcessDidTerminate:))]
        fn process_terminated(&self, _webview: &WKWebView) {
            close(self.ivars().id, false);
        }
    }

    unsafe impl WKUIDelegate for ChildDelegate {
        /// The sandbox's window never opens another.
        #[unsafe(method_id(webView:createWebViewWithConfiguration:forNavigationAction:windowFeatures:))]
        fn create_web_view(
            &self,
            _webview: &WKWebView,
            _configuration: &WKWebViewConfiguration,
            _action: &WKNavigationAction,
            _features: &WKWindowFeatures,
        ) -> Option<Retained<WKWebView>> {
            None
        }

        /// The sandbox's window uses neither camera nor microphone, and
        /// WebKit's own prompt must never come up for a window that is hidden.
        #[unsafe(method(webView:requestMediaCapturePermissionForOrigin:initiatedByFrame:type:decisionHandler:))]
        fn media_capture(
            &self,
            _webview: &WKWebView,
            _origin: &WKSecurityOrigin,
            _frame: &WKFrameInfo,
            _kind: WKMediaCaptureType,
            handler: &DynBlock<dyn Fn(WKPermissionDecision)>,
        ) {
            handler.call((WKPermissionDecision::Deny,));
        }

        /// `window.close()`, from the page or from its opener.
        #[unsafe(method(webViewDidClose:))]
        fn did_close(&self, _webview: &WKWebView) {
            close(self.ivars().id, false);
        }
    }

    unsafe impl NSWindowDelegate for ChildDelegate {
        /// The user closed the window.
        #[unsafe(method(windowWillClose:))]
        fn will_close(&self, _notification: &NSNotification) {
            close(self.ivars().id, true);
        }
    }
);

impl ChildDelegate {
    fn new(mtm: MainThreadMarker, id: u64) -> Retained<Self> {
        let this = Self::alloc(mtm).set_ivars(ChildFields { id });
        // SAFETY: `NSObject`'s designated initializer.
        unsafe { msg_send![super(this), init] }
    }

    /// The allowed origin and whether this child has shown a sandbox document;
    /// `None` once the child is closed.
    fn child_state(&self) -> Option<(Origin, bool)> {
        let id = self.ivars().id;
        STATE.with_borrow(|state| {
            let child = state.children.iter().find(|child| child.id == id)?;
            Some((state.policy.as_ref()?.origin.clone(), child.committed))
        })
    }
}

fn nsurl(url: Option<Retained<NSURL>>) -> Option<Url> {
    url.and_then(|url| url.absoluteString())
        .and_then(|url| Url::parse(&url.to_string()).ok())
}

/// A document committed in a child's main frame.
fn committed(id: u64, url: Option<&Url>) {
    let outcome = STATE.with_borrow_mut(|state| {
        let origin = state.policy.as_ref()?.origin.clone();
        let child = state.children.iter_mut().find(|child| child.id == id)?;
        let outcome = commit_outcome(&origin, url, child.committed);
        let first = outcome == Commit::Show && !child.committed;
        if outcome == Commit::Show {
            child.committed = true;
        }
        Some((outcome, first))
    });
    match outcome {
        Some((Commit::Close, _)) => close(id, false),
        Some((Commit::Show, true)) => appear(id),
        _ => {}
    }
}

/// Puts a child whose first sandbox document just committed on screen over its
/// opener's window, or closes it when it may not appear now.
///
/// What comes on screen is the window with a transparent webview, taking no
/// input. WebKit keeps displaying the previous document's layers until the new
/// one has something to show, and the previous document here is a blank page
/// its opener could draw in. The webview is revealed by the first presentation
/// update WebKit delivers after this point, which carries the sandbox
/// document's layers.
fn appear(id: u64) {
    let Some(mtm) = MainThreadMarker::new() else {
        return;
    };
    let found = STATE.with_borrow(|state| {
        let child = state.children.iter().find(|child| child.id == id)?;
        let opener = state.openers.iter().find(|opener| opener.key == child.opener)?;
        let sibling_on_screen = state
            .children
            .iter()
            .any(|other| other.opener == child.opener && other.id != id && other.on_screen);
        Some((
            child.window.clone(),
            child.webview.clone(),
            opener.webview.load(),
            sibling_on_screen,
            opener.child_left_screen.map(|left| left.elapsed()),
        ))
    });
    let Some((window, webview, opener, sibling_on_screen, since_sibling_left)) = found else {
        close(id, false);
        return;
    };
    let parent = opener.and_then(|opener| opener.window());
    let attention = Attention {
        app_active: NSApplication::sharedApplication(mtm).isActive(),
        opener_on_screen: parent
            .as_ref()
            .is_some_and(|parent| parent.isVisible() && !parent.isMiniaturized()),
        sibling_on_screen,
        since_sibling_left,
    };
    let Some(parent) = parent.filter(|_| may_appear(attention)) else {
        close(id, false);
        return;
    };
    STATE.with_borrow_mut(|state| {
        if let Some(child) = state.children.iter_mut().find(|child| child.id == id) {
            child.on_screen = true;
        }
    });
    let over = parent.frame();
    let size = window.frame().size;
    window.setFrameOrigin(NSPoint::new(
        over.origin.x + (over.size.width - size.width) / 2.0,
        over.origin.y + (over.size.height - size.height) / 2.0,
    ));
    // Attaching orders the window in above its parent, without making it key.
    // SAFETY: Both windows are live and distinct; the child is detached again
    // before it closes.
    unsafe { parent.addChildWindow_ordered(&window, NSWindowOrderingMode::Above) };
    let presented = RcBlock::new(move || reveal(id));
    // SAFETY: `adopt` checked that the webview class has this method, which
    // takes one block and calls it on the main thread.
    let _: () = unsafe { msg_send![&*webview, _doAfterNextPresentationUpdate: &*presented] };
}

/// The sandbox document is what the webview now displays: show it, and give
/// the window the keyboard, which a passkey ceremony needs.
fn reveal(id: u64) {
    let found = STATE.with_borrow(|state| {
        let child = state.children.iter().find(|child| child.id == id)?;
        (child.committed && child.on_screen).then(|| (child.window.clone(), child.webview.clone()))
    });
    let Some((window, webview)) = found else {
        return;
    };
    window.ivars().revealed.set(true);
    webview.setAlphaValue(1.0);
    window.setIgnoresMouseEvents(false);
    window.makeKeyAndOrderFront(None);
    window.makeFirstResponder(Some(&webview));
}

/// Ends a child: the page is destroyed, which its opener sees as a closed
/// popup, and the native window closes. `window_closing` is set when AppKit is
/// already closing the window.
fn close(id: u64, window_closing: bool) {
    let child = STATE.with_borrow_mut(|state| {
        let index = state.children.iter().position(|child| child.id == id)?;
        let child = state.children.remove(index);
        if child.on_screen {
            if let Some(opener) = state.openers.iter_mut().find(|opener| opener.key == child.opener) {
                opener.child_left_screen = Some(Instant::now());
            }
        }
        Some(child)
    });
    let Some(child) = child else {
        return;
    };
    // SAFETY: Main thread; every object is live and owned by `child`. `adopt`
    // checked that the webview class has `_close`, which takes no argument.
    unsafe {
        child.webview.setNavigationDelegate(None);
        child.webview.setUIDelegate(None);
        // Destroys the page now. Releasing the webview would do the same, but
        // WebKit keeps it alive while a presentation update is still awaited.
        let _: () = msg_send![&*child.webview, _close];
    }
    child.window.setDelegate(None);
    let was_key = child.window.isKeyWindow();
    let parent = child.window.parentWindow();
    if let Some(parent) = &parent {
        parent.removeChildWindow(&child.window);
    }
    if !window_closing {
        child.window.close();
    }
    child.window.makeFirstResponder(None);
    child.webview.removeFromSuperview();
    if was_key {
        if let Some(parent) = &parent {
            parent.makeKeyWindow();
        }
    }
    // WebKit or AppKit may be in the middle of a callback on these objects.
    // They are released when the current event has been handled.
    let _ = Retained::autorelease_ptr(child.webview);
    let _ = Retained::autorelease_ptr(child.window);
    let _ = Retained::autorelease_ptr(child.delegate);
}

type CreateImp = unsafe extern "C-unwind" fn(
    &AnyObject,
    Sel,
    &WKWebView,
    &WKWebViewConfiguration,
    &WKNavigationAction,
    &WKWindowFeatures,
) -> *mut WKWebView;

/// wry's answer to a request for a new webview, which the shell defers to for
/// everything that is not a sandbox window, and the delegate class it came from.
static WRY_CREATE: OnceLock<(usize, CreateImp)> = OnceLock::new();

/// Puts the shell in front of wry for this webview's new-window requests and
/// guards the custom URL schemes a child would inherit. Returns whether both
/// hold; a child is never created otherwise.
fn adopt(webview: &WKWebView) -> bool {
    // SAFETY: Main thread; the delegate and the configuration are live objects.
    unsafe {
        let Some(delegate) = webview.UIDelegate() else {
            return false;
        };
        let delegate: &AnyObject = delegate.as_ref();
        let class = delegate.class() as *const AnyClass as usize;
        let selector = sel!(webView:createWebViewWithConfiguration:forNavigationAction:windowFeatures:);
        // wry defines one UI delegate class for every webview it builds, so the
        // first webview adopts it for the process.
        let hooked = WRY_CREATE.get_or_init(|| {
            let Some(method) = delegate.class().instance_method(selector) else {
                return (0, create_web_view as CreateImp);
            };
            // SAFETY: The selector fixes the signature of both implementations.
            let wry_create = std::mem::transmute::<Imp, CreateImp>(method.implementation());
            method.set_implementation(std::mem::transmute::<CreateImp, Imp>(create_web_view));
            (class, wry_create)
        });
        if hooked.0 != class {
            return false;
        }
        // A child is revealed by WebKit's presentation update and by nothing
        // else, and its page is destroyed by `_close`; without either method
        // there is no child.
        if !webview.respondsToSelector(sel!(_doAfterNextPresentationUpdate:))
            || !webview.respondsToSelector(sel!(_close))
        {
            return false;
        }
        let Some(mtm) = MainThreadMarker::new() else {
            return false;
        };
        if child_preferences(mtm).is_none() {
            return false;
        }
        if !guard_url_schemes() {
            return false;
        }
        // Tauri registers `ipc` on every webview. If the guard does not cover
        // its handler, the guard did not find what a child would inherit.
        let Some(ipc) = webview.configuration().urlSchemeHandlerForURLScheme(ns_string!("ipc")) else {
            return false;
        };
        let ipc: &AnyObject = ipc.as_ref();
        [sel!(webView:startURLSchemeTask:), sel!(webView:stopURLSchemeTask:)]
            .into_iter()
            .all(|selector| {
                ipc.class()
                    .instance_method(selector)
                    .is_some_and(|method| scheme_original(method as *const _ as usize).is_some())
            })
    }
}

unsafe extern "C-unwind" fn create_web_view(
    delegate: &AnyObject,
    selector: Sel,
    opener: &WKWebView,
    configuration: &WKWebViewConfiguration,
    action: &WKNavigationAction,
    features: &WKWindowFeatures,
) -> *mut WKWebView {
    // SAFETY: WebKit calls this on the main thread with live arguments.
    let url = unsafe { nsurl(action.request().URL()) };
    let key = opener as *const WKWebView as usize;
    let admitted = STATE.with_borrow(|state| {
        let opener = state.openers.iter().find(|opener| opener.key == key)?;
        opener.webview.load()?;
        let policy = state.policy.clone()?;
        match classify(url.as_ref(), &policy.origin) {
            Request::Other => None,
            Request::Sandbox | Request::Reserve => {
                Some((state.children.len() < MAX_CHILDREN).then(|| (policy, opener.app.clone())))
            }
        }
    });
    match admitted {
        // Not this module's request: wry sends it to the browser or drops it.
        None => {
            let (_, wry_create) = WRY_CREATE.get().expect("installed before it can be called");
            // SAFETY: The arguments are the ones WebKit passed for this selector.
            unsafe { wry_create(delegate, selector, opener, configuration, action, features) }
        }
        Some(None) => std::ptr::null_mut(),
        Some(Some((policy, app))) => match open_child(key, configuration, features, &policy, app) {
            Some(webview) => Retained::autorelease_return(Retained::into_super(webview)),
            None => std::ptr::null_mut(),
        },
    }
}

/// Builds a child, hidden, from the configuration WebKit prepared for it.
fn open_child(
    opener: usize,
    configuration: &WKWebViewConfiguration,
    features: &WKWindowFeatures,
    policy: &Policy,
    app: AppHandle,
) -> Option<Retained<SandboxWebView>> {
    let mtm = MainThreadMarker::new()?;
    // SAFETY: Main thread; `configuration` is WebKit's copy of the opener's,
    // made for this request.
    let (window, webview) = unsafe {
        // The copy still points at the opener's controller, with every script
        // and message handler wry registered, and at the opener's preferences.
        // The child gets an empty controller and preferences of its own.
        let shell_controller = configuration.userContentController();
        let shell_preferences = configuration.preferences();
        let empty = WKUserContentController::new(mtm);
        configuration.setUserContentController(&empty);
        let preferences = child_preferences(mtm)?;
        configuration.setPreferences(&preferences);
        let (width, height) = child_size(
            features.width().map(|width| width.doubleValue()),
            features.height().map(|height| height.doubleValue()),
        );
        let frame = NSRect::new(NSPoint::new(0.0, 0.0), NSSize::new(width, height));
        let webview: Retained<SandboxWebView> = msg_send![
            super(SandboxWebView::alloc(mtm).set_ivars(())),
            initWithFrame: frame,
            configuration: configuration
        ];
        let held = webview.configuration();
        let (held_controller, held_preferences) = (held.userContentController(), held.preferences());
        if !std::ptr::eq(&*held_controller, &*empty)
            || std::ptr::eq(&*held_controller, &*shell_controller)
            || std::ptr::eq(&*held_preferences, &*shell_preferences)
            || held_preferences.isElementFullscreenEnabled()
        {
            return None;
        }
        let window: Retained<SandboxWindow> = msg_send![
            super(SandboxWindow::alloc(mtm).set_ivars(WindowFields {
                revealed: Cell::new(false),
            })),
            initWithContentRect: frame,
            styleMask: NSWindowStyleMask::Titled | NSWindowStyleMask::Closable | NSWindowStyleMask::Resizable,
            backing: NSBackingStoreType::Buffered,
            defer: false
        ];
        // This module owns the window; AppKit must not release it on close.
        window.setReleasedWhenClosed(false);
        let _: () = msg_send![&window, setRestorable: false];
        (window, webview)
    };
    // The title names the only origin the window can load. It is the page's
    // title that is ignored; it is not proof of what the window is.
    window.setTitle(&NSString::from_str(&policy.origin.title()));
    // Until the sandbox document has been presented, the webview draws
    // nothing and the window passes clicks through. See `appear`.
    webview.setAlphaValue(0.0);
    webview.setAutoresizingMask(
        NSAutoresizingMaskOptions::ViewWidthSizable | NSAutoresizingMaskOptions::ViewHeightSizable,
    );
    window.setIgnoresMouseEvents(true);
    let content = NSView::initWithFrame(mtm.alloc(), webview.frame());
    content.addSubview(&webview);
    window.setContentView(Some(&content));
    let id = STATE.with_borrow_mut(|state| {
        state.next_id += 1;
        state.next_id
    });
    let delegate = ChildDelegate::new(mtm, id);
    // SAFETY: Main thread; the delegate is kept alive by the child's entry.
    unsafe {
        webview.setNavigationDelegate(Some(ProtocolObject::from_ref(&*delegate)));
        webview.setUIDelegate(Some(ProtocolObject::from_ref(&*delegate)));
    }
    window.setDelegate(Some(ProtocolObject::from_ref(&*delegate)));
    STATE.with_borrow_mut(|state| {
        state.children.push(Child {
            id,
            opener,
            window,
            webview: webview.clone(),
            delegate,
            committed: false,
            on_screen: false,
        });
    });
    let deadline = policy.reserve_deadline;
    tauri::async_runtime::spawn(async move {
        tokio::time::sleep(deadline).await;
        let _ = app.run_on_main_thread(move || {
            let waiting = STATE.with_borrow(|state| {
                state
                    .children
                    .iter()
                    .any(|child| child.id == id && !child.window.ivars().revealed.get())
            });
            if waiting {
                close(id, false);
            }
        });
    });
    Some(webview)
}

/// WebKit features a child is created without: the sandbox's window uses
/// neither picture in picture nor a video presentation mode, and both can put
/// content on screen outside the window.
const FEATURES_OFF: [&str; 3] = [
    "AllowsPictureInPictureMediaPlayback",
    "PictureInPictureAPIEnabled",
    "VideoPresentationModeAPIEnabled",
];

/// Default preferences, not the opener's, with element fullscreen and the
/// features above off. `None` when WebKit does not confirm each one is off;
/// a child is not created then.
fn child_preferences(mtm: MainThreadMarker) -> Option<Retained<WKPreferences>> {
    // SAFETY: Main thread. The feature list and its two accessors are WebKit's
    // own, checked to exist before they are called.
    unsafe {
        let preferences = WKPreferences::new(mtm);
        preferences.setElementFullscreenEnabled(false);
        if preferences.isElementFullscreenEnabled()
            || WKPreferences::class().class_method(sel!(_features)).is_none()
            || !preferences.respondsToSelector(sel!(_setEnabled:forFeature:))
            || !preferences.respondsToSelector(sel!(_isEnabledForFeature:))
        {
            return None;
        }
        let features: Retained<NSArray> = msg_send![WKPreferences::class(), _features];
        let mut off = 0;
        for feature in features.iter() {
            let key: Option<Retained<NSString>> = msg_send![&*feature, key];
            let Some(key) = key.map(|key| key.to_string()) else {
                continue;
            };
            if !FEATURES_OFF.contains(&key.as_str()) {
                continue;
            }
            let _: () = msg_send![&*preferences, _setEnabled: false, forFeature: &*feature];
            let enabled: bool = msg_send![&*preferences, _isEnabledForFeature: &*feature];
            if enabled {
                return None;
            }
            off += 1;
        }
        (off == FEATURES_OFF.len()).then_some(preferences)
    }
}

type SchemeImp = unsafe extern "C-unwind" fn(&AnyObject, Sel, &WKWebView, &ProtocolObject<dyn WKURLSchemeTask>);

/// The implementation each guarded URL scheme method had, by method address.
static SCHEME_ORIGINALS: Mutex<Vec<(usize, SchemeImp)>> = Mutex::new(Vec::new());

fn scheme_original(method: usize) -> Option<SchemeImp> {
    SCHEME_ORIGINALS
        .lock()
        .ok()?
        .iter()
        .find(|(guarded, _)| *guarded == method)
        .map(|(_, original)| *original)
}

/// Guards every URL scheme handler class registered at run time, which is how
/// wry makes one per scheme. Returns `false` when the classes cannot be listed.
fn guard_url_schemes() -> bool {
    let Some(classes) = dynamic_classes() else {
        return false;
    };
    let Ok(mut originals) = SCHEME_ORIGINALS.lock() else {
        return false;
    };
    let guards = [
        (sel!(webView:startURLSchemeTask:), guarded_start as SchemeImp),
        (sel!(webView:stopURLSchemeTask:), guarded_stop as SchemeImp),
    ];
    for class in classes {
        // The class's own methods: reading them runs no code of the class.
        for method in class.instance_methods().iter() {
            let Some((_, guard)) = guards.iter().find(|(selector, _)| *selector == method.name()) else {
                continue;
            };
            let key = *method as *const _ as usize;
            if originals.iter().any(|(guarded, _)| *guarded == key) {
                continue;
            }
            // SAFETY: The selector fixes the signature of both implementations.
            unsafe {
                let original = std::mem::transmute::<Imp, SchemeImp>(method.implementation());
                originals.push((key, original));
                method.set_implementation(std::mem::transmute::<SchemeImp, Imp>(*guard));
            }
        }
    }
    true
}

/// Classes created at run time, as wry's and objc2's are. `objc_enumerateClasses`
/// exists from macOS 13; it is looked up by name so that the app still starts on
/// older systems, where this module is never installed.
fn dynamic_classes() -> Option<Vec<&'static AnyClass>> {
    type Enumerate = unsafe extern "C-unwind" fn(
        *const c_void,
        *const c_char,
        *const c_void,
        *const c_void,
        &DynBlock<dyn Fn(NonNull<AnyClass>, NonNull<Bool>)>,
    );
    extern "C" {
        fn dlsym(handle: *mut c_void, symbol: *const c_char) -> *mut c_void;
    }
    // `RTLD_DEFAULT` and `OBJC_DYNAMIC_CLASSES`, from <dlfcn.h> and <objc/runtime.h>.
    let default_handle = -2isize as *mut c_void;
    let dynamic_classes = -1isize as *const c_void;
    // SAFETY: The symbol name is a constant C string.
    let symbol = unsafe { dlsym(default_handle, c"objc_enumerateClasses".as_ptr()) };
    if symbol.is_null() {
        return None;
    }
    // SAFETY: The signature is the one <objc/runtime.h> declares.
    let enumerate = unsafe { std::mem::transmute::<*mut c_void, Enumerate>(symbol) };
    let found = Rc::new(RefCell::new(Vec::new()));
    let sink = found.clone();
    let block = RcBlock::new(move |class: NonNull<AnyClass>, _stop: NonNull<Bool>| {
        // SAFETY: A registered class lives for the rest of the process.
        sink.borrow_mut().push(unsafe { class.as_ref() });
    });
    // SAFETY: The block is only called during this call.
    unsafe {
        enumerate(
            dynamic_classes,
            std::ptr::null(),
            std::ptr::null(),
            std::ptr::null(),
            &block,
        )
    };
    let classes = found.borrow().clone();
    Some(classes)
}

fn is_child(webview: &WKWebView) -> bool {
    webview.downcast_ref::<SandboxWebView>().is_some()
}

unsafe extern "C-unwind" fn guarded_start(
    handler: &AnyObject,
    selector: Sel,
    webview: &WKWebView,
    task: &ProtocolObject<dyn WKURLSchemeTask>,
) {
    let original = handler
        .class()
        .instance_method(selector)
        .and_then(|method| scheme_original(method as *const _ as usize));
    match original {
        // SAFETY: The arguments are the ones WebKit passed for this selector.
        Some(original) if !is_child(webview) => unsafe { original(handler, selector, webview, task) },
        _ => {
            // NSURLErrorUnsupportedURL: the scheme does not exist for a child.
            let error = NSError::new(-1002, ns_string!("NSURLErrorDomain"));
            // SAFETY: The task has not been answered or stopped yet.
            unsafe { task.didFailWithError(&error) };
        }
    }
}

unsafe extern "C-unwind" fn guarded_stop(
    handler: &AnyObject,
    selector: Sel,
    webview: &WKWebView,
    task: &ProtocolObject<dyn WKURLSchemeTask>,
) {
    if is_child(webview) {
        return;
    }
    let original = handler
        .class()
        .instance_method(selector)
        .and_then(|method| scheme_original(method as *const _ as usize));
    if let Some(original) = original {
        // SAFETY: The arguments are the ones WebKit passed for this selector.
        unsafe { original(handler, selector, webview, task) }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn url(raw: &str) -> Url {
        Url::parse(raw).expect("test URL")
    }

    #[test]
    fn only_the_exact_sandbox_origin_is_the_sandbox() {
        let sandbox = Origin::sandbox();
        for raw in [
            "https://sandbox.avibe.bot/",
            "https://sandbox.avibe.bot/r/abc/index.html#mode=authorize&id=1",
            "https://sandbox.avibe.bot:443/",
            "HTTPS://SANDBOX.AVIBE.BOT/",
        ] {
            assert!(sandbox.holds(&url(raw)), "{raw}");
        }
        // WebKit and this parser both read a backslash as a path separator
        // here, so the host is still the sandbox.
        assert!(sandbox.holds(&url("https://sandbox.avibe.bot\\@evil.example/")));
        for raw in [
            "http://sandbox.avibe.bot/",
            "https://sandbox.avibe.bot:8443/",
            "https://sandbox.avibe.bot./",
            "https://avibe.bot/",
            "https://evil.sandbox.avibe.bot/",
            "https://sandbox.avibe.bot.evil.example/",
            "https://sandbox.avibe.bot@evil.example/",
            "https://user@sandbox.avibe.bot/",
            "https://user:secret@sandbox.avibe.bot/",
            "wss://sandbox.avibe.bot/",
            "about:blank",
            "blob:https://sandbox.avibe.bot/7f1c",
            "data:text/html,sandbox.avibe.bot",
            "javascript:alert(1)",
            "file:///sandbox.avibe.bot",
        ] {
            assert!(!sandbox.holds(&url(raw)), "{raw}");
        }
        assert_eq!(sandbox.title(), "sandbox.avibe.bot");
    }

    #[test]
    fn a_window_is_opened_only_for_the_sandbox_or_its_reserved_blank_page() {
        let sandbox = Origin::sandbox();
        assert_eq!(
            classify(Some(&url("https://sandbox.avibe.bot/#mode=setup")), &sandbox),
            Request::Sandbox
        );
        assert_eq!(classify(Some(&url("about:blank")), &sandbox), Request::Reserve);
        for raw in [
            "about:blank#x",
            "about:blank?x",
            "about:srcdoc",
            "https://github.com/avibe-bot/avibe",
            "http://127.0.0.1:5123/show/page",
            "https://avibe.bot/",
            "blob:http://127.0.0.1:5123/7f1c",
            "javascript:alert(1)",
            "data:text/html,hi",
            "file:///etc/hosts",
        ] {
            assert_eq!(classify(Some(&url(raw)), &sandbox), Request::Other, "{raw}");
        }
        assert_eq!(classify(None, &sandbox), Request::Other);
    }

    #[test]
    fn a_child_only_navigates_within_the_sandbox() {
        let sandbox = Origin::sandbox();
        let page = url("https://sandbox.avibe.bot/r/abc/index.html");
        let blank = url("about:blank");
        let elsewhere = url("http://127.0.0.1:5123/show/page");
        // (url, new context, download, main frame, committed) -> allowed
        for (url, new_context, download, main_frame, committed, allowed) in [
            (Some(&page), false, false, true, false, true),
            (Some(&page), false, false, true, true, true),
            (Some(&page), false, false, false, true, true),
            // The reserved page, until a sandbox document has been shown.
            (Some(&blank), false, false, true, false, true),
            (Some(&blank), false, false, true, true, false),
            (Some(&blank), false, false, false, false, false),
            // The opener, the page or a redirect leaving the sandbox.
            (Some(&elsewhere), false, false, true, false, false),
            (Some(&elsewhere), false, false, true, true, false),
            (Some(&elsewhere), false, false, false, true, false),
            // A nested window, a download, or no destination at all.
            (Some(&page), true, false, false, true, false),
            (Some(&page), false, true, true, true, false),
            (None, false, false, true, false, false),
        ] {
            assert_eq!(
                child_may_navigate(&sandbox, url, new_context, download, main_frame, committed),
                allowed,
                "{url:?} new_context={new_context} download={download} main_frame={main_frame} committed={committed}"
            );
        }
    }

    #[test]
    fn a_child_is_on_screen_only_for_a_committed_sandbox_document() {
        let sandbox = Origin::sandbox();
        let page = url("https://sandbox.avibe.bot/r/abc/index.html");
        let blank = url("about:blank");
        assert_eq!(commit_outcome(&sandbox, Some(&page), false), Commit::Show);
        assert_eq!(commit_outcome(&sandbox, Some(&page), true), Commit::Show);
        assert_eq!(commit_outcome(&sandbox, Some(&blank), false), Commit::Wait);
        // A shown window never goes back to a page its opener can script.
        assert_eq!(commit_outcome(&sandbox, Some(&blank), true), Commit::Close);
        for committed in [false, true] {
            assert_eq!(
                commit_outcome(&sandbox, Some(&url("http://127.0.0.1:5123/")), committed),
                Commit::Close
            );
            assert_eq!(commit_outcome(&sandbox, None, committed), Commit::Close);
        }
    }

    #[test]
    fn a_child_appears_only_with_the_users_attention_and_alone() {
        let attentive = Attention {
            app_active: true,
            opener_on_screen: true,
            sibling_on_screen: false,
            since_sibling_left: None,
        };
        assert!(may_appear(attentive));
        // The user is in another app, or the Workbench is hidden or minimized.
        assert!(!may_appear(Attention {
            app_active: false,
            ..attentive
        }));
        assert!(!may_appear(Attention {
            opener_on_screen: false,
            ..attentive
        }));
        // One sandbox window at a time.
        assert!(!may_appear(Attention {
            sibling_on_screen: true,
            ..attentive
        }));
        // Not in the place of one that just closed.
        for (since, allowed) in [
            (Duration::ZERO, false),
            (Duration::from_millis(500), false),
            (Duration::from_millis(749), false),
            (Duration::from_millis(750), true),
            (Duration::from_secs(60), true),
        ] {
            assert_eq!(
                may_appear(Attention {
                    since_sibling_left: Some(since),
                    ..attentive
                }),
                allowed,
                "{since:?}"
            );
        }
    }

    #[test]
    fn a_child_has_a_size_within_fixed_bounds_whatever_is_asked() {
        assert_eq!(child_size(None, None), (520.0, 760.0));
        assert_eq!(child_size(Some(440.0), Some(620.0)), (440.0, 620.0));
        assert_eq!(child_size(Some(5000.0), Some(5000.0)), (900.0, 1000.0));
        assert_eq!(child_size(Some(1.0), Some(0.0)), (360.0, 400.0));
        assert_eq!(child_size(Some(-300.0), None), (360.0, 760.0));
        assert_eq!(child_size(Some(f64::INFINITY), Some(f64::NAN)), (520.0, 760.0));
    }

    #[test]
    fn the_marker_needs_passkey_prf_and_the_signed_association() {
        let signed = vec![
            "webcredentials:avibe.bot".to_owned(),
            "webcredentials:sandbox.avibe.bot".to_owned(),
        ];
        assert!(advertises_passkeys(15, Some(&signed)));
        assert!(advertises_passkeys(26, Some(&signed)));
        assert!(!advertises_passkeys(14, Some(&signed)));
        assert!(!advertises_passkeys(15, None));
        assert!(!advertises_passkeys(15, Some(&[])));
        for other in [
            "webcredentials:avibe.bot",
            "webcredentials:sandbox.avibe.bot.evil.example",
            "webcredentials:*.avibe.bot",
            "applinks:sandbox.avibe.bot",
            "sandbox.avibe.bot",
        ] {
            assert!(!advertises_passkeys(15, Some(&[other.to_owned()])), "{other}");
        }
    }

    #[test]
    fn a_build_without_the_signed_association_does_not_advertise_passkeys() {
        // The test binary is signed ad hoc, like a development build: whatever
        // the system, its real signature carries no associated domain.
        assert_eq!(signed_associated_domains(), None);
        assert!(!advertises_passkeys(
            i32::MAX as isize,
            signed_associated_domains().as_deref()
        ));
    }

    #[test]
    fn the_marker_is_defined_on_the_top_level_document_only() {
        assert!(DESKTOP_VAULT_PASSKEY_MARKER.starts_with("if (window.self === window.top)"));
        assert!(DESKTOP_VAULT_PASSKEY_MARKER
            .contains("Object.defineProperty(window, '__AVIBE_DESKTOP_VAULT_PASSKEY__', { value: true })"));
    }
}
