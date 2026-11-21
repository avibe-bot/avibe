//! Hermetic probe of the desktop app's Show sharing with real WebKit.
//!
//! A loopback server plays a Show window's document: the Show, a forged frame
//! claiming to be it, and another Show's hidden frame, as a same-origin Show
//! script could arrange (review of 9904474a, P1). The probe runs the shipping
//! share module (compiled in from its source file) against it:
//! - the isolated-world read gives the bound Show's link whatever the DOM claims;
//! - the export is the whole web view, pixel for pixel, not a frame the page names;
//! - PNG and PDF encoding;
//! - asking for the system share menu.
//!
//! Nothing is shared.
//!
//! By default the app is never activated, so the probe does not take the
//! keyboard on a desktop in use. Its window sits off screen, where WebKit
//! reports the page hidden and macOS shows no menu.
//! `AVIBE_SHOW_SHARE_PROBE_FOCUS=1` puts the window on screen, checks that the
//! page is visible, and opens and closes the real share menu. Run it only on a
//! desktop nobody is using.
//!
//! Output is one JSON line per check; the run fails on the first one that
//! does not hold.
//!
//!     cargo run --example show_share_probe

#[cfg(not(target_os = "macos"))]
fn main() {}

#[cfg(target_os = "macos")]
fn main() {
    probe::run();
}

#[cfg(target_os = "macos")]
#[path = "../src/macos_show_share.rs"]
// The probe shares a link only; the shell also shares files.
#[allow(dead_code)]
mod macos_show_share;

#[cfg(target_os = "macos")]
#[path = "../src/show_window_title.rs"]
mod show_window_title;

#[cfg(target_os = "macos")]
#[path = "../src/macos_show_titlebar.rs"]
mod macos_show_titlebar;

#[cfg(target_os = "macos")]
mod probe {
    use std::{
        io::{Read, Write},
        net::TcpListener,
        sync::mpsc,
        time::Duration,
    };

    use objc2::{rc::Retained, MainThreadMarker};
    use objc2_web_kit::WKWebView;
    use serde_json::{json, Value};
    use tauri::{WebviewUrl, WebviewWindowBuilder};

    use crate::macos_show_share::{self as native, Item, Outcome};

    const SESSION: &str = "ses-probe";

