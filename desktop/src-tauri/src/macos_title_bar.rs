//! The macOS window draws the Workbench under its title bar.
//!
//! `tauri.conf.json` gives the main window an overlay title bar with a hidden
//! title, so the traffic lights float over the page instead of sitting on a
//! separate grey strip. Two things then have to hold:
//!
//! - The page must keep the strip's region free of its own controls. The strip
//!   covers only the sidebar's top — the traffic lights float there — so the
//!   main pane's chat and search headers reach the window's top edge with their
//!   ordinary padding. Left-edge and full-window surfaces learn the strip's
//!   height from `--shell-titlebar-inset`, which [`inset_script`] sets on the
//!   root element before any Workbench script runs.
//! - The strip must move the window. WKWebView honours no `app-region` CSS and
//!   swallows the mouse-down that would start a native drag, and Tauri's
//!   drag-region attribute needs an IPC grant the remote Workbench origin
//!   deliberately does not have. So the shell lays one transparent native view
//!   over the strip, above the WebView: a drag there moves the window and a
//!   double-click does what the system title bar would, with no page script
//!   involved and no capability widened.
//! - The rest of the title bar band must move the window too, without covering
//!   the controls the main pane keeps there. Only the page knows where those
//!   are, so it asks: [`TITLE_BAR_MESSAGES`] is a WebKit message handler of the
//!   window's own, outside Tauri's IPC, that understands exactly two requests —
//!   start a window drag while the left button is down, or do what a title bar
//!   double-click does — and only from the top-level document. The Workbench
//!   sends them for a press on the band, or on a region it marks, that no
//!   control claims. A page that never sends them keeps just the strip.
//!
//! Both only hold for a page built against this exact geometry. The shell can
//! adopt a Runtime it did not ship, whose Workbench keeps a different region
//! free — or none at all — so every loaded page is asked, natively and without
//! IPC, whether it declares [`SUPPORT_META`] with the geometry this shell
//! draws. A page that does keeps the overlay; any other page, or a page that
//! cannot answer, gets the standard title bar back with the strip hidden and
//! the inset reset to zero.

use block2::RcBlock;
use objc2::rc::Retained;
use objc2::runtime::{AnyObject, ProtocolObject};
use objc2::{define_class, msg_send, ClassType, DefinedClass, MainThreadMarker, MainThreadOnly};
use objc2_app_kit::{
    NSAutoresizingMaskOptions, NSEvent, NSResponder, NSView, NSWindow, NSWindowOrderingMode, NSWindowStyleMask,
    NSWindowTitleVisibility,
};
use objc2_foundation::{
    ns_string, NSError, NSObject, NSObjectProtocol, NSPoint, NSRect, NSSize, NSString, NSUserDefaults,
};
use objc2_web_kit::{WKScriptMessage, WKScriptMessageHandler, WKUserContentController, WKWebView};
use tauri::{Webview, WebviewWindow};

/// Height, in points, of the strip the overlay title bar occupies: the standard
/// macOS title bar of a window without a toolbar, where the traffic lights sit.
pub const TITLE_BAR_INSET: f64 = 28.0;

/// Width, in points, of the strip: the Workbench sidebar's minimum width
/// (`MIN_SIDEBAR_WIDTH` in `ui/src/lib/sidebarWidth.ts`). The sidebar can only
/// grow from there and keeps its whole top edge free, so the strip never
/// reaches the main pane, whose headers therefore keep the window's full
/// height. The traffic lights float over the sidebar, above the strip.
pub const TITLE_BAR_STRIP_WIDTH: f64 = 248.0;

/// Publishes [`TITLE_BAR_INSET`] to the Workbench and the bootstrap page. User
/// scripts run after the document element exists and before any page script.
/// Top-level document only: Show Page content in subframes is laid out by the
/// Workbench window that hosts it.
pub fn inset_script() -> String {
    format!(
        "if (window.self === window.top) \
         document.documentElement.style.setProperty('--shell-titlebar-inset', '{TITLE_BAR_INSET}px');"
    )
}

/// The `<meta name>` a page carries when it keeps the strip's region free, with
/// the geometry it was built against as the content (`240x28`). The Workbench
/// (`ui/index.html`) and the bootstrap page (`index.html`) both declare it. A
/// page from another release that kept a different region free carries a
/// different name or content, so skew in either direction falls back to the
/// standard title bar instead of covering that page's controls.
const SUPPORT_META: &str = "avibe-shell-title-strip";

