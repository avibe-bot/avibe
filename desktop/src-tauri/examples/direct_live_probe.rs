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
//! all. Standard input then carries two lines: the origin the session was
//! issued for, and the session of a regression-only account.
//!
//! Where the session may go. It travels with its own origin, and the probe
//! runs only if that origin is a managed one and is exactly the origin named
//! on the command line; otherwise it reads nothing more and sends nothing.
//! The session is put, as the instance's own host-only secure cookie that
//! script cannot read, into this process's in-memory stores; each store is
//! read back, and a session is used only where it is held in that shape.
//! Each view is held to that origin:
//!
//! - a main-frame navigation is allowed for the one URL the probe asked
//!   for, once; every other main-frame navigation is cancelled before it is
//!   sent, a redirect included, to the same origin or to another;
//! - a subframe navigation is allowed on that exact origin only;
//! - the main document's answer is shown only if it is that URL's.
//!
//! Step 1 holds WebKit to this with a dummy session: a fixture that
//! redirects must not get a request at its target. What is not policed: the
//! requests the instance's own page makes for its resources. The cookie's
//! own scope bounds those: WebKit sends it to `https` requests for that
//! host. The session is never printed, never passed to the native core, and
//! no certificate is accepted that the system does not accept.
//!
//! Order, each step one JSON line on standard output:
//!
//! 1. Controls of WebKit itself in this environment, on loopback pages of
//!    the probe's own: a cookie of the real one's shape, with a dummy
//!    value, stored and read back (never sent), and one that script could
//!    read, which the check must refuse;
//!    a load that carries a dummy session to its origin;
//!    a redirect to another origin and one within the origin, neither
//!    followed; a load through a route to a CONNECT listener; and a load
//!    whose listener answers `502`, which must fail because the route
//!    allows no failover. If a control fails, what follows would say
//!    nothing about the product, and the probe stops.
//! 2. The session's document in a first store, on WebKit's own path: the
//!    rendezvous request is the instance's own top document's, as shipped.
//!    The instance serves its page to anyone, so the document proves no
//!    session: the page then asks the instance's own `/api/session`, and
//!    the probe goes on only if that says remote and signed in (it prints
//!    those two facts and whether the role is Owner, nothing else of it).
//! 3. The native qualification, which asks that document for the rendezvous
//!    answer through the shipping adapter (`webview_macos::fetch_rendezvous`).
//! 4. The fresh-store leg, the positive control of the page path: a second
//!    store that has never connected to the host gets the session, then the
//!    route to the qualified client's listener with its window credential
//!    and no failover, then its first load and a `/health` its own script
//!    fetches. The listener is direct-only and the route has no failover,
//!    so a payload that arrives came over the direct path.
//! 5. The live-store leg: the first store, which is connected to the host
//!    on WebKit's own path, gets the same route while it is live, as a
//!    window that was open before the first answer does. A route written to
//!    a store with a live connection is not used by that connection
//!    (measured, `webview_macos::install_route`), so the probe then asks
//!    the page for work of several kinds (a fetch, a fetch without
//!    credentials, a WebSocket, a reload) and reports for each what the
//!    page got and whether the listener committed a new direct stream.
//! 6. Every opening and closing of a direct stream, every change of the
//!    path's status and every withdrawal of the link, as they happen.
//!
//! Only standard output is the result, and only it may be pasted anywhere:
//! its lines carry fixed words, counts, HTTP statuses, the system's own
//! error numbers and elapsed milliseconds, and no host, identifier, address,
//! header, body or error text. The command line, standard input and
//! standard error are not for pasting. `elapsed_ms` counts from the probe's
//! start, except in the lines the native qualification prints itself, which
//! count from `native_qualification_start`.
//!
//! Exit code: 0 both legs got their payload over the direct path; 1 a
//! control failed; 2 the input was refused; 3 the session's document did not
//! load or the instance did not take the session; 4 the native
//! qualification failed; 5 the fresh-store leg failed;
//! 6 the live-store leg got no payload over a new direct stream.

#[cfg(not(target_os = "macos"))]
fn main() {}

#[cfg(target_os = "macos")]
fn main() {
    std::process::exit(probe::run());
}

#[cfg(target_os = "macos")]
#[path = "support/direct_transport_diag.rs"]
mod direct_transport_diag;

/// The probe's own loopback pages and CONNECT listener, for the controls.
#[cfg(target_os = "macos")]
mod control {
    use std::{
        io::{Read, Write},
        net::{Ipv4Addr, Shutdown, TcpListener, TcpStream},
        sync::{
            atomic::{AtomicUsize, Ordering},
            Arc, Mutex,
        },
        thread,
        time::Duration,
    };

