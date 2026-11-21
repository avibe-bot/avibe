//! Hermetic probe of the direct path's WebView adapters with a real WebView
//! and no window: a WKWebView on macOS, a WebView2 on Windows, on loopback
//! pages. Nothing is shown, the app is never activated, the browsing data is
//! not persistent (macOS) or lives in a folder under the system's temporary
//! directory that the probe removes (Windows), and no keychain or clipboard
//! is touched.
//!
//! One loopback server is the instance: its pages, its rendezvous route, and
//! a record of every request. A second listener is the direct listener's
//! stand-in: an HTTP CONNECT proxy that demands the route's credential,
//! serves one host, and records what it was asked.
//!
//! The shell's modules are compiled into this executable from their source
//! files, so the probe runs the shipping code without the library exporting
//! anything for it. Each case prints one JSON line; the exit code is the
//! number of failed cases.

#[cfg(not(any(target_os = "macos", windows)))]
fn main() {}

#[cfg(target_os = "macos")]
fn main() {
    std::process::exit(macos_probe::run());
}

#[cfg(windows)]
fn main() {
    std::process::exit(windows_probe::run());
}

// Side by side at the root, as in the shell's `direct` module: the adapters
// name the request through `super`.
// The shell's modules offer more than one system's probe calls.
#[cfg(any(target_os = "macos", windows))]
#[allow(dead_code)]
#[path = "../src/direct/rendezvous.rs"]
mod rendezvous;
#[cfg(target_os = "macos")]
#[allow(dead_code)]
#[path = "../src/direct/webview_macos.rs"]
mod webview_macos;
#[cfg(windows)]
#[allow(dead_code)]
#[path = "../src/direct/webview_windows.rs"]
mod webview_windows;

/// The instance, the direct listener's stand-in, and the report: the same on
/// both systems.
#[cfg(any(target_os = "macos", windows))]
#[allow(dead_code)]
mod fixture {
    use std::{
        collections::HashMap,
        io::{Read, Write},
        net::{Ipv4Addr, Shutdown, TcpListener, TcpStream},
        sync::{Arc, Mutex},
        thread,
        time::Duration,
    };

    use serde_json::json;

    use crate::rendezvous::{MAX_ANSWER_BYTES, PATH};

    pub const TOKEN: &str = "tok-123";
    pub const SESSION: &str = "s3cret-session";
    pub const ENDPOINT: &str = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";
    pub const ANSWER: &str = r#"{"version":1,"status":"ready","probe":"the instance's own answer"}"#;
    /// The host the route sends to the proxy. It resolves nowhere: only the
    /// proxy can reach it, so a load of it proves the route.
    pub const ROUTED: &str = "instance.avibe-probe.test";
    pub const USERNAME: &str = "avibe";
    pub const PASSWORD: &str = "probe-password-9f2c";

    // MARK: The instance

    #[derive(Clone, Debug)]
    pub struct Seen {
        pub method: String,
        pub path: String,
        pub headers: HashMap<String, String>,
        pub body: String,
    }

    #[derive(Clone, Copy, PartialEq)]
    pub enum Answer {
        Ready,
        Redirect,
        Forbidden,
        Oversize,
    }

    pub struct Instance {
        pub port: u16,
        seen: Arc<Mutex<Vec<Seen>>>,
        pub answer: Arc<Mutex<Answer>>,
    }

    impl Instance {
        pub fn start() -> Self {
            let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).expect("instance port");
            let port = listener.local_addr().expect("instance address").port();
            let seen = Arc::new(Mutex::new(Vec::new()));
            let answer = Arc::new(Mutex::new(Answer::Ready));
            let (log, mode) = (seen.clone(), answer.clone());
            thread::spawn(move || {
                for stream in listener.incoming().flatten() {
                    let (log, mode) = (log.clone(), mode.clone());
                    thread::spawn(move || serve_instance(stream, &log, &mode));
                }
            });
            Self { port, seen, answer }
        }

        pub fn origin(&self) -> String {
            format!("http://127.0.0.1:{}", self.port)
        }

        pub fn rendezvous_requests(&self) -> Vec<Seen> {
            self.requests(PATH)
        }

        pub fn requests(&self, path: &str) -> Vec<Seen> {
            self.seen
                .lock()
                .expect("seen")
                .iter()
                .filter(|seen| seen.path == path)
                .cloned()
                .collect()
        }

