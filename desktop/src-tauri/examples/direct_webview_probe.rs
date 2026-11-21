//! Hermetic probe of the direct path's macOS WebView adapters with a real,
//! windowless WKWebView on loopback pages. Nothing is shown, the app is never
//! activated, the browsing data is not persistent, and no keychain or
//! clipboard is touched.
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

#[cfg(not(target_os = "macos"))]
fn main() {}

#[cfg(target_os = "macos")]
fn main() {
    std::process::exit(probe::run());
}

// Side by side at the root, as in the shell's `direct` module: the adapter
// names the request through `super`.
#[cfg(target_os = "macos")]
#[path = "../src/direct/rendezvous.rs"]
mod rendezvous;
#[cfg(target_os = "macos")]
#[path = "../src/direct/webview_macos.rs"]
mod webview_macos;

#[cfg(target_os = "macos")]
mod probe {
    use std::{
        cell::RefCell,
        collections::HashMap,
        ffi::c_void,
        io::{Read, Write},
        net::{Ipv4Addr, Shutdown, SocketAddrV4, TcpListener, TcpStream},
        rc::Rc,
        sync::{Arc, Mutex},
        thread,
        time::{Duration, Instant},
    };

    use block2::RcBlock;
    use objc2::{rc::Retained, runtime::AnyObject, MainThreadMarker, MainThreadOnly};
    use objc2_foundation::{NSError, NSPoint, NSRect, NSSize, NSString, NSURLRequest, NSURL};
    use objc2_web_kit::{WKWebView, WKWebViewConfiguration, WKWebsiteDataStore};
    use serde_json::json;

    use crate::{
        rendezvous::{Reply, Request, MAX_ANSWER_BYTES, PATH},
        webview_macos::{fetch_rendezvous, install_route, remove_route},
    };

    const TOKEN: &str = "tok-123";
    const SESSION: &str = "s3cret-session";
    const ENDPOINT: &str = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";
    const ANSWER: &str = r#"{"version":1,"status":"ready","probe":"the instance's own answer"}"#;
    /// The host the route sends to the proxy. It resolves nowhere: only the
    /// proxy can reach it, so a load of it proves the route.
    const ROUTED: &str = "instance.avibe-probe.test";
    const USERNAME: &str = "avibe";
    const PASSWORD: &str = "probe-password-9f2c";
    const WAIT: Duration = Duration::from_secs(20);

    // MARK: The instance

    #[derive(Clone, Debug)]
    struct Seen {
        method: String,
        path: String,
        headers: HashMap<String, String>,
        body: String,
    }

    #[derive(Clone, Copy, PartialEq)]
    enum Answer {
        Ready,
        Redirect,
        Forbidden,
        Oversize,
    }

    struct Instance {
        port: u16,
        seen: Arc<Mutex<Vec<Seen>>>,
        answer: Arc<Mutex<Answer>>,
    }

