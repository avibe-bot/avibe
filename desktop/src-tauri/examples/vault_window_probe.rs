//! Hermetic probe of the Vault sandbox window boundary with real WebKit windows.
//!
//! A loopback server plays both sides: `http://127.0.0.1:<port>` is the
//! Workbench (and "elsewhere"), `http://localhost:<port>` is the sandbox, which
//! makes them different sites as the real ones are. No passkey, keychain or
//! system browser is involved, and nothing outside the test root is written.
//! Run it through `tests/run_cookie_probe.py`.
//!
//! There are three runs. By default the app is never activated, so the probe
//! does not take the keyboard on a desktop in use; the shell then closes a
//! sandbox window instead of showing it, and that is what this run checks.
//! `AVIBE_VAULT_WINDOW_PROBE_FOCUS=1` activates the app and checks everything a
//! window on screen must satisfy, including which window is key.
//!
//! The shell's module is compiled into this executable from its source file, so
//! the probe runs the shipping code without the library exporting anything for
//! it. The shell installs the window only for a build whose signature carries
//! an Apple-issued entitlement, which no test executable can have, and only for
//! `https://sandbox.avibe.bot`, which no test can serve. The probe therefore
//! enters one step below that admission and names a loopback origin.
//!
//! `AVIBE_VAULT_WINDOW_PROBE_SHIPPING=1` runs the shell's own install, admission
//! included. This executable is signed ad hoc, as a development build is, so it
//! must come out exactly as a shell without the feature.

#[cfg(not(target_os = "macos"))]
fn main() {}

#[cfg(target_os = "macos")]
fn main() {
    probe::run();
}

#[cfg(target_os = "macos")]
#[path = "../src/macos_vault_window.rs"]
mod macos_vault_window;

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

    use crate::macos_vault_window;

    const RESERVE_DEADLINE: Duration = Duration::from_secs(5);
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
    fullscreen: document.fullscreenEnabled === true || document.webkitFullscreenEnabled === true,
    pictureInPicture: document.pictureInPictureEnabled === true,
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

    /// A sandbox page that cannot paint until its stylesheet arrives, which the
    /// server holds back. Its own colour is green.
    const SLOW_PAGE: &str = r#"<!doctype html><meta charset="utf-8"><title>slow</title>
<link rel="stylesheet" href="/slow.css"><body>
<script>fetch('/report', { method: 'POST', body: JSON.stringify({ case: 'slow-loaded' }) });</script>"#;
    const SLOW_STYLE: &str = "html, body { margin: 0; height: 100%; background: #00ff00; }";
    const STYLE_HOLD: Duration = Duration::from_secs(2);
    /// The same page with a stylesheet that outlasts the shell's deadline.
    const STUCK_PAGE: &str = r#"<!doctype html><meta charset="utf-8"><title>stuck</title>
<link rel="stylesheet" href="/never.css"><body>"#;
    const STUCK_HOLD: Duration = Duration::from_secs(8);

    /// A sandbox page that behaves as the sandbox's card does where it matters
    /// here: it arms itself half a second after it starts running, whether or
    /// not anyone can see it. It logs every click and key it receives and
    /// reports the log when its opener asks. With `#slow` it cannot paint
    /// until the server releases its stylesheet, two seconds later.
    const CARD_PAGE: &str = r#"<!doctype html><meta charset="utf-8"><title>card</title>
<script>
const log = { case: 'card-log', loadedAt: Date.now(), armedAt: null, clicks: [], keys: [], commands: [] };
setTimeout(() => { log.armedAt = Date.now(); }, 500);
addEventListener('click', () => log.clicks.push(Date.now()), true);
addEventListener('keydown', (event) => { (event.metaKey ? log.commands : log.keys).push(Date.now()); event.preventDefault(); }, true);
addEventListener('keyup', (event) => event.preventDefault(), true);
addEventListener('message', (event) => {
  if (event.data === 'log') fetch('/report', { method: 'POST', body: JSON.stringify(log) });
});
if (location.hash === '#slow') document.write('<link rel="stylesheet" href="/slow.css">');
</script>
<body style="margin:0"><button style="position:fixed;inset:0;width:100%;height:100%">Confirm</button>"#;

    /// A sandbox page that reports nothing, for windows the shell must close.
    const QUIET_PAGE: &str = r#"<!doctype html><meta charset="utf-8"><title>quiet</title><p>sandbox</p>"#;

    const ELSEWHERE_PAGE: &str = r#"<!doctype html><meta charset="utf-8"><title>elsewhere</title>