    pub const TITLE: &str = "direct live probe control";
    pub const USERNAME: &str = "avibe";
    pub const PASSWORD: &str = "control-only";
    /// Resolves nowhere: only the listener can carry a load of it.
    pub const ROUTED: &str = "direct-live-probe-control.invalid";
    /// The dummy session of the controls. A cookie is its host's, whatever
    /// the port, so a second loopback server would be sent it.
    pub const SESSION_COOKIE: &str = "direct_live_probe_control_session";
    pub const SESSION: &str = "dummy-session-of-the-controls";

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

    /// One request a page server got: its path, and whether it carried the
    /// dummy session.
    #[derive(Clone)]
    pub struct Seen {
        pub path: String,
        pub session: bool,
    }

    /// A loopback page server that records what it is asked. `/` and
    /// `/landed` are pages; `/redirect-same` redirects to `/landed`;
    /// `/redirect-away` redirects to `away`, if there is one.
    pub struct Pages {
        pub port: u16,
        seen: Arc<Mutex<Vec<Seen>>>,
    }

    impl Pages {
        pub fn start(away: Option<String>) -> Self {
            let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).expect("control page port");
            let port = listener.local_addr().expect("control page address").port();
            let seen = Arc::new(Mutex::new(Vec::new()));
            let log = seen.clone();
            thread::spawn(move || {
                for stream in listener.incoming().flatten() {
                    let (log, away) = (log.clone(), away.clone());
                    thread::spawn(move || answer(stream, away.as_deref(), &log));
                }
            });
            Self { port, seen }
        }

