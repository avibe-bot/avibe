//! macOS transparent title-bar setup. Tauri owns drag gestures and window
//! commands; no native hit-test overlay or custom WebKit IPC is installed.
//! Every page uses the same transparent native frame, including older Runtime
//! pages that predate the drag-region metadata.

use objc2::rc::Retained;
use objc2::MainThreadMarker;
use objc2_app_kit::{NSView, NSWindow, NSWindowButton, NSWindowStyleMask, NSWindowTitleVisibility};
use tauri::{Manager, Webview, Window};

/// Existing clearance under the traffic lights; this does not add a bar over
/// the chat header, which continues to reach the top edge.
pub const TITLE_BAR_INSET: f64 = 34.0;
#[cfg(test)]
const SUPPORT_META: &str = "avibe-shell-drag-regions";

/// Advertise the built-in drag capability only on macOS, before React renders.
/// Window restore still keeps a grab handle inside the sidebar's existing
/// clearance; replacing its native overlay with a DOM region preserves that area.
pub fn inset_script(config: &tauri::Config) -> String {
    let horizontal = configured_controls(config)
        .map(|(x, _)| x)
        .filter(|x| x.is_finite())
        .map(|x| format!("document.documentElement.style.setProperty('--shell-window-controls-left', '{x}px'); "))
        .unwrap_or_default();
    format!(
        "if (window.self === window.top) {{ \
         Object.defineProperty(window, '__AVIBE_DESKTOP_DRAG__', {{ value: true }}); \
         document.documentElement.style.setProperty('--shell-titlebar-inset', '{TITLE_BAR_INSET}px'); \
         {horizontal}}}"
    )
}

fn configured_controls(config: &tauri::Config) -> Option<(f64, f64)> {
    config
        .app
        .windows
        .iter()
        .find(|window| window.label == crate::MAIN_WINDOW)
        .and_then(|window| window.traffic_light_position.as_ref())
        .map(|position| (position.x, position.y))
}

/// The commands a page's drag strip sends: a press starts a window drag, a
/// double click zooms the window. The same pair `capabilities/window-drag.json`
/// grants the loopback Workbench.
const DRAG_PERMISSIONS: [&str; 2] = [
    "core:window:allow-start-dragging",
    "core:window:allow-internal-toggle-maximize",
];

/// Another computer's instance draws the same drag strip as this Mac's, from
/// its own origin, which no static capability names: without a grant its press
/// was refused and the window could not be moved by its top. This grants that
/// one web view, for that one origin, the window-drag pair only; every other
/// native command stays refused to it.
pub fn remote_drag_capability(label: &str, destination: &url::Url) -> Option<tauri::ipc::CapabilityBuilder> {
    if destination.scheme() != "https" && destination.scheme() != "http" {
        return None;
    }
    let host = destination.host_str()?;
    let origin = match destination.port() {
        Some(port) => format!("{}://{host}:{port}/*", destination.scheme()),
        None => format!("{}://{host}/*", destination.scheme()),
    };
    let mut capability = tauri::ipc::CapabilityBuilder::new(format!("remote-window-drag-{label}"))
        .webview(label)
        .remote(origin)
        .local(false)
        .platform(tauri::utils::platform::Target::MacOS);
    for permission in DRAG_PERMISSIONS {
        capability = capability.permission(permission);
    }
    Some(capability)
}

/// Grants `remote_drag_capability` to a web view showing another computer's instance.
pub fn grant_remote_drag(app: &tauri::AppHandle, label: &str, destination: &url::Url) {
    if let Some(capability) = remote_drag_capability(label, destination) {
        let _ = app.add_capability(capability);
    }
}

/// Applies the transparent native frame to every loaded page. The frame is a
/// shell property, so it must not depend on a metadata marker served by the
/// Runtime's UI version.
pub fn sync(webview: &Webview) {
    sync_window(&webview.window());
}

/// The physical main owns geometry even after its original WebView is retired.
/// Wry stores its initial inset on that view's parent; a new child does not
/// inherit the redraw callback. Apply the same configured coordinates after
/// AppKit lays out the window, without changing the content inset or spacing.
pub fn sync_window(window: &Window) {
    if window.label() != crate::MAIN_WINDOW {
        return;
    }
    let position = configured_controls(window.app_handle().config());
    let owned = window.clone();
    let _ = window.run_on_main_thread(move || {
        let Some(mtm) = MainThreadMarker::new() else {
            return;
        };
        let Ok(pointer) = owned.ns_window() else { return };
        // SAFETY: this is Tauri's live NSWindow, accessed on its main thread.
        let Some(window) = (unsafe { Retained::retain(pointer.cast::<NSWindow>()) }) else {
            return;
        };
        set_overlay(&window, mtm);
        if let Some((x, y)) = position {
            apply_controls(&window, x, y);
        }
    });
}

fn apply_controls(window: &NSWindow, x: f64, y: f64) {
    let (Some(close), Some(minimize), Some(zoom)) = (
        window.standardWindowButton(NSWindowButton::CloseButton),
        window.standardWindowButton(NSWindowButton::MiniaturizeButton),
        window.standardWindowButton(NSWindowButton::ZoomButton),
    ) else {
        return;
    };
    // SAFETY: all controls belong to this retained live window on the main thread.
    let Some(container) = (unsafe { close.superview().and_then(|parent| parent.superview()) }) else {
        return;
    };
    let first = NSView::frame(&close);
    let spacing = NSView::frame(&minimize).origin.x - first.origin.x;
    let mut frame = container.frame();
    frame.size.height = first.size.height + y;
    frame.origin.y = window.frame().size.height - frame.size.height;
    container.setFrame(frame);
    for (index, button) in [close, minimize, zoom].into_iter().enumerate() {
        let mut origin = NSView::frame(&button).origin;
        origin.x = x + index as f64 * spacing;
        button.setFrameOrigin(origin);
    }
}

