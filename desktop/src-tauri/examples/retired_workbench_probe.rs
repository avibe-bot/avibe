//! Hermetic native WebView probe for retired Workbench web views. Does not
//! start Avibe or use an OS credential store. Run the built executable through
//! `tests/run_cookie_probe.py`, which supplies an empty HOME and a test root.
//!
//! Each Workbench stand-in is a child web view of one physical window, built
//! the way `build_main_child` builds a Runtime document (about:blank, hidden,
//! then navigated). Its page and an iframe in it ping the loopback server every
//! 200 ms and hold an event stream open. A control view stays hidden and keeps
//! running, which proves a hidden page is observable here. The others are
//! retired through the shell's own helpers and must go silent.
//!
//! On macOS the probe also records what showing and focusing the Runtime does
//! while an account web view covers it: the Runtime is un-hidden beneath the
//! account view and becomes first responder.
use std::{
    collections::HashMap,
    io::{Read, Write},
    net::{SocketAddr, TcpListener, TcpStream},
    path::PathBuf,
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};
use tauri::{webview::WebviewBuilder, AppHandle, LogicalPosition, Manager, Webview, WebviewUrl};

const GRACE: Duration = Duration::from_millis(750);
const QUIET: Duration = Duration::from_secs(4);

#[derive(Default)]
struct Seen {
    pings: HashMap<String, Vec<Instant>>,
    streams_open: HashMap<String, usize>,
    streams_closed: HashMap<String, usize>,
}

type Shared = Arc<Mutex<Seen>>;

fn page(name: &str) -> String {
    format!(
        "<!doctype html><title>{name}</title><script>\
         setInterval(() => fetch('/ping/{name}', {{cache: 'no-store'}}).catch(() => {{}}), 200);\
         new EventSource('/events/{name}');\
         </script><iframe src='/frame/{name}'></iframe>"
    )
}

