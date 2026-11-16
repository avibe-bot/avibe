//! Hermetic probe of the Vault sandbox window boundary with real WebKit windows.
//!
//! A loopback server plays both sides: `http://127.0.0.1:<port>` is the
//! Workbench (and "elsewhere"), `http://localhost:<port>` is the sandbox, which
//! makes them different sites as the real ones are. No passkey, keychain or
//! system browser is involved, and nothing outside the test root is written.
//! Run it through `tests/run_cookie_probe.py`.
//!
//! By default the windows are never activated, so the probe does not take the
//! keyboard on a desktop in use. `AVIBE_VAULT_WINDOW_PROBE_FOCUS=1` also
//! activates the app and checks which window is key.
//!
//! `AVIBE_VAULT_WINDOW_PROBE_SHIPPING=1` runs the shell's own install instead of
//! the probe's. This executable is signed ad hoc, as a development build is, so
//! it must come out exactly as a shell without the feature.

#[cfg(not(target_os = "macos"))]
fn main() {}

#[cfg(target_os = "macos")]
fn main() {
    probe::run();
}

#[cfg(target_os = "macos")]
mod probe {
    use std::{
        io::{Read, Write},
        net::TcpListener,
        path::PathBuf,
        sync::mpsc::{self, Receiver, Sender},
        time::{Duration, Instant},
    };

    use objc2::{
        rc::Retained,
        runtime::{AnyClass, AnyObject},
        MainThreadMarker,
    };
    use objc2_app_kit::{NSApplication, NSWindow};
    use objc2_web_kit::WKWebView;
    use serde_json::{json, Value};
    use tauri::{webview::NewWindowResponse, AppHandle, WebviewUrl, WebviewWindow, WebviewWindowBuilder};

    const RESERVE_DEADLINE: Duration = Duration::from_secs(3);
    const CHILD_CLASS: &std::ffi::CStr = c"AvibeVaultSandboxWebView";
    /// Stands in for the shell's own init scripts: it must run once per
    /// Workbench document and never in a child.
    const SHELL_SCRIPT: &str = "window.__PROBE_SHELL__ = (window.__PROBE_SHELL__ || 0) + 1;";

    const WORKBENCH_PAGE: &str = r#"<!doctype html><meta charset="utf-8"><title>workbench</title>
<iframe id="sandbox" src="http://localhost:PORT/frame"></iframe>
<script>
const report = (data) => fetch('/report', { method: 'POST', body: JSON.stringify(data) });
window.probe = { report, windows: {} };
addEventListener('message', (event) => report({ case: 'workbench-message', data: event.data, origin: event.origin }));
document.getElementById('sandbox').addEventListener('load', () => report({
  case: 'workbench-ready',
  marker: window.__AVIBE_DESKTOP_VAULT_PASSKEY__ === true,
  shell: window.__PROBE_SHELL__,
  handler: typeof (window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.ipc),
}));
</script>"#;

    const FRAME_PAGE: &str = r#"<!doctype html><meta charset="utf-8">
<script>
const report = (data) => fetch('/report', { method: 'POST', body: JSON.stringify(data) });
let own = null;
addEventListener('message', (event) => {
  if (event.data === 'open-own') {
    own = window.open('/window#case=own', 'avibe-vault-setup', 'popup,width=440,height=620');
    report({ case: 'frame-opened', opened: !!own });
    const poll = setInterval(() => {
      if (own && own.closed) { clearInterval(poll); report({ case: 'frame-saw-closed' }); }
    }, 100);
  } else if (event.data && event.data.type === 'child-ready') {
    report({ case: 'frame-message', from: event.data.name, origin: event.origin, opened: event.source === own });
    event.source.postMessage({ type: 'frame-reply' }, event.origin);
  }
});
report({
  case: 'frame-ready',
  marker: typeof window.__AVIBE_DESKTOP_VAULT_PASSKEY__,
  shell: typeof window.__PROBE_SHELL__,
});
</script>"#;

