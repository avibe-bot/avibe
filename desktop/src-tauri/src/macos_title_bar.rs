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
use objc2::runtime::AnyObject;
use objc2::{define_class, msg_send, ClassType, MainThreadMarker, MainThreadOnly};
use objc2_app_kit::{
    NSAutoresizingMaskOptions, NSEvent, NSResponder, NSView, NSWindow, NSWindowOrderingMode, NSWindowStyleMask,
    NSWindowTitleVisibility,
};
use objc2_foundation::{
    ns_string, NSError, NSObject, NSObjectProtocol, NSPoint, NSRect, NSSize, NSString, NSUserDefaults,
};
use objc2_web_kit::WKWebView;
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
    let _ = window.with_webview(|webview| {
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