fn frame(name: &str) -> String {
    format!(
        "<!doctype html><script>\
         setInterval(() => fetch('/ping/{name}-frame', {{cache: 'no-store'}}).catch(() => {{}}), 200);\
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
            seen.lock().unwrap().pings.entry(name).or_default().push(Instant::now());
            let _ =
                stream.write_all(b"HTTP/1.1 204 No Content\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n");
        }
        "events" => {
            let head =
                "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nCache-Control: no-store\r\n\r\n: open\n\n";
            if stream.write_all(head.as_bytes()).is_err() {
                return;
            }
            *seen.lock().unwrap().streams_open.entry(name.clone()).or_default() += 1;
            // The client never sends more; a read returns only when it closes.
            let _ = stream.set_read_timeout(None);
            let mut reader = stream.try_clone().unwrap();
            let writer = std::thread::spawn(move || {
                while stream.write_all(b": keepalive\n\n").is_ok() {
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

fn pings_since(seen: &Shared, name: &str, since: Instant) -> usize {
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
    let started = Instant::now();
    view.navigate(format!("http://{address}/page/{name}").parse().unwrap())
        .unwrap();
    wait_for(&format!("{name} to run"), Duration::from_secs(20), || {
        pings_since(seen, &name, started) >= 3
            && pings_since(seen, &format!("{name}-frame"), started) >= 3
            && seen.lock().unwrap().streams_open.get(&name).copied().unwrap_or(0) >= 1
    });
}

/// After retirement returns, the page, its iframe and its event stream stop.
fn assert_stopped(name: &str, seen: &Shared) {
    std::thread::sleep(GRACE);
    let quiet = Instant::now();
    std::thread::sleep(QUIET);
    let page = pings_since(seen, name, quiet);
    let frame = pings_since(seen, &format!("{name}-frame"), quiet);
    assert_eq!(
        (page, frame),
        (0, 0),
        "{name} kept running after retirement (page, iframe pings in {QUIET:?})"
    );
    wait_for(&format!("{name} event stream to close"), Duration::from_secs(5), || {
        let seen = seen.lock().unwrap();
        seen.streams_closed.get(name).copied().unwrap_or(0) >= seen.streams_open.get(name).copied().unwrap_or(0)
    });
}

#[cfg(target_os = "macos")]
mod native {
    use objc2::{msg_send, rc::Retained, runtime::AnyObject};
    use objc2_app_kit::{NSView, NSWindow};
    use tauri::Webview;

    pub struct Facts {
        pub hidden: bool,
        pub first_responder: bool,
        pub index: usize,
    }

    /// Reads the WKWebView's hidden flag, whether its window's first responder
    /// is it or inside it, and its z-order among its superview's subviews.
    pub fn facts(view: &Webview) -> Facts {
        let (sender, receiver) = std::sync::mpsc::channel();
        view.with_webview(move |platform| {
            // SAFETY: Tauri hands out the live WKWebView on the main thread.
            let view = unsafe { Retained::retain(platform.inner().cast::<NSView>()) }.unwrap();
            let window: Option<Retained<NSWindow>> = view.window();
            let first_responder = window
                .and_then(|window| window.firstResponder())
                .is_some_and(|responder| {
                    let responder: &AnyObject = responder.as_ref();
                    let is_view: bool = unsafe { msg_send![responder, isKindOfClass: objc2::class!(NSView)] };
                    is_view && unsafe { msg_send![responder, isDescendantOf: &*view] }
                });
            let index = unsafe {
                let superview = view.superview().unwrap();
                let subviews: Retained<AnyObject> = msg_send![&superview, subviews];
                let index: usize = msg_send![&subviews, indexOfObject: &*view];
                index
            };
            sender
                .send(Facts {
                    hidden: view.isHidden(),
                    first_responder,
                    index,
                })
                .unwrap();
        })
        .unwrap();
        receiver.recv_timeout(std::time::Duration::from_secs(10)).unwrap()
    }
}

/// What the focus path did before the fix when a shown account document had
/// not finished reloading: show and focus the Runtime. Records the result.
#[cfg(target_os = "macos")]
fn record_covered_focus(app: &AppHandle, address: SocketAddr, seen: &Shared) {
    let runtime = workbench(app, "item1-runtime", false, None);
    start(&runtime, address, seen);
    runtime.show().unwrap();
    let account = workbench(app, "item1-account", true, None);
    account
        .navigate(format!("http://{address}/frame/item1-account").parse().unwrap())
        .unwrap();
    wait_for("account page", Duration::from_secs(20), || {
        pings_since(seen, "item1-account-frame", Instant::now() - Duration::from_secs(1)) >= 1
    });
    // The account screen as the shell shows it: account shown and focused,
    // the Runtime covered by hiding it.
    account.show().unwrap();
    account.set_focus().unwrap();
    runtime.hide().unwrap();
    std::thread::sleep(Duration::from_millis(300));
    let before = native::facts(&runtime);
    assert!(
        before.hidden && !before.first_responder,
        "setup: the Runtime must start hidden and unfocused"
    );
    assert!(
        native::facts(&account).first_responder,
        "setup: the account view must hold focus"
    );
    runtime.show().unwrap();
    runtime.set_focus().unwrap();
    std::thread::sleep(Duration::from_millis(300));
    let after = native::facts(&runtime);
    let account_facts = native::facts(&account);
    println!(
        "item 1: showing and focusing the covered Runtime left it hidden={} first_responder={}, account first_responder={} above_runtime={}",
        after.hidden,
        after.first_responder,
        account_facts.first_responder,
        account_facts.index > after.index
    );
    // Recorded, not asserted: this describes WebKit and AppKit, and the shell
    // no longer takes this path (`displayed_webview`).
    avibe_desktop_lib::instance_store::destroy_completed(&account).unwrap();
    avibe_desktop_lib::instance_store::destroy_completed(&runtime).unwrap();
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
                avibe_desktop_lib::instance_store::retire(&disposable).unwrap();
                assert!(handle.get_webview("disposable").is_none());
                assert_stopped("disposable", &seen);
                println!("item 2: instance_store::retire stopped the page, its iframe and its event stream");

                // The local Runtime and the account view are destroyed
                // without an erase.
                avibe_desktop_lib::instance_store::destroy_completed(&persistent).unwrap();
                assert!(handle.get_webview("persistent").is_none());
                assert_stopped("persistent", &seen);
                println!("item 2: instance_store::destroy_completed stopped the page, its iframe and its event stream");

                // The silence above is not the server or WebKit going quiet:
                // the hidden control page kept running throughout.
                let since = Instant::now() - QUIET;
                let (page, frame) = (
                    pings_since(&seen, "control", since),
                    pings_since(&seen, "control-frame", since),
                );
                println!(
                    "control: a hidden, unretired page pinged {page} times (iframe {frame}) in the last {QUIET:?}"
                );
                assert!(
                    page >= 2 && frame >= 2,
                    "the hidden control page stopped too; the probe cannot tell"
                );
                avibe_desktop_lib::instance_store::retire(&control).unwrap();
                assert_stopped("control", &seen);

                #[cfg(target_os = "macos")]
                record_covered_focus(&handle, address, &seen);
                println!("retired workbench probe passed");
            }));
            handle.exit(if outcome.is_ok() { 0 } else { 1 });
        });
    });
}
