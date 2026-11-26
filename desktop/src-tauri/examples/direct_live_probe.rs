//! Live diagnostic of the direct path, end to end, with a real WKWebView and
//! no window: the native qualification (`support/direct_transport_diag.rs`:
//! the real rendezvous, relay, probe, strict listener, inner TLS and
//! `/health`) and then the same session's page, loaded through that listener.
//! macOS only. Nothing is shown, the app is never activated, the browsing
//! data is never written to disk, and no keychain or clipboard is touched.
//!
//! Usage: `direct_live_probe --controls` runs step 1 below alone: WebKit in
//! this environment, on loopback, with no session and nothing sent anywhere.
//! `direct_live_probe https://<name>-app.avibe.bot <instance-id>` runs it
//! all, with the session of a regression-only account on standard input, one
//! line. The session is put into this process's own in-memory stores for
//! that exact origin and nowhere else: it is never printed, never passed to
//! the native core, and never sent to another origin (a navigation that
//! leaves the origin, a sign-in page included, is cancelled and reported).
//! No certificate is ever accepted that the system does not.
//!
//! Order, each step one JSON line on standard output:
//!
//! 1. Three controls of WebKit itself in this environment, on loopback pages
//!    of the probe's own: a plain load; a load through a route to a CONNECT
//!    listener; and a load whose listener answers `502`, which must fail
//!    because the route allows no failover. If a control fails, what follows
//!    would say nothing about the product, and the probe stops.
//! 2. The session's document in a first store, on WebKit's own path: the
//!    rendezvous request is the instance's own top document's, as shipped.
//! 3. The native qualification, which asks that document for the rendezvous
//!    answer through the shipping adapter (`webview_macos::fetch_rendezvous`).
//! 4. The page leg, in a second store that has never connected to the host:
//!    the session, then the route to the qualified client's listener with
//!    its window credential and no failover, then the first load. A route
//!    written to a store with a live connection to the host is not used
//!    (measured, `webview_macos::install_route`), so the first store cannot
//!    carry this leg. The listener is direct-only and the route has no
//!    failover, so a page that loads, and a fresh `/health` its own script
//!    fetches, came over the direct path.
//! 5. Every withdrawal of the link, as it happens, until the probe ends.
//!
//! Lines carry fixed words, counts and elapsed milliseconds only: no host,
//! identifier, address, header, body or error text. `elapsed_ms` counts from
//! the probe's start, except in the lines the native qualification prints
//! itself, which count from `native_qualification_start`.
//!
//! Exit code: 0 the page's payload arrived over the direct path; 1 a control
//! failed; 2 the input was refused; 3 the session's document did not load;
//! 4 the native qualification failed; 5 the page leg failed.

#[cfg(not(target_os = "macos"))]
fn main() {}

#[cfg(target_os = "macos")]
fn main() {
    std::process::exit(probe::run());
}

#[cfg(target_os = "macos")]
#[path = "support/direct_transport_diag.rs"]
mod direct_transport_diag;

/// The probe's own loopback page and CONNECT listener, for the controls.
#[cfg(target_os = "macos")]
mod control {
    use std::{
        io::{Read, Write},
        net::{Ipv4Addr, Shutdown, TcpListener, TcpStream},
        sync::{
            atomic::{AtomicUsize, Ordering},
            Arc,
        },
        thread,
        time::Duration,
    };

    pub const TITLE: &str = "direct live probe control";
    pub const USERNAME: &str = "avibe";
    pub const PASSWORD: &str = "control-only";
    /// Resolves nowhere: only the listener can carry a load of it.
    pub const ROUTED: &str = "direct-live-probe-control.invalid";

    fn read_head(stream: &mut TcpStream) -> Option<String> {
        let mut head = Vec::new();
        let mut byte = [0u8; 1];
        while head.len() < 8192 && !head.ends_with(b"\r\n\r\n") {
            if stream.read(&mut byte).ok()? == 0 {
                return None;
            }
            head.push(byte[0]);
        }
        head.ends_with(b"\r\n\r\n")
            .then(|| String::from_utf8_lossy(&head).into_owned())
    }

