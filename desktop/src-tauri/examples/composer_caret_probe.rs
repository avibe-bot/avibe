//! Native WebView probe for the chat composer's caret, for CI only. It
//! launches a regular app, which macOS activates and which takes focus, clicks
//! with synthesized events and captures its own window: do not run it on a
//! live desktop. Its data is hermetic: it does not start Avibe, and
//! `tests/run_cookie_probe.py` supplies an empty HOME and a test root.
//!
//! WebKit can keep an empty input's caret (focus and selection are right) and
//! not draw it, which only the screen shows: the composer below a conversation
//! that scrolls showed no caret after a click until a key was typed, in the
//! Desktop app and in a plain WKWebView alike, while Playwright's WebKit drew
//! it. So this probe reads the window's pixels: the darkest pixel at the start
//! of the empty line, before the click and over a little more than a blink
//! after it, must go from the placeholder's grey to the caret's near black.
//!
//! The page is the composer fixture with a scrolled transcript above it
//! (`ui/e2e/voice-shortcut/fixture.html?transcript=long`), served by the UI's
//! dev server at `AVIBE_CARET_PROBE_URL`.
#[cfg(not(target_os = "macos"))]
fn main() {}

#[cfg(target_os = "macos")]
fn main() {
    probe::main();
}

#[cfg(target_os = "macos")]
mod probe {
    use std::{process::Command, sync::mpsc, time::Duration};

    use objc2::{msg_send, rc::Retained, runtime::AnyObject, ClassType};
    use objc2_app_kit::{
        NSApplication, NSBitmapImageRep, NSEvent, NSEventModifierFlags, NSEventType, NSView, NSWindow,
    };
    use objc2_foundation::{MainThreadMarker, NSData, NSPoint, NSProcessInfo};
    use tauri::{AppHandle, WebviewUrl, WebviewWindow, WebviewWindowBuilder};

    const LABEL: &str = "caret";

    /// The darkest the caret's spot may stay, below its darkest before the click.
    const CARET_CONTRAST: f64 = 40.0;