        pub fn forget(&self) {
            self.seen.lock().expect("seen").clear();
        }
    }

    fn read_head(stream: &mut TcpStream) -> Option<(String, Vec<u8>)> {
        let mut data = Vec::new();
        let mut buffer = [0u8; 2048];
        loop {
            if let Some(end) = data.windows(4).position(|window| window == b"\r\n\r\n") {
                let head = String::from_utf8_lossy(&data[..end]).into_owned();
                return Some((head, data[end + 4..].to_vec()));
            }
            if data.len() > 64 * 1024 {
                return None;
            }
            let read = stream.read(&mut buffer).ok()?;
            if read == 0 {
                return None;
            }
            data.extend_from_slice(&buffer[..read]);
        }
    }

    fn headers_of(head: &str) -> HashMap<String, String> {
        head.split("\r\n")
            .skip(1)
            .filter_map(|line| line.split_once(':'))
            .map(|(name, value)| (name.trim().to_ascii_lowercase(), value.trim().to_owned()))
            .collect()
    }

    fn serve_instance(mut stream: TcpStream, log: &Mutex<Vec<Seen>>, mode: &Mutex<Answer>) {
        let _ = stream.set_read_timeout(Some(Duration::from_secs(10)));
        let Some((head, mut body)) = read_head(&mut stream) else {
            return;
        };
        let mut first = head.split("\r\n").next().unwrap_or("").split(' ');
        let (method, path) = (
            first.next().unwrap_or("").to_owned(),
            first.next().unwrap_or("").to_owned(),
        );
        let headers = headers_of(&head);
        let length: usize = headers
            .get("content-length")
            .and_then(|value| value.parse().ok())
            .unwrap_or(0);
        let mut buffer = [0u8; 2048];
        while body.len() < length {
            match stream.read(&mut buffer) {
                Ok(read) if read > 0 => body.extend_from_slice(&buffer[..read]),
                _ => break,
            }
        }
        log.lock().expect("seen").push(Seen {
            method: method.clone(),
            path: path.clone(),
            headers,
            body: String::from_utf8_lossy(&body).into_owned(),
        });
        let page = |title: &str, script: &str| {
            let html =
                format!("<!doctype html><meta charset=\"utf-8\"><title>{title}</title><script>{script}</script>");
            format!(
                "HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nCache-Control: no-store\r\n\
                 Set-Cookie: vibe_csrf_token={TOKEN}; Path=/\r\nSet-Cookie: session={SESSION}; Path=/; HttpOnly\r\n\
                 Content-Length: {}\r\nConnection: close\r\n\r\n{html}",
                html.len()
            )
        };
        let plain = |status: &str, kind: &str, text: &str| {
            format!(
                "HTTP/1.1 {status}\r\nContent-Type: {kind}\r\nCache-Control: no-store\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{text}",
                text.len()
            )
        };
        let response = match (method.as_str(), path.as_str()) {
            ("GET", "/") | ("GET", "/routed") | ("GET", "/unrouted") => page("plain", ""),
            ("GET", "/tamper") => page("tamper", TAMPER),
            ("GET", "/worker-page") => page("registering", WORKER_PAGE),
            ("GET", "/worker.js") => plain("200 OK", "text/javascript", WORKER),
            ("GET", "/elsewhere") => plain("200 OK", "application/json", r#"{"moved":true}"#),
            // A site's own challenge, in the listener's realm.
            ("GET", "/basic") => "HTTP/1.1 401 Unauthorized\r\nWWW-Authenticate: Basic realm=\"Avibe Direct\"\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".to_owned(),
            ("POST", PATH) => match *mode.lock().expect("mode") {
                Answer::Ready => plain("200 OK", "application/json", ANSWER),
                Answer::Redirect => "HTTP/1.1 307 Temporary Redirect\r\nLocation: /elsewhere\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".to_owned(),
                Answer::Forbidden => plain("403 Forbidden", "application/json", r#"{"error":"forbidden"}"#),
                Answer::Oversize => plain("200 OK", "application/json", &"x".repeat(MAX_ANSWER_BYTES + 1)),
            },
            _ => plain("404 Not Found", "text/plain", "not found"),
        };
        let _ = stream.write_all(response.as_bytes());
        let _ = stream.shutdown(Shutdown::Both);
    }

    /// A page that replaces everything the rendezvous script calls, in its
    /// own world, counts every use of a replacement, and remembers what its
    /// global object had before.
    pub const TAMPER: &str = r#"
window.__calls = 0;
const count = () => { window.__calls += 1; };
window.fetch = () => { count(); return Promise.resolve(new Response('{"forged":true}')); };
XMLHttpRequest.prototype.open = function () { count(); };
JSON.stringify = () => { count(); return '{"ok":true,"status":200,"body":"forged"}'; };
JSON.parse = () => { count(); return {}; };
Object.defineProperty(Document.prototype, 'cookie', { get() { count(); return 'vibe_csrf_token=forged'; }, set() {} });
window.TextDecoder = class { decode() { count(); return 'forged'; } };
window.AbortController = class { constructor() { count(); } abort() {} };
Response.prototype.text = () => { count(); return Promise.resolve('forged'); };
ReadableStream.prototype.getReader = function () { count(); return { read: () => Promise.resolve({ done: true }) }; };
String.prototype.startsWith = function () { count(); return true; };
window.setTimeout = () => { count(); return 0; };
Object.defineProperty(window, 'origin', { get() { count(); return 'http://forged.example'; } });
window.__before = [];
window.__before = Object.getOwnPropertyNames(window);
"#;

    pub const WORKER_PAGE: &str = r#"
(async () => {
  if (!navigator.serviceWorker) { document.title = 'no-worker-container'; return; }
  try {
    await navigator.serviceWorker.register('/worker.js');
    await navigator.serviceWorker.ready;
    for (let tries = 0; tries < 200 && !navigator.serviceWorker.controller; tries += 1) {
      await new Promise((resolve) => setTimeout(resolve, 50));
    }
    document.title = navigator.serviceWorker.controller ? 'controlled' : 'not-controlled';
  } catch (error) {
    document.title = 'worker-error';
  }
})();
"#;

    /// A worker that answers the rendezvous in the instance's place.
    pub const WORKER: &str = r#"
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (event) => event.waitUntil(self.clients.claim()));
self.addEventListener('fetch', (event) => {
  if (new URL(event.request.url).pathname === '/api/direct/rendezvous') {
    event.respondWith(new Response('{"forged":"by the worker"}', { status: 200, headers: { 'Content-Type': 'application/json' } }));
  }
});
"#;

    // MARK: The direct listener's stand-in

    #[derive(Clone, Debug, PartialEq)]
    pub struct Asked {
        pub target: String,
        /// `none`, `wrong`, or `right`.
        pub credential: &'static str,
    }

    pub struct Proxy {
        pub port: u16,
        asked: Arc<Mutex<Vec<Asked>>>,
    }

    impl Proxy {
        /// Serves `CONNECT` to `target` alone. With `upstream` the tunnel
        /// runs to that loopback port; without, it is granted and closed,
        /// which is enough to see who asked and with what.
        pub fn start(target: String, upstream: Option<u16>) -> Self {
            let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).expect("proxy port");
            let port = listener.local_addr().expect("proxy address").port();
            let asked = Arc::new(Mutex::new(Vec::new()));
            let log = asked.clone();
            thread::spawn(move || {
                for stream in listener.incoming().flatten() {
                    let (log, target) = (log.clone(), target.clone());
                    thread::spawn(move || serve_proxy(stream, &target, upstream, &log));
                }
            });
            Self { port, asked }
        }

        pub fn asked(&self) -> Vec<Asked> {
            self.asked.lock().expect("asked").clone()
        }

        pub fn forget(&self) {
            self.asked.lock().expect("asked").clear();
        }
    }

    fn serve_proxy(mut browser: TcpStream, served: &str, upstream: Option<u16>, log: &Mutex<Vec<Asked>>) {
        use base64::{engine::general_purpose::STANDARD, Engine};
        let _ = browser.set_read_timeout(Some(Duration::from_secs(10)));
        let Some((head, early)) = read_head(&mut browser) else {
            return;
        };
        let mut first = head.split("\r\n").next().unwrap_or("").split(' ');
        let (method, target) = (first.next().unwrap_or(""), first.next().unwrap_or("").to_owned());
        let expected = format!("Basic {}", STANDARD.encode(format!("{USERNAME}:{PASSWORD}")));
        let credential = match headers_of(&head).get("proxy-authorization") {
            None => "none",
            Some(value) if *value == expected => "right",
            Some(_) => "wrong",
        };
        log.lock().expect("asked").push(Asked {
            target: target.clone(),
            credential,
        });
        let refuse = |browser: &mut TcpStream, answer: &str| {
            let _ = browser.write_all(answer.as_bytes());
            let _ = browser.shutdown(Shutdown::Both);
        };
        if method != "CONNECT" {
            return refuse(
                &mut browser,
                "HTTP/1.1 405 Method Not Allowed\r\nConnection: close\r\n\r\n",
            );
        }
        if credential != "right" {
            return refuse(
                &mut browser,
                "HTTP/1.1 407 Proxy Authentication Required\r\nProxy-Authenticate: Basic realm=\"Avibe Direct\"\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",
            );
        }
        // One host, as the real listener: a child host is refused.
        if target != served {
            return refuse(&mut browser, "HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n");
        }
        let Some(upstream) = upstream else {
            return refuse(&mut browser, "HTTP/1.1 200 Connection Established\r\n\r\n");
        };
        let Ok(mut upstream) = TcpStream::connect((Ipv4Addr::LOCALHOST, upstream)) else {
            return refuse(&mut browser, "HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n");
        };
        if browser
            .write_all(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            .is_err()
            || upstream.write_all(&early).is_err()
        {
            return;
        }
        let _ = browser.set_read_timeout(None);
        let (Ok(mut browser_read), Ok(mut upstream_write)) = (browser.try_clone(), upstream.try_clone()) else {
            return;
        };
        let up = thread::spawn(move || {
            let _ = std::io::copy(&mut browser_read, &mut upstream_write);
            let _ = upstream_write.shutdown(Shutdown::Write);
        });
        let _ = std::io::copy(&mut upstream, &mut browser);
        let _ = browser.shutdown(Shutdown::Both);
        let _ = up.join();
    }

    /// What a page needs before it registers the product's push worker
    /// (`ui/src/lib/webPush.ts`): the three APIs, and a permission that is
    /// granted when it asks. Read in the page's own world.
    pub const PUSH_OFFER: &str = "window.__asked = 'pending'; \
        try { Notification.requestPermission().then((value) => { window.__asked = String(value); }, () => { window.__asked = 'threw'; }); } \
        catch (error) { window.__asked = 'absent'; } \
        JSON.stringify({ serviceWorker: 'serviceWorker' in navigator, pushManager: 'PushManager' in window, \
        notification: 'Notification' in window, permission: 'Notification' in window ? Notification.permission : 'absent' })";
    pub const PUSH_ASKED: &str = "String(window.__asked)";

    /// One of the three APIs, as `PUSH_OFFER` read it: `None` when the
    /// reading itself is missing or is not what `PUSH_OFFER` answers.
    pub fn offers(offered: Option<&str>, api: &str) -> Option<bool> {
        offered
            .and_then(|text| serde_json::from_str::<serde_json::Value>(text).ok())?
            .get(api)?
            .as_bool()
    }

    /// Whether a page that read `offered` (`PUSH_OFFER`) and was answered
    /// `asked` (`PUSH_ASKED`) gets as far as registering its push worker.
    pub fn registers_push_worker(offered: Option<&str>, asked: Option<&str>) -> bool {
        ["serviceWorker", "pushManager", "notification"]
            .iter()
            .all(|api| offers(offered, api) == Some(true))
            && asked == Some("granted")
    }

    pub struct Report {
        pub failed: i32,
    }

    impl Report {
        pub fn case(&mut self, name: &str, ok: bool, detail: serde_json::Value) {
            if !ok {
                self.failed += 1;
            }
            println!("{}", json!({ "case": name, "ok": ok, "detail": detail }));
        }
    }
}