    /// One page, for any path.
    pub fn page() -> u16 {
        let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).expect("control page port");
        let port = listener.local_addr().expect("control page address").port();
        thread::spawn(move || {
            for mut stream in listener.incoming().flatten() {
                thread::spawn(move || {
                    let _ = stream.set_read_timeout(Some(Duration::from_secs(10)));
                    if read_head(&mut stream).is_none() {
                        return;
                    }
                    let body = format!("<!doctype html><title>{TITLE}</title><p>control</p>");
                    let _ = stream.write_all(
                        format!(
                            "HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: {}\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n{body}",
                            body.len()
                        )
                        .as_bytes(),
                    );
                    let _ = stream.shutdown(Shutdown::Both);
                });
            }
        });
        port
    }

    /// A CONNECT listener for `served` alone, with the route's credential.
    /// With `upstream` it carries the tunnel to that loopback port; without,
    /// it answers `502`, as a session's listener does when it has no path.
    pub struct Listener {
        pub port: u16,
        asked: Arc<AtomicUsize>,
    }

    impl Listener {
        pub fn start(served: String, upstream: Option<u16>) -> Self {
            let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).expect("control listener port");
            let port = listener.local_addr().expect("control listener address").port();
            let asked = Arc::new(AtomicUsize::new(0));
            let count = asked.clone();
            thread::spawn(move || {
                for stream in listener.incoming().flatten() {
                    let (served, count) = (served.clone(), count.clone());
                    thread::spawn(move || serve(stream, &served, upstream, &count));
                }
            });
            Self { port, asked }
        }

        /// How many authenticated CONNECTs for the served host arrived.
        pub fn asked(&self) -> usize {
            self.asked.load(Ordering::SeqCst)
        }
    }

    fn serve(mut browser: TcpStream, served: &str, upstream: Option<u16>, asked: &AtomicUsize) {
        use base64::{engine::general_purpose::STANDARD, Engine};
        let _ = browser.set_read_timeout(Some(Duration::from_secs(10)));
        let Some(head) = read_head(&mut browser) else { return };
        let mut first = head.split("\r\n").next().unwrap_or("").split(' ');
        let (method, target) = (first.next().unwrap_or(""), first.next().unwrap_or(""));
        let expected = format!("Basic {}", STANDARD.encode(format!("{USERNAME}:{PASSWORD}")));
        let authorized = head.split("\r\n").skip(1).any(|line| {
            line.split_once(':').is_some_and(|(name, value)| {
                name.eq_ignore_ascii_case("proxy-authorization") && value.trim() == expected
            })
        });
        let answer = |browser: &mut TcpStream, text: &str| {
            let _ = browser.write_all(text.as_bytes());
            let _ = browser.shutdown(Shutdown::Both);
        };
        if method != "CONNECT" {
            return answer(
                &mut browser,
                "HTTP/1.1 405 Method Not Allowed\r\nConnection: close\r\n\r\n",
            );
        }
        if !authorized {
            return answer(
                &mut browser,
                "HTTP/1.1 407 Proxy Authentication Required\r\nProxy-Authenticate: Basic realm=\"Avibe Direct\"\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",
            );
        }
        if target != served {
            return answer(&mut browser, "HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n");
        }
        asked.fetch_add(1, Ordering::SeqCst);
        let Some(mut upstream) = upstream.and_then(|port| TcpStream::connect((Ipv4Addr::LOCALHOST, port)).ok()) else {
            return answer(&mut browser, "HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n");
        };
        if browser
            .write_all(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            .is_err()
        {
            return;
        }
        let _ = browser.set_read_timeout(None);
        let (Ok(mut from_browser), Ok(mut to_browser)) = (browser.try_clone(), upstream.try_clone()) else {
            return;
        };
        let up = thread::spawn(move || {
            let _ = std::io::copy(&mut from_browser, &mut upstream);
            let _ = upstream.shutdown(Shutdown::Write);
        });
        let _ = std::io::copy(&mut to_browser, &mut browser);
        let _ = browser.shutdown(Shutdown::Both);
        let _ = up.join();
    }
}

#[cfg(target_os = "macos")]
mod probe {
    use std::{
        cell::{Cell, RefCell},
        ffi::c_void,
        io::{BufRead, Read},
        net::{Ipv4Addr, SocketAddrV4},
        rc::Rc,
        sync::mpsc::{self, TryRecvError},
        time::{Duration, Instant},
    };

    use avibe_desktop_lib::direct::{
        rendezvous::{origin_of, Reply, Request},
        webview_macos::{fetch_rendezvous, install_route},
        Fault, FaultCode, SelectedPath, Stage,
    };
    use block2::{DynBlock, RcBlock};
    use objc2::{
        class, define_class, msg_send,
        rc::Retained,
        runtime::{AnyObject, NSObject, NSObjectProtocol, ProtocolObject},
        sel, DefinedClass, MainThreadMarker, MainThreadOnly,
    };
    use objc2_foundation::{NSDictionary, NSError, NSPoint, NSRect, NSSize, NSString, NSURLRequest, NSURL};
    use objc2_web_kit::{
        WKContentWorld, WKNavigation, WKNavigationAction, WKNavigationActionPolicy, WKNavigationDelegate,
        WKNavigationResponse, WKNavigationResponsePolicy, WKWebView, WKWebViewConfiguration, WKWebsiteDataStore,
    };
    use serde_json::json;