    const WINDOW_PAGE: &str = r#"<!doctype html><meta charset="utf-8"><title>a title the page chose</title><body>
<script>
const name = location.hash.replace('#case=', '');
const report = (data) => fetch('/report', { method: 'POST', body: JSON.stringify({ window: name, ...data }) });
const settle = (request) => request.then(() => 'loaded', () => 'failed');
const image = (source) => new Promise((resolve) => {
  const element = new Image();
  element.onload = () => resolve('loaded');
  element.onerror = () => resolve('failed');
  element.src = source;
});
addEventListener('message', (event) => {
  if (event.data === 'close') window.close();
  if (event.data === 'focus') report({ case: 'child-focus', focused: document.hasFocus() });
  if (event.data && event.data.type === 'frame-reply') report({ case: 'child-reply', origin: event.origin });
});
(async () => {
  await report({
    case: 'child',
    opener: !!window.opener,
    frames: window.opener ? window.opener.frames.length : -1,
    shell: typeof window.__PROBE_SHELL__,
    marker: typeof window.__AVIBE_DESKTOP_VAULT_PASSKEY__,
    tauri: typeof window.__TAURI_INTERNALS__,
    ipc: typeof window.ipc,
    handler: typeof (window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.ipc),
    ipcFetch: await settle(fetch('ipc://localhost/probe', { method: 'POST', body: '{}' })),
    tauriFetch: await settle(fetch('tauri://localhost/index.html')),
    tauriImage: await image('tauri://localhost/favicon.ico'),
  });
  if (window.opener) {
    const targets = [window.opener];
    for (let index = 0; index < window.opener.frames.length; index += 1) targets.push(window.opener.frames[index]);
    for (const target of targets) target.postMessage({ type: 'child-ready', name }, location.origin);
  }
  if (name === 'escape') {
    const nested = window.open('/window#case=nested');
    const web = window.open('https://example.com/from-child');
    const link = document.createElement('a');
    link.href = 'https://example.com/link-from-child';
    link.target = '_blank';
    document.body.appendChild(link);
    link.click();
    location.href = 'http://127.0.0.1:PORT/elsewhere';
    setTimeout(() => report({ case: 'escape-result', href: location.href, nested: !!nested, web: !!web }), 1000);
  }
})();
</script>"#;

    const ELSEWHERE_PAGE: &str = r#"<!doctype html><meta charset="utf-8"><title>elsewhere</title>
<script>fetch('/report', { method: 'POST', body: JSON.stringify({ case: 'elsewhere', top: window.self === window.top }) });</script>"#;

    /// One native child window, as AppKit and WebKit describe it.
    #[derive(Debug, Clone)]
    struct Seen {
        visible: bool,
        key: bool,
        attached: bool,
        title: String,
        url: String,
        width: f64,
        height: f64,
        shares_store: bool,
        persistent: bool,
        own_controller: bool,
        scripts: usize,
    }