<script>fetch('/report', { method: 'POST', body: JSON.stringify({ case: 'elsewhere', top: window.self === window.top }) });</script>"#;

    /// One native child window, as AppKit and WebKit describe it.
    #[derive(Debug, Clone)]
    struct Seen {
        /// The native window is on screen.
        visible: bool,
        /// Its webview is drawn.
        revealed: bool,
        /// Clicks on it would fall through to what is underneath.
        passes_clicks: bool,
        /// AppKit says no part of it can be seen.
        occluded: bool,
        /// Its window number.
        number: isize,
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
        own_preferences: bool,
        fullscreen: bool,
    }

    impl Seen {
        /// On screen and showing its page.
        fn shown(&self) -> bool {
            self.visible && self.revealed && !self.passes_clicks
        }
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
                    .visible(true)
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
                            macos_vault_window::opener_navigated(&window);
                        }
                    })
                    .build()
                    .unwrap();
                if shipping {
                    macos_vault_window::install(&main);
                } else {
                    macos_vault_window::install_with(
                        &main,
                        macos_vault_window::Policy {
                            origin: macos_vault_window::Origin {
                                scheme: "http".to_owned(),
                                host: "localhost".to_owned(),
                                port: Some(port),
                            },
                            reserve_deadline: RESERVE_DEADLINE,
                        },
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
                        } else if focus {
                            probe.run_active();
                        } else {
                            probe.run_inactive();
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
        /// The app is active and its main window is on screen, as when the user
        /// has just clicked in the Workbench: sandbox windows may appear.
        fn run_active(mut self) {
            let sandbox = format!("http://localhost:{}", self.port);
            let workbench = format!("http://127.0.0.1:{}", self.port);
            if self.focus {
                self.activate();
            }
            self.open_workbench();
            self.reserve_and_draw();

            // Pointed at the sandbox, the reserved window appears once that
            // document has been presented, linked to its opener both ways.
            self.eval(&format!(
                "probe.windows.reserved.location.replace('{sandbox}/window#case=reserved')"
            ));
            self.a_window_opened_by_the_workbench_loaded("reserved");
            let shown = self.wait_children("the committed sandbox window", |children| {
                children.len() == 1 && children[0].shown()
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
            assert!(still[0].shown(), "{still:?}");
            assert_eq!(still[0].url, format!("{sandbox}/window#case=reserved"));

            // One sandbox window on screen at a time: a second one is closed,
            // not shown beside or after the first.
            self.eval(&format!("probe.windows.second = window.open('{sandbox}/quiet')"));
            self.never_on_screen("a second sandbox window", "second", 1);

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

            // A page keeps a window in reserve and points it at the sandbox
            // the moment the one on screen closes. The second window appears;
            // what protects a click meant for the first is that no window
            // takes input when it has only just appeared (see "input" below).
            self.eval(&format!(
                "probe.windows.first = window.open('{sandbox}/window#case=first'); probe.windows.next = window.open('about:blank')"
            ));
            self.a_window_opened_by_the_workbench_loaded("first");
            self.wait_children("the first window and the one in reserve", |children| {
                children.len() == 2 && children.iter().filter(|child| child.shown()).count() == 1
            });
            self.eval(&format!(
                "probe.windows.first.close(); probe.windows.next.location.replace('{sandbox}/quiet')"
            ));
            self.wait_children("the window in reserve to take the first one's place", |children| {
                children.len() == 1 && children[0].shown() && children[0].url.ends_with("/quiet")
            });
            self.eval("probe.windows.next.close()");
            self.wait_children("the second window to close", |children| children.is_empty());

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
                children.len() == 1 && children[0].shown()
            });
            assert_requested_size(&shown[0], 440.0, 620.0);
            assert_no_shell_state(&shown[0]);
            // The user closes it; the page that opened it sees a closed popup.
            self.close_child_as_the_user();
            self.report("frame-saw-closed");
            assert!(self.children().is_empty());

            self.a_redirect_never_shows();

            // The sandbox page itself cannot leave, open a window, or reach
            // the browser. The size it is given stays within fixed bounds.
            self.eval(&format!(
                "probe.windows.escape = window.open('{sandbox}/window#case=escape', '_blank', 'popup,width=5000,height=5000')"
            ));
            self.a_window_opened_by_the_workbench_loaded("escape");
            let escape = self.report("escape-result");
            assert_eq!(escape["href"], format!("{sandbox}/window#case=escape"), "{escape}");
            assert_eq!(escape["nested"], false, "{escape}");
            assert_eq!(escape["web"], false, "{escape}");
            let stays = self.wait_children("the page that tried to leave", |children| {
                children.len() == 1 && children[0].shown()
            });
            assert_eq!(stays[0].url, format!("{sandbox}/window#case=escape"));
            assert_eq!(stays[0].width, 900.0, "{stays:?}");
            assert!(stays[0].height <= 1000.0, "{stays:?}");
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

            // What the opener drew in the reserved page is never displayed, not
            // even between the sandbox document's commit and its first paint.
            // The opener paints the page magenta. The sandbox page is green and
            // cannot paint until the server releases its stylesheet.
            self.eval(
                r#"(() => {
                    const child = probe.windows.painted = window.open('about:blank', '_blank', 'popup,width=520,height=760');
                    child.document.write('<body style="margin:0;background:#ff00ff"><div style="height:3000px"></div>');
                })()"#,
            );
            std::thread::sleep(Duration::from_millis(1500));
            self.eval(&format!(
                "probe.windows.painted.location.replace('{sandbox}/slow-window')"
            ));
            let started = Instant::now();
            let (mut before_release, mut after_release) = (0usize, 0usize);
            let mut released = false;
            loop {
                released = released || self.arrived("style-released");
                if let Some(colours) = self.child_pixels() {
                    assert!(
                        !colours.iter().any(|colour| is_magenta(*colour)),
                        "the opener's page was displayed {:?} after the navigation: {colours:?}",
                        started.elapsed()
                    );
                    if released {
                        after_release += 1;
                        if colours.iter().all(|colour| is_green(*colour)) {
                            break;
                        }
                    } else {
                        before_release += 1;
                    }
                }
                assert!(
                    started.elapsed() < Duration::from_secs(15),
                    "the sandbox page was never displayed ({before_release} + {after_release} samples; \
                     the window now: {:?}; read from it: {:?})",
                    self.children(),
                    self.child_pixels().map(|colours| colours[..3].to_vec())
                );
                std::thread::sleep(Duration::from_millis(20));
            }
            // The window was on screen, and was looked at, while the sandbox
            // page could not paint.
            assert!(
                before_release >= 10,
                "only {before_release} samples before the first paint"
            );
            let painted = self.children();
            assert!(painted.len() == 1 && painted[0].shown(), "{painted:?}");
            self.eval("probe.windows.painted.close()");
            self.wait_children("the painted window to close", |children| children.is_empty());

            // A sandbox page that never paints: the window stays empty, takes
            // neither clicks nor keys, never shows what the opener drew, and is
            // closed at the deadline.
            self.eval(&format!(
                r#"(() => {{
                    const child = probe.windows.stuck = window.open('about:blank');
                    child.document.write('<body style="margin:0;background:#ff00ff">');
                    setTimeout(() => child.location.replace('{sandbox}/stuck-window'), 500);
                }})()"#
            ));
            self.wait_children("the stuck window to come on screen", |children| {
                children.len() == 1 && children[0].visible
            });
            let mut samples = 0usize;
            loop {
                let children = self.children();
                if children.is_empty() {
                    break;
                }
                assert!(
                    !children[0].revealed && !children[0].key && !children[0].passes_clicks,
                    "an empty window is key, or lets clicks through: {children:?}"
                );
                if let Some(colours) = self.child_pixels() {
                    samples += 1;
                    assert!(
                        !colours.iter().any(|colour| is_magenta(*colour)),
                        "the opener's page was displayed: {colours:?}"
                    );
                }
                std::thread::sleep(Duration::from_millis(50));
            }
            assert!(samples >= 10, "only {samples} samples of the stuck window");
            self.eval("probe.report({ case: 'stuck-closed', closed: probe.windows.stuck.closed })");
            let stuck = self.report("stuck-closed");
            assert_eq!(stuck["closed"], true, "{stuck}");

            // While the main window is hidden, nothing appears; the window is
            // closed, not kept for later.
            self.hide_the_main_window();
            self.eval(&format!("probe.windows.unseen = window.open('{sandbox}/quiet')"));
            self.never_on_screen("a sandbox window over a hidden main window", "unseen", 0);

            self.main.show().unwrap();
            if self.focus {
                self.activate();
            }

            self.a_card_takes_no_input_until_it_has_been_seen();

            self.other_requests_keep_their_handler();
            self.reservations_are_bounded();

            // No child outlives the main window: not when the user closes it,
            // which the shell answers by hiding it, and not when it is destroyed.
            for last in ["closed", "destroyed"] {
                self.eval(&format!(
                    "window.open('about:blank'); window.open('{sandbox}/window#case={last}')"
                ));
                self.a_window_opened_by_the_workbench_loaded(last);
                self.wait_children("a shown and a reserved window", |children| {
                    children.len() == 2 && children.iter().filter(|child| child.shown()).count() == 1
                });
                if last == "closed" {
                    self.main.close().unwrap();
                    self.wait_children("the closed main window to take its children along", |children| {
                        children.is_empty()
                    });
                }
            }
            self.the_main_window_takes_its_children_along();

            println!(
                "vault window probe (app active{}): marker scope, hidden reservation, reveal only after the \
                 sandbox document is presented, opener link, empty content controller and own preferences, \
                 URL scheme guard, navigation refusals, one window at a time, quiet interval, hidden main \
                 window, no input before a card has been seen, size bounds, close paths, \
                 deadline, cap, reload and main-window teardown passed",
                if self.focus { ", with focus" } else { "" }
            );
        }

        /// The app is not the active one, as when the user has gone elsewhere:
        /// a sandbox window is closed instead of appearing. Nothing here takes
        /// the keyboard, so this run is safe on a desktop in use.
        fn run_inactive(mut self) {
            let sandbox = format!("http://localhost:{}", self.port);
            assert!(
                !self.on_main(|mtm| NSApplication::sharedApplication(mtm).isActive()),
                "this run needs the app not to be active"
            );
            assert!(self.on_main_window(|window, _| window.isVisible()));
            self.open_workbench();
            self.reserve_and_draw();

            self.eval(&format!("probe.windows.reserved.location.replace('{sandbox}/quiet')"));
            self.never_on_screen("a sandbox window while the app is not active", "reserved", 0);

            self.a_redirect_never_shows();
            self.other_requests_keep_their_handler();
            self.reservations_are_bounded();

            self.eval("window.open('about:blank'); window.open('about:blank')");
            self.wait_children("two reserved windows", |children| children.len() == 2);
            self.main.close().unwrap();
            self.wait_children("the closed main window to take its children along", |children| {
                children.is_empty()
            });
            self.eval("window.open('about:blank')");
            self.wait_children("a reserved window", |children| children.len() == 1);
            self.the_main_window_takes_its_children_along();

            println!(
                "vault window probe (app not active): marker scope, hidden reservation, URL scheme guard, no \
                 window while the app is not active, redirect refusal, deadline, cap and main-window teardown \
                 passed"
            );
        }

        /// The sandbox page in `probe.windows[name]` ran: it has nothing of the
        /// shell, reaches the sandbox iframe through its opener, and hears back.
        fn a_window_opened_by_the_workbench_loaded(&mut self, name: &str) {
            let sandbox = format!("http://localhost:{}", self.port);
            let child = self.report("child");
            assert_child_page(&child, name, 1);
            let heard = self.report("frame-message");
            assert_eq!(heard["from"], name, "{heard}");
            assert_eq!(heard["origin"], sandbox, "{heard}");
            let reply = self.report("child-reply");
            assert_eq!(reply["origin"], sandbox, "{reply}");
        }

        fn activate(&self) {
            self.on_main(|mtm| {
                #[allow(deprecated)]
                NSApplication::sharedApplication(mtm).activateIgnoringOtherApps(true);
            });
        }

        /// The Workbench's top-level document gets the marker and the shell's
        /// script; the sandbox iframe inside it gets neither.
        fn open_workbench(&mut self) {
            self.main
                .navigate(format!("http://127.0.0.1:{}/workbench", self.port).parse().unwrap())
                .unwrap();
            let ready = self.report("workbench-ready");
            assert_eq!(ready["marker"], true, "{ready}");
            assert_eq!(ready["shell"], 1, "{ready}");
            assert_eq!(ready["handler"], "object", "{ready}");
            let frame = self.report("frame-ready");
            assert_eq!(frame["marker"], "undefined", "{frame}");
            assert_eq!(frame["shell"], "undefined", "{frame}");
            assert!(self.children().is_empty());
        }

        /// The Workbench reserves a blank window and writes into it, as any
        /// page of its origin could. The window exists but is not on screen,
        /// and the page's requests to the shell's URL schemes fail.
        fn reserve_and_draw(&mut self) {
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
        }

        /// The window `probe.windows[name]`, whose sandbox document commits
        /// now, is closed by the shell without ever being on screen. `others`
        /// is how many windows are on screen meanwhile and stay.
        fn never_on_screen(&mut self, what: &str, name: &str, others: usize) {
            let started = Instant::now();
            loop {
                let children = self.children();
                assert_eq!(
                    children.iter().filter(|child| child.visible).count(),
                    others,
                    "{what} appeared: {children:?}"
                );
                if children.len() == others && started.elapsed() > Duration::from_millis(1500) {
                    break;
                }
                assert!(
                    started.elapsed() < Duration::from_secs(10),
                    "{what} was not closed: {children:?}"
                );
                std::thread::sleep(Duration::from_millis(40));
            }
            self.eval(&format!(
                "probe.report({{ case: 'refused', name: '{name}', closed: probe.windows['{name}'].closed }})"
            ));
            let refused = self.report("refused");
            assert_eq!(refused["closed"], true, "{what}: {refused}");
        }

        /// Makes the Finder the active app, as a user clicking its window would.
        /// This app's windows stay where they are. (`-[NSApplication deactivate]`
        /// alone leaves the app active when nothing else asks for the front.)
        /// Returns whether this app stopped being the active one.
        fn go_to_another_app(&self) -> bool {
            self.on_main(|mtm| {
                // SAFETY: Main thread; plain AppKit calls with constant arguments.
                unsafe {
                    let finder: Retained<objc2_foundation::NSArray> = objc2::msg_send![
                        objc2::class!(NSRunningApplication),
                        runningApplicationsWithBundleIdentifier: objc2_foundation::ns_string!("com.apple.finder")
                    ];
                    let finder = finder.firstObject().expect("the Finder is running");
                    // NSApplicationActivateAllWindows: an ordinary activation.
                    let _: bool = objc2::msg_send![&*finder, activateWithOptions: 1usize];
                }
                NSApplication::sharedApplication(mtm).deactivate();
            });
            let deadline = Instant::now() + Duration::from_millis(1500);
            while self.on_main(|mtm| NSApplication::sharedApplication(mtm).isActive()) {
                if Instant::now() > deadline {
                    return false;
                }
                std::thread::sleep(Duration::from_millis(50));
            }
            true
        }

        fn hide_the_main_window(&self) {
            self.main.hide().unwrap();
            let deadline = Instant::now() + Duration::from_secs(5);
            while self.on_main_window(|window, _| window.isVisible()) {
                assert!(Instant::now() < deadline, "the main window did not hide");
                std::thread::sleep(Duration::from_millis(50));
            }
        }

        /// No click and no key reaches a card the user has not had in front
        /// of them: for 0.75 s when it first appears, and for 2 s whenever
        /// they come back to it or the app comes forward.
        ///
        /// The card page arms itself half a second after it starts running, as
        /// the sandbox's Confirm does, and logs every click and key it gets.
        /// Clicks and a Return key are delivered to the window the whole time,
        /// through this application's own event dispatch.
        fn a_card_takes_no_input_until_it_has_been_seen(&mut self) {
            let sandbox = format!("http://localhost:{}", self.port);
            let input = self.start_input();

            // First sight. The page runs, and arms, two seconds before it can
            // paint: when the card is displayed it would take a confirming
            // click at once, were the window to deliver one.
            self.eval(&format!("probe.windows.card = window.open('{sandbox}/card#slow')"));
            let mut before = now_ms();
            let displayed_after = loop {
                let children = self.children();
                if children.len() == 1 && children[0].shown() {
                    break before;
                }
                before = now_ms();
                assert!(self.child_windows() <= 1, "{children:?}");
                std::thread::sleep(Duration::from_millis(20));
            };
            let displayed_by = now_ms();
            self.arrived("style-released");
            std::thread::sleep(Duration::from_millis(2500));
            let log = self.card_log();
            assert!(
                log.armed_at < displayed_by,
                "the card was to be armed before it was displayed: {log:?}, displayed by {displayed_by}"
            );
            log.nothing_before("first sight", displayed_after + 750.0);
            log.something_after("first sight", displayed_after + 750.0);
            let shown = self.children();
            assert!(shown.len() == 1 && shown[0].shown(), "{shown:?}");
            if self.focus {
                assert!(shown[0].key, "a live card has the keyboard: {shown:?}");
            }

            // The main window, and the card with it, goes away and comes back
            // while the app stays where it is: two seconds from the return.
            self.hide_the_main_window();
            self.wait_children("the card to go with the main window", |children| {
                children.len() == 1 && !children[0].visible
            });
            let gone = now_ms();
            std::thread::sleep(Duration::from_millis(500));
            let back = now_ms();
            self.main.show().unwrap();
            self.wait_children("the card to come back with the main window", |children| {
                children.len() == 1 && children[0].visible
            });
            std::thread::sleep(Duration::from_millis(3500));
            let log = self.card_log();
            log.nothing_between("the card came back with the main window", gone, back + 2000.0);
            log.something_after("the card came back with the main window", back + 2000.0);

            // Another window of the app covers the card completely and goes
            // away again: the same two seconds, from AppKit's own word on
            // whether the card can be seen.
            let cover = self.another_window_of_the_app();
            let card = self.children()[0].number;
            self.on_main({
                let cover = cover.clone();
                move |mtm| {
                    let card = child_windows(mtm).into_iter().next().expect("the card");
                    let cover = ns_window(&cover);
                    cover.setFrame_display(card.frame(), true);
                    cover.orderFront(None);
                }
            });
            let deadline = Instant::now() + Duration::from_secs(4);
            let covered = loop {
                let children = self.children();
                if children[0].occluded {
                    break true;
                }
                if Instant::now() > deadline {
                    break false;
                }
                std::thread::sleep(Duration::from_millis(50));
            };
            if covered {
                let covered = now_ms();
                std::thread::sleep(Duration::from_millis(500));
                let back = now_ms();
                cover.hide().unwrap();
                self.wait_children("the card to be uncovered", |children| !children[0].occluded);
                std::thread::sleep(Duration::from_millis(3500));
                let log = self.card_log();
                log.nothing_between("the card was uncovered", covered, back + 2000.0);
                log.something_after("the card was uncovered", back + 2000.0);
                println!("vault window probe: input after the card was fully covered and uncovered checked");
            } else {
                cover.hide().unwrap();
                std::thread::sleep(Duration::from_millis(2500));
                println!(
                    "vault window probe: AppKit did not report the covered card as hidden; full covering not checked"
                );
            }
            assert_eq!(self.children()[0].number, card);

            if self.focus {
                // The user goes to another app and returns.
                if self.go_to_another_app() {
                    let left = now_ms();
                    std::thread::sleep(Duration::from_millis(500));
                    let back = now_ms();
                    self.activate();
                    self.wait_until_active();
                    std::thread::sleep(Duration::from_millis(3500));
                    let log = self.card_log();
                    log.nothing_between("the user returned from another app", left, back + 2000.0);
                    log.something_after("the user returned from another app", back + 2000.0);
                    println!("vault window probe: input after returning from another app checked");
                } else {
                    println!(
                        "vault window probe: the system kept this app in front; returning from another app not checked"
                    );
                }

                // The app is hidden and comes forward again.
                let hidden = self.hide_the_app();
                std::thread::sleep(Duration::from_millis(500));
                let back = now_ms();
                self.activate();
                self.wait_until_active();
                std::thread::sleep(Duration::from_millis(3500));
                let log = self.card_log();
                log.nothing_between("the app came forward again", hidden, back + 2000.0);
                log.something_after("the app came forward again", back + 2000.0);
            }
            let commands = self.card_log().commands.len();
            println!("vault window probe: {commands} command-key events reached the live card");
            self.eval("probe.windows.card.close()");
            self.wait_children("the card to close", |children| children.is_empty());

            // The main window comes back and a card is asked for at once: it
            // is displayed at once, and takes input two seconds after the
            // main window came back, not 0.75 s after it appeared.
            self.hide_the_main_window();
            std::thread::sleep(Duration::from_millis(300));
            let back = now_ms();
            self.main.show().unwrap();
            self.a_card_asked_for_on_arrival("the main window came back", back);

            if self.focus {
                // The same when the app itself comes forward.
                self.hide_the_app();
                let forward = now_ms();
                self.activate();
                self.wait_until_active();
                self.a_card_asked_for_on_arrival("the app came forward", forward);
            }
            input.stop();

            // A card is displayed in front of the app's other windows and with
            // the keyboard, and nothing moves or changes hands when it starts
            // to take input.
            cover.show().unwrap();
            self.on_main({
                let cover = cover.clone();
                move |_| ns_window(&cover).makeKeyAndOrderFront(None)
            });
            let in_front = self.windows_front_to_back();
            self.eval(&format!("probe.windows.card = window.open('{sandbox}/card')"));
            let shown = self.wait_children("a card with another window in front", |children| {
                children.len() == 1 && children[0].shown()
            });
            let (order, key) = (self.windows_front_to_back(), self.key_window());
            assert_ne!(in_front.first(), Some(&shown[0].number));
            assert_eq!(
                order.first(),
                Some(&shown[0].number),
                "the displayed card is not the app's frontmost window: {order:?}"
            );
            if self.focus {
                assert_eq!(
                    key,
                    Some(shown[0].number),
                    "the displayed card does not have the keyboard"
                );
            }
            std::thread::sleep(Duration::from_millis(1500));
            assert_eq!(
                (self.windows_front_to_back(), self.key_window()),
                (order, key),
                "windows moved or the keyboard changed hands when the card began to take input"
            );
            self.eval("probe.windows.card.close()");
            self.wait_children("the card to close", |children| children.is_empty());
            cover.destroy().unwrap();
        }

        /// A card asked for right after the user arrived (`arrived` is taken
        /// before the arrival) is displayed at once and takes input no sooner
        /// than two seconds after the arrival.
        fn a_card_asked_for_on_arrival(&mut self, what: &str, arrived: f64) {
            self.eval(&format!(
                "probe.windows.card = window.open('http://localhost:{}/card')",
                self.port
            ));
            self.wait_children("the card asked for on arrival", |children| {
                children.len() == 1 && children[0].shown()
            });
            let displayed = now_ms();
            assert!(
                displayed < arrived + 1500.0,
                "{what}: the card was held back {} ms",
                displayed - arrived
            );
            std::thread::sleep(Duration::from_millis(3500));
            let log = self.card_log();
            log.nothing_before(what, arrived + 2000.0);
            log.something_after(what, arrived + 2000.0);
            self.eval("probe.windows.card.close()");
            self.wait_children("the card to close", |children| children.is_empty());
        }

        /// Hides the app, as Command-H does; returns when it had left the front.
        fn hide_the_app(&self) -> f64 {
            self.on_main(|mtm| NSApplication::sharedApplication(mtm).hide(None));
            let deadline = Instant::now() + Duration::from_secs(10);
            while self.on_main(|mtm| NSApplication::sharedApplication(mtm).isActive()) {
                assert!(Instant::now() < deadline, "the app did not leave the front");
                std::thread::sleep(Duration::from_millis(50));
            }
            now_ms()
        }

        /// A plain second window of this app, as a native app window is.
        fn another_window_of_the_app(&self) -> WebviewWindow {
            WebviewWindowBuilder::new(&self.app, "other", WebviewUrl::External("about:blank".parse().unwrap()))
                .title("another window")
                .visible(false)
                .incognito(true)
                .build()
                .unwrap()
        }

        /// The numbers of this app's windows that are on screen, front to back.
        fn windows_front_to_back(&self) -> Vec<isize> {
            self.on_main(|mtm| {
                let app = NSApplication::sharedApplication(mtm);
                // SAFETY: Main thread; AppKit's list of this app's windows.
                let windows: Retained<objc2_foundation::NSArray<NSWindow>> =
                    unsafe { objc2::msg_send![&*app, orderedWindows] };
                windows
                    .iter()
                    .filter(|window| window.isVisible())
                    .map(|window| window.windowNumber())
                    .collect()
            })
        }

        fn key_window(&self) -> Option<isize> {
            self.on_main(|mtm| {
                NSApplication::sharedApplication(mtm)
                    .keyWindow()
                    .map(|window| window.windowNumber())
            })
        }

        fn wait_until_active(&self) {
            let deadline = Instant::now() + Duration::from_secs(10);
            while !self.on_main(|mtm| NSApplication::sharedApplication(mtm).isActive()) {
                assert!(Instant::now() < deadline, "the app did not come forward");
                std::thread::sleep(Duration::from_millis(50));
            }
        }

        /// What the card page has received so far, on the page's own clock,
        /// which is this machine's.
        fn card_log(&mut self) -> CardLog {
            self.eval("probe.windows.card.postMessage('log', '*')");
            let log = self.report("card-log");
            let times = |key: &str| -> Vec<f64> {
                log[key]
                    .as_array()
                    .expect("a list of times")
                    .iter()
                    .map(|time| time.as_f64().expect("a time"))
                    .collect()
            };
            CardLog {
                armed_at: log["armedAt"].as_f64().expect("the card armed itself"),
                clicks: times("clicks"),
                keys: times("keys"),
                commands: times("commands"),
            }
        }

        /// Delivers a click on the middle of the child window's content and a
        /// Return key to that window every 30 ms, until stopped. The events are
        /// made for the probe's own window and handed to its own application
        /// object, the way AppKit hands on what the system sends it; nothing
        /// is posted to the system.
        fn start_input(&self) -> InputStream {
            let stop = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false));
            let stopped = stop.clone();
            let app = self.app.clone();
            let thread = std::thread::spawn(move || {
                while !stopped.load(std::sync::atomic::Ordering::SeqCst) {
                    let _ = app.run_on_main_thread(|| {
                        let mtm = MainThreadMarker::new().expect("main thread");
                        if let Some(window) = child_windows(mtm).into_iter().find(|window| window.isVisible()) {
                            deliver_input(mtm, &window);
                        }
                    });
                    std::thread::sleep(Duration::from_millis(30));
                }
            });
            InputStream { stop, thread }
        }

        /// A sandbox address that redirects elsewhere never shows anything, and
        /// the unanswered window is closed at the deadline.
        fn a_redirect_never_shows(&mut self) {
            self.eval(&format!(
                "probe.windows.redirect = window.open('http://localhost:{}/redirect')",
                self.port
            ));
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
        }

        /// Every other request for a window still reaches the shell's handler,
        /// which hands web links to the browser and drops the rest.
        fn other_requests_keep_their_handler(&mut self) {
            let workbench = format!("http://127.0.0.1:{}", self.port);
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
        }

        /// The main webview's pages cannot hold more than a few hidden windows,
        /// and those do not outlive the deadline.
        fn reservations_are_bounded(&mut self) {
            self.eval(
                "probe.report({ case: 'many', opened: [0, 1, 2, 3, 4, 5].map(() => !!window.open('about:blank')) })",
            );
            let many = self.report("many");
            assert_eq!(many["opened"], json!([true, true, true, true, false, false]), "{many}");
            let held = self.children();
            assert_eq!(held.len(), 4, "{held:?}");
            assert!(held.iter().all(|child| !child.visible), "{held:?}");
            self.wait_children("the deadline to close reserved windows", |children| children.is_empty());
        }

        fn the_main_window_takes_its_children_along(&mut self) {
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
        }

        /// Whether a report named `case` has arrived, without waiting for it.
        fn arrived(&mut self, case: &str) -> bool {
            self.backlog.extend(self.reports.try_iter());
            assert!(
                self.backlog.iter().all(|report| report["case"] != "elsewhere"),
                "a page outside the sandbox loaded"
            );
            match self.backlog.iter().position(|report| report["case"] == case) {
                Some(index) => {
                    self.backlog.remove(index);
                    true
                }
                None => false,
            }
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
                    let main_preferences = main_configuration.preferences();
                    NSApplication::sharedApplication(mtm)
                        .windows()
                        .iter()
                        .filter_map(|window| {
                            let view = child_view(&window)?;
                            let webview = Retained::retain(Retained::as_ptr(&view) as *mut WKWebView)?;
                            let configuration = webview.configuration();
                            let store = configuration.websiteDataStore();
                            let controller = configuration.userContentController();
                            let preferences = configuration.preferences();
                            let size = window.contentRectForFrameRect(window.frame()).size;
                            Some(Seen {
                                visible: window.isVisible(),
                                revealed: view.alphaValue() == 1.0,
                                passes_clicks: window.ignoresMouseEvents(),
                                occluded: !window
                                    .occlusionState()
                                    .contains(objc2_app_kit::NSWindowOcclusionState::Visible),
                                number: window.windowNumber(),
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
                                own_preferences: !std::ptr::eq(&*preferences, &*main_preferences),
                                fullscreen: preferences.isElementFullscreenEnabled(),
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

        /// A grid of colours from the first child window that is on screen, as
        /// its own views draw it. `None` while no child is on screen.
        fn child_pixels(&self) -> Option<Vec<(u8, u8, u8)>> {
            self.on_main(|mtm| {
                let window = child_windows(mtm).into_iter().find(|window| window.isVisible())?;
                window_pixels(&window)
            })
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

    fn now_ms() -> f64 {
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .expect("a clock after 1970")
            .as_secs_f64()
            * 1000.0
    }

    struct InputStream {
        stop: std::sync::Arc<std::sync::atomic::AtomicBool>,
        thread: std::thread::JoinHandle<()>,
    }

    impl InputStream {
        fn stop(self) {
            self.stop.store(true, std::sync::atomic::Ordering::SeqCst);
            self.thread.join().expect("the input thread ended");
        }
    }

    /// When the card armed itself, and when it received each click and key.
    #[derive(Debug)]
    struct CardLog {
        armed_at: f64,
        clicks: Vec<f64>,
        keys: Vec<f64>,
        /// Keys pressed with Command, which reach a window another way.
        commands: Vec<f64>,
    }

    impl CardLog {
        fn received(&self) -> impl Iterator<Item = f64> + '_ {
            self.clicks.iter().chain(&self.keys).chain(&self.commands).copied()
        }

        fn nothing_before(&self, what: &str, time: f64) {
            self.nothing_between(what, 0.0, time);
        }

        fn nothing_between(&self, what: &str, from: f64, until: f64) {
            let early: Vec<f64> = self
                .received()
                .filter(|at| *at > from && *at < until)
                .map(|at| at - until)
                .collect();
            assert!(
                early.is_empty(),
                "{what}: the card received input before it could (ms relative to the earliest allowed moment): {early:?}"
            );
        }

        /// The stream was really being delivered: without this, "nothing
        /// arrived early" could mean that nothing ever arrives.
        fn something_after(&self, what: &str, time: f64) {
            assert!(
                self.clicks.iter().any(|at| *at >= time),
                "{what}: no click reached the card afterwards: {self:?}"
            );
        }
    }

    /// One click and one Return key for `window`, handed to the application.
    fn deliver_input(mtm: MainThreadMarker, window: &NSWindow) {
        use objc2_app_kit::{NSEvent, NSEventModifierFlags, NSEventType};
        use objc2_foundation::{ns_string, NSPoint, NSString};
        let Some(content) = window.contentView() else {
            return;
        };
        let frame = content.frame();
        let at = NSPoint::new(
            frame.origin.x + frame.size.width / 2.0,
            frame.origin.y + frame.size.height / 2.0,
        );
        let number = window.windowNumber();
        let nothing: Option<&AnyObject> = None;
        let app = NSApplication::sharedApplication(mtm);
        // SAFETY: Main thread. The two class methods are AppKit's own event
        // constructors, called with the argument types their selectors declare.
        unsafe {
            let uptime: f64 = objc2::msg_send![&*objc2_foundation::NSProcessInfo::processInfo(), systemUptime];
            for kind in [NSEventType::LeftMouseDown, NSEventType::LeftMouseUp] {
                let event: Option<Retained<NSEvent>> = objc2::msg_send![
                    objc2::class!(NSEvent),
                    mouseEventWithType: kind,
                    location: at,
                    modifierFlags: NSEventModifierFlags::empty(),
                    timestamp: uptime,
                    windowNumber: number,
                    context: nothing,
                    eventNumber: 0isize,
                    clickCount: 1isize,
                    pressure: 1.0f32
                ];
                let event = event.expect("a mouse event");
                let _: () = objc2::msg_send![&*app, sendEvent: &*event];
            }
            for kind in [NSEventType::KeyDown, NSEventType::KeyUp] {
                let key: &NSString = ns_string!("\r");
                let event: Option<Retained<NSEvent>> = objc2::msg_send![
                    objc2::class!(NSEvent),
                    keyEventWithType: kind,
                    location: at,
                    modifierFlags: NSEventModifierFlags::empty(),
                    timestamp: uptime,
                    windowNumber: number,
                    context: nothing,
                    characters: key,
                    charactersIgnoringModifiers: key,
                    isARepeat: false,
                    keyCode: 36u16
                ];
                let event = event.expect("a key event");
                // A key goes to the window it is made for, not to whichever
                // window of the app has the keyboard.
                let _: () = objc2::msg_send![window, sendEvent: &*event];
            }
            // A key with Command is offered to a window as a key equivalent
            // before it is sent as an event.
            let key: &NSString = ns_string!("j");
            let event: Option<Retained<NSEvent>> = objc2::msg_send![
                objc2::class!(NSEvent),
                keyEventWithType: NSEventType::KeyDown,
                location: at,
                modifierFlags: NSEventModifierFlags::Command,
                timestamp: uptime,
                windowNumber: number,
                context: nothing,
                characters: key,
                charactersIgnoringModifiers: key,
                isARepeat: false,
                keyCode: 38u16
            ];
            let event = event.expect("a command-key event");
            let _: bool = objc2::msg_send![window, performKeyEquivalent: &*event];
        }
    }

    /// The native window of a window Tauri built.
    fn ns_window(window: &WebviewWindow) -> Retained<NSWindow> {
        // SAFETY: Tauri hands out its live NSWindow; this is the main thread.
        unsafe { Retained::retain(window.ns_window().expect("a native window").cast::<NSWindow>()) }
            .expect("a native window")
    }

    fn is_magenta((red, green, blue): (u8, u8, u8)) -> bool {
        red > 200 && blue > 200 && green < 80
    }

    fn is_green((red, green, blue): (u8, u8, u8)) -> bool {
        green > 200 && red < 80 && blue < 80
    }

    /// What the window's own layer tree draws, rendered in this process; no
    /// screen capture is involved and nothing is saved. A 9 x 9 grid over the
    /// content area. Rendering starts at the window's frame view, so the
    /// opacity and order of everything inside the window apply.
    fn window_pixels(window: &NSWindow) -> Option<Vec<(u8, u8, u8)>> {
        let content = window.contentView()?;
        // SAFETY: Main thread; AppKit objects that live for this call.
        unsafe {
            let frame_view = content.superview()?;
            let bounds = frame_view.bounds();
            let bitmap: Option<Retained<AnyObject>> =
                objc2::msg_send![&*frame_view, bitmapImageRepForCachingDisplayInRect: bounds];
            let bitmap = bitmap?;
            let _: () = objc2::msg_send![&*frame_view, cacheDisplayInRect: bounds, toBitmapImageRep: &*bitmap];
            let data: *const u8 = objc2::msg_send![&*bitmap, bitmapData];
            let (width, height, row, samples): (isize, isize, isize, isize) = (
                objc2::msg_send![&*bitmap, pixelsWide],
                objc2::msg_send![&*bitmap, pixelsHigh],
                objc2::msg_send![&*bitmap, bytesPerRow],
                objc2::msg_send![&*bitmap, samplesPerPixel],
            );
            if data.is_null() || samples < 3 || width < 40 || height < 80 {
                return None;
            }
            let (width, height, row, samples) = (width as usize, height as usize, row as usize, samples as usize);
            let bytes = std::slice::from_raw_parts(data, row * height);
            // The bitmap's first row is the top of the window; skip the title bar.
            let scale = height as f64 / bounds.size.height;
            let title_bar = bounds.size.height - content.frame().size.height;
            let top = ((title_bar + 4.0) * scale) as usize;
            let mut colours = Vec::new();
            for grid_y in 1..10 {
                for grid_x in 1..10 {
                    let x = width * grid_x / 10;
                    let y = top + (height - top) * grid_y / 10;
                    let pixel = &bytes[y * row + x * samples..y * row + x * samples + 3];
                    colours.push((pixel[0], pixel[1], pixel[2]));
                }
            }
            Some(colours)
        }
    }

    fn child_windows(mtm: MainThreadMarker) -> Vec<Retained<NSWindow>> {
        NSApplication::sharedApplication(mtm)
            .windows()
            .iter()
            .filter(|window| child_view(window).is_some())
            .collect()
    }

    /// The child webview a window holds, inside its plain content view.
    fn child_view(window: &NSWindow) -> Option<Retained<objc2_app_kit::NSView>> {
        window.contentView()?.subviews().iter().find(|view| is_child_view(view))
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
        for off in ["fullscreen", "pictureInPicture"] {
            assert_eq!(child[off], false, "{off}: {child}");
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
        assert!(child.own_preferences && !child.fullscreen, "{child:?}");
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
            if path == "/slow.css" || path == "/never.css" {
                let reports = reports.clone();
                let hold = if path == "/slow.css" { STYLE_HOLD } else { STUCK_HOLD };
                std::thread::spawn(move || {
                    std::thread::sleep(hold);
                    if hold == STYLE_HOLD {
                        let _ = reports.send(json!({ "case": "style-released" }));
                    }
                    let _ = stream.write_all(
                        format!(
                            "HTTP/1.1 200 OK\r\nContent-Type: text/css\r\nCache-Control: no-store\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                            SLOW_STYLE.len(),
                            SLOW_STYLE
                        )
                        .as_bytes(),
                    );
                });
                continue;
            }
            let response = match path {
                "/slow-window" => page(SLOW_PAGE),
                "/stuck-window" => page(STUCK_PAGE),
                "/quiet" => page(QUIET_PAGE),
                "/card" => page(CARD_PAGE),
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