    /// The input, and the empty line in it where the caret goes, in CSS pixels.
    const GEOMETRY: &str = r#"(() => {
      if (!document.querySelector('[data-hold-to-talk="ready"]') || !document.querySelector('[data-transcript]')) return null;
      const input = document.querySelector('[contenteditable="true"]');
      const line = input && input.querySelector('p');
      if (!line) return null;
      const a = input.getBoundingClientRect(), b = line.getBoundingClientRect();
      return JSON.stringify({ x: a.x, y: a.y, w: a.width, h: a.height, lineX: b.x, lineY: b.y, lineH: b.height });
    })()"#;

    /// Whether the input has the focus and a collapsed selection: the caret's state.
    const CARET_STATE: &str = r#"(() => {
      const input = document.querySelector('[contenteditable="true"]');
      const selection = getSelection();
      return document.activeElement === input && selection.rangeCount === 1 && selection.isCollapsed
        && input.contains(selection.anchorNode);
    })()"#;

    /// What has the focus and the selection, for a failure's message.
    const FOCUS: &str = r#"(() => {
      const active = document.activeElement, selection = getSelection();
      return `hit=${document.elementsFromPoint(__X__, __Y__).slice(0, 3).map((e) => e.tagName + '.' + String(e.className).slice(0, 40)).join(' > ')} active=${active && active.tagName} editable=${active && active.isContentEditable} ranges=${selection.rangeCount} anchor=${selection.anchorNode && selection.anchorNode.nodeName} focused=${document.hasFocus()}`;
    })()"#;

    #[derive(serde::Deserialize)]
    struct Geometry {
        x: f64,
        y: f64,
        w: f64,
        h: f64,
        #[serde(rename = "lineX")]
        line_x: f64,
        #[serde(rename = "lineY")]
        line_y: f64,
        #[serde(rename = "lineH")]
        line_h: f64,
    }

    fn on_main<T: Send + 'static>(app: &AppHandle, f: impl FnOnce() -> T + Send + 'static) -> T {
        let (sender, receiver) = mpsc::channel();
        app.run_on_main_thread(move || {
            let _ = sender.send(f());
        })
        .unwrap();
        receiver.recv_timeout(Duration::from_secs(10)).unwrap()
    }

    /// The script's result, as the JSON text WebKit returned for it.
    fn eval(window: &WebviewWindow, script: &str) -> String {
        let (sender, receiver) = mpsc::channel();
        window
            .eval_with_callback(script, move |raw| {
                let _ = sender.send(raw);
            })
            .unwrap();
        receiver.recv_timeout(Duration::from_secs(10)).unwrap_or_default()
    }

    fn ns_window(window: &WebviewWindow) -> Retained<NSWindow> {
        // SAFETY: Tauri's live NSWindow, used on the main thread by every caller.
        unsafe { Retained::retain(window.ns_window().unwrap().cast::<NSWindow>()) }.unwrap()
    }

    /// A left-button event at a point in the page, sent through the window as AppKit sends a click.
    fn mouse(window: &WebviewWindow, kind: NSEventType, x: f64, y: f64) {
        let (sender, receiver) = mpsc::channel();
        window
            .with_webview(move |platform| {
                // SAFETY: Tauri hands out the live WKWebView and its NSWindow on the main thread.
                let view = unsafe { Retained::retain(platform.inner().cast::<NSView>()) }.unwrap();
                let window = unsafe { Retained::retain(platform.ns_window().cast::<NSWindow>()) }.unwrap();
                // The page's point (top-left origin) in the web view's own coordinates, then the window's.
                let height = view.bounds().size.height;
                let local = if view.isFlipped() {
                    NSPoint::new(x, y)
                } else {
                    NSPoint::new(x, height - y)
                };
                let location = view.convertPoint_toView(local, None);
                let time = NSProcessInfo::processInfo().systemUptime();
                let pressure: f32 = if kind == NSEventType::LeftMouseDown { 1.0 } else { 0.0 };
                let context: Option<&AnyObject> = None;
                // SAFETY: NSEvent's documented constructor and argument types.
                let event: Option<Retained<NSEvent>> = unsafe {
                    msg_send![NSEvent::class(), mouseEventWithType: kind, location: location,
                        modifierFlags: NSEventModifierFlags::empty(), timestamp: time,
                        windowNumber: window.windowNumber(), context: context, eventNumber: 0isize,
                        clickCount: 1isize, pressure: pressure]
                };
                window.sendEvent(&event.unwrap());
                let _ = sender.send(());
            })
            .unwrap();
        receiver.recv_timeout(Duration::from_secs(10)).unwrap();
    }

    /// A click: down, a short press, up.
    fn click(window: &WebviewWindow, x: f64, y: f64) {
        mouse(window, NSEventType::LeftMouseDown, x, y);
        std::thread::sleep(Duration::from_millis(40));
        mouse(window, NSEventType::LeftMouseUp, x, y);
    }

    /// The darkest pixel (mean of R, G, B) in the caret's spot, read from a capture of the window.
    fn darkest(app: &AppHandle, window: &WebviewWindow, geometry: &Geometry, capture: &std::path::Path) -> f64 {
        let window = window.clone();
        let (number, frame_width, title_bar) = on_main(app, move || {
            let window = ns_window(&window);
            let frame = window.frame();
            let content = window.contentRectForFrameRect(frame);
            (
                window.windowNumber(),
                frame.size.width,
                frame.size.height - content.size.height,
            )
        });
        let status = Command::new("/usr/sbin/screencapture")
            .args(["-x", "-o", &format!("-l{number}")])
            .arg(capture)
            .status()
            .unwrap();
        assert!(status.success(), "screencapture failed: {status}");
        let bytes = std::fs::read(capture).unwrap();
        let image = NSBitmapImageRep::imageRepWithData(&NSData::with_bytes(&bytes)).expect("a PNG capture");
        let scale = image.pixelsWide() as f64 / frame_width;
        let samples = image.samplesPerPixel() as usize;
        assert!(image.bitsPerSample() == 8 && samples >= 3, "unexpected capture format");
        // A few pixels either side of the line's start, over the line's height.
        let left = ((geometry.line_x - 2.0) * scale).floor() as isize;
        let right = ((geometry.line_x + 3.0) * scale).ceil() as isize;
        let top = ((title_bar + geometry.line_y + 2.0) * scale).floor() as isize;
        let bottom = ((title_bar + geometry.line_y + geometry.line_h - 2.0) * scale).ceil() as isize;
        let mut min = 255.0_f64;
        let mut pixel = [0usize; 8];
        for y in top..bottom {
            for x in left..right {
                // SAFETY: `pixel` holds every sample of one pixel of this 8-bit image.
                unsafe { image.getPixel_atX_y(std::ptr::NonNull::new(pixel.as_mut_ptr()).unwrap(), x, y) };
                min = min.min((pixel[0] + pixel[1] + pixel[2]) as f64 / 3.0);
            }
        }
        min
    }

    fn run(app: &AppHandle, url: tauri::Url) {
        let handle = app.clone();
        let window = on_main(app, move || {
            let window = WebviewWindowBuilder::new(&handle, LABEL, WebviewUrl::External(url))
                .inner_size(900., 640.)
                // The shell's main window draws its page under a transparent title bar
                // (`tauri.conf.json`), so the page fills the window, as it does here.
                .title_bar_style(tauri::TitleBarStyle::Overlay)
                .hidden_title(true)
                .focused(true)
                .build()
                .unwrap();
            // The app is in front and the window is key, as a user's click would leave them.
            let mtm = MainThreadMarker::new().unwrap();
            #[allow(deprecated)]
            NSApplication::sharedApplication(mtm).activateIgnoringOtherApps(true);
            ns_window(&window).makeKeyAndOrderFront(None);
            window
        });
        window.set_focus().unwrap();
        // A click on a window that is not key only makes it key (wry accepts no first mouse).
        let mut key = false;
        for _ in 0..100 {
            let probe = window.clone();
            key = on_main(app, move || {
                let mtm = MainThreadMarker::new().unwrap();
                NSApplication::sharedApplication(mtm).isActive() && ns_window(&probe).isKeyWindow()
            });
            if key {
                break;
            }
            std::thread::sleep(Duration::from_millis(100));
        }
        assert!(key, "the probe window never became the active app's key window");
        let mut raw = String::new();
        for _ in 0..300 {
            raw = eval(&window, GEOMETRY);
            if raw.starts_with('"') {
                break;
            }
            std::thread::sleep(Duration::from_millis(200));
        }
        let geometry: Geometry =
            serde_json::from_str(&serde_json::from_str::<String>(&raw).expect("the composer to be ready")).unwrap();
        // The transcript is scrolled and the page is still; the hold's prewarm has run.
        std::thread::sleep(Duration::from_millis(1500));
        let capture = std::env::temp_dir().join("composer-caret.png");
        let (x, y) = (geometry.x + geometry.w * 0.4, geometry.y + geometry.h / 2.0);
        // WebKit can take the first press of a fresh page late, carrying it past the hold: one click
        // first, then the measured one from a blurred input (as ui/e2e/voice-shortcut/caret.spec.ts).
        click(&window, x, y);
        std::thread::sleep(Duration::from_millis(600));
        eval(&window, "document.activeElement && document.activeElement.blur(), true");
        std::thread::sleep(Duration::from_millis(300));
        let before = darkest(app, &window, &geometry, &capture);
        click(&window, x, y);
        // Past the moment a press becomes a hold (350 ms), so this was a click.
        std::thread::sleep(Duration::from_millis(450));
        assert_eq!(
            eval(&window, CARET_STATE),
            "true",
            "the click left the input without focus or a caret position: {}",
            eval(
                &window,
                &FOCUS.replace("__X__", &x.to_string()).replace("__Y__", &y.to_string())
            )
        );
        let mut after = Vec::new();
        for _ in 0..12 {
            after.push(darkest(app, &window, &geometry, &capture));
            std::thread::sleep(Duration::from_millis(120));
        }
        let drawn = after.iter().copied().fold(255.0_f64, f64::min);
        println!("caret spot, darkest before the click: {before:.0}; after it: {after:?}");
        assert!(
            drawn < before - CARET_CONTRAST,
            "the input has the caret but it is not drawn: darkest {drawn:.0} after the click, {before:.0} before"
        );
        println!("composer caret probe passed");
    }

    pub fn main() {
        let url: tauri::Url = std::env::var("AVIBE_CARET_PROBE_URL")
            .expect("AVIBE_CARET_PROBE_URL")
            .parse()
            .unwrap();
        let app = tauri::Builder::default().build(tauri::generate_context!()).unwrap();
        let mut url = Some(url);
        app.run(move |app, event| {
            let tauri::RunEvent::Ready = event else { return };
            let Some(url) = url.take() else { return };
            let handle = app.clone();
            std::thread::spawn(move || {
                let outcome = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| run(&handle, url)));
                handle.exit(if outcome.is_ok() { 0 } else { 1 });
            });
        });
    }
}
