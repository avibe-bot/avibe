//! The macOS window draws the Workbench under its title bar.
//!
//! `tauri.conf.json` gives the main window an overlay title bar with a hidden
//! title, so the traffic lights float over the page instead of sitting on a
//! separate grey strip. Two things then have to hold:
//!
//! - The page must keep its own controls out of the strip. It learns the
//!   strip's height from `--shell-titlebar-inset`, which [`INSET_SCRIPT`] sets on
//!   the root element before any Workbench script runs.
//! - The strip must still move the window. WKWebView honours no `app-region`
//!   CSS and swallows the mouse-down that would start a native drag, and Tauri's
//!   drag-region attribute needs an IPC grant the remote Workbench origin
//!   deliberately does not have. So the shell lays one transparent native view
//!   over the strip, above the WebView: a drag there moves the window and a
//!   double-click does what the system title bar would, with no page script
//!   involved and no capability widened.

use objc2::rc::Retained;
use objc2::{define_class, msg_send, MainThreadMarker, MainThreadOnly};
use objc2_app_kit::{NSAutoresizingMaskOptions, NSEvent, NSResponder, NSView, NSWindow, NSWindowOrderingMode};
use objc2_foundation::{ns_string, NSObject, NSObjectProtocol, NSPoint, NSRect, NSSize, NSUserDefaults};
use tauri::WebviewWindow;

/// Height, in points, of the strip the overlay title bar occupies: the standard
/// macOS title bar of a window without a toolbar, where the traffic lights sit.
pub const TITLE_BAR_INSET: f64 = 28.0;

/// Publishes [`TITLE_BAR_INSET`] to the Workbench and the bootstrap page. User
/// scripts run after the document element exists and before any page script.
/// Top-level document only: Show Page content in subframes is laid out by the
/// Workbench window that hosts it.
pub const INSET_SCRIPT: &str = "if (window.self === window.top) \
     document.documentElement.style.setProperty('--shell-titlebar-inset', '28px');";

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
            NSRect::new(NSPoint::new(0.0, y), NSSize::new(bounds.size.width, TITLE_BAR_INSET)),
        );
        // Pinned to the top edge and stretched with the width on every resize.
        let pin_top = if flipped {
            NSAutoresizingMaskOptions::ViewMaxYMargin
        } else {
            NSAutoresizingMaskOptions::ViewMinYMargin
        };
        strip.setAutoresizingMask(NSAutoresizingMaskOptions::ViewWidthSizable | pin_top);
        content.addSubview_positioned_relativeTo(&strip, NSWindowOrderingMode::Above, None);
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_published_inset_is_the_strip_height() {
        assert!(INSET_SCRIPT.contains(&format!("'{}px'", TITLE_BAR_INSET)));
        assert!(INSET_SCRIPT.starts_with("if (window.self === window.top)"));
    }
}
