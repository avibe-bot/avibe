//! What `shell.rs` asks of a WebView2. Every call ends on its apartment.
use crate::direct::{rendezvous::Request, webview_windows, wiring::Material};
use tauri::{AppHandle, Manager, Webview};

/// WebView2's own defaults, which giving any argument replaces
/// (`wry`'s `webview2` module): no "mini menu", no SmartScreen.
const DEFAULTS: &str = "--disable-features=msWebOOUI,msPdfOOUI,msSmartScreenProtection";

pub fn ask(
    view: &Webview,
    request: Request,
    current: impl Fn() -> bool + Send + 'static,
    done: impl FnOnce(Option<String>) + Send + 'static,
) {
    let _ = view.with_webview(move |platform| {
        // SAFETY: Tauri runs this closure on the WebView2 apartment.
        let Ok(core) = (unsafe { platform.controller().CoreWebView2() }) else {
            return;
        };
        webview_windows::fetch_rendezvous(&core, &request, current, move |reply| done(reply.body()));
    });
}

/// Routing here is given at creation: nothing is installed later.
pub fn install(_: &AppHandle, _: String, _: String, _: Material) {}

pub fn answer(app: &AppHandle, window: String, material: Material) {
    let handle = app.clone();
    let _ = app.run_on_main_thread(move || {
        let Some(view) = handle.get_webview(&window) else {
            return;
        };
        let _ = view.with_webview(move |platform| {
            // SAFETY: Tauri runs this closure on the WebView2 apartment.
            let Ok(core) = (unsafe { platform.controller().CoreWebView2() }) else {
                return;
            };
            let _ = webview_windows::answer_listener_challenge(
                &core,
                material.proxy,
                &material.username,
                &material.password,
            );
        });
    });
}

pub fn arguments(host: &str, material: &Material) -> Option<String> {
    Some(format!(
        "{DEFAULTS} {}",
        webview_windows::route_argument(host, material.proxy)?
    ))
}

/// The route of an open window cannot be taken off. When its client is
/// dropped the listener's port closes, and the route's own `DIRECT`
/// carries the window on the tunnel.
pub async fn detach(_: &AppHandle, _: String) {}
