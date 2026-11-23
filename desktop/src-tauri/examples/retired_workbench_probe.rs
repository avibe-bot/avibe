//! Native WebView probe for retired Workbench web views, for CI only. It
//! launches a regular app, which macOS activates and which takes focus: do not
//! run it on a live desktop. Its data is hermetic: it does not start Avibe or
//! use an OS credential store, and `tests/run_cookie_probe.py` supplies an
//! empty HOME and a test root.
//!
//! Each Workbench stand-in is a child web view of one physical window, built
//! the way `build_main_child` builds a Runtime document (about:blank, hidden,
//! then navigated). Its page and an iframe in it ping the loopback server every
//! 200 ms, and the page holds an event stream open and answers each event with
//! a ping. The views are retired through the shell's own `instance_store::retire`.
//! A retired view must be freed natively (macOS: a weak reference to its
//! WKWebView goes nil), its event stream must close, and its page, iframe and
//! event handler must go silent.
//!
//! A page told to close can act once more in the moment after: its process
//! hears of the close a little later, and a hidden page's throttled timer may
//! fire once in between (measured on macos-26: the last sign at most 1.16 s
//! after `retire` returned, in 600 retirements, and never more than one ping
//! per view). So a sign is a failure only when it is later than [`BOUND`]
//! after `retire` returned, or when a view sends more than [`LATE_SIGNS`]
//! of them, or reopens its event stream; a page that kept running would ping
//! every 200 ms and reconnect. Each sign is dated by the page's own clock,
//! carried in the ping, so a ping handled late is not mistaken for one sent
//! late. The last sign's lag is printed for the record.
//!
//! WebKit throttles timers in a hidden page, nearly to nothing within seconds,
//! so silence alone proves nothing; the weak reference and the stream decide.
//! A hidden control view that is not retired must keep its WKWebView and its
//! event stream and still run script; its ping counts are printed.
use std::{
    collections::HashMap,
    io::{Read, Write},
    net::{SocketAddr, TcpListener, TcpStream},
    path::PathBuf,
    sync::{Arc, Mutex},
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};
use tauri::{webview::WebviewBuilder, AppHandle, LogicalPosition, Manager, Webview, WebviewUrl};

/// No sign of a retired page later than this after `retire` returned.
const BOUND: f64 = 5000.0;
/// Signs a dying page may still give after `retire` returned.
const LATE_SIGNS: usize = 5;
/// The control view's ping counts are printed over this window.
const CONTROL_WINDOW: f64 = 1500.0;

/// Wall-clock milliseconds, the clock the page's `Date.now()` uses.
fn now() -> f64 {
    SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_secs_f64() * 1000.0
}

#[derive(Default)]
struct Seen {
    /// Per ping: when the page sent it (its own clock), or when it was handled.
    pings: HashMap<String, Vec<f64>>,
    streams_open: HashMap<String, Vec<f64>>,
    streams_closed: HashMap<String, usize>,
}

type Shared = Arc<Mutex<Seen>>;

fn page(name: &str) -> String {
    format!(
        "<!doctype html><title>{name}</title><script>\
         setInterval(() => fetch('/ping/{name}?t=' + Date.now(), {{cache: 'no-store'}}).catch(() => {{}}), 200);\
         new EventSource('/events/{name}').onmessage = () => fetch('/ping/{name}-stream?t=' + Date.now(), {{cache: 'no-store'}}).catch(() => {{}});\
         </script><iframe src='/frame/{name}'></iframe>"
    )
}

fn frame(name: &str) -> String {
    format!(
        "<!doctype html><script>\
         setInterval(() => fetch('/ping/{name}-frame?t=' + Date.now(), {{cache: 'no-store'}}).catch(() => {{}}), 200);\
         </script>"
    )
}