#[cfg(target_os = "macos")]
mod macos_probe {
    use std::{
        cell::RefCell,
        ffi::c_void,
        net::{Ipv4Addr, SocketAddrV4},
        rc::Rc,
        time::{Duration, Instant},
    };

    use block2::RcBlock;
    use objc2::{rc::Retained, runtime::AnyObject, MainThreadMarker, MainThreadOnly};
    use objc2_foundation::{NSError, NSPoint, NSRect, NSSize, NSString, NSURLRequest, NSURL};
    use objc2_web_kit::{WKWebView, WKWebViewConfiguration, WKWebsiteDataStore};
    use serde_json::json;

    use crate::{
        fixture::*,
        rendezvous::{Reply, Request},
        webview_macos::{fetch_rendezvous, install_route, remove_route},
    };

    const WAIT: Duration = Duration::from_secs(20);

    // MARK: The WebView

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

    fn new_webview(mtm: MainThreadMarker) -> Retained<WKWebView> {
        // SAFETY: Main thread; a fresh configuration with browsing data that
        // is never written to disk.
        unsafe {
            let configuration = WKWebViewConfiguration::new(mtm);
            configuration.setWebsiteDataStore(&WKWebsiteDataStore::nonPersistentDataStore(mtm));
            let frame = NSRect::new(NSPoint::new(0.0, 0.0), NSSize::new(800.0, 600.0));
            WKWebView::initWithFrame_configuration(WKWebView::alloc(mtm), frame, &configuration)
        }
    }

    /// Evaluates `script` in the page's own world: what page script sees.
    fn page_eval(webview: &WKWebView, script: &str) -> Option<String> {
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
        // SAFETY: Main thread.
        unsafe { webview.evaluateJavaScript_completionHandler(&NSString::from_str(script), Some(&block)) };
        pump_until(WAIT, || slot.borrow().is_some());
        let answered = slot.borrow_mut().take().flatten();
        answered
    }

    /// Loads `url` and waits until its document says `title`.
    fn load(webview: &WKWebView, url: &str, title: &str) -> bool {
        // SAFETY: Main thread; `url` is one of this probe's own URLs.
        unsafe {
            let Some(target) = NSURL::URLWithString(&NSString::from_str(url)) else {
                return false;
            };
            webview.loadRequest(&NSURLRequest::requestWithURL(&target));
        }
        let wanted = format!("{url}|{title}");
        pump_until(WAIT, || {
            // SAFETY: Main thread.
            !unsafe { webview.isLoading() }
                && page_eval(webview, "String(location.href) + '|' + String(document.title)").as_deref()
                    == Some(&wanted)
        })
    }

    fn fetch(
        webview: &WKWebView,
        mtm: MainThreadMarker,
        request: &Request,
        current: impl Fn() -> bool + 'static,
    ) -> Option<Reply> {
        let slot: Rc<RefCell<Option<Reply>>> = Rc::new(RefCell::new(None));
        let filled = slot.clone();
        fetch_rendezvous(webview, mtm, request, current, move |reply| {
            *filled.borrow_mut() = Some(reply)
        });
        pump_until(WAIT, || slot.borrow().is_some());
        let answered = slot.borrow_mut().take();
        answered
    }