/// The geometry half of the [`SUPPORT_META`] contract, `<width>x<height>` in
/// points, derived from the constants the strip is drawn with.
fn support_geometry() -> String {
    format!("{TITLE_BAR_STRIP_WIDTH}x{TITLE_BAR_INSET}")
}

/// Answers whether the loaded top-level page declares [`SUPPORT_META`] for this
/// shell's exact strip geometry, and makes the published inset agree.
fn support_probe() -> String {
    let geometry = support_geometry();
    format!(
        "(function () {{ \
         var supported = document.querySelector('meta[name=\"{SUPPORT_META}\"][content=\"{geometry}\"]') !== null; \
         document.documentElement.style.setProperty('--shell-titlebar-inset', supported ? '{TITLE_BAR_INSET}px' : '0px'); \
         return supported ? 'supported' : 'unsupported'; }})()"
    )
}

define_class!(
    #[unsafe(super(NSView, NSResponder, NSObject))]
    #[thread_kind = MainThreadOnly]
    #[name = "AvibeTitleBarDragStrip"]
    struct TitleBarDragStrip;

    unsafe impl NSObjectProtocol for TitleBarDragStrip {}

    impl TitleBarDragStrip {
        #[unsafe(method(mouseDown:))]
        fn mouse_down(&self, event: &NSEvent) {
            let Some(window) = self.window() else {
                return;
            };
            if event.clickCount() == 2 {
                perform_title_bar_double_click(&window);
            } else {
                window.performWindowDragWithEvent(event);
            }
        }

        /// A click on an inactive window drags it at once, as the system title
        /// bar does, instead of being spent on activation.
        #[unsafe(method(acceptsFirstMouse:))]
        fn accepts_first_mouse(&self, _event: Option<&NSEvent>) -> bool {
            true
        }
    }
);

impl TitleBarDragStrip {
    fn new(mtm: MainThreadMarker, frame: NSRect) -> Retained<Self> {
        let this = Self::alloc(mtm).set_ivars(());
        unsafe { msg_send![super(this), initWithFrame: frame] }
    }
}

/// The name the page posts to: `window.webkit.messageHandlers.<name>`. The
/// Workbench (`ui/src/lib/desktopShell.ts`) sends one of [`TitleBarRequest`].
const TITLE_BAR_MESSAGES: &str = "avibeShellTitleBar";

/// What a page may ask of the title bar. Nothing else is understood.
#[derive(Debug, PartialEq, Eq)]
enum TitleBarRequest {
    /// A press on the band that no control claims: move the window with it.
    Drag,
    /// A double-click there: whatever the system title bar would do.
    DoubleClick,
}

impl TitleBarRequest {
    fn parse(body: &str) -> Option<Self> {
        match body {
            "drag" => Some(Self::Drag),
            "double-click" => Some(Self::DoubleClick),
            _ => None,
        }
    }
}

define_class!(
    #[unsafe(super(NSObject))]
    #[thread_kind = MainThreadOnly]
    #[name = "AvibeTitleBarMessages"]
    #[ivars = WebviewWindow]
    struct TitleBarMessages;

    unsafe impl NSObjectProtocol for TitleBarMessages {}

    unsafe impl WKScriptMessageHandler for TitleBarMessages {
        #[unsafe(method(userContentController:didReceiveScriptMessage:))]
        fn did_receive_script_message(&self, _controller: &WKUserContentController, message: &WKScriptMessage) {
            // SAFETY: WebKit hands over a live message on the main thread.
            let (main_frame, body) = unsafe { (message.frameInfo().isMainFrame(), message.body()) };
            // Show Pages run in subframes of the Workbench and never move the window.
            if !main_frame {
                return;
            }
            let Some(request) = body
                .downcast_ref::<NSString>()
                .and_then(|body| TitleBarRequest::parse(&body.to_string()))
            else {
                return;
            };
            // A drag follows the press the person is making right now; with the
            // button already up there is nothing to follow.
            if request == TitleBarRequest::Drag && NSEvent::pressedMouseButtons() & 1 == 0 {
                return;
            }
            let window = self.ivars();
            match request {
                TitleBarRequest::Drag => {
                    let _ = window.start_dragging();
                }
                TitleBarRequest::DoubleClick => {
                    let Ok(ns_window) = window.ns_window() else {
                        return;
                    };
                    // SAFETY: Tauri's live NSWindow for this webview window, used on the main thread.
                    perform_title_bar_double_click(unsafe { &*ns_window.cast::<NSWindow>() });
                }
            }
        }
    }
);