fn serve(mut stream: TcpStream, seen: Shared) {
    let _ = stream.set_read_timeout(Some(Duration::from_secs(10)));
    let mut request = Vec::new();
    while !request.windows(4).any(|bytes| bytes == b"\r\n\r\n") {
        let mut chunk = [0u8; 2048];
        match stream.read(&mut chunk) {
            Ok(0) | Err(_) => return,
            Ok(count) => request.extend_from_slice(&chunk[..count]),
        }
        if request.len() > 65536 {
            return;
        }
    }
    let text = String::from_utf8_lossy(&request).into_owned();
    let path = text
        .lines()
        .next()
        .and_then(|line| line.split_whitespace().nth(1))
        .unwrap_or("/")
        .to_owned();
    let (path, query) = path.split_once('?').unwrap_or((&path, ""));
    let mut parts = path.trim_start_matches('/').splitn(2, '/');
    let (kind, name) = (parts.next().unwrap_or(""), parts.next().unwrap_or("").to_owned());
    let html = |body: String| {
        format!(
            "HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nCache-Control: no-store\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
            body.len()
        )
    };
    match kind {
        "page" => {
            let _ = stream.write_all(html(page(&name)).as_bytes());
        }
        "frame" => {
            let _ = stream.write_all(html(frame(&name)).as_bytes());
        }
        "ping" => {
            // Date the sign by when the page sent it; a request handled late
            // is not a page acting late.
            let sent = query
                .strip_prefix("t=")
                .and_then(|value| value.parse::<f64>().ok())
                .unwrap_or_else(now);
            seen.lock().unwrap().pings.entry(name).or_default().push(sent);
            let _ =
                stream.write_all(b"HTTP/1.1 204 No Content\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n");
        }
        "events" => {
            let head =
                "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nCache-Control: no-store\r\n\r\n: open\n\n";
            if stream.write_all(head.as_bytes()).is_err() {
                return;
            }
            seen.lock()
                .unwrap()
                .streams_open
                .entry(name.clone())
                .or_default()
                .push(now());
            // The client never sends more; a read returns only when it closes.
            let _ = stream.set_read_timeout(None);
            let mut reader = stream.try_clone().unwrap();
            let writer = std::thread::spawn(move || {
                while stream.write_all(b"data: tick\n\n").is_ok() {
                    std::thread::sleep(Duration::from_millis(200));
                }
            });
            let mut byte = [0u8; 1];
            while matches!(reader.read(&mut byte), Ok(count) if count > 0) {}
            let _ = reader.shutdown(std::net::Shutdown::Both);
            let _ = writer.join();
            *seen.lock().unwrap().streams_closed.entry(name).or_default() += 1;
        }
        _ => {
            let _ = stream.write_all(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n");
        }
    }
}

fn pings_since(seen: &Shared, name: &str, since: f64) -> usize {
    seen.lock()
        .unwrap()
        .pings
        .get(name)
        .map_or(0, |pings| pings.iter().filter(|at| **at >= since).count())
}

fn wait_for(what: &str, timeout: Duration, mut done: impl FnMut() -> bool) {
    let deadline = Instant::now() + timeout;
    while !done() {
        assert!(Instant::now() < deadline, "timed out waiting for {what}");
        std::thread::sleep(Duration::from_millis(50));
    }
}

/// The child web view `build_main_child` makes: about:blank, auto-resized,
/// hidden at once, then navigated by the instance client.
fn workbench(app: &AppHandle, label: &str, disposable: bool, directory: Option<PathBuf>) -> Webview {
    let (sender, receiver) = std::sync::mpsc::channel();
    let handle = app.clone();
    let label = label.to_owned();
    app.run_on_main_thread(move || {
        let physical = handle.get_window("main").unwrap();
        let mut builder = WebviewBuilder::new(&label, WebviewUrl::External("about:blank".parse().unwrap()))
            .incognito(disposable)
            .auto_resize();
        if let Some(directory) = directory {
            builder = builder.data_directory(directory);
        }
        let size = physical
            .inner_size()
            .unwrap()
            .to_logical::<f64>(physical.scale_factor().unwrap());
        let view = physical.add_child(builder, LogicalPosition::new(0., 0.), size).unwrap();
        view.hide().unwrap();
        sender.send(view).unwrap();
    })
    .unwrap();
    receiver.recv_timeout(Duration::from_secs(10)).unwrap()
}

fn start(view: &Webview, address: SocketAddr, seen: &Shared) {
    let name = view.label().to_owned();
    let started = now();
    view.navigate(format!("http://{address}/page/{name}").parse().unwrap())
        .unwrap();
    wait_for(&format!("{name} to run"), Duration::from_secs(20), || {
        pings_since(seen, &name, started) >= 3
            && pings_since(seen, &format!("{name}-frame"), started) >= 3
            && seen.lock().unwrap().streams_open.get(&name).map_or(0, Vec::len) >= 1
    });
    #[cfg(target_os = "macos")]
    native::track(view);
}

fn stream_open(seen: &Shared, name: &str) -> bool {
    let seen = seen.lock().unwrap();
    seen.streams_open.get(name).map_or(0, Vec::len) > seen.streams_closed.get(name).copied().unwrap_or(0)
}

/// Retires a view through the shell's own helper; returns when it returned.
fn retire(app: &AppHandle, view: &Webview) -> f64 {
    avibe_desktop_lib::instance_store::retire(view).unwrap();
    let returned = now();
    assert!(app.get_webview(view.label()).is_none());
    returned
}

/// After retirement returns, the web view is freed, its event stream closes,
/// and no sign of its page, iframe or event handler comes later than [`BOUND`]
/// after `returned`. Watches until just past the bound, failing at the first
/// sign of a page that kept running.
fn assert_stopped(app: &AppHandle, name: &str, seen: &Shared, returned: f64) {
    #[cfg(target_os = "macos")]
    wait_for(
        &format!("{name}'s WKWebView to be freed"),
        Duration::from_secs(5),
        || native::freed(app, name),
    );
    #[cfg(not(target_os = "macos"))]
    let _ = app;
    wait_for(&format!("{name} event stream to close"), Duration::from_secs(5), || {
        !stream_open(seen, name)
    });
    let signals = [name.to_owned(), format!("{name}-frame"), format!("{name}-stream")];
    let late = || {
        let seen = seen.lock().unwrap();
        let signs: Vec<f64> = signals
            .iter()
            .filter_map(|signal| seen.pings.get(signal))
            .flatten()
            .copied()
            .filter(|at| *at > returned)
            .collect();
        let reopened = seen
            .streams_open
            .get(name)
            .map_or(0, |opened| opened.iter().filter(|at| **at > returned).count());
        (signs, reopened)
    };
    let check = || {
        let (signs, reopened) = late();
        let last = signs.iter().copied().fold(0.0_f64, f64::max).max(returned) - returned;
        assert!(
            last <= BOUND,
            "{name} kept running after retirement: a sign {last:.0} ms after retire returned"
        );
        assert!(
            signs.len() <= LATE_SIGNS,
            "{name} kept running after retirement: {} signs after retire returned",
            signs.len()
        );
        assert_eq!(reopened, 0, "{name} reopened its event stream after retirement");
        (signs.len(), last)
    };
    while now() < returned + BOUND + 250.0 {
        check();
        std::thread::sleep(Duration::from_millis(50));
    }
    let (count, last) = check();
    if count == 0 {
        println!("{name}: no sign after retire returned");
    } else {
        println!("{name}: {count} late sign(s), the last {last:.0} ms after retire returned");
    }
}

#[cfg(target_os = "macos")]
mod native {
    use std::{cell::RefCell, collections::HashMap};

    use objc2::rc::{Retained, Weak};
    use objc2_app_kit::NSView;
    use tauri::{AppHandle, Webview};

    thread_local! {
        /// Main-thread only: weak references to each tracked WKWebView.
        static TRACKED: RefCell<HashMap<String, Weak<NSView>>> = RefCell::new(HashMap::new());
    }

    /// Records a weak reference to the view's WKWebView. It goes nil only
    /// once the last strong reference, wry's or WebKit's, is released.
    pub fn track(view: &Webview) {
        let (sender, receiver) = std::sync::mpsc::channel();
        let label = view.label().to_owned();
        view.with_webview(move |platform| {
            // SAFETY: Tauri hands out the live WKWebView on the main thread.
            let view = unsafe { Retained::retain(platform.inner().cast::<NSView>()) }.unwrap();
            TRACKED.with(|tracked| tracked.borrow_mut().insert(label, Weak::from_retained(&view)));
            sender.send(()).unwrap();
        })
        .unwrap();
        receiver.recv_timeout(std::time::Duration::from_secs(10)).unwrap();
    }

    /// Whether the tracked WKWebView has been deallocated.
    pub fn freed(app: &AppHandle, label: &str) -> bool {
        let (sender, receiver) = std::sync::mpsc::channel();
        let label = label.to_owned();
        app.run_on_main_thread(move || {
            let freed = TRACKED.with(|tracked| tracked.borrow()[&label].load().is_none());
            sender.send(freed).unwrap();
        })
        .unwrap();
        receiver.recv_timeout(std::time::Duration::from_secs(10)).unwrap()
    }
}

fn main() {
    let root = PathBuf::from(std::env::var_os("AVIBE_COOKIE_PROBE_ROOT").expect("explicit test root"));
    assert!(root.is_dir());
    let home = PathBuf::from(std::env::var_os("HOME").expect("test HOME"));
    assert!(home.starts_with(&root), "HOME must be inside the test root");
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let address = listener.local_addr().unwrap();
    let seen = Shared::default();
    let server_seen = seen.clone();
    std::thread::spawn(move || {
        for stream in listener.incoming().flatten() {
            let seen = server_seen.clone();
            std::thread::spawn(move || serve(stream, seen));
        }
    });
    let app = tauri::Builder::default().build(tauri::generate_context!()).unwrap();
    let mut seen = Some(seen);
    app.run(move |app, event| {
        let tauri::RunEvent::Ready = event else { return };
        let Some(seen) = seen.take() else { return };
        // A visible, unfocused physical window, as the shell's main window is
        // while a Workbench is covered.
        tauri::window::WindowBuilder::new(app, "main")
            .inner_size(900., 600.)
            .focused(false)
            .build()
            .unwrap();
        let handle = app.clone();
        let root = root.clone();
        // Retirement must run off the UI thread, as the shell runs it.
        std::thread::spawn(move || {
            let outcome = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                let control = workbench(&handle, "control", true, Some(root.join("control")));
                start(&control, address, &seen);
                let disposable = workbench(&handle, "disposable", true, Some(root.join("disposable")));
                start(&disposable, address, &seen);
                let persistent = workbench(&handle, "persistent", false, None);
                start(&persistent, address, &seen);

                // A disposable store is retired the way sign-out, an
                // instance switch and a rebind retire it.
                let disposable_returned = retire(&handle, &disposable);
                // A persistent store goes through the same destruction
                // (`destroy_completed`, which `retire` ends with and which the
                // local Runtime and the account view use directly).
                let persistent_returned = retire(&handle, &persistent);
                assert_stopped(&handle, "disposable", &seen, disposable_returned);
                println!("item 2: instance_store::retire freed the WKWebView and stopped the page, its iframe and its event stream");
                assert_stopped(&handle, "persistent", &seen, persistent_returned);
                println!("item 2: retiring a persistent-store view freed the WKWebView and stopped the page, its iframe and its event stream");

                // The silence above is not WebKit idling a hidden page: the
                // hidden control view, never retired, still has its WKWebView
                // and its event stream, and its page still runs script.
                #[cfg(target_os = "macos")]
                assert!(!native::freed(&handle, "control"), "the control WKWebView was freed");
                assert!(stream_open(&seen, "control"), "the control event stream closed");
                let asked = now();
                control
                    .eval("fetch('/ping/control-eval', {cache: 'no-store'})")
                    .unwrap();
                wait_for("the hidden control page to run script", Duration::from_secs(10), || {
                    pings_since(&seen, "control-eval", asked) >= 1
                });
                let since = now() - CONTROL_WINDOW;
                println!(
                    "control: hidden and unretired, WKWebView alive, event stream open, script runs; in the last {CONTROL_WINDOW} ms its timers pinged {} times (iframe {}) and it answered {} events",
                    pings_since(&seen, "control", since),
                    pings_since(&seen, "control-frame", since),
                    pings_since(&seen, "control-stream", since),
                );
                let control_returned = retire(&handle, &control);
                assert_stopped(&handle, "control", &seen, control_returned);

                println!("retired workbench probe passed");
            }));
            handle.exit(if outcome.is_ok() { 0 } else { 1 });
        });
    });
}