        pub fn seen(&self) -> Vec<Seen> {
            self.seen.lock().expect("seen").clone()
        }
    }

    fn answer(mut stream: TcpStream, away: Option<&str>, log: &Mutex<Vec<Seen>>) {
        let _ = stream.set_read_timeout(Some(Duration::from_secs(10)));
        let Some(head) = read_head(&mut stream) else { return };
        let path = head
            .split("\r\n")
            .next()
            .and_then(|line| line.split(' ').nth(1))
            .unwrap_or("")
            .to_owned();
        let session = head.split("\r\n").skip(1).any(|line| {
            line.split_once(':').is_some_and(|(name, value)| {
                name.eq_ignore_ascii_case("cookie") && value.contains(&format!("{SESSION_COOKIE}={SESSION}"))
            })
        });
        log.lock().expect("seen").push(Seen {
            path: path.clone(),
            session,
        });
        let redirect = match (path.as_str(), away) {
            ("/redirect-same", _) => Some("/landed".to_owned()),
            ("/redirect-away", Some(away)) => Some(away.to_owned()),
            _ => None,
        };
        let text = match redirect {
            Some(location) => format!(
                "HTTP/1.1 302 Found\r\nLocation: {location}\r\nContent-Length: 0\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n"
            ),
            None => {
                let body = format!("<!doctype html><title>{TITLE}</title><p>control</p>");
                format!(
                    "HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: {}\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n{body}",
                    body.len()
                )
            }
        };
        let _ = stream.write_all(text.as_bytes());
        let _ = stream.shutdown(Shutdown::Both);
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
        Fault, FaultCode, PathStatus, PreferredPath, SelectedPath, Stage,
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
    /// A web content process may take this long to launch, and a first
    /// script this long to answer, on a loaded or display-less machine (the
    /// first script took 12 s in the macOS guest).
    const LOAD: Duration = Duration::from_secs(90);
    const SCRIPT: Duration = Duration::from_secs(60);
    /// Rendezvous, dial, probe, strict CONNECT, TLS and `/health`, each with
    /// its own bound inside the native half.
    const QUALIFICATION: Duration = Duration::from_secs(60);
    /// How long a stream of an earlier step may take to end before the next
    /// step counts new ones.
    const RELEASE: Duration = Duration::from_secs(10);
    /// How long after a page's work the listener is given to show it.
    const SETTLE: Duration = Duration::from_millis(500);
    /// How long the link is watched after the last leg.
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
        /// A navigation or an answer the view is not held to was cancelled:
        /// a redirect, a sign-in page, a script that moved the page.
        Blocked,
        /// The main document's answer, by its status.
        Answered(i64),
        Committed,
        Finished,
        /// A fixed word for the failure, and the system's own number for it.
        Failed(&'static str, &'static str, i64),
        ContentProcessEnded,
    }

    struct Fields {
        /// The one origin this view may be on.
        origin: String,
        /// The one URL a main-frame navigation may ask for next; taken by
        /// the navigation that asks for it, so nothing follows it.
        allowed: RefCell<Option<String>>,
        /// The URL of the navigation that was allowed: its answer's.
        asked: RefCell<Option<String>>,
        events: Rc<RefCell<Vec<Event>>>,
    }

    fn url_text(url: Option<Retained<NSURL>>) -> Option<String> {
        url.and_then(|url| url.absoluteString()).map(|url| url.to_string())
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
                let (main, url) = unsafe {
                    (
                        action.targetFrame().is_none_or(|frame| frame.isMainFrame()),
                        url_text(action.request().URL()),
                    )
                };
                let fields = self.ivars();
                let allowed = if main {
                    // The one URL the probe asked for, once. A redirect is a
                    // navigation action of its own and finds nothing allowed:
                    // it is refused before anything is sent to where it
                    // leads, on this origin or off it.
                    let wanted = fields.allowed.borrow_mut().take();
                    let allowed = wanted.is_some() && wanted == url;
                    if allowed {
                        *fields.asked.borrow_mut() = url;
                    } else {
                        fields.events.borrow_mut().push(Event::Blocked);
                    }
                    allowed
                } else {
                    url.as_deref().and_then(origin_of).as_deref() == Some(fields.origin.as_str())
                };
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
                let (main, url, status) = unsafe {
                    let answer = response.response();
                    let status: i64 = if answer.respondsToSelector(sel!(statusCode)) {
                        let status: isize = msg_send![&*answer, statusCode];
                        status as i64
                    } else {
                        0
                    };
                    (response.isForMainFrame(), url_text(answer.URL()), status)
                };
                let fields = self.ivars();
                let allowed = if main {
                    // The answer of the URL that was asked for, and no other.
                    let allowed = url.is_some() && url == *fields.asked.borrow();
                    fields.events.borrow_mut().push(if allowed {
                        Event::Answered(status)
                    } else {
                        Event::Blocked
                    });
                    allowed
                } else {
                    url.as_deref().and_then(origin_of).as_deref() == Some(fields.origin.as_str())
                };
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
                allowed: RefCell::new(None),
                asked: RefCell::new(None),
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
        origin: String,
        events: Rc<RefCell<Vec<Event>>>,
        // WebKit keeps its delegate weakly.
        delegate: Retained<Delegate>,
    }

    /// How a load ended.
    #[derive(Clone, Copy, Debug, PartialEq, Eq)]
    enum Loaded {
        Finished,
        Blocked,
        Failed(&'static str, &'static str, i64),
        ContentProcessEnded,
        TimedOut,
    }

    /// A session cookie as the store holds it after seeding.
    #[derive(Clone, Copy)]
    struct Seeded {
        /// For the origin's host exactly, not for a domain.
        host_only: bool,
        secure: bool,
        http_only: bool,
    }

    /// The shape the instance's own session cookie has for an origin of this
    /// scheme: for the host alone, out of script's reach, and secure when
    /// the origin is. A session is used only in a store that holds it so.
    fn seeded_ok(seeded: Seeded, secure: bool) -> bool {
        seeded.host_only && seeded.http_only && seeded.secure == secure
    }

    /// Says what the store holds, and whether it is that shape.
    fn report_seeded(report: &Report, boundary: &'static str, seeded: Option<Seeded>, secure: bool) -> bool {
        let Some(seeded) = seeded else {
            report.failed(boundary, "cookie_not_stored");
            return false;
        };
        let ok = seeded_ok(seeded, secure);
        report.line(
            boundary,
            json!({ "ok": ok, "host_only": seeded.host_only, "secure": seeded.secure, "http_only": seeded.http_only }),
        );
        ok
    }

    /// Whether the instance takes the view's session: its own `/api/session`
    /// answer, read by the page and reduced to three facts. The instance
    /// serves its page to anyone, so a document that loads proves nothing.
    #[derive(Clone, Copy)]
    struct SessionCheck {
        remote: bool,
        authenticated: bool,
        owner: bool,
    }

    const SESSION_CHECK: &str = r#"try {
  const response = await fetch("/api/session", {cache: "no-store", redirect: "error", credentials: "same-origin"});
  if (response.status !== 200) return "status";
  const body = await response.json();
  const bit = (value) => (value === true ? "1" : "0");
  return bit(body.remote) + bit(body.authenticated) + bit(body.instance_role === "owner");
} catch (error) {
  return "failed";
}"#;

    /// Asks and reports. `true` when the instance says the session is a
    /// remote, signed-in one.
    fn session_accepted(view: &View, mtm: MainThreadMarker, report: &Report, boundary: &'static str) -> bool {
        let answer = view.page_async(mtm, SESSION_CHECK);
        let check = answer.as_deref().and_then(|text| {
            let bits: Vec<bool> = text
                .bytes()
                .map(|byte| match byte {
                    b'1' => Some(true),
                    b'0' => Some(false),
                    _ => None,
                })
                .collect::<Option<_>>()?;
            (bits.len() == 3).then(|| SessionCheck {
                remote: bits[0],
                authenticated: bits[1],
                owner: bits[2],
            })
        });
        let Some(check) = check else {
            report.failed(
                boundary,
                match answer.as_deref() {
                    Some("status") => "unexpected_status",
                    Some("failed") => "fetch_failed",
                    Some(_) => "unexpected_answer",
                    None => "no_answer",
                },
            );
            return false;
        };
        let ok = check.remote && check.authenticated;
        report.line(
            boundary,
            json!({ "ok": ok, "remote": check.remote, "authenticated": check.authenticated, "owner": check.owner }),
        );
        ok
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
                origin: origin.to_owned(),
                events,
                delegate,
            }
        }

        /// Puts a session cookie into this view's store as the instance sets
        /// its own: for the host of the view's origin and that host alone
        /// (made from the origin's URL, with no domain attribute, which a
        /// `__Host-` cookie may not have), for every path, not for script,
        /// and secure when the origin is. Then reads the store back: what
        /// it holds under that name, or `None` when it holds nothing.
        fn seed_session(&self, name: &str, session: &str) -> Option<Seeded> {
            self.seed_cookie(name, session, true)
        }

        /// `seed_session`, or, with `http_only` off, a cookie script can
        /// read: only the controls make one, to see the check refuse it.
        fn seed_cookie(&self, name: &str, session: &str, http_only: bool) -> Option<Seeded> {
            let (scheme, authority) = self.origin.split_once("://")?;
            let host = authority.split(':').next().unwrap_or("").to_owned();
            let url = format!("{}/", self.origin);
            let mut entries = vec![
                ("Name", name),
                ("Value", session),
                ("OriginURL", url.as_str()),
                ("Path", "/"),
            ];
            if http_only {
                entries.push(("HttpOnly", "TRUE"));
            }
            if scheme == "https" {
                entries.push(("Secure", "TRUE"));
            }
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
            let cookies: Retained<AnyObject> = unsafe {
                let cookie: Option<Retained<AnyObject>> =
                    msg_send![class!(NSHTTPCookie), cookieWithProperties: &*properties];
                let cookie = cookie?;
                let cookies: Retained<AnyObject> = msg_send![&*self.store, httpCookieStore];
                let _: () = msg_send![&*cookies, setCookie: &*cookie, completionHandler: &*block];
                cookies
            };
            if !pump_until(Duration::from_secs(10), || stored.get()) {
                return None;
            }
            // What the store itself says it holds.
            let found: Rc<RefCell<Option<Option<Seeded>>>> = Rc::new(RefCell::new(None));
            let filled = found.clone();
            let wanted = name.to_owned();
            let read = RcBlock::new(move |all: *mut AnyObject| {
                let mut seeded = None;
                if !all.is_null() {
                    // SAFETY: WebKit passes a live NSArray of NSHTTPCookie
                    // for the duration of the call; the selectors are theirs.
                    unsafe {
                        let count: usize = msg_send![all, count];
                        for index in 0..count {
                            let cookie: Retained<AnyObject> = msg_send![all, objectAtIndex: index];
                            let cookie_name: Retained<NSString> = msg_send![&*cookie, name];
                            if cookie_name.to_string() != wanted {
                                continue;
                            }
                            let domain: Retained<NSString> = msg_send![&*cookie, domain];
                            let secure: bool = msg_send![&*cookie, isSecure];
                            let http_only: bool = msg_send![&*cookie, isHTTPOnly];
                            seeded = Some(Seeded {
                                host_only: domain.to_string() == host,
                                secure,
                                http_only,
                            });
                        }
                    }
                }
                *filled.borrow_mut() = Some(seeded);
            });
            // SAFETY: Main thread; the block is copied by the call.
            unsafe {
                let _: () = msg_send![&*cookies, getAllCookies: &*read];
            }
            pump_until(Duration::from_secs(10), || found.borrow().is_some());
            let seeded = found.borrow_mut().take().flatten();
            seeded
        }

        /// Loads `path` of the view's own origin and waits for its main
        /// document to finish or fail. Nothing else is asked for: the view's
        /// delegate allows this one URL, once. Reports the answer's status
        /// and the commit as they come.
        fn load(&self, path: &str, report: &Report, answered: &'static str, committed: &'static str) -> Loaded {
            let url = format!("{}{path}", self.origin);
            self.events.borrow_mut().clear();
            // SAFETY: Main thread; the URL is of the view's own origin.
            unsafe {
                let Some(target) = NSURL::URLWithString(&NSString::from_str(&url)) else {
                    return Loaded::Failed("other", "other", 0);
                };
                // As WebKit will name it when it asks.
                *self.delegate.ivars().allowed.borrow_mut() = url_text(Some(target.clone()));
                *self.delegate.ivars().asked.borrow_mut() = None;
                self.webview.loadRequest(&NSURLRequest::requestWithURL(&target));
            }
            let mut seen = 0;
            let mut outcome = None;
            let mut shown = false;
            pump_until(LOAD, || {
                let events = self.events.borrow().clone();
                for event in &events[seen..] {
                    match *event {
                        Event::Answered(status) => {
                            report.line(answered, json!({ "ok": status == 200, "status": status }))
                        }
                        Event::Committed => {
                            shown = true;
                            report.ok(committed)
                        }
                        Event::Finished => outcome = outcome.or(Some(Loaded::Finished)),
                        // The document asked for is shown, and its own script
                        // then tried to go elsewhere: refused, and said, but
                        // the document stays.
                        Event::Blocked if shown => report.failed("navigation_blocked_after_commit", "refused"),
                        // Cancelling a navigation also fails it; the first
                        // cause is the one.
                        Event::Blocked => outcome = outcome.or(Some(Loaded::Blocked)),
                        Event::Failed(family, word, code) => {
                            outcome = outcome.or(Some(Loaded::Failed(family, word, code)))
                        }
                        Event::ContentProcessEnded => outcome = outcome.or(Some(Loaded::ContentProcessEnded)),
                    }
                }
                seen = events.len();
                outcome.is_some()
            });
            // Nothing may follow this load on its own.
            *self.delegate.ivars().allowed.borrow_mut() = None;
            outcome.unwrap_or(Loaded::TimedOut)
        }

        /// Whether the top document is the view's own origin's.
        fn shows_its_origin(&self) -> bool {
            // SAFETY: Main thread; reading the web view's current URL.
            url_text(unsafe { self.webview.URL() })
                .as_deref()
                .and_then(origin_of)
                .as_deref()
                == Some(self.origin.as_str())
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
            Loaded::Blocked => {
                report.failed(boundary, "navigation_blocked");
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

    /// WebKit itself, in this environment. A page loads and its origin gets
    /// the view's session. A redirect is not followed, to another origin or
    /// within the origin: its target gets no request. A routed page loads
    /// through its listener. A routed page whose listener answers 502 does
    /// not load, because its route allows no failover.
    fn controls(mtm: MainThreadMarker, report: &Report) -> bool {
        // The same host as the pages, so the dummy session would be sent
        // to it: a cookie is its host's, whatever the port.
        let elsewhere = control::Pages::start(None);
        let pages = control::Pages::start(Some(format!("http://127.0.0.1:{}/landed", elsewhere.port)));
        let origin = format!("http://127.0.0.1:{}", pages.port);

        // The real cookie's own shape (its name, secure, host-only), with a
        // dummy value, for a host that resolves nowhere: stored and read
        // back, never sent.
        let shaped = View::new(mtm, &format!("https://{}", control::ROUTED));
        let held = shaped.seed_session(SESSION_COOKIE, control::SESSION);
        if !report_seeded(report, "control_host_cookie_stored", held, true) {
            return false;
        }
        // The check itself: a cookie that script can read must be refused.
        // One is stored, in a store of its own, and read back as such.
        let readable = View::new(mtm, &format!("https://{}", control::ROUTED));
        match readable.seed_cookie(SESSION_COOKIE, control::SESSION, false) {
            Some(seeded) if !seeded.http_only && !seeded_ok(seeded, true) => {
                report.ok("control_script_readable_cookie_refused")
            }
            Some(seeded) => {
                report.failed(
                    "control_script_readable_cookie_refused",
                    if seeded.http_only {
                        "flag_not_observed"
                    } else {
                        "accepted"
                    },
                );
                return false;
            }
            None => {
                report.failed("control_script_readable_cookie_refused", "cookie_not_stored");
                return false;
            }
        }

        let plain = View::new(mtm, &origin);
        let held = plain.seed_session(control::SESSION_COOKIE, control::SESSION);
        if !report_seeded(report, "control_session_seeded", held, false) {
            return false;
        }
        let loaded = plain.load("/", report, "control_plain_answered", "control_plain_committed");
        if !report_load(report, "control_plain_loaded", loaded) {
            return false;
        }
        let title = plain.page_async(mtm, "return String(document.title);");
        if title.as_deref() != Some(control::TITLE) {
            report.failed("control_plain_script", "no_answer");
            return false;
        }
        report.ok("control_plain_script");
        // The checks below mean something only if the session does travel.
        if !pages.seen().iter().any(|seen| seen.path == "/" && seen.session) {
            report.failed("control_session_sent_to_its_origin", "session_not_sent");
            return false;
        }
        report.ok("control_session_sent_to_its_origin");

        // A redirect to another origin: cancelled before it is sent.
        let loaded = plain.load(
            "/redirect-away",
            report,
            "control_redirect_away_answered",
            "control_redirect_away_committed",
        );
        pump_until(SETTLE, || false);
        let asked = pages.seen().iter().any(|seen| seen.path == "/redirect-away");
        let reached = elsewhere.seen();
        if loaded == Loaded::Finished || !asked || !reached.is_empty() {
            report.line(
                "control_redirect_away_not_followed",
                json!({
                    "ok": false,
                    "reason": if !asked { "redirect_not_asked" } else if reached.iter().any(|seen| seen.session) { "session_sent_elsewhere" } else { "redirect_followed" },
                    "requests_elsewhere": reached.len(),
                }),
            );
            return false;
        }
        report.ok("control_redirect_away_not_followed");

        // A redirect within the origin: not followed either.
        let loaded = plain.load(
            "/redirect-same",
            report,
            "control_redirect_same_answered",
            "control_redirect_same_committed",
        );
        pump_until(SETTLE, || false);
        let seen = pages.seen();
        let asked = seen.iter().any(|seen| seen.path == "/redirect-same");
        let landed = seen.iter().any(|seen| seen.path == "/landed");
        if loaded == Loaded::Finished || !asked || landed {
            report.failed(
                "control_redirect_same_not_followed",
                if asked {
                    "redirect_followed"
                } else {
                    "redirect_not_asked"
                },
            );
            return false;
        }
        report.ok("control_redirect_same_not_followed");

        let served = format!("{}:{}", control::ROUTED, pages.port);
        let routed_origin = format!("http://{served}");
        let through = control::Listener::start(served.clone(), Some(pages.port));
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
        let loaded = routed.load("/", report, "control_routed_answered", "control_routed_committed");
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
        let loaded = refused.load("/", report, "control_refused_answered", "control_refused_committed");
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

    /// The host of an exact managed origin (`https://<label>-app.avibe.bot`,
    /// no port, no path), by the rule the client itself applies to its host
    /// (`connect::Authority::managed`); `None` for anything else.
    fn managed_host(origin: &str) -> Option<&str> {
        let host = origin.strip_prefix("https://")?;
        let label = host.strip_suffix("-app.avibe.bot")?;
        let plain = !label.is_empty()
            && label.len() + 4 <= 63
            && !label.starts_with('-')
            && !label.ends_with('-')
            && label
                .bytes()
                .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'-');
        (plain && origin_of(origin).as_deref() == Some(origin)).then_some(host)
    }

    /// One line of standard input, without its end; `None` when it is
    /// longer than `limit` bytes or cannot be read.
    fn read_line(input: &mut impl BufRead, limit: usize) -> Option<String> {
        let mut line = String::new();
        let read = input.take(limit as u64 + 2).read_line(&mut line).ok()?;
        let text = line.trim_end_matches(['\r', '\n']);
        (read > 0 && text.len() <= limit).then(|| text.to_owned())
    }

    /// A cookie value and nothing else.
    fn session_ok(session: &str) -> bool {
        let plain = |byte: u8| matches!(byte, 0x21..=0x7e) && !matches!(byte, b'"' | b',' | b';' | b'\\');
        !session.is_empty() && session.len() <= SESSION_MAX_BYTES && session.bytes().all(plain)
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

    /// What the main thread asks of the native half once it is qualified.
    enum ToNative {
        Streams(mpsc::Sender<Streams>),
        Stop,
    }

    /// The link's committed streams, as sampled: how many times one opened
    /// while none was open, and whether one is open now. A second stream
    /// that opens while another is open is not counted.
    #[derive(Clone, Copy, Default)]
    struct Streams {
        opened: u64,
        open: bool,
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

    fn report_status(report: &Report, status: PathStatus) {
        let preferred = match status.preferred {
            PreferredPath::Tunnel => "tunnel",
            PreferredPath::Direct(path) => path_word(Some(path)),
        };
        report.line(
            "path_status",
            json!({
                "preferred": preferred,
                "sequence": status.sequence,
                "closures": status.closure,
                "strict_failures": status.strict_failures,
            }),
        );
    }

    /// Runs the native qualification on a thread and a runtime of its own,
    /// and keeps the qualified client there until it is told to stop: every
    /// opening and closing of a direct stream, every change of the path's
    /// status and every withdrawal of the link is printed as it happens.
    fn start_native(
        host: String,
        instance: String,
        report: Report,
        to_main: mpsc::Sender<FromNative>,
        mut commands: tokio::sync::mpsc::UnboundedReceiver<ToNative>,
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

                let mut status = live.client.path_changes();
                report_status(&report, *status.borrow_and_update());
                let (mut status_live, mut retirements_live) = (true, true);
                let mut streams = Streams::default();
                let mut opened_at = None;
                let mut sample = tokio::time::interval(Duration::from_millis(5));
                loop {
                    tokio::select! {
                        command = commands.recv() => match command {
                            Some(ToNative::Streams(answer)) => {
                                let _ = answer.send(streams);
                            }
                            Some(ToNative::Stop) | None => break,
                        },
                        retired = live.retirements.recv(), if retirements_live => match retired {
                            Ok(fault) => report.fault("link_retired", fault),
                            Err(tokio::sync::broadcast::error::RecvError::Lagged(_)) => {}
                            Err(tokio::sync::broadcast::error::RecvError::Closed) => retirements_live = false,
                        },
                        changed = status.changed(), if status_live => match changed {
                            Ok(()) => report_status(&report, *status.borrow_and_update()),
                            Err(_) => status_live = false,
                        },
                        _ = sample.tick() => {
                            // A committed stream on the link: after the native
                            // check has let go of its own, only a page's.
                            let path = live.client.selected_path();
                            match (streams.open, path) {
                                (false, Some(_)) => {
                                    streams.open = true;
                                    streams.opened += 1;
                                    opened_at = Some(Instant::now());
                                    report.line("direct_stream_opened", json!({ "path": path_word(path) }));
                                }
                                (true, None) => {
                                    streams.open = false;
                                    let open_ms = opened_at.take().map_or(0, |at: Instant| at.elapsed().as_millis() as u64);
                                    report.line("direct_stream_closed", json!({ "open_ms": open_ms }));
                                }
                                _ => {}
                            }
                        }
                    }
                }
                report.line(
                    "link_at_end",
                    json!({ "path": path_word(live.client.selected_path()) }),
                );
            });
        })
    }

    /// The native half's count of direct streams, asked now.
    fn streams(native: &tokio::sync::mpsc::UnboundedSender<ToNative>) -> Option<Streams> {
        let (answer, answered) = mpsc::channel();
        native.send(ToNative::Streams(answer)).ok()?;
        answered.recv_timeout(Duration::from_secs(2)).ok()
    }

    /// Waits until no direct stream is open, for `RELEASE` at most.
    fn released(native: &tokio::sync::mpsc::UnboundedSender<ToNative>) -> bool {
        pump_until(RELEASE, || streams(native).is_some_and(|now| !now.open))
    }

    // MARK: The live-store leg

    /// One piece of work the live store's page is asked for: the word it
    /// answers ("ok" for a payload) and what the listener showed for it.
    fn live_attempt(
        document: &View,
        mtm: MainThreadMarker,
        native: &tokio::sync::mpsc::UnboundedSender<ToNative>,
        report: &Report,
        boundary: &'static str,
        script: &str,
    ) -> (bool, u64, bool) {
        let before = streams(native).unwrap_or_default();
        let answer = document.page_async(mtm, script);
        pump_until(SETTLE, || false);
        let after = streams(native).unwrap_or_default();
        let word = match answer.as_deref() {
            Some("ok") => "ok",
            Some("failed") => "failed",
            Some("opened") => "opened",
            Some("closed") => "closed",
            Some("error") => "error",
            Some("timeout") => "timeout",
            Some(_) => "unexpected_answer",
            None => "no_answer",
        };
        let opened = after.opened.saturating_sub(before.opened);
        report.line(
            boundary,
            json!({
                "ok": word == "ok" || word == "opened",
                "result": word,
                "direct_streams_opened": opened,
                "direct_stream_open_before": before.open,
                "direct_stream_open_after": after.open,
            }),
        );
        (word == "ok", opened, before.open)
    }

    /// A `/health` the page's own script fetches now, with or without the
    /// page's credentials.
    fn health_script(nonce: u128, credentials: &str) -> String {
        format!(
            r#"try {{
  const response = await fetch("/health?probe={nonce}", {{cache: "no-store", redirect: "error", credentials: "{credentials}"}});
  const body = await response.json();
  return response.status === 200 && body && body.status === "ok" ? "ok" : "unexpected";
}} catch (error) {{
  return "failed";
}}"#
        )
    }

    /// A WebSocket to the page's own host: a connection of its own. Whether
    /// the instance accepts it there says nothing; that it was tried does.
    fn websocket_script(nonce: u128) -> String {
        format!(
            r#"return await new Promise((resolve) => {{
  let socket;
  const timer = setTimeout(() => {{ try {{ socket.close(); }} catch {{}} resolve("timeout"); }}, 10000);
  const end = (word) => {{ clearTimeout(timer); resolve(word); }};
  try {{
    socket = new WebSocket("wss://" + location.host + "/health?probe={nonce}");
    socket.onopen = () => {{ socket.close(); end("opened"); }};
    socket.onerror = () => end("error");
    socket.onclose = () => end("closed");
  }} catch (error) {{
    end("error");
  }}
}});"#
        )
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
        let Some(host) = managed_host(&origin).map(str::to_owned) else {
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
        // The session travels with the origin it was issued for. It is read
        // only once that origin is known to be the one this run is for.
        let mut input = std::io::stdin().lock();
        let Some(session_origin) = read_line(&mut input, 300) else {
            report.failed("input", "session_origin_missing");
            return 2;
        };
        if managed_host(&session_origin).is_none() || session_origin != origin {
            report.failed("input", "session_is_for_another_origin");
            return 2;
        }
        let Some(session) = read_line(&mut input, SESSION_MAX_BYTES).filter(|session| session_ok(session)) else {
            report.failed("input", "session_invalid");
            return 2;
        };
        drop(input);
        report.ok("input");

        if !controls(mtm, &report) {
            return 1;
        }

        // The session's own document, on WebKit's own path: the rendezvous
        // request must come from it.
        let document = View::new(mtm, &origin);
        let held = document.seed_session(SESSION_COOKIE, &session);
        if !report_seeded(&report, "session_seeded", held, true) {
            return 3;
        }
        let loaded = document.load("/", &report, "document_answered", "document_committed");
        if !report_load(&report, "document_loaded", loaded) || !document.shows_its_origin() {
            return 3;
        }
        // The page loads for anyone. Whether the instance takes the session
        // is its own answer, before anything is asked of the direct path.
        if !session_accepted(&document, mtm, &report, "session_accepted") {
            return 3;
        }

        let (to_main, from_native) = mpsc::channel();
        let (native, commands) = tokio::sync::mpsc::unbounded_channel();
        report.ok("native_qualification_start");
        let worker = start_native(host.clone(), instance, report, to_main, commands);
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
            let _ = native.send(ToNative::Stop);
            let _ = worker.join();
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
        // The native check's own stream ends before a page's is counted.
        if released(&native) {
            report.ok("native_stream_released");
        } else {
            report.failed("native_stream_released", "stream_still_open");
        }

        // The fresh-store leg, the positive control of the page path: a
        // store that has never connected to the host, so the route carries
        // it from its first request. The session, then the route (the
        // qualified client's listener, its window credential, no failover),
        // then the first load.
        let fresh = {
            let page = View::new(mtm, &origin);
            let held = page.seed_session(SESSION_COOKIE, &session);
            if !report_seeded(&report, "fresh_session_seeded", held, true) {
                return finish(5);
            }
            if !install_route(
                &page.webview,
                mtm,
                &host,
                leg.proxy,
                &leg.username,
                &leg.password,
                false,
            ) {
                report.failed("fresh_route_installed", "refused_by_system");
                return finish(5);
            }
            report.ok("fresh_route_installed");
            let loaded = page.load("/", &report, "fresh_answered", "fresh_committed");
            if !report_load(&report, "fresh_loaded", loaded) || !page.shows_its_origin() {
                return finish(5);
            }
            // Over the direct path now: the instance still takes the session.
            if !session_accepted(&page, mtm, &report, "fresh_session_accepted") {
                return finish(5);
            }
            // A payload of the page's own, asked for now: not the native check's.
            let nonce = report.start.elapsed().as_nanos();
            let health = page.page_async(mtm, &health_script(nonce, "same-origin"));
            let ok = health.as_deref() == Some("ok");
            if ok {
                report.ok("fresh_payload");
            } else {
                report.failed(
                    "fresh_payload",
                    match health.as_deref() {
                        Some("failed") => "fetch_failed",
                        Some(_) => "unexpected_answer",
                        None => "no_answer",
                    },
                );
            }
            ok
            // The view and its store end here, and their connections with them.
        };
        if !fresh {
            return finish(5);
        }
        let clean = released(&native);
        if clean {
            report.ok("fresh_store_released");
        } else {
            // A stream that stays open hides a new one from the count.
            report.failed("fresh_store_released", "stream_still_open");
        }

        // The live-store leg: the session's document is connected to the
        // host on WebKit's own path. It gets the route now, while it is
        // live, and is asked for work of several kinds.
        if !install_route(
            &document.webview,
            mtm,
            &host,
            leg.proxy,
            &leg.username,
            &leg.password,
            false,
        ) {
            report.failed("live_route_installed", "refused_by_system");
            return finish(6);
        }
        report.ok("live_route_installed");
        let nonce = || report.start.elapsed().as_nanos();
        let mut payload_on_new_stream = false;
        let mut stream_without_payload = false;
        let mut any_stream = false;
        let mut attempt = |boundary: &'static str, script: String, payload: bool| {
            let (ok, opened, busy) = live_attempt(&document, mtm, &native, &report, boundary, &script);
            any_stream |= opened > 0;
            if payload && opened > 0 && !busy {
                payload_on_new_stream |= ok;
                stream_without_payload |= !ok;
            }
        };
        attempt("live_fetch", health_script(nonce(), "same-origin"), true);
        attempt("live_fetch_without_credentials", health_script(nonce(), "omit"), true);
        attempt("live_websocket", websocket_script(nonce()), false);
        let reloaded = document.load("/", &report, "live_reload_answered", "live_reload_committed");
        if report_load(&report, "live_reloaded", reloaded) && document.shows_its_origin() {
            attempt("live_fetch_after_reload", health_script(nonce(), "same-origin"), true);
        }
        let code = if payload_on_new_stream {
            report.ok("live_store_direct_payload");
            0
        } else {
            report.failed(
                "live_store_direct_payload",
                if stream_without_payload {
                    "direct_stream_without_payload"
                } else if any_stream {
                    "direct_stream_not_for_a_payload"
                } else if clean {
                    "route_not_taken"
                } else {
                    "route_not_taken_or_hidden"
                },
            );
            6
        };
        // Whatever ends the link in the next moments is printed by the
        // native half as it happens.
        pump_until(AFTERWARDS, || false);
        finish(code)
    }
}