impl TitleBarMessages {
    fn new(mtm: MainThreadMarker, window: WebviewWindow) -> Retained<Self> {
        let this = Self::alloc(mtm).set_ivars(window);
        unsafe { msg_send![super(this), init] }
    }
}

/// The user's System Settings choice for double-clicking a title bar
/// (Desktop & Dock > "Double-click a window's title bar to"). An unset value is
/// the system default, zoom.
fn perform_title_bar_double_click(window: &NSWindow) {
    let action = NSUserDefaults::standardUserDefaults().stringForKey(ns_string!("AppleActionOnDoubleClick"));
    match action.map(|action| action.to_string()).as_deref() {
        Some("Minimize") => window.performMiniaturize(None),
        Some("None") => {}
        _ => window.performZoom(None),
    }
}

/// Lays the drag strip over the top of the window's content, above the WebView.
/// The window's content view holds the WebView for the window's whole life, and
/// navigation replaces only the page inside it, so one strip per window suffices.
pub fn install(window: &WebviewWindow) {
    let owner = window.clone();
    let _ = window.with_webview(move |webview| {
        let Some(mtm) = MainThreadMarker::new() else {
            return;
        };
        // SAFETY: Tauri hands out the live NSWindow of this webview's window, on
        // the main thread, for the duration of this closure.
        let window: &NSWindow = unsafe { &*webview.ns_window().cast() };
        let Some(content) = window.contentView() else {
            return;
        };
        let bounds = content.bounds();
        let flipped = content.isFlipped();
        let y = if flipped {
            0.0
        } else {
            bounds.size.height - TITLE_BAR_INSET
        };
        let strip = TitleBarDragStrip::new(
            mtm,
            NSRect::new(
                NSPoint::new(0.0, y),
                NSSize::new(TITLE_BAR_STRIP_WIDTH, TITLE_BAR_INSET),
            ),
        );
        // Pinned to the top-left corner, fixed size, on every resize.
        let pin_top = if flipped {
            NSAutoresizingMaskOptions::ViewMaxYMargin
        } else {
            NSAutoresizingMaskOptions::ViewMinYMargin
        };
        strip.setAutoresizingMask(NSAutoresizingMaskOptions::ViewMaxXMargin | pin_top);
        content.addSubview_positioned_relativeTo(&strip, NSWindowOrderingMode::Above, None);

        // SAFETY: Tauri hands out the live WKWebView, on the main thread, for the
        // duration of this closure; its configuration's controller outlives it.
        let Some(web_view) = (unsafe { Retained::retain(webview.inner().cast::<WKWebView>()) }) else {
            return;
        };
        let messages = TitleBarMessages::new(mtm, owner.clone());
        unsafe {
            web_view
                .configuration()
                .userContentController()
                .addScriptMessageHandler_name(
                    ProtocolObject::from_ref(&*messages),
                    &NSString::from_str(TITLE_BAR_MESSAGES),
                );
        }
    });
}

/// Keeps the overlay title bar only while the loaded page declares
/// [`SUPPORT_META`]. Called for every finished main-window page load; a later
/// load re-decides, so a stale answer never outlives the page that gave it.
pub fn sync(webview: &Webview) {
    let _ = webview.with_webview(|platform| {
        let Some(mtm) = MainThreadMarker::new() else {
            return;
        };
        // SAFETY: Tauri hands out the live WKWebView and its NSWindow, on the
        // main thread, for the duration of this closure; retaining them keeps
        // both alive until the completion handler has run.
        let (Some(web_view), Some(window)) = (
            unsafe { Retained::retain(platform.inner().cast::<WKWebView>()) },
            unsafe { Retained::retain(platform.ns_window().cast::<NSWindow>()) },
        ) else {
            return;
        };
        let handler = RcBlock::new(move |result: *mut AnyObject, _error: *mut NSError| {
            // SAFETY: WebKit passes the script's result, or null, as a live object.
            let supported = unsafe { result.as_ref() }
                .and_then(|result| result.downcast_ref::<NSString>())
                .is_some_and(|answer| answer.to_string() == "supported");
            set_overlay(&window, supported, mtm);
        });
        // SAFETY: called on the main thread with a live WKWebView; WebKit calls
        // the handler once, on the main thread.
        unsafe { web_view.evaluateJavaScript_completionHandler(&NSString::from_str(&support_probe()), Some(&handler)) };
    });
}