    use crate::{control, direct_transport_diag::qualify};

    /// The instance's session cookie (`vibe/remote_access.py`). `__Host-`:
    /// secure, for the whole host, and for that host alone.
    const SESSION_COOKIE: &str = "__Host-vibe_remote_session";
    const SESSION_MAX_BYTES: usize = 3800;
    /// A web content process may take this long to launch on a loaded or
    /// display-less machine.
    const LOAD: Duration = Duration::from_secs(60);
    const SCRIPT: Duration = Duration::from_secs(20);
    /// Rendezvous, dial, probe, strict CONNECT, TLS and `/health`, each with
    /// its own bound inside the native half.
    const QUALIFICATION: Duration = Duration::from_secs(60);
    /// How long the link is watched after the page's payload.
    const AFTERWARDS: Duration = Duration::from_secs(5);

    // MARK: The report

    #[derive(Clone, Copy)]
    struct Report {
        start: Instant,
    }

    impl Report {
        fn line(&self, boundary: &'static str, fields: serde_json::Value) {
            let mut line = json!({ "elapsed_ms": self.start.elapsed().as_millis() as u64, "boundary": boundary });
            if let (Some(line), Some(fields)) = (line.as_object_mut(), fields.as_object()) {
                for (key, value) in fields {
                    line.insert(key.clone(), value.clone());
                }
            }
            println!("{line}");
        }

        fn ok(&self, boundary: &'static str) {
            self.line(boundary, json!({ "ok": true }));
        }

        fn failed(&self, boundary: &'static str, reason: &'static str) {
            self.line(boundary, json!({ "ok": false, "reason": reason }));
        }

        fn fault(&self, boundary: &'static str, fault: Fault) {
            self.line(boundary, json!({ "ok": false, "stage": fault.0, "code": fault.1 }));
        }
    }

