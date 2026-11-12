//! Hermetic native WebView probe. Does not start Avibe or use an OS credential store.
//! Run the built executable with an empty HOME and a test-owned probe root.
use std::{
    io::{Read, Write},
    net::TcpListener,
    path::PathBuf,
    sync::mpsc,
    time::Duration,
};
use tauri::{Manager, WebviewUrl, WebviewWindowBuilder};

fn main() {
    let root = PathBuf::from(std::env::var_os("AVIBE_COOKIE_PROBE_ROOT").expect("explicit test root"));
    assert!(root.is_dir());
    let home = PathBuf::from(std::env::var_os("HOME").expect("test HOME"));
    assert!(home.starts_with(&root), "HOME must be inside the test root");
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let address = listener.local_addr().unwrap();
    let (result_tx, result_rx) = mpsc::channel();
    let (late_tx, late_rx) = mpsc::channel();
    let (release_tx, release_rx) = mpsc::channel();
    let fixture = std::thread::spawn(move || {
        let mut release = Some(release_rx);
        let mut delayed = None;
        for stream in listener.incoming() {
            let mut stream = stream.unwrap();
            stream.set_read_timeout(Some(Duration::from_secs(10))).unwrap();
            let mut request = Vec::new();
            loop {
                let mut chunk = [0u8; 4096];
                let count = stream.read(&mut chunk).unwrap();
                assert!(count > 0);
                request.extend_from_slice(&chunk[..count]);
                if let Some(split) = request.windows(4).position(|bytes| bytes == b"\r\n\r\n") {
                    let headers = String::from_utf8_lossy(&request[..split]);
                    let length = headers
                        .lines()
                        .find_map(|line| {
                            line.to_lowercase()
                                .strip_prefix("content-length:")
                                .map(|n| n.trim().parse::<usize>().unwrap())
                        })
                        .unwrap_or(0);
                    if request.len() >= split + 4 + length {
                        break;
                    }
                }
                assert!(request.len() < 65536);
            }
            let text = String::from_utf8(request).unwrap();
            let path = text.lines().next().unwrap().split_whitespace().nth(1).unwrap();
            if path == "/late" {
                let release = release.take().unwrap();
                late_tx.send(()).unwrap();
                let completed = late_tx.clone();
                delayed = Some(std::thread::spawn(move || {
                    if release.recv_timeout(Duration::from_secs(20)).is_ok() {
                        // This actual response arrives after the old view was
                        // retired. It may fail to write because Stop cancelled it.
                        let _ = write!(stream, "HTTP/1.1 200 OK\r\nSet-Cookie: late=retired; Path=/; HttpOnly\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok");
                    }
                    let _ = completed.send(());
                }));
                continue;
            }
            let body = if path.starts_with("/result") {
                result_tx.send(text.clone()).unwrap();
                "ok"
            } else {
                "<script>fetch('/result'+location.pathname,{method:'POST',body:document.cookie})</script>"
            };
            write!(
                stream,
                "HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                body.len(),
                body
            )
            .unwrap();
            if path == "/result/second" {
                break;
            }
        }
        if let Some(delayed) = delayed {
            delayed.join().unwrap();
        }
    });
    let app = tauri::Builder::default().build(tauri::generate_context!()).unwrap();
    let handle = app.handle().clone();
    let root_clone = root.clone();
    let mut result_rx = Some(result_rx);
    let mut late_rx = Some(late_rx);
    let mut release_tx = Some(release_tx);
    app.run(move |app, event| {
        if let tauri::RunEvent::Ready = event {
            let first = WebviewWindowBuilder::new(app, "probe-first", WebviewUrl::External("about:blank".parse().unwrap()))
                .visible(false).incognito(true).data_directory(root_clone.join("first")).build().unwrap();
            let second = WebviewWindowBuilder::new(app, "probe-second", WebviewUrl::External("about:blank".parse().unwrap()))
                .visible(false).incognito(true).data_directory(root_clone.join("second")).build().unwrap();
            let handle = handle.clone();
            let results = result_rx.take().unwrap();
            let late = late_rx.take().unwrap();
            let release = release_tx.take().unwrap();
            // These operations must run off the UI thread, especially on Windows.
            std::thread::spawn(move || {
                let outcome = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                    let cookie = tauri::webview::Cookie::build(("fixture_session", "private-fixture"))
                        .domain("127.0.0.1").path("/").http_only(true).same_site(cookie::SameSite::Lax).build();
                    first.set_cookie(cookie).unwrap();
                    let cookies = first.cookies().unwrap();
                    assert_eq!(cookies.len(),1);
                    assert_eq!(cookies[0].http_only(),Some(true));
                    assert_eq!(cookies[0].same_site(),Some(cookie::SameSite::Lax));
                    first.set_cookie(tauri::webview::Cookie::build(("__Host-vibe_remote_session", "secure-fixture"))
                        .domain("studio.avibe.bot").path("/").secure(true).http_only(true)
                        .same_site(cookie::SameSite::Lax).build()).unwrap();
                    let secure = first.cookies_for_url("https://studio.avibe.bot/".parse().unwrap()).unwrap();
                    assert_eq!(secure.len(), 1);
                    assert_eq!(secure[0].name(), "__Host-vibe_remote_session");
                    assert_eq!(secure[0].secure(), Some(true));
                    assert_eq!(secure[0].http_only(), Some(true));
                    assert_eq!(secure[0].same_site(), Some(cookie::SameSite::Lax));
                    assert!(first.cookies_for_url("https://other.avibe.bot/".parse().unwrap()).unwrap().is_empty());
                    assert!(first.cookies_for_url("https://child.studio.avibe.bot/".parse().unwrap()).unwrap().is_empty(),
                        "host-only cookie crossed into a child domain");
                    assert!(second.cookies().unwrap().is_empty());
                    first.navigate(format!("http://{address}/first").parse().unwrap()).unwrap();
                    let text = results.recv_timeout(Duration::from_secs(20)).unwrap();
                    let (headers, body) = text.split_once("\r\n\r\n").unwrap();
                    assert!(headers.starts_with("POST /result/first "));
                    assert!(headers.to_lowercase().contains("cookie: fixture_session=private-fixture"));
                    assert!(!body.contains("private-fixture"), "HttpOnly leaked into JavaScript");
                    first.eval("fetch('/late').catch(() => {})").unwrap();
                    late.recv_timeout(Duration::from_secs(10)).unwrap();
                    second.navigate(format!("http://{address}/second").parse().unwrap()).unwrap();
                    let text = results.recv_timeout(Duration::from_secs(20)).unwrap();
                    assert!(text.starts_with("POST /result/second "));
                    assert!(!text.to_lowercase().contains("cookie:"), "cookie crossed disposable stores");
                    avibe_desktop_lib::instance_store::retire(&first).unwrap();
                    assert!(handle.get_webview_window("probe-first").is_none());
                    release.send(()).unwrap();
                    late.recv_timeout(Duration::from_secs(10)).unwrap();
                    assert!(second.cookies().unwrap().is_empty());
                    println!("cookie probe: __Host attributes, exact host, isolated stores, native erase completion, destruction and delayed response retirement passed");
                }));
                handle.exit(if outcome.is_ok() {0} else {1});
            });
        }

    });
    fixture.join().unwrap();
}