    pub fn run() -> i32 {
        let mtm = MainThreadMarker::new().expect("the probe runs on the main thread");
        let instance = Instance::start();
        let proxy = Proxy::start(format!("{ROUTED}:{}", instance.port), Some(instance.port));
        let origin = instance.origin();
        let request = Request::new(&origin, ENDPOINT).expect("a valid request");
        let mut report = Report { failed: 0 };
        let webview = new_webview(mtm);

        // 1. The instance's own document: the answer comes back exactly, and
        //    the request carried this WebView's session and the usual checks.
        let loaded = load(&webview, &format!("{origin}/"), "plain");
        instance.forget();
        let reply = fetch(&webview, mtm, &request, || true);
        let seen = instance.rendezvous_requests();
        let one = seen.first();
        let header = |name: &str| one.and_then(|seen| seen.headers.get(name).cloned()).unwrap_or_default();
        report.case(
            "the-instances-document-gets-the-instances-answer",
            loaded
                && reply == Some(Reply::Ready(ANSWER.to_owned()))
                && seen.len() == 1
                && one.is_some_and(|seen| seen.method == "POST")
                && header("x-vibe-csrf-token") == TOKEN
                && header("cookie").contains(&format!("session={SESSION}"))
                && header("origin") == origin
                && header("content-type") == "application/json"
                && one.is_some_and(|seen| seen.body == format!(r#"{{"client_endpoint_id":"{ENDPOINT}","version":1}}"#)),
            json!({
                "loaded": loaded, "ready": matches!(reply, Some(Reply::Ready(_))), "requests": seen.len(),
                "csrf": header("x-vibe-csrf-token") == TOKEN, "session": header("cookie").contains("session="),
                "origin": header("origin") == origin, "body": one.map(|seen| seen.body.clone()),
            }),
        );

        // What this WebView offers a page that wants web push. WKWebView has
        // no `PushManager`, so the page never registers its push worker, and
        // an instance window is never controlled by it: the rendezvous is
        // not refused for that reason. The case is the missing API alone,
        // read successfully: it fails if a later WebKit adds it, whatever an
        // unattended permission request answers (shown only for the record).
        let offered = page_eval(&webview, PUSH_OFFER);
        pump_until(Duration::from_secs(3), || false);
        let asked = page_eval(&webview, PUSH_ASKED);
        report.case(
            "a-page-cannot-register-its-push-worker",
            offers(offered.as_deref(), "serviceWorker").is_some()
                && offers(offered.as_deref(), "pushManager") == Some(false),
            json!({ "offered": offered, "asked": asked }),
        );

        // 2. A page that replaced everything the script calls: the script
        //    used none of it, the answer is still the instance's, and the
        //    page's world gained nothing it could read.
        let loaded = load(&webview, &format!("{origin}/tamper"), "tamper");
        instance.forget();
        let reply = fetch(&webview, mtm, &request, || true);
        let seen = instance.rendezvous_requests();
        let page = page_eval(
            &webview,
            "String(window.__calls) + '|' + Object.getOwnPropertyNames(window).filter((name) => !window.__before.includes(name)).join(',')",
        );
        report.case(
            "page-script-cannot-see-or-alter-the-request",
            loaded
                && reply == Some(Reply::Ready(ANSWER.to_owned()))
                && seen.len() == 1
                && seen[0].headers.get("x-vibe-csrf-token").map(String::as_str) == Some(TOKEN)
                && page.as_deref() == Some("0|"),
            json!({ "loaded": loaded, "ready": matches!(reply, Some(Reply::Ready(_))), "requests": seen.len(), "page": page }),
        );

        // 3. Another origin's document, and a selection that is no longer
        //    current: nothing is asked.
        instance.forget();
        let elsewhere =
            Request::new(&format!("http://localhost:{}", instance.port), ENDPOINT).expect("a valid request");
        let wrong_origin = fetch(&webview, mtm, &elsewhere, || true);
        let stale = fetch(&webview, mtm, &request, || false);
        report.case(
            "another-origin-or-a-stale-selection-asks-nothing",
            wrong_origin == Some(Reply::NotReady)
                && stale == Some(Reply::SelectionChanged)
                && instance.rendezvous_requests().is_empty(),
            json!({ "wrong_origin": format!("{wrong_origin:?}"), "stale": format!("{stale:?}"), "requests": instance.rendezvous_requests().len() }),
        );

        // 4. A selection that changes while the request runs: the instance
        //    answered, and the answer is dropped.
        let loaded = load(&webview, &format!("{origin}/"), "plain");
        instance.forget();
        let asked = Rc::new(RefCell::new(0));
        let counter = asked.clone();
        let changed = fetch(&webview, mtm, &request, move || {
            *counter.borrow_mut() += 1;
            *counter.borrow() == 1
        });
        let requests = instance.rendezvous_requests().len();
        // The same when the instance refused meanwhile: the selection's
        // change is what is said, whatever the instance said.
        *instance.answer.lock().expect("mode") = Answer::Forbidden;
        let first = Rc::new(RefCell::new(true));
        let refused = fetch(&webview, mtm, &request, move || first.replace(false));
        *instance.answer.lock().expect("mode") = Answer::Ready;
        report.case(
            "an-answer-for-a-selection-that-changed-is-dropped",
            loaded
                && changed == Some(Reply::SelectionChanged)
                && requests == 1
                && *asked.borrow() == 2
                && refused == Some(Reply::SelectionChanged),
            json!({ "reply": format!("{changed:?}"), "requests": requests, "asked": *asked.borrow(), "refused": format!("{refused:?}") }),
        );

        // 5. A redirect is refused and not followed; a refusal and an answer
        //    that is too large give no body.
        let mut outcomes = Vec::new();
        for (answer, expected) in [
            (Answer::Redirect, Reply::Failed),
            (Answer::Forbidden, Reply::Refused(403)),
            (Answer::Oversize, Reply::Failed),
        ] {
            *instance.answer.lock().expect("mode") = answer;
            instance.forget();
            let reply = fetch(&webview, mtm, &request, || true);
            let followed = !instance.requests("/elsewhere").is_empty();
            outcomes.push((reply == Some(expected), followed, format!("{reply:?}")));
        }
        *instance.answer.lock().expect("mode") = Answer::Ready;
        report.case(
            "a-redirect-a-refusal-and-an-oversize-answer-give-no-body",
            outcomes
                .iter()
                .all(|(as_expected, followed, _)| *as_expected && !followed),
            json!(outcomes
                .iter()
                .map(|(ok, followed, reply)| json!({ "ok": ok, "followed": followed, "reply": reply }))
                .collect::<Vec<_>>()),
        );

        // 6. A service worker that answers in the instance's place.
        let state = {
            // SAFETY: Main thread.
            unsafe {
                if let Some(target) = NSURL::URLWithString(&NSString::from_str(&format!("{origin}/worker-page"))) {
                    webview.loadRequest(&NSURLRequest::requestWithURL(&target));
                }
            }
            let mut title = None;
            pump_until(WAIT, || {
                title = page_eval(&webview, "String(document.title)");
                matches!(
                    title.as_deref(),
                    Some("controlled" | "not-controlled" | "no-worker-container" | "worker-error")
                )
            });
            title.unwrap_or_default()
        };
        instance.forget();
        let reply = fetch(&webview, mtm, &request, || true);
        let reached = instance.rendezvous_requests().len();
        let ok = match state.as_str() {
            // A worker controls the document: refused, whatever it answered.
            "controlled" => reply == Some(Reply::Controlled),
            // This WebView gives pages no service worker: nothing can stand
            // between the request and the instance, and the answer is its own.
            "no-worker-container" => reply == Some(Reply::Ready(ANSWER.to_owned())) && reached == 1,
            _ => false,
        };
        report.case(
            "a-controlling-service-worker-is-refused",
            ok,
            json!({ "document": state, "reply": format!("{reply:?}").chars().take(40).collect::<String>(), "reached_instance": reached }),
        );

        // 7. The route: only the instance's host goes to the listener, with
        //    its credential; the credential never reaches the instance.
        let routed_origin = format!("http://{ROUTED}:{}", instance.port);
        let before = load(&webview, &format!("{routed_origin}/routed"), "plain");
        let installed = install_route(
            &webview,
            mtm,
            ROUTED,
            SocketAddrV4::new(Ipv4Addr::LOCALHOST, proxy.port),
            USERNAME,
            PASSWORD,
        );
        proxy.forget();
        instance.forget();
        let routed = load(&webview, &format!("{routed_origin}/routed"), "plain");
        let asked = proxy.asked();
        let through = instance.requests("/routed");
        report.case(
            "the-instances-host-loads-through-the-listener-with-its-credential",
            !before
                && installed
                && routed
                && asked.iter().any(|asked| asked.target == format!("{ROUTED}:{}", instance.port) && asked.credential == "right")
                && asked.iter().all(|asked| asked.credential != "wrong")
                && through.len() == 1
                && !through[0].headers.contains_key("proxy-authorization")
                && through[0].headers.get("host").map(String::as_str) == Some(&format!("{ROUTED}:{}", instance.port)),
            json!({
                "loads_without_the_route": before, "installed": installed, "loaded": routed,
                "asked": asked.iter().map(|asked| format!("{} {}", asked.target.replace(&instance.port.to_string(), "PORT"), asked.credential)).collect::<Vec<_>>(),
                "instance_saw_proxy_credential": through.iter().any(|seen| seen.headers.contains_key("proxy-authorization")),
            }),
        );

        // 8. The rendezvous from a routed document goes through the listener
        //    too, and is still the instance's own answer.
        let routed_request = Request::new(&routed_origin, ENDPOINT).expect("a valid request");
        instance.forget();
        let reply = fetch(&webview, mtm, &routed_request, || true);
        report.case(
            "the-rendezvous-of-a-routed-document-is-the-instances",
            reply == Some(Reply::Ready(ANSWER.to_owned())) && instance.rendezvous_requests().len() == 1,
            json!({ "ready": matches!(reply, Some(Reply::Ready(_))), "requests": instance.rendezvous_requests().len() }),
        );

        // 9. Any other host is not routed: the listener is not asked.
        proxy.forget();
        let unrouted = load(&webview, &format!("{origin}/unrouted"), "plain");
        report.case(
            "another-host-is-not-routed",
            unrouted && proxy.asked().is_empty(),
            json!({ "loaded": unrouted, "listener_asked": proxy.asked().len() }),
        );

        // 10. A child host matches the system's rule and is refused by the
        //     listener: it does not load, and the instance never hears of it.
        proxy.forget();
        instance.forget();
        let child = load(
            &webview,
            &format!("http://child.{ROUTED}:{}/routed", instance.port),
            "plain",
        );
        report.case(
            "a-child-host-is-refused-by-the-listener",
            !child && instance.requests("/routed").is_empty(),
            json!({
                "loaded": child, "reached_instance": instance.requests("/routed").len(),
                "asked": proxy.asked().iter().map(|asked| format!("{} {}", asked.target.replace(&instance.port.to_string(), "PORT"), asked.credential)).collect::<Vec<_>>(),
            }),
        );

        // 11. With the route removed the listener is not asked any more.
        let removed = remove_route(&webview, mtm);
        proxy.forget();
        let after = load(&webview, &format!("{routed_origin}/routed"), "plain");
        report.case(
            "a-removed-route-sends-nothing-to-the-listener",
            removed && !after && proxy.asked().is_empty(),
            json!({ "removed": removed, "loaded": after, "listener_asked": proxy.asked().len() }),
        );

        report.failed
    }
}

#[cfg(windows)]
mod windows_probe {
    use std::{
        cell::RefCell,
        ffi::c_void,
        net::{Ipv4Addr, SocketAddrV4},
        rc::Rc,
        sync::mpsc,
        time::{Duration, Instant},
    };

    use base64::{engine::general_purpose::STANDARD, Engine};
    use serde_json::json;
    use webview2_com::{
        take_pwstr, BasicAuthenticationRequestedEventHandler, CoreWebView2EnvironmentOptions,
        CreateCoreWebView2ControllerCompletedHandler, CreateCoreWebView2EnvironmentCompletedHandler,
        ExecuteScriptCompletedHandler,
        Microsoft::Web::WebView2::Win32::{
            CreateCoreWebView2EnvironmentWithOptions, ICoreWebView2, ICoreWebView2Controller, ICoreWebView2Environment,
            ICoreWebView2Environment10, ICoreWebView2EnvironmentOptions, ICoreWebView2_10,
        },
    };
    use windows::{
        core::{Interface, HSTRING, PCWSTR, PWSTR},
        Win32::Foundation::{E_POINTER, HWND},
    };
    use windows_sys::Win32::UI::WindowsAndMessaging::{
        DispatchMessageW, PeekMessageW, TranslateMessage, MSG, PM_REMOVE,
    };

    use crate::{
        fixture::*,
        rendezvous::{Reply, Request},
        webview_windows::{answer_listener_challenge, fetch_rendezvous, route_argument},
    };

    const WAIT: Duration = Duration::from_secs(30);
    /// The whole probe. A WebView2 that never answers must not hold the job.
    const LIMIT: Duration = Duration::from_secs(300);

    #[link(name = "ole32")]
    extern "system" {
        fn CoInitializeEx(reserved: *const c_void, model: u32) -> i32;
    }

    /// Dispatches this thread's messages until `ready`, or for `limit`.
    /// WebView2 delivers every completion and event as a message.
    fn pump_until(limit: Duration, mut ready: impl FnMut() -> bool) -> bool {
        let deadline = Instant::now() + limit;
        loop {
            // SAFETY: This thread's own message queue.
            unsafe {
                let mut message: MSG = std::mem::zeroed();
                while PeekMessageW(&mut message, std::ptr::null_mut(), 0, 0, PM_REMOVE) != 0 {
                    TranslateMessage(&message);
                    DispatchMessageW(&message);
                }
            }
            if ready() {
                return true;
            }
            if Instant::now() >= deadline {
                return false;
            }
            std::thread::sleep(Duration::from_millis(5));
        }
    }

    /// A WebView2 with no window, on a new user-data folder, made with
    /// `arguments`.
    /// `private`: an InPrivate WebView, as the app's instance windows are.
    fn new_webview(
        folder: &std::path::Path,
        arguments: String,
        private: bool,
    ) -> webview2_com::Result<(ICoreWebView2Controller, ICoreWebView2)> {
        let options = CoreWebView2EnvironmentOptions::default();
        // SAFETY: Set before the options are handed to WebView2.
        unsafe { options.set_additional_browser_arguments(arguments) };
        let options: ICoreWebView2EnvironmentOptions = options.into();
        let folder = HSTRING::from(folder.as_os_str());
        let (sender, receiver) = mpsc::channel::<ICoreWebView2Environment>();
        CreateCoreWebView2EnvironmentCompletedHandler::wait_for_async_operation(
            Box::new(move |handler| {
                // SAFETY: A single-threaded apartment; the strings outlive the call.
                unsafe { CreateCoreWebView2EnvironmentWithOptions(PCWSTR::null(), &folder, &options, &handler) }
                    .map_err(webview2_com::Error::WindowsError)
            }),
            Box::new(move |status, environment| {
                status?;
                let _ = sender.send(environment.ok_or_else(|| windows::core::Error::from(E_POINTER))?);
                Ok(())
            }),
        )?;
        let environment = receiver.recv().map_err(|_| webview2_com::Error::SendError)?;
        let (sender, receiver) = mpsc::channel::<ICoreWebView2Controller>();
        CreateCoreWebView2ControllerCompletedHandler::wait_for_async_operation(
            Box::new(move |handler| {
                // `HWND_MESSAGE`: a message-only parent, so nothing is shown.
                let parent = HWND(-3isize as *mut c_void);
                // SAFETY: A single-threaded apartment.
                unsafe {
                    if private {
                        let environment = environment.cast::<ICoreWebView2Environment10>()?;
                        let options = environment.CreateCoreWebView2ControllerOptions()?;
                        options.SetIsInPrivateModeEnabled(true)?;
                        environment.CreateCoreWebView2ControllerWithOptions(parent, &options, &handler)
                    } else {
                        environment.CreateCoreWebView2Controller(parent, &handler)
                    }
                }
                .map_err(webview2_com::Error::WindowsError)
            }),
            Box::new(move |status, controller| {
                status?;
                let _ = sender.send(controller.ok_or_else(|| windows::core::Error::from(E_POINTER))?);
                Ok(())
            }),
        )?;
        let controller = receiver.recv().map_err(|_| webview2_com::Error::SendError)?;
        // SAFETY: A single-threaded apartment.
        let view = unsafe { controller.CoreWebView2() }?;
        Ok((controller, view))
    }

    /// Evaluates `script` in the page's own world: what page script sees.
    /// The value comes back as WebView2 gives it, JSON.
    fn page_eval(view: &ICoreWebView2, script: &str) -> Option<serde_json::Value> {
        let slot: Rc<RefCell<Option<Option<String>>>> = Rc::new(RefCell::new(None));
        let filled = slot.clone();
        let handler = ExecuteScriptCompletedHandler::create(Box::new(move |status, result: String| {
            *filled.borrow_mut() = Some(status.ok().map(|()| result));
            Ok(())
        }));
        // SAFETY: A single-threaded apartment; the string outlives the call.
        if unsafe { view.ExecuteScript(&HSTRING::from(script), &handler) }.is_err() {
            return None;
        }
        pump_until(WAIT, || slot.borrow().is_some());
        let answered = slot.borrow_mut().take().flatten();
        answered.and_then(|text| serde_json::from_str(&text).ok())
    }

    fn navigate(view: &ICoreWebView2, url: &str) -> bool {
        // SAFETY: A single-threaded apartment; `url` is one of this probe's own.
        unsafe { view.Navigate(&HSTRING::from(url)) }.is_ok()
    }

    /// Loads `url` and waits until its document says `title`.
    fn load(view: &ICoreWebView2, url: &str, title: &str) -> bool {
        let wanted = json!(format!("{url}|{title}"));
        navigate(view, url)
            && pump_until(WAIT, || {
                page_eval(view, "String(location.href) + '|' + String(document.title)").as_ref() == Some(&wanted)
            })
    }

    fn fetch(view: &ICoreWebView2, request: &Request, current: impl Fn() -> bool + 'static) -> Option<Reply> {
        let slot: Rc<RefCell<Option<Reply>>> = Rc::new(RefCell::new(None));
        let filled = slot.clone();
        fetch_rendezvous(view, request, current, move |reply| *filled.borrow_mut() = Some(reply));
        pump_until(WAIT, || slot.borrow().is_some());
        let answered = slot.borrow_mut().take();
        answered
    }

    pub fn run() -> i32 {
        std::thread::spawn(|| {
            std::thread::sleep(LIMIT);
            eprintln!("direct_webview_probe: no result within {LIMIT:?}");
            std::process::exit(98);
        });
        // SAFETY: First use of COM on this thread; `2` is the single-threaded apartment.
        unsafe { CoInitializeEx(std::ptr::null(), 2) };

        let instance = Instance::start();
        // The route is for `https` and `wss` alone, so the listener is asked
        // for port 443. The probe has no certificate for that host: it sees
        // who asked and with what, and ends the tunnel there.
        let proxy = Proxy::start(format!("{ROUTED}:443"), None);
        let listener = SocketAddrV4::new(Ipv4Addr::LOCALHOST, proxy.port);
        let origin = instance.origin();
        let request = Request::new(&origin, ENDPOINT).expect("a valid request");
        let mut report = Report { failed: 0 };

        let argument = route_argument(ROUTED, listener);
        report.case(
            "the-route-argument-holds-one-host-and-no-credential",
            argument.as_deref().is_some_and(|argument| {
                argument.starts_with("--proxy-pac-url=data:application/x-ns-proxy-autoconfig;base64,")
                    && !argument.contains(PASSWORD)
                    && !argument.contains(&STANDARD.encode(PASSWORD))
            }) && route_argument("Instance.Example", listener).is_none()
                && route_argument("a\" || true || \"", listener).is_none()
                && route_argument("", listener).is_none(),
            json!({ "made": argument.is_some() }),
        );
        let Some(argument) = argument else { return report.failed };

        let folder = std::env::temp_dir().join(format!("avibe-direct-webview-probe-{}", std::process::id()));
        let (controller, view) = match new_webview(&folder, argument.clone(), false) {
            Ok(made) => made,
            Err(error) => {
                report.case(
                    "a-webview2-without-a-window-exists",
                    false,
                    json!({ "error": format!("{error:?}") }),
                );
                return report.failed;
            }
        };
        // What WebView2 says of each challenge, for the report: the URI is a
        // loopback address of this probe's own.
        let challenges: Rc<RefCell<Vec<String>>> = Rc::new(RefCell::new(Vec::new()));
        let heard = challenges.clone();
        let recorder = BasicAuthenticationRequestedEventHandler::create(Box::new(move |_, arguments| {
            let Some(arguments) = arguments else { return Ok(()) };
            let (mut uri, mut challenge) = (PWSTR::null(), PWSTR::null());
            // SAFETY: A single-threaded apartment; WebView2 allocates both
            // strings, and `take_pwstr` frees them.
            unsafe {
                arguments.Uri(&mut uri)?;
                arguments.Challenge(&mut challenge)?;
            }
            heard
                .borrow_mut()
                .push(format!("{} | {}", take_pwstr(uri), take_pwstr(challenge)));
            Ok(())
        }));
        // SAFETY: A single-threaded apartment.
        let recording = unsafe {
            view.cast::<ICoreWebView2_10>()
                .and_then(|view| view.add_BasicAuthenticationRequested(&recorder, &mut 0i64))
        }
        .is_ok();
        let answering = recording && answer_listener_challenge(&view, listener, USERNAME, PASSWORD).is_ok();

        // 1. The instance's own document: the answer comes back exactly, and
        //    the request carried this WebView's session and the usual checks.
        let loaded = load(&view, &format!("{origin}/"), "plain");
        instance.forget();
        let reply = fetch(&view, &request, || true);
        let seen = instance.rendezvous_requests();
        let one = seen.first();
        let header = |name: &str| one.and_then(|seen| seen.headers.get(name).cloned()).unwrap_or_default();
        report.case(
            "the-instances-document-gets-the-instances-answer",
            loaded
                && reply == Some(Reply::Ready(ANSWER.to_owned()))
                && seen.len() == 1
                && one.is_some_and(|seen| seen.method == "POST")
                && header("x-vibe-csrf-token") == TOKEN
                && header("cookie").contains(&format!("session={SESSION}"))
                && header("origin") == origin
                && header("content-type") == "application/json"
                && one.is_some_and(|seen| seen.body == format!(r#"{{"client_endpoint_id":"{ENDPOINT}","version":1}}"#)),
            json!({
                "loaded": loaded, "reply": format!("{reply:?}").chars().take(60).collect::<String>(), "requests": seen.len(),
                "csrf": header("x-vibe-csrf-token") == TOKEN, "session": header("cookie").contains("session="),
                "origin": header("origin") == origin, "body": one.map(|seen| seen.body.clone()),
            }),
        );

        // 2. A page that replaced everything the script calls.
        let loaded = load(&view, &format!("{origin}/tamper"), "tamper");
        instance.forget();
        let reply = fetch(&view, &request, || true);
        let seen = instance.rendezvous_requests();
        let page = page_eval(
            &view,
            "String(window.__calls) + '|' + Object.getOwnPropertyNames(window).filter((name) => !window.__before.includes(name)).join(',')",
        );
        report.case(
            "page-script-cannot-see-or-alter-the-request",
            loaded
                && reply == Some(Reply::Ready(ANSWER.to_owned()))
                && seen.len() == 1
                && seen[0].headers.get("x-vibe-csrf-token").map(String::as_str) == Some(TOKEN)
                && page == Some(json!("0|")),
            json!({ "loaded": loaded, "ready": matches!(reply, Some(Reply::Ready(_))), "requests": seen.len(), "page": page }),
        );

        // 3. Another origin's document, and a selection that is no longer
        //    current: nothing is asked.
        instance.forget();
        let elsewhere =
            Request::new(&format!("http://localhost:{}", instance.port), ENDPOINT).expect("a valid request");
        let wrong_origin = fetch(&view, &elsewhere, || true);
        let stale = fetch(&view, &request, || false);
        report.case(
            "another-origin-or-a-stale-selection-asks-nothing",
            wrong_origin == Some(Reply::NotReady)
                && stale == Some(Reply::SelectionChanged)
                && instance.rendezvous_requests().is_empty(),
            json!({ "wrong_origin": format!("{wrong_origin:?}"), "stale": format!("{stale:?}"), "requests": instance.rendezvous_requests().len() }),
        );

        // 4. A selection that changes while the request runs.
        let loaded = load(&view, &format!("{origin}/"), "plain");
        instance.forget();
        let asked = Rc::new(RefCell::new(0));
        let counter = asked.clone();
        let changed = fetch(&view, &request, move || {
            *counter.borrow_mut() += 1;
            *counter.borrow() == 1
        });
        let requests = instance.rendezvous_requests().len();
        // The same when the instance refused meanwhile, on the path that
        // delivers at once: the selection's change is what is said,
        // whatever the instance said.
        *instance.answer.lock().expect("mode") = Answer::Forbidden;
        let first = Rc::new(RefCell::new(true));
        let refused = fetch(&view, &request, move || first.replace(false));
        *instance.answer.lock().expect("mode") = Answer::Ready;
        report.case(
            "an-answer-for-a-selection-that-changed-is-dropped",
            loaded
                && changed == Some(Reply::SelectionChanged)
                && requests == 1
                // Asked before the request, when its ready answer arrived,
                // and on delivery. Three, not two: the answer's arrival is
                // where the change is seen, before anything more is asked
                // of WebView2, so the change is never said late.
                && *asked.borrow() == 3
                && refused == Some(Reply::SelectionChanged),
            json!({ "reply": format!("{changed:?}"), "requests": requests, "asked": *asked.borrow(), "refused": format!("{refused:?}") }),
        );

        // 5. A redirect, a refusal, an answer that is too large.
        let mut outcomes = Vec::new();
        for (answer, expected) in [
            (Answer::Redirect, Reply::Failed),
            (Answer::Forbidden, Reply::Refused(403)),
            (Answer::Oversize, Reply::Failed),
        ] {
            *instance.answer.lock().expect("mode") = answer;
            instance.forget();
            let reply = fetch(&view, &request, || true);
            let followed = !instance.requests("/elsewhere").is_empty();
            outcomes.push((reply == Some(expected), followed, format!("{reply:?}")));
        }
        *instance.answer.lock().expect("mode") = Answer::Ready;
        report.case(
            "a-redirect-a-refusal-and-an-oversize-answer-give-no-body",
            outcomes
                .iter()
                .all(|(as_expected, followed, _)| *as_expected && !followed),
            json!(outcomes
                .iter()
                .map(|(ok, followed, reply)| json!({ "ok": ok, "followed": followed, "reply": reply }))
                .collect::<Vec<_>>()),
        );

        // 6. A service worker that answers in the instance's place.
        navigate(&view, &format!("{origin}/worker-page"));
        let mut state = String::new();
        pump_until(WAIT, || {
            state = page_eval(&view, "String(document.title)")
                .and_then(|title| title.as_str().map(str::to_owned))
                .unwrap_or_default();
            matches!(
                state.as_str(),
                "controlled" | "not-controlled" | "no-worker-container" | "worker-error"
            )
        });
        instance.forget();
        let reply = fetch(&view, &request, || true);
        let reached = instance.rendezvous_requests().len();
        report.case(
            "a-controlling-service-worker-is-refused",
            // WebView2 gives pages a service worker: the worker must be there
            // for this to have been tried at all.
            state == "controlled" && reply == Some(Reply::Controlled) && reached == 0,
            json!({ "document": state, "reply": format!("{reply:?}").chars().take(40).collect::<String>(), "reached_instance": reached }),
        );

        // 7. The route, as WebView2's own script engine reads it: the
        //    instance's `https` and `wss`, and nothing else.
        let pac = argument
            .rsplit_once(',')
            .and_then(|(_, encoded)| STANDARD.decode(encoded).ok())
            .and_then(|script| String::from_utf8(script).ok())
            .unwrap_or_default();
        let decided = page_eval(
            &view,
            &format!(
                "(() => {{ {pac}; return [\
                 FindProxyForURL('https://{ROUTED}/a', '{ROUTED}'), FindProxyForURL('wss://{ROUTED}/a', '{ROUTED}'),\
                 FindProxyForURL('http://{ROUTED}/a', '{ROUTED}'), FindProxyForURL('https://child.{ROUTED}/a', 'child.{ROUTED}'),\
                 FindProxyForURL('https://other.example/a', 'other.example'), FindProxyForURL('https://{ROUTED}.example/a', '{ROUTED}.example')\
                 ].join('|'); }})()"
            ),
        );
        let through = format!("PROXY {listener}; DIRECT");
        report.case(
            "the-route-names-the-instances-https-and-wss-only",
            decided == Some(json!(format!("{through}|{through}|DIRECT|DIRECT|DIRECT|DIRECT"))),
            json!({ "decided": decided.map(|decided| decided.to_string().replace(&proxy.port.to_string(), "PORT")) }),
        );

        // 8. The instance's host goes to the listener, and its challenge is
        //    answered with the listener's credential.
        proxy.forget();
        navigate(&view, &format!("https://{ROUTED}/"));
        let right = Asked {
            target: format!("{ROUTED}:443"),
            credential: "right",
        };
        let answered = pump_until(WAIT, || proxy.asked().contains(&right));
        let asked = proxy.asked();
        report.case(
            "the-instances-host-reaches-the-listener-with-its-credential",
            answering && answered && asked.iter().all(|asked| asked.target == right.target && asked.credential != "wrong"),
            json!({
                "answering": answering,
                "asked": asked.iter().map(|asked| format!("{} {}", asked.target, asked.credential)).collect::<Vec<_>>(),
                "challenges": challenges.borrow().iter().map(|heard| heard.replace(&proxy.port.to_string(), "PORT")).collect::<Vec<_>>(),
            }),
        );

        // 9. Any other host, a child host, and plain `http` to the
        //    instance's host are not routed: the listener is not asked.
        proxy.forget();
        for url in [
            "https://other.avibe-probe.test/".to_owned(),
            format!("https://child.{ROUTED}/"),
            format!("http://{ROUTED}/"),
        ] {
            navigate(&view, &url);
            // Each fails to resolve; long enough for a routed one to be seen.
            pump_until(Duration::from_secs(3), || false);
        }
        let unrouted = load(&view, &format!("{origin}/unrouted"), "plain");
        report.case(
            "another-host-a-child-host-and-plain-http-are-not-routed",
            // A late retry for the routed host itself may still arrive; any
            // other target, or a request that is not a CONNECT, would be one
            // of these three.
            unrouted && proxy.asked().iter().all(|asked| asked.target == right.target),
            json!({ "loaded": unrouted, "asked": proxy.asked().iter().map(|asked| asked.target.clone()).collect::<Vec<_>>() }),
        );

        // 10. A site's own challenge in the listener's realm is not the
        //     listener's: the credential is never sent to a site.
        instance.forget();
        navigate(&view, &format!("{origin}/basic"));
        let challenged = pump_until(WAIT, || !instance.requests("/basic").is_empty());
        pump_until(Duration::from_secs(3), || false);
        let basic = instance.requests("/basic");
        report.case(
            "a-sites-own-challenge-is-not-answered",
            challenged && basic.iter().all(|seen| !seen.headers.contains_key("authorization")),
            json!({ "challenged": challenged, "requests": basic.len(), "answered": basic.iter().any(|seen| seen.headers.contains_key("authorization")) }),
        );

        // 11. What WebView2 offers a page that wants web push: in this
        //     WebView, and in an InPrivate one, as the app's instance windows
        //     are, where it is also seen whether a worker can take control.
        let offer = |view: &ICoreWebView2, worker: bool| {
            let loaded = load(view, &format!("{origin}/"), "plain");
            let offered = page_eval(view, PUSH_OFFER);
            pump_until(Duration::from_secs(3), || false);
            let asked = page_eval(view, PUSH_ASKED);
            let mut state = String::new();
            if worker {
                navigate(view, &format!("{origin}/worker-page"));
                pump_until(WAIT, || {
                    state = page_eval(view, "String(document.title)")
                        .and_then(|title| title.as_str().map(str::to_owned))
                        .unwrap_or_default();
                    matches!(
                        state.as_str(),
                        "controlled" | "not-controlled" | "no-worker-container" | "worker-error"
                    )
                });
            }
            let text =
                |value: &Option<serde_json::Value>| value.as_ref().and_then(|value| value.as_str().map(str::to_owned));
            let (offered, asked) = (text(&offered), text(&asked));
            let registers = registers_push_worker(offered.as_deref(), asked.as_deref());
            json!({ "loaded": loaded, "offered": offered, "asked": asked, "registers": registers, "worker": state })
        };
        let here = offer(&view, false);
        let private_folder =
            std::env::temp_dir().join(format!("avibe-direct-webview-probe-private-{}", std::process::id()));
        let private = match new_webview(&private_folder, String::new(), true) {
            Ok((private_controller, private_view)) => {
                let detail = offer(&private_view, true);
                // SAFETY: A single-threaded apartment; done with this WebView.
                let _ = unsafe { private_controller.Close() };
                detail
            }
            Err(error) => json!({ "error": format!("{error:?}") }),
        };
        // A report, not yet a rule: whether WebView2 lets a page get as far
        // as registering its push worker decides nothing by itself.
        report.case(
            "what-the-webview-offers-a-push-worker",
            true,
            json!({ "here": here, "private": private }),
        );

        // SAFETY: A single-threaded apartment; the probe is done with the WebView.
        let _ = unsafe { controller.Close() };
        pump_until(Duration::from_millis(500), || false);
        let _ = std::fs::remove_dir_all(&folder);
        let _ = std::fs::remove_dir_all(&private_folder);
        report.failed
    }
}