    fn path_word(path: Option<SelectedPath>) -> &'static str {
        match path {
            Some(SelectedPath::Relay) => "relay",
            Some(SelectedPath::Punched(_)) => "punched",
            Some(SelectedPath::Lan(_)) => "lan",
            None => "none",
        }
    }

    // MARK: The run loop

    #[link(name = "CoreFoundation", kind = "framework")]
    extern "C" {
        static kCFRunLoopDefaultMode: *const c_void;
        fn CFRunLoopRunInMode(mode: *const c_void, seconds: f64, return_after_source_handled: u8) -> i32;
    }

    /// Runs the main run loop until `ready`, or for `limit`.
    fn pump_until(limit: Duration, mut ready: impl FnMut() -> bool) -> bool {
        let deadline = Instant::now() + limit;
        loop {
            if ready() {
                return true;
            }
            if Instant::now() >= deadline {
                return false;
            }
            // SAFETY: Main thread; the mode is CoreFoundation's own constant.
            unsafe { CFRunLoopRunInMode(kCFRunLoopDefaultMode, 0.02, 0) };
        }
    }

    // MARK: Navigation

    /// What a view's main frame did, in order.
    #[derive(Clone, Copy, Debug, PartialEq, Eq)]
    enum Event {
        /// A main-frame navigation or response left the view's origin and was
        /// cancelled: a redirect, a sign-in page.
        LeftOrigin,
        /// The main document's answer, by its status.
        Answered(i64),
        Committed,
        Finished,
        /// A fixed word for the failure, and the system's own number for it.
        Failed(&'static str, &'static str, i64),
        ContentProcessEnded,
    }

    struct Fields {
        origin: String,
        events: Rc<RefCell<Vec<Event>>>,
    }

    fn origin_of_url(url: Option<Retained<NSURL>>) -> Option<String> {
        url.and_then(|url| url.absoluteString())
            .and_then(|url| origin_of(&url.to_string()))
    }

    /// The failure as fixed words: which family of error, and what it says
    /// in general. The number is the system's own code within that family.
    fn failure(error: &NSError) -> Event {
        let code = error.code() as i64;
        let (family, word) = match error.domain().to_string().as_str() {
            "NSURLErrorDomain" => (
                "url",
                match code {
                    -999 => "cancelled",
                    -1001 => "timeout",
                    -1003 | -1006 => "name_not_resolved",
                    -1004 => "cannot_connect",
                    -1005 => "connection_lost",
                    -1009 => "offline",
                    -1200 => "tls_failed",
                    -1206..=-1201 => "certificate",
                    _ => "other",
                },
            ),
            "kCFErrorDomainCFNetwork" => (
                "cfnetwork",
                match code {
                    306..=311 => "proxy",
                    _ => "other",
                },
            ),
            "WebKitErrorDomain" => (
                "webkit",
                match code {
                    102 => "interrupted_by_policy",
                    _ => "other",
                },
            ),
            "NSPOSIXErrorDomain" => ("posix", "other"),
            _ => ("other", "other"),
        };
        Event::Failed(family, word, code)
    }

    define_class!(
        // SAFETY: `NSObject` has no subclassing requirements, and this class
        // is used on the main thread only.
        #[unsafe(super(NSObject))]
        #[thread_kind = MainThreadOnly]
        #[name = "AvibeDirectLiveProbeDelegate"]
        #[ivars = Fields]
        struct Delegate;

        unsafe impl NSObjectProtocol for Delegate {}

        unsafe impl WKNavigationDelegate for Delegate {
            #[unsafe(method(webView:decidePolicyForNavigationAction:decisionHandler:))]
            fn decide_action(
                &self,
                _webview: &WKWebView,
                action: &WKNavigationAction,
                handler: &DynBlock<dyn Fn(WKNavigationActionPolicy)>,
            ) {
                // SAFETY: WebKit calls this on the main thread with a live action.
                let (main, origin) = unsafe {
                    (
                        action.targetFrame().is_none_or(|frame| frame.isMainFrame()),
                        origin_of_url(action.request().URL()),
                    )
                };
                // The main frame stays on the view's origin: a redirect is a
                // navigation action of its own, and is refused here before
                // anything is sent to where it leads.
                let allowed = !main || origin.as_deref() == Some(self.ivars().origin.as_str());
                if !allowed {
                    self.ivars().events.borrow_mut().push(Event::LeftOrigin);
                }
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
                // SAFETY: WebKit calls this on the main thread with a live response.
                let (main, origin, status) = unsafe {
                    let answer = response.response();
                    let status: i64 = if answer.respondsToSelector(sel!(statusCode)) {
                        let status: isize = msg_send![&*answer, statusCode];
                        status as i64
                    } else {
                        0
                    };
                    (response.isForMainFrame(), origin_of_url(answer.URL()), status)
                };
                let allowed = !main || origin.as_deref() == Some(self.ivars().origin.as_str());
                if main {
                    self.ivars().events.borrow_mut().push(if allowed {
                        Event::Answered(status)
                    } else {
                        Event::LeftOrigin
                    });
                }
                handler.call((if allowed {
                    WKNavigationResponsePolicy::Allow
                } else {
                    WKNavigationResponsePolicy::Cancel
                },));
            }

            #[unsafe(method(webView:didCommitNavigation:))]
            fn did_commit(&self, _webview: &WKWebView, _navigation: Option<&WKNavigation>) {
                self.ivars().events.borrow_mut().push(Event::Committed);
            }

            #[unsafe(method(webView:didFinishNavigation:))]
            fn did_finish(&self, _webview: &WKWebView, _navigation: Option<&WKNavigation>) {
                self.ivars().events.borrow_mut().push(Event::Finished);
            }

            #[unsafe(method(webView:didFailNavigation:withError:))]
            fn did_fail(&self, _webview: &WKWebView, _navigation: Option<&WKNavigation>, error: &NSError) {
                self.ivars().events.borrow_mut().push(failure(error));
            }

            #[unsafe(method(webView:didFailProvisionalNavigation:withError:))]
            fn did_fail_provisional(&self, _webview: &WKWebView, _navigation: Option<&WKNavigation>, error: &NSError) {
                self.ivars().events.borrow_mut().push(failure(error));
            }

            #[unsafe(method(webViewWebContentProcessDidTerminate:))]
            fn content_process_ended(&self, _webview: &WKWebView) {
                self.ivars().events.borrow_mut().push(Event::ContentProcessEnded);
            }
        }
    );

    impl Delegate {
        fn new(mtm: MainThreadMarker, origin: &str, events: Rc<RefCell<Vec<Event>>>) -> Retained<Self> {
            let this = Self::alloc(mtm).set_ivars(Fields {
                origin: origin.to_owned(),
                events,
            });
            // SAFETY: `NSObject`'s designated initializer.
            unsafe { msg_send![super(this), init] }
        }
    }

    // MARK: A view

    /// One WKWebView with a store of its own that is never written to disk,
    /// held to one origin. It is in no window.
    struct View {
        webview: Retained<WKWebView>,
        store: Retained<WKWebsiteDataStore>,
        events: Rc<RefCell<Vec<Event>>>,
        // WebKit keeps its delegate weakly.
        _delegate: Retained<Delegate>,
    }

    /// How a load ended.
    #[derive(Clone, Copy, Debug, PartialEq, Eq)]
    enum Loaded {
        Finished,
        LeftOrigin,
        Failed(&'static str, &'static str, i64),
        ContentProcessEnded,
        TimedOut,
    }

    impl View {
        fn new(mtm: MainThreadMarker, origin: &str) -> Self {
            let events = Rc::new(RefCell::new(Vec::new()));
            let delegate = Delegate::new(mtm, origin, events.clone());
            // SAFETY: Main thread; a fresh configuration with browsing data
            // that is never written to disk.
            let (webview, store) = unsafe {
                let configuration = WKWebViewConfiguration::new(mtm);
                let store = WKWebsiteDataStore::nonPersistentDataStore(mtm);
                configuration.setWebsiteDataStore(&store);
                let frame = NSRect::new(NSPoint::new(0.0, 0.0), NSSize::new(800.0, 600.0));
                let webview = WKWebView::initWithFrame_configuration(WKWebView::alloc(mtm), frame, &configuration);
                webview.setNavigationDelegate(Some(ProtocolObject::from_ref(&*delegate)));
                (webview, store)
            };
            Self {
                webview,
                store,
                events,
                _delegate: delegate,
            }
        }

        /// Puts the session into this view's store for `host` alone, as the
        /// instance sets it: secure, for every path, and host-only.
        fn seed_session(&self, host: &str, session: &str) -> bool {
            let entries = [
                ("Name", SESSION_COOKIE),
                ("Value", session),
                ("Domain", host),
                ("Path", "/"),
                ("Secure", "TRUE"),
                ("HttpOnly", "TRUE"),
            ];
            let keys: Vec<Retained<NSString>> = entries.iter().map(|(key, _)| NSString::from_str(key)).collect();
            let values: Vec<Retained<AnyObject>> = entries
                .iter()
                .map(|(_, value)| Retained::into_super(Retained::into_super(NSString::from_str(value))))
                .collect();
            let key_refs: Vec<&NSString> = keys.iter().map(|key| &**key).collect();
            let properties = NSDictionary::from_retained_objects(&key_refs, &values);
            let stored = Rc::new(Cell::new(false));
            let done = stored.clone();
            let block = RcBlock::new(move || done.set(true));
            // SAFETY: Main thread. `cookieWithProperties:` answers nil for
            // properties it cannot make a cookie of; the store and the
            // cookie are live for the call, and the block is copied by it.
            unsafe {
                let cookie: Option<Retained<AnyObject>> =
                    msg_send![class!(NSHTTPCookie), cookieWithProperties: &*properties];
                let Some(cookie) = cookie else { return false };
                let cookies: Retained<AnyObject> = msg_send![&*self.store, httpCookieStore];
                let _: () = msg_send![&*cookies, setCookie: &*cookie, completionHandler: &*block];
            }
            pump_until(Duration::from_secs(10), || stored.get())
        }

        /// Loads `url` and waits for its main document to finish or fail.
        /// Reports the answer's status and the commit as they come.
        fn load(&self, url: &str, report: &Report, answered: &'static str, committed: &'static str) -> Loaded {
            self.events.borrow_mut().clear();
            // SAFETY: Main thread; `url` is the probe's own or the validated origin's.
            unsafe {
                let Some(target) = NSURL::URLWithString(&NSString::from_str(url)) else {
                    return Loaded::Failed("other", "other", 0);
                };
                self.webview.loadRequest(&NSURLRequest::requestWithURL(&target));
            }
            let mut seen = 0;
            let mut outcome = None;
            pump_until(LOAD, || {
                let events = self.events.borrow().clone();
                for event in &events[seen..] {
                    match *event {
                        Event::Answered(status) => {
                            report.line(answered, json!({ "ok": status == 200, "status": status }))
                        }
                        Event::Committed => report.ok(committed),
                        Event::Finished => outcome = outcome.or(Some(Loaded::Finished)),
                        Event::LeftOrigin => outcome = outcome.or(Some(Loaded::LeftOrigin)),
                        Event::Failed(family, word, code) => {
                            // Cancelling a navigation that left the origin
                            // also fails it; the first cause is the one.
                            outcome = outcome.or(Some(Loaded::Failed(family, word, code)))
                        }
                        Event::ContentProcessEnded => outcome = outcome.or(Some(Loaded::ContentProcessEnded)),
                    }
                }
                seen = events.len();
                outcome.is_some()
            });
            outcome.unwrap_or(Loaded::TimedOut)
        }

        /// Whether the top document is `origin`'s and complete.
        fn shows(&self, origin: &str) -> bool {
            // SAFETY: Main thread; reading the web view's current URL.
            origin_of_url(unsafe { self.webview.URL() }).as_deref() == Some(origin)
        }

        /// Runs `body` as an async function in the page's own world and
        /// waits for the string it answers.
        fn page_async(&self, mtm: MainThreadMarker, body: &str) -> Option<String> {
            let slot: Rc<RefCell<Option<Option<String>>>> = Rc::new(RefCell::new(None));
            let filled = slot.clone();
            let block = RcBlock::new(move |result: *mut AnyObject, error: *mut NSError| {
                let text = if error.is_null() && !result.is_null() {
                    // SAFETY: WebKit passes a live object for the duration of the call.
                    unsafe { &*result }
                        .downcast_ref::<NSString>()
                        .map(|value| value.to_string())
                } else {
                    None
                };
                *filled.borrow_mut() = Some(text);
            });
            // SAFETY: Main thread; no arguments, the main frame, the page's world.
            unsafe {
                let world = WKContentWorld::pageWorld(mtm);
                self.webview
                    .callAsyncJavaScript_arguments_inFrame_inContentWorld_completionHandler(
                        &NSString::from_str(body),
                        None,
                        None,
                        &world,
                        Some(&block),
                    );
            }
            pump_until(SCRIPT, || slot.borrow().is_some());
            let answered = slot.borrow_mut().take().flatten();
            answered
        }
    }

    fn report_load(report: &Report, boundary: &'static str, loaded: Loaded) -> bool {
        match loaded {
            Loaded::Finished => {
                report.ok(boundary);
                true
            }
            Loaded::LeftOrigin => {
                report.failed(boundary, "left_origin");
                false
            }
            Loaded::Failed(family, word, code) => {
                report.line(
                    boundary,
                    json!({ "ok": false, "reason": word, "family": family, "code": code }),
                );
                false
            }
            Loaded::ContentProcessEnded => {
                report.failed(boundary, "content_process_ended");
                false
            }
            Loaded::TimedOut => {
                report.failed(boundary, "timeout");
                false
            }
        }
    }

    // MARK: The controls

    /// WebKit itself, in this environment: a page loads; a routed page loads
    /// through its listener; and a routed page whose listener answers 502
    /// does not load, because its route allows no failover.
    fn controls(mtm: MainThreadMarker, report: &Report) -> bool {
        let page = control::page();

        let plain_origin = format!("http://127.0.0.1:{page}");
        let plain = View::new(mtm, &plain_origin);
        let loaded = plain.load(
            &format!("{plain_origin}/"),
            report,
            "control_plain_answered",
            "control_plain_committed",
        );
        if !report_load(report, "control_plain_loaded", loaded) {
            return false;
        }
        let title = plain.page_async(mtm, "return String(document.title);");
        if title.as_deref() != Some(control::TITLE) {
            report.failed("control_plain_script", "no_answer");
            return false;
        }
        report.ok("control_plain_script");

        let served = format!("{}:{page}", control::ROUTED);
        let routed_origin = format!("http://{served}");
        let through = control::Listener::start(served.clone(), Some(page));
        let routed = View::new(mtm, &routed_origin);
        if !install_route(
            &routed.webview,
            mtm,
            control::ROUTED,
            SocketAddrV4::new(Ipv4Addr::LOCALHOST, through.port),
            control::USERNAME,
            control::PASSWORD,
            false,
        ) {
            report.failed("control_route_installed", "refused_by_system");
            return false;
        }
        report.ok("control_route_installed");
        let loaded = routed.load(
            &format!("{routed_origin}/"),
            report,
            "control_routed_answered",
            "control_routed_committed",
        );
        if !report_load(report, "control_routed_loaded", loaded) {
            return false;
        }
        if through.asked() == 0 {
            report.failed("control_routed_through_listener", "listener_not_asked");
            return false;
        }
        report.ok("control_routed_through_listener");

        let refusing = control::Listener::start(served, None);
        let refused = View::new(mtm, &routed_origin);
        if !install_route(
            &refused.webview,
            mtm,
            control::ROUTED,
            SocketAddrV4::new(Ipv4Addr::LOCALHOST, refusing.port),
            control::USERNAME,
            control::PASSWORD,
            false,
        ) {
            report.failed("control_no_failover", "refused_by_system");
            return false;
        }
        let loaded = refused.load(
            &format!("{routed_origin}/"),
            report,
            "control_refused_answered",
            "control_refused_committed",
        );
        // The host resolves nowhere and the route allows no failover: the
        // only honest end is a failure after the listener was asked.
        let held = matches!(loaded, Loaded::Failed(..)) && refusing.asked() > 0;
        if !held {
            report.failed(
                "control_no_failover",
                if loaded == Loaded::Finished {
                    "loaded_without_listener"
                } else {
                    "listener_not_asked"
                },
            );
            return false;
        }
        report.ok("control_no_failover");
        true
    }

    // MARK: The input

    /// The exact managed origin and its host; `None` for anything else.
    fn managed_origin(argument: &str) -> Option<(String, String)> {
        let host = argument.strip_prefix("https://")?;
        (origin_of(argument).as_deref() == Some(argument) && !host.contains(':') && host.ends_with("-app.avibe.bot"))
            .then(|| (argument.to_owned(), host.to_owned()))
    }

    /// One line of standard input: a cookie value and nothing else.
    fn read_session() -> Option<String> {
        let mut line = String::new();
        std::io::stdin()
            .lock()
            .take(SESSION_MAX_BYTES as u64 + 2)
            .read_line(&mut line)
            .ok()?;
        let session = line.trim_end_matches(['\r', '\n']);
        let plain = |byte: u8| matches!(byte, 0x21..=0x7e) && !matches!(byte, b'"' | b',' | b';' | b'\\');
        (!session.is_empty() && session.len() <= SESSION_MAX_BYTES && session.bytes().all(plain))
            .then(|| session.to_owned())
    }

    // MARK: The native half

    /// What the native half asks of, and tells, the main thread.
    enum FromNative {
        /// The rendezvous answer for this client endpoint, from the session's
        /// own document.
        Rendezvous(String, tokio::sync::oneshot::Sender<Reply>),
        /// The qualified client's listener and its window credential.
        Qualified(Result<Leg, Fault>),
    }

    struct Leg {
        proxy: SocketAddrV4,
        username: String,
        password: String,
    }

    fn reply_word(reply: &Reply) -> &'static str {
        match reply {
            Reply::Ready(_) => "ready",
            Reply::NotReady => "not_ready",
            Reply::SelectionChanged => "selection_changed",
            Reply::Refused(_) => "refused",
            Reply::Controlled => "controlled",
            Reply::Failed => "failed",
        }
    }

    /// Runs the native qualification on a thread and a runtime of its own,
    /// and keeps the qualified client there until `stop`: its link is
    /// watched and every withdrawal printed as it happens.
    fn start_native(
        host: String,
        instance: String,
        report: Report,
        to_main: mpsc::Sender<FromNative>,
        stop: tokio::sync::oneshot::Receiver<()>,
    ) -> std::thread::JoinHandle<()> {
        std::thread::spawn(move || {
            let Ok(runtime) = tokio::runtime::Builder::new_current_thread().enable_all().build() else {
                let _ = to_main.send(FromNative::Qualified(Err(Fault(Stage::Handover, FaultCode::Other))));
                return;
            };
            runtime.block_on(async move {
                let asks = to_main.clone();
                let fetch = move |client_endpoint_id: String| async move {
                    let (answer, answered) = tokio::sync::oneshot::channel();
                    if asks.send(FromNative::Rendezvous(client_endpoint_id, answer)).is_err() {
                        return Reply::Failed;
                    }
                    answered.await.unwrap_or(Reply::Failed)
                };
                let mut live = match qualify(&host, instance, fetch).await {
                    Ok(live) => live,
                    Err(fault) => {
                        let _ = to_main.send(FromNative::Qualified(Err(fault)));
                        return;
                    }
                };
                let leg = live
                    .client
                    .route()
                    .map(|route| route.proxy)
                    .map(|proxy| {
                        let (username, password) = live.registration.native_login();
                        Leg {
                            proxy,
                            username: username.to_owned(),
                            password: password.to_owned(),
                        }
                    })
                    .ok_or(Fault(Stage::Handover, FaultCode::ListenerClosed));
                let _ = to_main.send(FromNative::Qualified(leg));
                let mut stop = stop;
                loop {
                    tokio::select! {
                        _ = &mut stop => break,
                        retired = live.retirements.recv() => match retired {
                            Ok(fault) => report.fault("link_retired", fault),
                            Err(tokio::sync::broadcast::error::RecvError::Lagged(_)) => {}
                            Err(tokio::sync::broadcast::error::RecvError::Closed) => break,
                        },
                    }
                }
                report.line("link_at_end", json!({ "path": path_word(live.client.selected_path()) }));
            });
        })
    }

    // MARK: The run

    pub fn run() -> i32 {
        let mtm = MainThreadMarker::new().expect("the probe runs on the main thread");
        let report = Report { start: Instant::now() };

        let mut arguments = std::env::args().skip(1).peekable();
        if arguments.peek().map(String::as_str) == Some("--controls") {
            // WebKit in this environment, and nothing else: no session is
            // read and nothing leaves this computer.
            return if arguments.nth(1).is_none() && controls(mtm, &report) {
                0
            } else {
                1
            };
        }
        let (Some(origin), Some(instance), None) = (arguments.next(), arguments.next(), arguments.next()) else {
            report.failed("input", "usage");
            return 2;
        };
        let Some((origin, host)) = managed_origin(&origin) else {
            report.failed("input", "origin_not_managed");
            return 2;
        };
        let instance_ok = !instance.is_empty()
            && instance.len() <= 128
            && instance
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-' || byte == b'_');
        if !instance_ok {
            report.failed("input", "instance_invalid");
            return 2;
        }
        let Some(session) = read_session() else {
            report.failed("input", "session_invalid");
            return 2;
        };
        report.ok("input");

        if !controls(mtm, &report) {
            return 1;
        }

        // The session's own document, on WebKit's own path: the rendezvous
        // request must come from it.
        let document = View::new(mtm, &origin);
        if !document.seed_session(&host, &session) {
            report.failed("session_seeded", "cookie_refused");
            return 3;
        }
        report.ok("session_seeded");
        let loaded = document.load(
            &format!("{origin}/"),
            &report,
            "document_answered",
            "document_committed",
        );
        if !report_load(&report, "document_loaded", loaded) || !document.shows(&origin) {
            return 3;
        }

        let (to_main, from_native) = mpsc::channel();
        let (stop, stopped) = tokio::sync::oneshot::channel();
        report.ok("native_qualification_start");
        let native = start_native(host.clone(), instance, report, to_main, stopped);
        let mut qualified = None;
        pump_until(QUALIFICATION, || {
            match from_native.try_recv() {
                Ok(FromNative::Rendezvous(client_endpoint_id, answer)) => {
                    report.ok("rendezvous_asked");
                    let Some(request) = Request::new(&origin, &client_endpoint_id) else {
                        report.failed("rendezvous_answered", "request_refused");
                        let _ = answer.send(Reply::Failed);
                        return false;
                    };
                    // The shipping adapter, on the session's own document.
                    fetch_rendezvous(
                        &document.webview,
                        mtm,
                        &request,
                        || true,
                        move |reply| {
                            let word = reply_word(&reply);
                            let status = match reply {
                                Reply::Refused(status) => i64::from(status),
                                _ => 0,
                            };
                            report.line(
                                "rendezvous_answered",
                                json!({ "ok": word == "ready", "reason": word, "status": status }),
                            );
                            let _ = answer.send(reply);
                        },
                    );
                }
                Ok(FromNative::Qualified(result)) => qualified = Some(result),
                Err(TryRecvError::Empty) => {}
                Err(TryRecvError::Disconnected) => {
                    qualified = Some(Err(Fault(Stage::Handover, FaultCode::Other)));
                }
            }
            qualified.is_some()
        });
        let finish = |code: i32| {
            let _ = stop.send(());
            let _ = native.join();
            code
        };
        let leg = match qualified {
            Some(Ok(leg)) => leg,
            Some(Err(fault)) => {
                report.fault("native_qualified", fault);
                return finish(4);
            }
            None => {
                report.failed("native_qualified", "timeout");
                return finish(4);
            }
        };
        report.ok("native_qualified");

        // The page leg: a store that has never connected to the host, so the
        // route carries it from its first request. The session, then the
        // route (the qualified client's listener, its window credential, no
        // failover), then the first load.
        let page = View::new(mtm, &origin);
        if !page.seed_session(&host, &session) {
            report.failed("page_session_seeded", "cookie_refused");
            return finish(5);
        }
        report.ok("page_session_seeded");
        if !install_route(
            &page.webview,
            mtm,
            &host,
            leg.proxy,
            &leg.username,
            &leg.password,
            false,
        ) {
            report.failed("page_route_installed", "refused_by_system");
            return finish(5);
        }
        report.ok("page_route_installed");
        let loaded = page.load(&format!("{origin}/"), &report, "page_answered", "page_committed");
        if !report_load(&report, "page_loaded", loaded) || !page.shows(&origin) {
            return finish(5);
        }
        // A payload of the page's own, asked for now: not the native check's.
        let nonce = report.start.elapsed().as_nanos();
        let health = page.page_async(
            mtm,
            &format!(
                r#"try {{
  const response = await fetch("/health?probe={nonce}", {{cache: "no-store", redirect: "error"}});
  const body = await response.json();
  return String(response.status) + "|" + String(body && body.status);
}} catch (error) {{
  return "failed";
}}"#
            ),
        );
        let code = match health.as_deref() {
            Some("200|ok") => {
                report.ok("page_payload");
                0
            }
            Some("failed") => {
                report.failed("page_payload", "fetch_failed");
                5
            }
            Some(_) => {
                report.failed("page_payload", "unexpected_answer");
                5
            }
            None => {
                report.failed("page_payload", "no_answer");
                5
            }
        };
        // Whatever ends the link in the next moments is printed by the
        // native half as it happens.
        pump_until(AFTERWARDS, || false);
        finish(code)
    }
}
