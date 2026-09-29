//! macOS overlay layout negotiation. Tauri owns drag gestures and window
//! commands; no native hit-test overlay or custom WebKit IPC is installed.
//! Older pages without explicit Tauri drag support get a standard title bar.

use block2::RcBlock;
use objc2::rc::Retained;
use objc2::runtime::AnyObject;
use objc2::MainThreadMarker;
use objc2_app_kit::{NSWindow, NSWindowStyleMask, NSWindowTitleVisibility};
use objc2_foundation::{NSError, NSString};
use objc2_web_kit::WKWebView;
use tauri::Webview;

/// Existing clearance under the traffic lights; this does not add a bar over
/// the chat header, which continues to reach the top edge.
pub const TITLE_BAR_INSET: f64 = 28.0;
const SUPPORT_META: &str = "avibe-shell-drag-regions";

/// Advertise the built-in drag capability only on macOS, before React renders.
/// Window restore still keeps a grab handle inside the sidebar's existing
/// clearance; replacing its native overlay with a DOM region preserves that area.
pub fn inset_script() -> String {
    format!(
        "if (window.self === window.top) {{ \
         Object.defineProperty(window, '__AVIBE_DESKTOP_DRAG__', {{ value: true }}); \
         document.documentElement.style.setProperty('--shell-titlebar-inset', '{TITLE_BAR_INSET}px'); }}"
    )
}

/// Answers whether the loaded top-level page declares [`SUPPORT_META`] for this
/// shell's Tauri drag support, and makes the published inset agree.
fn support_probe() -> String {
    format!(
        "(function () {{ \
         var supported = document.querySelector('meta[name=\"{SUPPORT_META}\"][content=\"tauri\"]') !== null; \
         document.documentElement.style.setProperty('--shell-titlebar-inset', supported ? '{TITLE_BAR_INSET}px' : '0px'); \
         return supported ? 'supported' : 'unsupported'; }})()"
    )
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

/// Switches between the overlay title bar and the standard
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
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn shipping_pages_declare_native_drag_support() {
        let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR"));
        for path in ["../index.html", "../../ui/index.html"] {
            let html = std::fs::read_to_string(root.join(path)).unwrap();
            assert!(html.contains(&format!("<meta name=\"{SUPPORT_META}\" content=\"tauri\"")));
        }
    }
}