    impl Instance {
        fn start() -> Self {
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

        fn origin(&self) -> String {
            format!("http://127.0.0.1:{}", self.port)
        }

        fn rendezvous_requests(&self) -> Vec<Seen> {
            self.requests(PATH)
        }

        fn requests(&self, path: &str) -> Vec<Seen> {
            self.seen.lock().expect("seen").iter().filter(|seen| seen.path == path).cloned().collect()
        }

        fn forget(&self) {
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
        let Some((head, mut body)) = read_head(&mut stream) else { return };
        let mut first = head.split("\r\n").next().unwrap_or("").split(' ');
        let (method, path) = (first.next().unwrap_or("").to_owned(), first.next().unwrap_or("").to_owned());
        let headers = headers_of(&head);
        let length: usize = headers.get("content-length").and_then(|value| value.parse().ok()).unwrap_or(0);
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
            let html = format!("<!doctype html><meta charset=\"utf-8\"><title>{title}</title><script>{script}</script>");
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
    const TAMPER: &str = r#"
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

    const WORKER_PAGE: &str = r#"
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
    const WORKER: &str = r#"
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
    struct Asked {
        target: String,
        /// `none`, `wrong`, or `right`.
        credential: &'static str,
    }

    struct Proxy {
        port: u16,
        asked: Arc<Mutex<Vec<Asked>>>,
    }

    impl Proxy {
        fn start(instance_port: u16) -> Self {
            let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).expect("proxy port");
            let port = listener.local_addr().expect("proxy address").port();
            let asked = Arc::new(Mutex::new(Vec::new()));
            let log = asked.clone();
            thread::spawn(move || {
                for stream in listener.incoming().flatten() {
                    let log = log.clone();
                    thread::spawn(move || serve_proxy(stream, instance_port, &log));
                }
            });
            Self { port, asked }
        }

        fn asked(&self) -> Vec<Asked> {
            self.asked.lock().expect("asked").clone()
        }

        fn forget(&self) {
            self.asked.lock().expect("asked").clear();
        }
    }

    fn serve_proxy(mut browser: TcpStream, instance_port: u16, log: &Mutex<Vec<Asked>>) {
        use base64::{engine::general_purpose::STANDARD, Engine};
        let _ = browser.set_read_timeout(Some(Duration::from_secs(10)));
        let Some((head, early)) = read_head(&mut browser) else { return };
        let mut first = head.split("\r\n").next().unwrap_or("").split(' ');
        let (method, target) = (first.next().unwrap_or(""), first.next().unwrap_or("").to_owned());
        let expected = format!("Basic {}", STANDARD.encode(format!("{USERNAME}:{PASSWORD}")));
        let credential = match headers_of(&head).get("proxy-authorization") {
            None => "none",
            Some(value) if *value == expected => "right",
            Some(_) => "wrong",
        };
        log.lock().expect("asked").push(Asked { target: target.clone(), credential });
        let refuse = |browser: &mut TcpStream, answer: &str| {
            let _ = browser.write_all(answer.as_bytes());
            let _ = browser.shutdown(Shutdown::Both);
        };
        if method != "CONNECT" {
            return refuse(&mut browser, "HTTP/1.1 405 Method Not Allowed\r\nConnection: close\r\n\r\n");
        }
        if credential != "right" {
            return refuse(
                &mut browser,
                "HTTP/1.1 407 Proxy Authentication Required\r\nProxy-Authenticate: Basic realm=\"Avibe Direct\"\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",
            );
        }
        // One host, as the real listener: a child host is refused.
        if target != format!("{ROUTED}:{instance_port}") {
            return refuse(&mut browser, "HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n");
        }
        let Ok(mut upstream) = TcpStream::connect((Ipv4Addr::LOCALHOST, instance_port)) else {
            return refuse(&mut browser, "HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n");
        };
        if browser.write_all(b"HTTP/1.1 200 Connection Established\r\n\r\n").is_err() || upstream.write_all(&early).is_err() {
            return;
        }
        let _ = browser.set_read_timeout(None);
        let (Ok(mut browser_read), Ok(mut upstream_write)) = (browser.try_clone(), upstream.try_clone()) else { return };
        let up = thread::spawn(move || {
            let _ = std::io::copy(&mut browser_read, &mut upstream_write);
            let _ = upstream_write.shutdown(Shutdown::Write);
        });
        let _ = std::io::copy(&mut upstream, &mut browser);
        let _ = browser.shutdown(Shutdown::Both);
        let _ = up.join();
    }

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
                unsafe { &*result }.downcast_ref::<NSString>().map(|value| value.to_string())
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
            let Some(target) = NSURL::URLWithString(&NSString::from_str(url)) else { return false };
            webview.loadRequest(&NSURLRequest::requestWithURL(&target));
        }
        let wanted = format!("{url}|{title}");
        pump_until(WAIT, || {
            // SAFETY: Main thread.
            !unsafe { webview.isLoading() }
                && page_eval(webview, "String(location.href) + '|' + String(document.title)").as_deref() == Some(&wanted)
        })
    }

    fn fetch(webview: &WKWebView, mtm: MainThreadMarker, request: &Request, current: impl Fn() -> bool + 'static) -> Option<Reply> {
        let slot: Rc<RefCell<Option<Reply>>> = Rc::new(RefCell::new(None));
        let filled = slot.clone();
        fetch_rendezvous(webview, mtm, request, current, move |reply| *filled.borrow_mut() = Some(reply));
        pump_until(WAIT, || slot.borrow().is_some());
        let answered = slot.borrow_mut().take();
        answered
    }

    // MARK: The cases

    struct Report {
        failed: i32,
    }

    impl Report {
        fn case(&mut self, name: &str, ok: bool, detail: serde_json::Value) {
            if !ok {
                self.failed += 1;
            }
            println!("{}", json!({ "case": name, "ok": ok, "detail": detail }));
        }
    }

    pub fn run() -> i32 {
        let mtm = MainThreadMarker::new().expect("the probe runs on the main thread");
        let instance = Instance::start();
        let proxy = Proxy::start(instance.port);
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
        let elsewhere = Request::new(&format!("http://localhost:{}", instance.port), ENDPOINT).expect("a valid request");
        let wrong_origin = fetch(&webview, mtm, &elsewhere, || true);
        let stale = fetch(&webview, mtm, &request, || false);
        report.case(
            "another-origin-or-a-stale-selection-asks-nothing",
            wrong_origin == Some(Reply::NotReady) && stale == Some(Reply::NotReady) && instance.rendezvous_requests().is_empty(),
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
        report.case(
            "an-answer-for-a-selection-that-changed-is-dropped",
            loaded && changed == Some(Reply::Failed) && instance.rendezvous_requests().len() == 1 && *asked.borrow() == 2,
            json!({ "reply": format!("{changed:?}"), "requests": instance.rendezvous_requests().len(), "asked": *asked.borrow() }),
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
            outcomes.iter().all(|(as_expected, followed, _)| *as_expected && !followed),
            json!(outcomes.iter().map(|(ok, followed, reply)| json!({ "ok": ok, "followed": followed, "reply": reply })).collect::<Vec<_>>()),
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
                matches!(title.as_deref(), Some("controlled" | "not-controlled" | "no-worker-container" | "worker-error"))
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
        let child = load(&webview, &format!("http://child.{ROUTED}:{}/routed", instance.port), "plain");
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