    pub fn run() {
        let root = PathBuf::from(std::env::var_os("AVIBE_VAULT_WINDOW_PROBE_ROOT").expect("explicit test root"));
        assert!(root.is_dir());
        let home = PathBuf::from(std::env::var_os("HOME").expect("test HOME"));
        assert!(home.starts_with(&root), "HOME must be inside the test root");
        let focus = std::env::var_os("AVIBE_VAULT_WINDOW_PROBE_FOCUS").is_some();
        let shipping = std::env::var_os("AVIBE_VAULT_WINDOW_PROBE_SHIPPING").is_some();

        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let (report_tx, report_rx) = mpsc::channel();
        std::thread::spawn(move || serve(listener, port, report_tx));

        let mut app = tauri::Builder::default().build(tauri::generate_context!()).unwrap();
        if !focus {
            // The event loop activates the app at launch unless it cannot be.
            app.set_activation_policy(tauri::ActivationPolicy::Prohibited);
        }
        let handle = app.handle().clone();
        let mut report_rx = Some(report_rx);
        app.run(move |app, event| {
            // The probe ends itself; closing the main window must not.
            if let tauri::RunEvent::ExitRequested { api, code: None, .. } = &event {
                api.prevent_exit();
            }
            // As in the shell, the user closing the main window hides it.
            if let tauri::RunEvent::WindowEvent {
                event: tauri::WindowEvent::CloseRequested { api, .. },
                ..
            } = &event
            {
                api.prevent_close();
            }
            if let tauri::RunEvent::Ready = event {
                let (browser_tx, browser_rx) = mpsc::channel();
                let main = WebviewWindowBuilder::new(app, "main", WebviewUrl::External("about:blank".parse().unwrap()))
                    .visible(focus)
                    .incognito(true)
                    .data_directory(root.join("webview"))
                    .initialization_script(SHELL_SCRIPT)
                    // The shell's handler would open the system browser here.
                    .on_new_window(move |url, _features| {
                        let _ = browser_tx.send(url.to_string());
                        NewWindowResponse::Deny
                    })
                    .on_page_load(|window, payload| {
                        if payload.event() == tauri::webview::PageLoadEvent::Started {
                            avibe_desktop_lib::vault_window_opener_navigated(&window);
                        }
                    })
                    .build()
                    .unwrap();
                if shipping {
                    avibe_desktop_lib::install_vault_window(&main);
                } else {
                    avibe_desktop_lib::install_vault_window_for_probe(
                        &main,
                        &format!("http://localhost:{port}"),
                        RESERVE_DEADLINE,
                    );
                }
                let handle = handle.clone();
                let reports = report_rx.take().unwrap();
                std::thread::spawn(move || {
                    let outcome = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                        let probe = Probe {
                            app: handle.clone(),
                            main,
                            port,
                            focus,
                            reports,
                            backlog: Vec::new(),
                            browser: browser_rx,
                        };
                        if shipping {
                            probe.run_without_the_signed_association();
                        } else {
                            probe.run();
                        }
                    }));
                    handle.exit(if outcome.is_ok() { 0 } else { 1 });
                });
            }
        });
    }

    struct Probe {
        app: AppHandle,
        main: WebviewWindow,
        port: u16,
        focus: bool,
        reports: Receiver<Value>,
        backlog: Vec<Value>,
        browser: Receiver<String>,
    }

    impl Probe {
        fn run(mut self) {
            let sandbox = format!("http://localhost:{}", self.port);
            let workbench = format!("http://127.0.0.1:{}", self.port);
            if self.focus {
                self.on_main(|mtm| {
                    #[allow(deprecated)]
                    NSApplication::sharedApplication(mtm).activateIgnoringOtherApps(true);
                });
            }

            // The Workbench's top-level document gets the marker and the
            // shell's script; the sandbox iframe inside it gets neither.
            self.main
                .navigate(format!("{workbench}/workbench").parse().unwrap())
                .unwrap();
            let ready = self.report("workbench-ready");
            assert_eq!(ready["marker"], true, "{ready}");
            assert_eq!(ready["shell"], 1, "{ready}");
            assert_eq!(ready["handler"], "object", "{ready}");
            let frame = self.report("frame-ready");
            assert_eq!(frame["marker"], "undefined", "{frame}");
            assert_eq!(frame["shell"], "undefined", "{frame}");
            assert!(self.children().is_empty());

            // The Workbench reserves a blank window and writes into it, as any
            // page of its origin could. The window exists but is not on screen,
            // and the page's requests to the shell's URL schemes fail.
            self.eval(
                r#"(async () => {
                    const child = probe.windows.reserved = window.open('about:blank', '_blank', 'popup,width=520,height=760');
                    child.document.write('<title>not the sandbox</title><h1 id="mark">drawn by the opener</h1><img src="tauri://localhost/x">');
                    const settle = (request) => request.then(() => 'loaded', () => 'failed');
                    probe.report({
                        case: 'reserved',
                        drawn: !!child.document.getElementById('mark'),
                        ipcFetch: await settle(child.fetch('ipc://localhost/probe', { method: 'POST', body: '{}' })),
                        tauriFetch: await settle(child.fetch('tauri://localhost/index.html')),
                        shell: typeof child.__PROBE_SHELL__,
                        handler: typeof (child.webkit && child.webkit.messageHandlers && child.webkit.messageHandlers.ipc),
                    });
                })()"#,
            );
            let reserved = self.report("reserved");
            assert_eq!(reserved["drawn"], true, "{reserved}");
            assert_eq!(reserved["ipcFetch"], "failed", "{reserved}");
            assert_eq!(reserved["tauriFetch"], "failed", "{reserved}");
            assert_eq!(reserved["shell"], "undefined", "{reserved}");
            assert_eq!(reserved["handler"], "undefined", "{reserved}");
            std::thread::sleep(Duration::from_millis(700));
            let hidden = self.children();
            assert_eq!(hidden.len(), 1, "{hidden:?}");
            assert!(!hidden[0].visible && !hidden[0].attached, "{hidden:?}");
            assert_eq!(hidden[0].title, format!("localhost:{}", self.port));
            assert_no_shell_state(&hidden[0]);

            // Pointed at the sandbox, it appears once that document commits,
            // linked to its opener in both directions.
            self.eval(&format!(
                "probe.windows.reserved.location.replace('{sandbox}/window#case=reserved')"
            ));
            let child = self.report("child");
            assert_child_page(&child, "reserved", 1);
            let heard = self.report("frame-message");
            assert_eq!(heard["from"], "reserved", "{heard}");
            assert_eq!(heard["origin"], sandbox, "{heard}");
            let reply = self.report("child-reply");
            assert_eq!(reply["origin"], sandbox, "{reply}");
            let shown = self.wait_children("the committed sandbox window", |children| {
                children.len() == 1 && children[0].visible
            });
            assert!(shown[0].attached, "{shown:?}");
            assert_eq!(shown[0].title, format!("localhost:{}", self.port));
            assert_eq!(shown[0].url, format!("{sandbox}/window#case=reserved"));
            assert_requested_size(&shown[0], 520.0, 760.0);
            assert_no_shell_state(&shown[0]);
            if self.focus {
                // A passkey ceremony needs the keyboard in the sandbox document.
                self.wait_children("the shown window to be key", |children| children[0].key);
                self.eval("probe.windows.reserved.postMessage('focus', '*')");
                let focus = self.report("child-focus");
                assert_eq!(focus["focused"], true, "{focus}");
            }

            // The opener can no longer read or replace what is on screen.
            self.eval(&format!(
                r#"(() => {{
                    const child = probe.windows.reserved;
                    let readable;
                    try {{ readable = !!child.document; }} catch (error) {{ readable = false; }}
                    child.location.replace('{workbench}/elsewhere');
                    setTimeout(() => {{ child.location = 'about:blank'; }}, 300);
                    setTimeout(() => probe.report({{ case: 'opener-view', readable, closed: child.closed }}), 900);
                }})()"#
            ));
            let view = self.report("opener-view");
            assert_eq!(view["readable"], false, "{view}");
            assert_eq!(view["closed"], false, "{view}");
            let still = self.children();
            assert_eq!(still.len(), 1, "{still:?}");
            assert!(still[0].visible, "{still:?}");
            assert_eq!(still[0].url, format!("{sandbox}/window#case=reserved"));

            // `window.close()` closes the native window, and the opener's own
            // script and message handler are untouched.
            self.eval("probe.windows.reserved.postMessage('close', '*')");
            self.wait_children("window.close() to close the native window", |children| {
                children.is_empty()
            });
            self.eval(
                r#"setTimeout(() => probe.report({
                    case: 'after-close',
                    closed: probe.windows.reserved.closed,
                    shell: window.__PROBE_SHELL__,
                    handler: typeof window.webkit.messageHandlers.ipc,
                }), 300)"#,
            );
            let after = self.report("after-close");
            assert_eq!(after["closed"], true, "{after}");
            assert_eq!(after["shell"], 1, "{after}");
            assert_eq!(after["handler"], "object", "{after}");
            if self.focus {
                let deadline = Instant::now() + Duration::from_secs(10);
                while !self.on_main_window(|window, _| window.isKeyWindow()) {
                    assert!(Instant::now() < deadline, "focus did not return to the main window");
                    std::thread::sleep(Duration::from_millis(100));
                }
            }

            // The sandbox iframe opens its own window. The request comes from a
            // message handler, without a user gesture: on macOS WebKit lets
            // scripts open windows by default, so nothing here may rely on one.
            self.eval("document.getElementById('sandbox').contentWindow.postMessage('open-own', '*')");
            let opened = self.report("frame-opened");
            assert_eq!(opened["opened"], true, "{opened}");
            let child = self.report("child");
            assert_child_page(&child, "own", 0);
            let heard = self.report("frame-message");
            assert_eq!(heard["from"], "own", "{heard}");
            assert_eq!(heard["opened"], true, "the frame hears its own window: {heard}");
            let shown = self.wait_children("the frame's own window", |children| {
                children.len() == 1 && children[0].visible
            });
            assert_requested_size(&shown[0], 440.0, 620.0);
            assert_no_shell_state(&shown[0]);
            // The user closes it; the page that opened it sees a closed popup.
            self.close_child_as_the_user();
            self.report("frame-saw-closed");
            assert!(self.children().is_empty());

            // A sandbox address that redirects elsewhere never shows anything,
            // and the unanswered window is closed at the deadline.
            self.eval(&format!("probe.windows.redirect = window.open('{sandbox}/redirect')"));
            let pending = self.wait_children("the redirecting window", |children| children.len() == 1);
            assert!(!pending[0].visible, "{pending:?}");
            std::thread::sleep(Duration::from_millis(1200));
            let pending = self.children();
            assert!(pending.len() == 1 && !pending[0].visible, "{pending:?}");
            self.wait_children("the deadline to close an unanswered window", |children| {
                children.is_empty()
            });
            self.eval("probe.report({ case: 'redirect-closed', closed: probe.windows.redirect.closed })");
            let redirect = self.report("redirect-closed");
            assert_eq!(redirect["closed"], true, "{redirect}");

            // The sandbox page itself cannot leave, open a window, or reach
            // the browser.
            self.eval(&format!(
                "probe.windows.escape = window.open('{sandbox}/window#case=escape')"
            ));
            let child = self.report("child");
            assert_child_page(&child, "escape", 1);
            let escape = self.report("escape-result");
            assert_eq!(escape["href"], format!("{sandbox}/window#case=escape"), "{escape}");
            assert_eq!(escape["nested"], false, "{escape}");
            assert_eq!(escape["web"], false, "{escape}");
            let stays = self.children();
            assert_eq!(stays.len(), 1, "{stays:?}");
            assert!(stays[0].visible, "{stays:?}");
            assert_eq!(stays[0].url, format!("{sandbox}/window#case=escape"));
            assert!(
                self.browser.try_recv().is_err(),
                "a child reached the new-window handler"
            );

            // Reloading the Workbench closes its children; the new document
            // has one copy of the shell's script, the marker and the handler.
            self.eval("location.reload()");
            let ready = self.report("workbench-ready");
            assert_eq!(ready["marker"], true, "{ready}");
            assert_eq!(ready["shell"], 1, "{ready}");
            assert_eq!(ready["handler"], "object", "{ready}");
            self.report("frame-ready");
            assert!(self.children().is_empty(), "a reload closes the children");

            // Every other request for a window still reaches the shell's
            // handler, which hands web links to the browser and drops the rest.
            self.eval(&format!(
                r#"probe.report({{ case: 'others', opened: [
                    'https://example.com/docs', '{workbench}/show/page', 'https://sandbox.avibe.bot/',
                    'https://localhost:{port}/window', 'http://localhost:1/window', 'about:blank#reserved',
                ].map((url) => !!window.open(url)) }})"#,
                port = self.port
            ));
            let others = self.report("others");
            assert_eq!(
                others["opened"],
                json!([false, false, false, false, false, false]),
                "{others}"
            );
            let handed: Vec<String> = self.browser.try_iter().collect();
            assert_eq!(
                handed[..5],
                [
                    "https://example.com/docs".to_owned(),
                    format!("{workbench}/show/page"),
                    "https://sandbox.avibe.bot/".to_owned(),
                    format!("https://localhost:{}/window", self.port),
                    "http://localhost:1/window".to_owned(),
                ]
            );
            // Only the exact blank page is a reservation. WebKit reports this
            // one with its fragment escaped.
            assert!(
                handed.len() == 6 && handed[5].starts_with("about:blank") && handed[5] != "about:blank",
                "{handed:?}"
            );
            assert!(self.children().is_empty());

            // A page cannot hold more than a few hidden windows, and they do
            // not outlive the deadline.
            self.eval(
                "probe.report({ case: 'many', opened: [0, 1, 2, 3, 4, 5].map(() => !!window.open('about:blank')) })",
            );
            let many = self.report("many");
            assert_eq!(many["opened"], json!([true, true, true, true, false, false]), "{many}");
            let held = self.children();
            assert_eq!(held.len(), 4, "{held:?}");
            assert!(held.iter().all(|child| !child.visible), "{held:?}");
            self.wait_children("the deadline to close reserved windows", |children| children.is_empty());

            // No child outlives the main window: not when the user closes it,
            // which the shell answers by hiding it, and not when it is destroyed.
            for last in ["closed", "destroyed"] {
                self.eval(&format!(
                    "window.open('about:blank'); window.open('{sandbox}/window#case={last}')"
                ));
                let child = self.report("child");
                assert_child_page(&child, last, 1);
                self.wait_children("a shown and a reserved window", |children| {
                    children.len() == 2 && children.iter().filter(|child| child.visible).count() == 1
                });
                if last == "closed" {
                    self.main.close().unwrap();
                    self.wait_children("the closed main window to take its children along", |children| {
                        children.is_empty()
                    });
                }
            }
            self.main.destroy().unwrap();
            let deadline = Instant::now() + Duration::from_secs(15);
            while self.child_windows() > 0 {
                assert!(Instant::now() < deadline, "children outlived the main window");
                std::thread::sleep(Duration::from_millis(100));
            }
            assert!(
                self.backlog.iter().all(|report| report["case"] != "elsewhere"),
                "{:?}",
                self.backlog
            );
            assert!(self.reports.try_iter().all(|report| report["case"] != "elsewhere"));

            println!(
                "vault window probe: marker scope, hidden reservation, show on sandbox commit, opener link, \
                 empty content controller, URL scheme guard, navigation refusals, close paths, deadline, cap, \
                 reload and main-window teardown passed{}",
                if self.focus { " (with focus)" } else { "" }
            );
        }

        /// The shell's own install on a build whose signature does not
        /// associate it with the sandbox: no marker, and the sandbox's windows
        /// go where every other window goes.
        fn run_without_the_signed_association(mut self) {
            self.main
                .navigate(format!("http://127.0.0.1:{}/workbench", self.port).parse().unwrap())
                .unwrap();
            let ready = self.report("workbench-ready");
            assert_eq!(ready["marker"], false, "{ready}");
            assert_eq!(ready["shell"], 1, "{ready}");
            self.eval(
                r#"probe.report({ case: 'shipping', opened: [
                    'about:blank', 'https://sandbox.avibe.bot/r/release/index.html#mode=authorize&id=1',
                ].map((url) => !!window.open(url)) })"#,
            );
            let opened = self.report("shipping");
            assert_eq!(opened["opened"], json!([false, false]), "{opened}");
            let handed: Vec<String> = self.browser.try_iter().collect();
            assert_eq!(
                handed,
                [
                    "about:blank",
                    "https://sandbox.avibe.bot/r/release/index.html#mode=authorize&id=1"
                ]
            );
            assert_eq!(self.child_windows(), 0);
            println!("vault window probe: without the signed association there is no marker and no in-app window");
        }

        fn eval(&self, script: &str) {
            self.main.eval(script).unwrap();
        }

        /// The next report named `case`. A page that loaded outside the
        /// sandbox fails the probe wherever it shows up.
        fn report(&mut self, case: &str) -> Value {
            if let Some(index) = self.backlog.iter().position(|report| report["case"] == case) {
                return self.backlog.remove(index);
            }
            let deadline = Instant::now() + Duration::from_secs(20);
            loop {
                let wait = deadline.saturating_duration_since(Instant::now());
                let report = self
                    .reports
                    .recv_timeout(wait)
                    .unwrap_or_else(|_| panic!("no {case:?} report; unread: {:?}", self.backlog));
                assert_ne!(
                    report["case"], "elsewhere",
                    "a page outside the sandbox loaded: {report}"
                );
                if report["case"] == case {
                    return report;
                }
                self.backlog.push(report);
            }
        }

        fn on_main<T: Send + 'static>(&self, work: impl FnOnce(MainThreadMarker) -> T + Send + 'static) -> T {
            let (tx, rx) = mpsc::channel();
            self.app
                .run_on_main_thread(move || {
                    let _ = tx.send(work(MainThreadMarker::new().expect("main thread")));
                })
                .unwrap();
            rx.recv_timeout(Duration::from_secs(10)).expect("main thread answered")
        }

        fn on_main_window<T: Send + 'static>(
            &self,
            work: impl FnOnce(&NSWindow, &WKWebView) -> T + Send + 'static,
        ) -> T {
            let (tx, rx) = mpsc::channel();
            self.main
                .with_webview(move |platform| {
                    // SAFETY: Tauri hands out the live objects on the main thread.
                    let (window, webview) = unsafe {
                        (
                            Retained::retain(platform.ns_window().cast::<NSWindow>()).expect("main NSWindow"),
                            Retained::retain(platform.inner().cast::<WKWebView>()).expect("main WKWebView"),
                        )
                    };
                    let _ = tx.send(work(&window, &webview));
                })
                .unwrap();
            rx.recv_timeout(Duration::from_secs(10)).expect("main thread answered")
        }

        /// Every native window that holds a child webview.
        fn children(&self) -> Vec<Seen> {
            self.on_main_window(|main_window, main_webview| {
                let mtm = MainThreadMarker::new().expect("main thread");
                // SAFETY: Main thread; every object is live for this closure.
                unsafe {
                    let main_configuration = main_webview.configuration();
                    let main_store = main_configuration.websiteDataStore();
                    let main_controller = main_configuration.userContentController();
                    NSApplication::sharedApplication(mtm)
                        .windows()
                        .iter()
                        .filter_map(|window| {
                            let view = window.contentView()?;
                            if !is_child_view(&view) {
                                return None;
                            }
                            let webview = Retained::retain(Retained::as_ptr(&view) as *mut WKWebView)?;
                            let configuration = webview.configuration();
                            let store = configuration.websiteDataStore();
                            let controller = configuration.userContentController();
                            let size = window.contentRectForFrameRect(window.frame()).size;
                            Some(Seen {
                                visible: window.isVisible(),
                                key: window.isKeyWindow(),
                                attached: window
                                    .parentWindow()
                                    .is_some_and(|parent| std::ptr::eq(&*parent, main_window)),
                                title: window.title().to_string(),
                                url: webview
                                    .URL()
                                    .and_then(|url| url.absoluteString())
                                    .map(|url| url.to_string())
                                    .unwrap_or_default(),
                                width: size.width,
                                height: size.height,
                                shares_store: std::ptr::eq(&*store, &*main_store),
                                persistent: store.isPersistent(),
                                own_controller: !std::ptr::eq(&*controller, &*main_controller),
                                scripts: controller.userScripts().len(),
                            })
                        })
                        .collect()
                }
            })
        }

        fn wait_children(&self, what: &str, done: impl Fn(&[Seen]) -> bool) -> Vec<Seen> {
            let deadline = Instant::now() + Duration::from_secs(15);
            loop {
                let children = self.children();
                if done(&children) {
                    return children;
                }
                assert!(Instant::now() < deadline, "timed out waiting for {what}: {children:?}");
                std::thread::sleep(Duration::from_millis(100));
            }
        }

        fn close_child_as_the_user(&self) {
            self.on_main(|mtm| {
                for window in child_windows(mtm) {
                    window.performClose(None);
                }
            });
        }

        /// Counted without the main window, which may be gone.
        fn child_windows(&self) -> usize {
            self.on_main(|mtm| child_windows(mtm).len())
        }
    }

    fn child_windows(mtm: MainThreadMarker) -> Vec<Retained<NSWindow>> {
        NSApplication::sharedApplication(mtm)
            .windows()
            .iter()
            .filter(|window| window.contentView().is_some_and(|view| is_child_view(&view)))
            .collect()
    }

    /// Whether a view is a child's webview. WebKit observes its webviews, so
    /// the object's class is a subclass the system derived from the child class.
    fn is_child_view(view: &objc2_app_kit::NSView) -> bool {
        // The class is registered when the first child is made.
        let Some(child) = AnyClass::get(CHILD_CLASS) else {
            return false;
        };
        let object: &AnyObject = view.as_ref();
        let mut class = Some(object.class());
        while let Some(current) = class {
            if std::ptr::eq(current, child) {
                return true;
            }
            class = current.superclass();
        }
        false
    }

    /// The sandbox page's own view of where it runs.
    /// `opener_frames` is 1 when the Workbench opened the window (its sandbox
    /// iframe) and 0 when that iframe did.
    fn assert_child_page(child: &Value, name: &str, opener_frames: u8) {
        assert_eq!(child["window"], name, "{child}");
        assert_eq!(child["opener"], true, "{child}");
        assert_eq!(
            child["frames"], opener_frames,
            "the opener's frames are readable: {child}"
        );
        for nothing_of_the_shell in ["shell", "marker", "tauri", "ipc", "handler"] {
            assert_eq!(
                child[nothing_of_the_shell], "undefined",
                "{nothing_of_the_shell}: {child}"
            );
        }
        for refused in ["ipcFetch", "tauriFetch", "tauriImage"] {
            assert_eq!(child[refused], "failed", "{refused}: {child}");
        }
    }

    /// The size the page asked for; AppKit shortens a window the screen cannot hold.
    fn assert_requested_size(child: &Seen, width: f64, height: f64) {
        assert_eq!(child.width, width, "{child:?}");
        assert!(child.height <= height && child.height >= height.min(400.0), "{child:?}");
    }

    /// The opener's browsing data, and nothing else of its webview.
    fn assert_no_shell_state(child: &Seen) {
        assert!(child.shares_store, "{child:?}");
        assert!(!child.persistent, "the probe's store is disposable: {child:?}");
        assert!(child.own_controller, "{child:?}");
        assert_eq!(child.scripts, 0, "{child:?}");
    }

    fn serve(listener: TcpListener, port: u16, reports: Sender<Value>) {
        for stream in listener.incoming() {
            let Ok(mut stream) = stream else {
                continue;
            };
            let _ = stream.set_read_timeout(Some(Duration::from_secs(5)));
            let mut request = Vec::new();
            let complete = loop {
                let mut chunk = [0u8; 4096];
                match stream.read(&mut chunk) {
                    Ok(count) if count > 0 => request.extend_from_slice(&chunk[..count]),
                    _ => break false,
                }
                if let Some(split) = request.windows(4).position(|bytes| bytes == b"\r\n\r\n") {
                    let headers = String::from_utf8_lossy(&request[..split]).to_lowercase();
                    let length = headers
                        .lines()
                        .find_map(|line| line.strip_prefix("content-length:"))
                        .map_or(0, |length| length.trim().parse::<usize>().unwrap());
                    if request.len() >= split + 4 + length {
                        break true;
                    }
                }
                if request.len() > 65536 {
                    break false;
                }
            };
            if !complete {
                continue;
            }
            let text = String::from_utf8_lossy(&request).into_owned();
            let path = text
                .lines()
                .next()
                .and_then(|line| line.split_whitespace().nth(1))
                .unwrap_or("/");
            let page = |body: &str| {
                format!(
                    "HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nCache-Control: no-store\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                    body.len(),
                    body
                )
            };
            let response = match path {
                "/workbench" => page(&WORKBENCH_PAGE.replace("PORT", &port.to_string())),
                "/frame" => page(FRAME_PAGE),
                "/window" => page(&WINDOW_PAGE.replace("PORT", &port.to_string())),
                "/redirect" => format!(
                    "HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1:{port}/elsewhere\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                ),
                "/report" => {
                    let body = text.split_once("\r\n\r\n").map_or("", |(_, body)| body);
                    if let Ok(report) = serde_json::from_str::<Value>(body) {
                        let _ = reports.send(report);
                    }
                    page("ok")
                }
                "/elsewhere" | "/show/page" => page(ELSEWHERE_PAGE),
                _ => "HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".to_owned(),
            };
            let _ = stream.write_all(response.as_bytes());
        }
    }
}