/// Keeps the content view under a transparent native title bar while retaining
/// AppKit's traffic-light buttons and native window behavior.
fn set_overlay(window: &NSWindow, _mtm: MainThreadMarker) {
    let mut mask = window.styleMask();
    mask.set(NSWindowStyleMask::FullSizeContentView, true);
    if window.styleMask() != mask {
        window.setStyleMask(mask);
    }
    // Only Tauri drag regions may move the window; transcript backgrounds cannot.
    window.setMovableByWindowBackground(false);
    window.setTitlebarAppearsTransparent(true);
    window.setTitleVisibility(NSWindowTitleVisibility::Hidden);
}

#[cfg(test)]
mod tests {
    use super::*;

    fn mock_context() -> tauri::Context<tauri::test::MockRuntime> {
        tauri::generate_context!()
    }

    fn invoke(view: &tauri::Webview<tauri::test::MockRuntime>, url: &str, cmd: &str) -> Result<(), String> {
        tauri::test::get_ipc_response(
            view,
            tauri::webview::InvokeRequest {
                cmd: cmd.into(),
                callback: tauri::ipc::CallbackFn(0),
                error: tauri::ipc::CallbackFn(1),
                url: url.parse().unwrap(),
                body: tauri::ipc::InvokeBody::default(),
                headers: Default::default(),
                invoke_key: tauri::test::INVOKE_KEY.to_string(),
            },
        )
        .map(|_| ())
        .map_err(|error| error.to_string())
    }

    /// Another computer's instance in main, through the real capability files:
    /// its drag strip is refused before the grant, may drag and zoom after it,
    /// and nothing else; another view, or another origin in this one, gains nothing.
    #[test]
    fn a_remote_instance_view_may_drag_the_window_and_nothing_more() {
        use tauri::{WebviewBuilder, WebviewUrl, WindowBuilder};
        let app = tauri::test::mock_builder().build(mock_context()).unwrap();
        let remote = "https://studio-app.avibe.bot/";
        let physical = WindowBuilder::new(&app, crate::MAIN_WINDOW).build().unwrap();
        let child = |label: &str| {
            physical
                .add_child(
                    WebviewBuilder::new(label, WebviewUrl::External(remote.parse().unwrap())),
                    tauri::LogicalPosition::new(0.0, 0.0),
                    tauri::LogicalSize::new(880.0, 600.0),
                )
                .unwrap()
        };
        let view = child("main-runtime-7");
        let other = child("main-runtime-8");
        let drag = "plugin:window|start_dragging";
        assert!(invoke(&view, remote, drag).is_err(), "refused before the grant");

        grant_remote_drag(app.handle(), "main-runtime-7", &remote.parse().unwrap());
        assert_eq!(invoke(&view, remote, drag), Ok(()));
        assert_eq!(invoke(&view, remote, "plugin:window|internal_toggle_maximize"), Ok(()));
        for cmd in [
            "plugin:window|close",
            "plugin:window|set_position",
            "plugin:event|emit",
            "pet_bind",
            "open_app_window",
        ] {
            assert!(invoke(&view, remote, cmd).is_err(), "{cmd} stays refused");
        }
        assert!(invoke(&other, remote, drag).is_err(), "another view gains nothing");
        assert!(
            invoke(&view, "https://elsewhere.example/", drag).is_err(),
            "another origin gains nothing"
        );
    }

    #[test]
    fn the_remote_grant_names_one_origin_and_ignores_other_schemes() {
        let built = |raw: &str| {
            remote_drag_capability("main-runtime-3", &raw.parse().unwrap())
                .map(|c| serde_json::to_value(tauri::ipc::RuntimeCapability::build(c)).unwrap())
        };
        let file = built("https://studio-app.avibe.bot/chat/1?x=2").unwrap();
        assert_eq!(
            file["remote"]["urls"],
            serde_json::json!(["https://studio-app.avibe.bot/*"])
        );
        assert_eq!(file["webviews"], serde_json::json!(["main-runtime-3"]));
        assert_eq!(file["local"], false);
        assert_eq!(file["permissions"], serde_json::json!(DRAG_PERMISSIONS));
        assert_eq!(
            built("https://host.example:8443/").unwrap()["remote"]["urls"],
            serde_json::json!(["https://host.example:8443/*"])
        );
        assert!(built("tauri://localhost/").is_none());
        assert!(built("about:blank").is_none());
    }

    #[test]
    fn shipping_pages_declare_native_drag_support() {
        let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR"));
        for path in ["../index.html", "../../ui/index.html"] {
            let html = std::fs::read_to_string(root.join(path)).unwrap();
            assert!(html.contains(&format!("<meta name=\"{SUPPORT_META}\" content=\"tauri\"")));
        }
    }

    // The configured native x coordinate owns horizontal alignment. Existing
    // HTML-marker coverage cannot catch a hardcoded or device-pixel offset.
    #[test]
    fn inset_uses_configured_logical_x_without_changing_vertical_clearance() {
        for x in [12.0, 19.5] {
            let config: tauri::Config = serde_json::from_value(serde_json::json!({
                "identifier":"bot.avibe.titlebar-test",
                "app":{"windows":[{"label":"main","trafficLightPosition":{"x":x,"y":17.0}}]}
            }))
            .unwrap();
            let script = inset_script(&config);
            assert!(script.contains(&format!("'--shell-window-controls-left', '{x}px'")));
            assert!(script.contains("'--shell-titlebar-inset', '34px'"));
            assert_eq!(configured_controls(&config), Some((x, 17.0)));
        }
        assert!(!inset_script(&tauri::Config::default()).contains("--shell-window-controls-left"));
    }
}