    thread_local! {
        /// Main thread only: a closed window, watched without keeping it.
        #[allow(clippy::type_complexity)]
        static WATCHED: std::cell::RefCell<Option<(objc2::rc::Weak<objc2_app_kit::NSWindow>, Vec<objc2::rc::Weak<objc2_app_kit::NSView>>)>> = const { std::cell::RefCell::new(None) };
        /// Main thread only: the open share menu.
        static PICKER: std::cell::RefCell<Option<native::Picker>> = const { std::cell::RefCell::new(None) };
    }
    const WORKBENCH: &str = r#"<!doctype html><meta charset="utf-8"><title>workbench</title>
<style>html,body{margin:0;height:100%;background:#fff}iframe{position:absolute;border:0}</style>
<iframe id="show" data-avibe-show-session="ses-probe" src="/show/ses-probe/" style="left:40px;top:60px;width:400px;height:300px"></iframe>
<iframe id="other" data-avibe-show-session="ses-other" src="/show/ses-other/" style="left:0;top:0;width:1px;height:1px;opacity:0"></iframe>
<script>
addEventListener('avibe:show-annotate-open', () => fetch('/report', { method: 'POST', body: '{"case":"annotate-open"}' }));
addEventListener('avibe:show-share-open', () => fetch('/report', { method: 'POST', body: '{"case":"share-open"}' }));
document.getElementById('show').addEventListener('load', () => {
  // A same-origin Show script forges a frame that claims to be this Show.
  const forged = document.createElement('iframe');
  forged.dataset.avibeShowSession = 'ses-probe';
  forged.src = '/settings';
  forged.style.cssText = 'left:460px;top:60px;width:300px;height:300px';
  forged.addEventListener('load', () => {
    forged.contentWindow.history.replaceState(null, '', '/show/ses-probe/');
    fetch('/report', { method: 'POST', body: '{"case":"ready"}' });
  });
  document.body.appendChild(forged);
});
</script>"#;
    const SHOW: &str = r#"<!doctype html><meta charset="utf-8"><title>漏斗</title>
<body style="margin:0;background:#00a050;font:24px sans-serif;color:#fff"><p style="margin:60px">Q4 漏斗看板</p></body>"#;

    fn check(name: &str, ok: bool, detail: Value) {
        println!("{}", json!({ "check": name, "ok": ok, "detail": detail }));
        if !ok {
            std::process::exit(1);
        }
    }

    pub fn run() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let (ready_tx, ready_rx) = mpsc::channel::<String>();
        std::thread::spawn(move || serve(listener, ready_tx));
        let origin = format!("http://127.0.0.1:{port}");

        let mut app = tauri::Builder::default().build(tauri::generate_context!()).unwrap();
        let focus = std::env::var_os("AVIBE_SHOW_SHARE_PROBE_FOCUS").is_some();
        if !focus {
            app.set_activation_policy(tauri::ActivationPolicy::Prohibited);
        }
        let mut ready_rx = Some(ready_rx);
        app.run(move |app, event| {
            if let tauri::RunEvent::ExitRequested { api, code: None, .. } = &event {
                api.prevent_exit();
            }
            let tauri::RunEvent::Ready = event else { return };
            let window = WebviewWindowBuilder::new(app, "probe", WebviewUrl::External("about:blank".parse().unwrap()))
                .inner_size(800.0, 600.0)
                .position(if focus { 120.0 } else { -4000.0 }, if focus { 120.0 } else { -4000.0 })
                .visible(true)
                .focused(false)
                .incognito(true)
                .build()
                .unwrap();

            let ready = ready_rx.take().unwrap();
            let origin = origin.clone();
            let handle = app.clone();
            std::thread::spawn(move || {
                let (action_tx, action_rx) = mpsc::channel();
                let destination: tauri::Url = format!("{origin}/workbench").parse().unwrap();
                crate::macos_show_titlebar::install(
                    &window,
                    ["Annotate", "Open Chat", "Share", "Open in Browser"].map(str::to_owned),
                    destination.clone(),
                    move |action| {
                        let _ = action_tx.send(action);
                    },
                );
                // Match production: install while blank, then navigate after setup.
                let (initial_url, installed) = on_main(&window, |webview, _, tx| {
                    let _ = tx.send((
                        native::document_url(webview),
                        title_bar::controls(&webview.window().unwrap()),
                    ));
                });
                check(
                    "controls-installed-before-runtime-navigation",
                    initial_url.as_deref().is_none_or(|url| url == "about:blank") && installed,
                    json!({ "url": initial_url, "installed": installed }),
                );
                window.navigate(destination).unwrap();
                assert_eq!(
                    ready.recv_timeout(Duration::from_secs(20)).expect("workbench loaded"),
                    "ready"
                );
                std::thread::sleep(Duration::from_millis(500));
                steps(&window, &origin, focus);
                title_bar(&window, &ready, action_rx);
                released(&handle, &origin);
                named(&handle, &origin);
                println!("{}", json!({ "done": true }));
                handle.exit(0);
            });
        });
    }