/// Switches between the overlay title bar with the drag strip and the standard
/// title bar with the window title, the only two frames the shell draws.
fn set_overlay(window: &NSWindow, overlay: bool, _mtm: MainThreadMarker) {
    let mut mask = window.styleMask();
    mask.set(NSWindowStyleMask::FullSizeContentView, overlay);
    window.setStyleMask(mask);
    window.setTitlebarAppearsTransparent(overlay);
    window.setTitleVisibility(if overlay {
        NSWindowTitleVisibility::Hidden
    } else {
        NSWindowTitleVisibility::Visible
    });
    let Some(content) = window.contentView() else {
        return;
    };
    for view in content.subviews().iter() {
        if view.isKindOfClass(TitleBarDragStrip::class()) {
            view.setHidden(!overlay);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The page can ask for a drag or a double-click and nothing else; any other
    /// body, including a near miss, is ignored rather than guessed at.
    #[test]
    fn the_title_bar_understands_only_its_two_requests() {
        assert_eq!(TitleBarRequest::parse("drag"), Some(TitleBarRequest::Drag));
        assert_eq!(
            TitleBarRequest::parse("double-click"),
            Some(TitleBarRequest::DoubleClick)
        );
        for body in ["", "Drag", "drag ", "close", "minimize", "zoom"] {
            assert_eq!(TitleBarRequest::parse(body), None, "{body:?}");
        }
    }

    /// Both pages post to the handler by name; renaming one side alone would
    /// silently leave only the strip draggable.
    #[test]
    fn every_page_this_release_serves_posts_to_the_handler_this_shell_registers() {
        let crate_dir = std::path::Path::new(env!("CARGO_MANIFEST_DIR"));
        for script in ["../src/main.ts", "../../ui/src/lib/desktopShell.ts"] {
            let source = std::fs::read_to_string(crate_dir.join(script)).expect("page script is readable");
            assert!(
                source.contains(TITLE_BAR_MESSAGES),
                "{script} must post to the {TITLE_BAR_MESSAGES} handler"
            );
            for body in ["'drag'", "'double-click'"] {
                assert!(source.contains(body), "{script} must send {body}");
            }
        }
    }

    /// A page that keeps the strip's region free must say so for this exact
    /// geometry, or the shell falls back to the standard title bar for it. Both
    /// pages the shell can show at the current release declare it.
    #[test]
    fn every_page_this_release_serves_declares_this_strip_geometry() {
        let crate_dir = std::path::Path::new(env!("CARGO_MANIFEST_DIR"));
        for page in ["../index.html", "../../ui/index.html"] {
            let html = std::fs::read_to_string(crate_dir.join(page)).expect("page is readable");
            assert!(
                html.contains(&format!(
                    "<meta name=\"{SUPPORT_META}\" content=\"{}\"",
                    support_geometry()
                )),
                "{page} must declare {SUPPORT_META} for {}",
                support_geometry()
            );
        }
    }

    /// The strip must never reach the main pane, whose headers use the full
    /// window height. The sidebar guarantees that by never shrinking below the
    /// strip's width; if its minimum changes, the strip and the geometry
    /// contract must change with it.
    #[test]
    fn the_strip_never_outgrows_the_workbench_sidebar() {
        let widths = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../ui/src/lib/sidebarWidth.ts");
        let widths = std::fs::read_to_string(widths).expect("sidebarWidth.ts is readable");
        assert!(
            widths.contains(&format!("MIN_SIDEBAR_WIDTH = {TITLE_BAR_STRIP_WIDTH};")),
            "the sidebar's minimum width must equal the native drag strip's width ({TITLE_BAR_STRIP_WIDTH}px)"
        );
    }
}
