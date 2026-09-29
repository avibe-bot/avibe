//! macOS transparent title-bar setup. Tauri owns drag gestures and window
//! commands; no native hit-test overlay or custom WebKit IPC is installed.
//! Every page uses the same transparent native frame, including older Runtime
//! pages that predate the drag-region metadata.

use objc2::rc::Retained;
use objc2::MainThreadMarker;
use objc2_app_kit::{NSWindow, NSWindowStyleMask, NSWindowTitleVisibility};
use tauri::Webview;

/// Existing clearance under the traffic lights; this does not add a bar over
/// the chat header, which continues to reach the top edge.
pub const TITLE_BAR_INSET: f64 = 28.0;
#[cfg(test)]
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

/// Applies the transparent native frame to every loaded page. The frame is a
/// shell property, so it must not depend on a metadata marker served by the
/// Runtime's UI version.
pub fn sync(webview: &Webview) {
    let _ = webview.with_webview(|platform| {
        let Some(mtm) = MainThreadMarker::new() else {
            return;
        };
        // SAFETY: Tauri hands out the live WKWebView and its NSWindow, on the
        // main thread, for the duration of this closure.
        let Some(window) = (unsafe { Retained::retain(platform.ns_window().cast::<NSWindow>()) }) else {
            return;
        };
        set_overlay(&window, mtm);
    });
}

/// Keeps the content view under a transparent native title bar while retaining
/// AppKit's traffic-light buttons and native window behavior.
fn set_overlay(window: &NSWindow, _mtm: MainThreadMarker) {
    let mut mask = window.styleMask();
    mask.set(NSWindowStyleMask::FullSizeContentView, true);
    window.setStyleMask(mask);
    // Only Tauri drag regions may move the window; transcript backgrounds cannot.
    window.setMovableByWindowBackground(false);
    window.setTitlebarAppearsTransparent(true);
    window.setTitleVisibility(NSWindowTitleVisibility::Hidden);
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