    /// Runs `body` on the main thread with the probe's WKWebView and waits for
    /// what it sends back.
    fn on_main<T: Send + 'static>(
        window: &tauri::WebviewWindow,
        body: impl FnOnce(&WKWebView, MainThreadMarker, mpsc::Sender<T>) + Send + 'static,
    ) -> T {
        let (tx, rx) = mpsc::channel();
        window
            .with_webview(move |platform| {
                let mtm = MainThreadMarker::new().unwrap();
                let webview = unsafe { Retained::retain(platform.inner().cast::<WKWebView>()) }.unwrap();
                body(&webview, mtm, tx);
            })
            .unwrap();
        rx.recv_timeout(Duration::from_secs(20)).expect("main thread answered")
    }

    fn script(window: &tauri::WebviewWindow, body: &'static str, args: Vec<(&'static str, String)>) -> Value {
        on_main(window, move |webview, mtm, tx| {
            let wrapped = format!("const run = async () => {{\n{body}\n}};\nreturn JSON.stringify(await run());");
            let pairs: Vec<(&str, &str)> = args.iter().map(|(k, v)| (*k, v.as_str())).collect();
            native::call_script(webview, mtm, &wrapped, &pairs, move |answer| {
                let _ = tx.send(
                    answer
                        .and_then(|text| serde_json::from_str(&text).ok())
                        .unwrap_or(Value::Null),
                );
            });
        })
    }

    fn read(window: &tauri::WebviewWindow, origin: &str, session: &str) -> Value {
        script(
            window,
            native::READ,
            vec![("sessionID", session.into()), ("origin", origin.into())],
        )
    }

    fn steps(window: &tauri::WebviewWindow, origin: &str, focus: bool) {
        // The link is the bound Show's, from its own read APIs; the forged
        // frame and the other Show's hidden frame do not enter into it.
        let state = read(window, origin, SESSION);
        check(
            "link-is-the-bound-show-s-own",
            state["allowed"] == true && state["url"] == format!("{origin}/show/{SESSION}/"),
            state.clone(),
        );
        // Off screen, WebKit may report the page hidden or not; the shell only
        // exports a page that says it is visible.
        check(
            "page-visibility-is-reported",
            state["visible"].is_boolean(),
            state["visible"].clone(),
        );
        let foreign = script(
            window,
            native::READ,
            vec![("sessionID", SESSION.into()), ("origin", "http://127.0.0.1:1".into())],
        );
        check("read-refuses-another-origin", foreign["allowed"] == false, foreign);
        // A limited Show whose public link is unavailable (Avibe Cloud not
        // connected) is still the user's to export; it only has no link.
        let limited = read(window, origin, "ses-limited");
        check(
            "a-show-without-a-link-may-still-be-exported",
            limited["allowed"] == true && limited["url"] == "",
            limited,
        );
        let unknown = read(window, origin, "ses-missing");
        check(
            "read-refuses-a-show-it-cannot-read",
            unknown["allowed"] != true,
            unknown,
        );

        // The export is the whole web view, so the page's own white and the
        // Show's green are both in it, and so is the forged frame: what the
        // window shows is what is exported, nothing chosen by a frame's claim.
        let (png, pdf, size, view) = on_main(window, move |webview, mtm, tx| {
            let (bounds, _, _) = native::geometry(webview);
            assert!(native::within_pixel_cap(webview));
            native::snapshot(webview, mtm, move |image| {
                let image = image.expect("snapshot");
                let size = image.size();
                let _ = tx.send((
                    native::png(&image),
                    native::pdf(&image, mtm),
                    (size.width, size.height),
                    (bounds.size.width, bounds.size.height),
                ));
            });
        });
        let png = png.expect("png");
        check(
            "png-encodes-the-capture",
            png.starts_with(b"\x89PNG\r\n\x1a\n") && png.len() > 100,
            json!({ "bytes": png.len() }),
        );
        check(
            "capture-is-the-whole-web-view",
            size.0 == view.0 && (view.1 - size.1).abs() <= 40.0,
            json!({ "capture": [size.0, size.1], "view": [view.0, view.1] }),
        );
        let pixels = png.clone();
        let samples = on_main(window, move |_, _, tx| {
            let data = objc2_foundation::NSData::with_bytes(&pixels);
            let bitmap = objc2_app_kit::NSBitmapImageRep::imageRepWithData(&data).expect("decode");
            let (width, height) = (bitmap.pixelsWide(), bitmap.pixelsHigh());
            let (row, step) = (bitmap.bytesPerRow(), bitmap.bitsPerPixel() / 8);
            let base = bitmap.bitmapData();
            let scale = width as f64 / size.0;
            let at = |x: f64, y: f64| unsafe {
                let p = base.offset((y * scale) as isize * row + (x * scale) as isize * step);
                [*p, *p.offset(1), *p.offset(2)]
            };
            // The page's white corner, and the Show's green inside its frame.
            let _ = tx.send((at(10.0, 10.0), at(200.0, 300.0), width, height));
        });
        check(
            "capture-holds-the-page-and-the-show",
            samples.0 == [255, 255, 255] && samples.1[0] < 40 && samples.1[1] > 120,
            json!({ "page": samples.0, "show": samples.1, "pixels": [samples.2, samples.3] }),
        );
        let pdf = pdf.expect("pdf");
        check(
            "pdf-encodes-the-capture",
            pdf.starts_with(b"%PDF"),
            json!({ "bytes": pdf.len() }),
        );

        // The share menu is requested; on screen, closing it is a cancellation.
        let (outcome_tx, outcome_rx) = mpsc::channel::<Outcome>();
        let link_url = state["url"].as_str().unwrap().to_owned();
        let shown = on_main(window, move |webview, mtm, tx| {
            let (bounds, _, _) = native::geometry(webview);
            let anchor = objc2_foundation::NSRect::new(
                objc2_foundation::NSPoint::new(bounds.size.width - 24.0, 0.0),
                objc2_foundation::NSSize::new(1.0, 1.0),
            );
            let picker = native::present(
                webview,
                mtm,
                Item::Link(&link_url),
                anchor,
                Box::new(move |outcome| {
                    let _ = outcome_tx.send(outcome);
                }),
            );
            let shown = picker.is_some();
            PICKER.with_borrow_mut(|slot| *slot = picker);
            let _ = tx.send(shown);
        });
        check("share-menu-is-requested", shown, json!(shown));
        std::thread::sleep(Duration::from_secs(1));
        on_main(window, |_, _, tx| {
            if let Some(picker) = PICKER.with_borrow_mut(Option::take) {
                picker.close();
            }
            let _ = tx.send(());
        });
        let outcome = outcome_rx.recv_timeout(Duration::from_secs(10)).ok();
        if focus {
            check(
                "closing-the-menu-is-a-cancellation",
                outcome == Some(Outcome::Cancelled),
                json!(format!("{outcome:?}")),
            );
        } else {
            println!(
                "{}",
                json!({ "check": "closing-the-menu-is-a-cancellation", "skipped": "needs AVIBE_SHOW_SHARE_PROBE_FOCUS=1", "detail": format!("{outcome:?}") })
            );
        }
    }

    /// The shipping title-bar button: installed on the window, pressed as a
    /// click would, and the page hears the open event, but only on the route the
    /// window was opened at (review of 45533723): `pushState` away silences it,
    /// and coming back restores it.
    fn title_bar(
        window: &tauri::WebviewWindow,
        reports: &mpsc::Receiver<String>,
        action_rx: mpsc::Receiver<crate::macos_show_titlebar::Action>,
    ) {
        let geometry = on_main(window, |webview, _, tx| {
            let window = webview.window().expect("window");
            let _ = tx.send(title_bar::controls(&window));
        });
        check(
            "four-controls-keep-the-approved-order-and-hit-targets",
            geometry,
            json!(geometry),
        );
        for (index, expected) in [
            (1, crate::macos_show_titlebar::Action::Chat),
            (3, crate::macos_show_titlebar::Action::Browser),
        ] {
            on_main(window, move |webview, _, tx| {
                let _ = tx.send(title_bar::press_index(&webview.window().unwrap(), index));
            });
            check(
                "native-control-dispatches-its-own-action",
                action_rx.recv_timeout(Duration::from_secs(2)).ok() == Some(expected),
                json!(format!("{expected:?}")),
            );
        }
        on_main(window, |webview, _, tx| {
            let _ = tx.send(title_bar::press_index(&webview.window().unwrap(), 0));
        });
        check(
            "annotation-press-reaches-its-document",
            reports.recv_timeout(Duration::from_secs(5)).ok().as_deref() == Some("annotate-open"),
            json!(true),
        );
        let press = || {
            on_main(window, |webview, _, tx| {
                let window = webview.window().expect("window");
                let _ = tx.send(title_bar::press(&window));
            })
        };
        let heard = |wait: u64| reports.recv_timeout(Duration::from_millis(wait)).ok();
        check("title-bar-button-is-installed-and-pressed", press(), json!(true));
        let on_route = heard(5000);
        check(
            "the-page-hears-the-open-event",
            on_route.as_deref() == Some("share-open"),
            json!(on_route),
        );

        script(
            window,
            "history.pushState(null, '', '/chat/elsewhere'); return true;",
            vec![],
        );
        press();
        let away = heard(1000);
        check("off-route-the-press-is-not-heard", away.is_none(), json!(away));
        let native = on_main(window, |webview, _, tx| {
            let _ = tx.send(native::document_url(webview));
        });
        check(
            "the-native-url-follows-pushstate",
            native.as_deref().is_some_and(|url| url.ends_with("/chat/elsewhere")),
            json!(native),
        );

        script(
            window,
            "history.pushState(null, '', '/workbench'); return true;",
            vec![],
        );
        press();
        let back = heard(5000);
        check(
            "back-on-route-it-is-heard-again",
            back.as_deref() == Some("share-open"),
            json!(back),
        );
        title(window);
        if let Some(directory) = std::env::var_os("AVIBE_SHOW_CONTROLS_SHOTS") {
            let directory = std::path::PathBuf::from(directory);
            std::fs::create_dir_all(&directory).unwrap();
            window.set_title("Q4 漏斗看板").unwrap();
            window.set_size(tauri::LogicalSize::new(1040.0, 720.0)).unwrap();
            std::thread::sleep(Duration::from_millis(300));
            on_main(window, move |webview, _, tx| {
                title_bar::capture(&webview.window().unwrap(), &directory);
                let _ = tx.send(());
            });
        }
    }

    /// The drawn title (macOS 26 and later): whole while it fits, cut off
    /// before the share button when it does not, hidden when nothing useful
    /// fits, and always the window's own title.
    fn title(window: &tauri::WebviewWindow) {
        let os = native::os_version();
        if os.0 < 26 {
            check("title-is-the-system-s-before-macos-26", true, json!(os));
            return;
        }
        let state = |title: Option<&'static str>, width: Option<f64>| {
            if let Some(title) = title {
                window.set_title(title).unwrap();
            }
            if let Some(width) = width {
                window.set_size(tauri::LogicalSize::new(width, 600.0)).unwrap();
            }
            std::thread::sleep(Duration::from_millis(300));
            on_main(window, |webview, _, tx| {
                let window = webview.window().expect("window");
                let _ = tx.send((window.title().to_string(), title_bar::state(&window)));
            })
        };
        let gap = |end: f64, start: f64| start - end;

        let (title, drawn) = state(Some("Q4 漏斗看板"), Some(1040.0));
        let (text, shown, end, start, natural, passes) = drawn.expect("a drawn title");
        check(
            "a-short-title-is-drawn-whole",
            text == title && shown && end - 82.0 >= natural,
            json!({ "text": text, "shown": shown, "room": end - 82.0, "natural": natural }),
        );
        check("a-click-on-the-title-reaches-the-title-bar", passes, json!(passes));

        const LONG: &str =
            "2026 年第四季度全渠道销售漏斗与转化归因分析看板（含区域拆分、渠道对比、周环比明细与下季度目标拆解）";
        let (title, drawn) = state(Some(LONG), Some(720.0));
        let (text, shown, end, start2, natural, _) = drawn.expect("a drawn title");
        check(
            "a-long-title-is-cut-off-before-the-button",
            text == title && title == LONG && shown && natural > end - 82.0 && gap(end, start2) >= 8.0,
            json!({ "shown": shown, "room": end - 82.0, "natural": natural, "gap": gap(end, start2), "button": start2, "wide_button": start }),
        );

        let (title, drawn) = state(None, Some(160.0));
        let (text, shown, ..) = drawn.expect("a drawn title");
        check(
            "too-narrow-for-any-title-shows-none",
            text == title && !shown,
            json!({ "shown": shown }),
        );
        let (_, drawn) = state(None, Some(800.0));
        check(
            "wide-again-the-title-is-back",
            drawn.is_some_and(|(_, shown, ..)| shown),
            json!(null),
        );
    }

    /// What a closed window leaves behind, after `destroy`, as the shell
    /// retires a Show window: whether the window is still referenced, by how
    /// many, and how many of the title-bar views are still alive.
    fn closed(app: &tauri::AppHandle, origin: &str, label: &str, titled: bool) -> (Option<usize>, usize, usize) {
        let window = WebviewWindowBuilder::new(
            app,
            label,
            WebviewUrl::External(format!("{origin}/workbench").parse().unwrap()),
        )
        .inner_size(720.0, 480.0)
        .position(-4000.0, -4000.0)
        .visible(true)
        .focused(false)
        .incognito(true)
        .build()
        .unwrap();
        if titled {
            crate::macos_show_titlebar::install(
                &window,
                ["Annotate", "Open Chat", "Share", "Open in Browser"].map(str::to_owned),
                format!("{origin}/workbench").parse().unwrap(),
                |_| {},
            );
        }
        std::thread::sleep(Duration::from_millis(800));
        let views = on_main(&window, |webview, _, tx| {
            objc2::rc::autoreleasepool(|_| {
                let window = webview.window().expect("window");
                let views: Vec<_> = title_bar::installed(&window)
                    .iter()
                    .map(objc2::rc::Weak::from_retained)
                    .collect();
                let count = views.len();
                WATCHED.with(|watched| *watched.borrow_mut() = Some((objc2::rc::Weak::from_retained(&window), views)));
                let _ = tx.send(count);
            });
        });
        window.destroy().unwrap();
        drop(window);
        std::thread::sleep(Duration::from_millis(1500));
        let (tx, rx) = mpsc::channel();
        app.run_on_main_thread(move || {
            objc2::rc::autoreleasepool(|_| {
                let left = WATCHED.with(|watched| {
                    let (window, views) = watched.borrow_mut().take().expect("watched");
                    // One reference is this load's own.
                    let references = window.load().map(|window| {
                        let count: usize = unsafe { objc2::msg_send![&*window, retainCount] };
                        count - 1
                    });
                    (references, views.iter().filter(|view| view.load().is_some()).count())
                });
                let _ = tx.send(left);
            });
        })
        .unwrap();
        let (references, alive) = rx.recv_timeout(Duration::from_secs(10)).expect("main thread answered");
        (references, views, alive)
    }

    /// A Show window closed through Tauri, as the shell retires it, keeps no
    /// title-bar object and no extra reference to the window: it is left
    /// exactly as a window without them is.
    fn released(app: &tauri::AppHandle, origin: &str) {
        let (plain, _, _) = closed(app, origin, "closing-plain", false);
        let (titled, installed, alive) = closed(app, origin, "closing-titled", true);
        check(
            "a-closed-show-window-keeps-nothing-of-its-title-bar",
            installed >= 1 && alive == 0 && titled == plain,
            json!({ "installed": installed, "alive": alive, "window_references": titled, "plain_window_references": plain }),
        );
    }

    /// A Show window opened by an entry that names nothing (a deep link, the
    /// `/apps/show` route) is named by its page, as the page names it: a long
    /// Show title is cut off before the share button, a rename follows, and a
    /// blank document title changes nothing.
    fn named(app: &tauri::AppHandle, origin: &str) {
        const LONG: &str =
            "2026 年第四季度全渠道销售漏斗与转化归因分析看板（含区域拆分、渠道对比、周环比明细与下季度目标拆解）";
        const RENAMED: &str = "Q4 漏斗看板（终版）";
        let window = crate::show_window_title::follow(
            WebviewWindowBuilder::new(
                app,
                "named-show",
                WebviewUrl::External(format!("{origin}/workbench").parse().unwrap()),
            )
            .title("Avibe"),
        )
        .inner_size(720.0, 480.0)
        .position(-4000.0, -4000.0)
        .visible(true)
        .focused(false)
        .incognito(true)
        .build()
        .unwrap();
        crate::macos_show_titlebar::install(
            &window,
            ["Annotate", "Open Chat", "Share", "Open in Browser"].map(str::to_owned),
            format!("{origin}/workbench").parse().unwrap(),
            |_| {},
        );
        // Runs `body` in the page, then reads the window's title and drawn title.
        let titled = |body: &'static str| {
            script(&window, body, vec![]);
            std::thread::sleep(Duration::from_millis(500));
            on_main(&window, |webview, _, tx| {
                let window = webview.window().expect("window");
                let _ = tx.send((window.title().to_string(), title_bar::state(&window)));
            })
        };
        std::thread::sleep(Duration::from_millis(1000));
        let (title, _) = titled("return true;");
        check(
            "a-window-that-was-named-nothing-takes-its-page-s-title",
            title == "workbench",
            json!(title),
        );

        let (title, drawn) = titled("document.title = '2026 年第四季度全渠道销售漏斗与转化归因分析看板（含区域拆分、渠道对比、周环比明细与下季度目标拆解）'; return true;");
        let os26 = native::os_version().0 >= 26;
        let cut = drawn.as_ref().is_some_and(|(text, shown, end, start, natural, _)| {
            text == LONG && *shown && *natural > end - 82.0 && start - end >= 8.0
        });
        check(
            "the-show-s-long-title-is-cut-off-before-the-button",
            title == LONG && (cut || !os26),
            json!({ "title_matches": title == LONG, "drawn": drawn.map(|(_, shown, end, start, natural, _)| json!({ "shown": shown, "room": end - 82.0, "natural": natural, "gap": start - end })) }),
        );

        let (title, drawn) = titled("document.title = 'Q4 漏斗看板（终版）'; return true;");
        check(
            "a-rename-reaches-the-window",
            title == RENAMED
                && (!os26
                    || drawn.is_some_and(|(text, shown, end, _, natural, _)| {
                        text == RENAMED && shown && end - 82.0 >= natural
                    })),
            json!(title),
        );

        let (title, _) = titled("document.title = ''; return true;");
        check("a-blank-document-title-changes-nothing", title == RENAMED, json!(title));
        window.destroy().unwrap();
    }

    /// Reads and drives what `macos_show_titlebar` put in a window, as AppKit
    /// shows it: by public AppKit calls and the classes it registers, so the
    /// shipping module carries nothing for the probe.
    mod title_bar {
        use objc2::{rc::Retained, runtime::AnyObject};
        use objc2_app_kit::{NSButton, NSTextField, NSView, NSWindow, NSWindowButton};

        const HOST: &str = "AvibeShowShareTitleBarHost";
        const TITLE: &str = "AvibeShowShareTitle";

        fn is(view: &NSView, class: &str) -> bool {
            let object: &AnyObject = view;
            object.class().name().to_str() == Ok(class)
        }

        fn bar(window: &NSWindow) -> Option<Retained<NSView>> {
            let zoom = window.standardWindowButton(NSWindowButton::ZoomButton)?;
            // SAFETY: Reading the button's own superview.
            unsafe { zoom.superview() }
        }

        fn host(window: &NSWindow) -> Option<Retained<NSView>> {
            window
                .titlebarAccessoryViewControllers()
                .iter()
                .map(|controller| controller.view())
                .find(|view| is(view, HOST))
        }

        fn button(host: &NSView) -> Option<Retained<NSButton>> {
            host.subviews().iter().find_map(|view| view.downcast::<NSButton>().ok())
        }

        fn title(bar: &NSView) -> Option<Retained<NSView>> {
            bar.subviews().into_iter().find(|view| is(view, TITLE))
        }

        /// Presses the share button, as a click would.
        pub fn press(window: &NSWindow) -> bool {
            press_index(window, 2)
        }

        pub fn press_index(window: &NSWindow, index: isize) -> bool {
            let Some(button) = host(window).and_then(|host| {
                host.subviews()
                    .iter()
                    .filter_map(|view| view.downcast::<NSButton>().ok())
                    .find(|button| button.tag() == index)
            }) else {
                return false;
            };
            // SAFETY: A plain button click, on the main thread.
            unsafe { button.performClick(None) };
            true
        }

        pub fn controls(window: &NSWindow) -> bool {
            let Some(host) = host(window) else {
                return false;
            };
            let buttons: Vec<_> = host
                .subviews()
                .iter()
                .filter_map(|view| view.downcast::<NSButton>().ok())
                .collect();
            buttons.len() == 4
                && buttons.iter().enumerate().all(|(index, button)| {
                    let frame = button.frame();
                    button.tag() == index as isize
                        && frame.size.width == 28.0
                        && frame.size.height == 28.0
                        && frame.origin.x == 4.0 + index as f64 * 32.0
                })
                && host.frame().size.width == 137.0
        }

        /// Capture this offscreen window's real system title-bar appearance, without pretending a
        /// per-window appearance override also changes AppKit's separate accessory backdrop.
        pub fn capture(window: &NSWindow, directory: &std::path::Path) {
            use objc2::msg_send;
            use objc2_app_kit::NSBitmapImageFileType;
            use objc2_foundation::NSDictionary;
            let bar = bar(window).unwrap();
            let host = host(window).unwrap();
            let buttons: Vec<_> = host
                .subviews()
                .iter()
                .filter_map(|view| view.downcast::<NSButton>().ok())
                .collect();
            for hover in [false, true] {
                // SAFETY: Shipping subclass accepts an optional event; only its hover state is changed.
                unsafe {
                    let _: () = if hover {
                        msg_send![&buttons[1], mouseEntered: std::ptr::null::<AnyObject>()]
                    } else {
                        msg_send![&buttons[1], mouseExited: std::ptr::null::<AnyObject>()]
                    };
                }
                bar.display();
                let rect = bar.bounds();
                let bitmap = bar.bitmapImageRepForCachingDisplayInRect(rect).unwrap();
                bar.cacheDisplayInRect_toBitmapImageRep(rect, &bitmap);
                // SAFETY: PNG takes an empty property dictionary.
                let data = unsafe {
                    bitmap.representationUsingType_properties(NSBitmapImageFileType::PNG, &NSDictionary::new())
                }
                .unwrap();
                std::fs::write(
                    directory.join(format!("titlebar-{}.png", if hover { "hover" } else { "idle" })),
                    data.to_vec(),
                )
                .unwrap();
            }
        }

        /// The drawn title's text, whether it shows, how far it reaches, where the
        /// button starts, its natural width (title-bar points), and whether a click
        /// on it reaches the title bar.
        pub fn state(window: &NSWindow) -> Option<(String, bool, f64, f64, f64, bool)> {
            let bar = bar(window)?;
            let title = title(&bar)?;
            let label = title
                .subviews()
                .iter()
                .find_map(|view| view.downcast::<NSTextField>().ok())?;
            let end = title.frame().origin.x + label.frame().size.width;
            let host = host(window)?;
            let button = button(&host)?;
            let start = bar.convertPoint_fromView(button.frame().origin, Some(&host)).x;
            let middle = objc2_foundation::NSPoint::new(
                title.frame().origin.x + 10.0,
                bar.frame().origin.y + bar.frame().size.height / 2.0,
            );
            let passes = bar
                .hitTest(middle)
                .is_some_and(|view| !is(&view, TITLE) && view.downcast_ref::<NSTextField>().is_none());
            Some((
                label.stringValue().to_string(),
                !label.isHidden(),
                end,
                start,
                label.intrinsicContentSize().width,
                passes,
            ))
        }

        /// The views it added and that are still in the window.
        pub fn installed(window: &NSWindow) -> Vec<Retained<NSView>> {
            host(window)
                .into_iter()
                .chain(bar(window).and_then(|bar| title(&bar)))
                .collect()
        }
    }

    fn serve(listener: TcpListener, ready: mpsc::Sender<String>) {
        for stream in listener.incoming() {
            let Ok(mut stream) = stream else { continue };
            let ready = ready.clone();
            std::thread::spawn(move || {
                let _ = stream.set_read_timeout(Some(Duration::from_secs(5)));
                // The whole request: headers, then as much body as they announce.
                let mut request = Vec::new();
                loop {
                    let mut chunk = [0u8; 8192];
                    let count = stream.read(&mut chunk).unwrap_or(0);
                    if count == 0 {
                        break;
                    }
                    request.extend_from_slice(&chunk[..count]);
                    if let Some(split) = request.windows(4).position(|bytes| bytes == b"\r\n\r\n") {
                        let headers = String::from_utf8_lossy(&request[..split]).to_lowercase();
                        let length = headers
                            .lines()
                            .find_map(|line| line.strip_prefix("content-length:"))
                            .map_or(0, |length| length.trim().parse::<usize>().unwrap_or(0));
                        if request.len() >= split + 4 + length {
                            break;
                        }
                    }
                }
                let text = String::from_utf8_lossy(&request).into_owned();
                let path = text
                    .lines()
                    .next()
                    .and_then(|line| line.split_whitespace().nth(1))
                    .unwrap_or("/")
                    .to_owned();
                let respond = |stream: &mut std::net::TcpStream, kind: &str, body: &str| {
                    let _ = stream.write_all(
                        format!(
                            "HTTP/1.1 200 OK\r\nContent-Type: {kind}\r\nCache-Control: no-store\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                            body.len()
                        )
                        .as_bytes(),
                    );
                };
                match path.as_str() {
                    "/workbench" => respond(&mut stream, "text/html; charset=utf-8", WORKBENCH),
                    "/show/ses-probe/" => respond(&mut stream, "text/html; charset=utf-8", SHOW),
                    "/show/ses-other/" | "/settings" => respond(
                        &mut stream,
                        "text/html; charset=utf-8",
                        "<!doctype html><body style=\"margin:0;background:#3050ff\">elsewhere</body>",
                    ),
                    "/api/show-pages/ses-limited/access" => {
                        respond(&mut stream, "application/json", r#"{"can_use":true}"#)
                    }
                    "/api/show-pages/ses-limited" => respond(
                        &mut stream,
                        "application/json",
                        r#"{"session_id":"ses-limited","visibility":"limited","public_url":null,"offline":false}"#,
                    ),
                    "/api/show-pages/ses-probe/access" => {
                        respond(&mut stream, "application/json", r#"{"can_use":true}"#)
                    }
                    "/api/show-pages/ses-probe" => respond(
                        &mut stream,
                        "application/json",
                        r#"{"session_id":"ses-probe","visibility":"private","active_url":"/show/ses-probe/","offline":false}"#,
                    ),
                    "/report" => {
                        respond(&mut stream, "text/plain", "ok");
                        let body = text.split_once("\r\n\r\n").map_or("", |(_, body)| body);
                        let case = serde_json::from_str::<Value>(body)
                            .ok()
                            .and_then(|v| v["case"].as_str().map(str::to_owned));
                        let _ = ready.send(case.unwrap_or_default());
                    }
                    _ => {
                        let _ = stream
                            .write_all(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n");
                    }
                }
            });
        }
    }
}
