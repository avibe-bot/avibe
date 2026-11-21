//! What `shell.rs` asks of a WebView2. Every call ends on its apartment.
use crate::direct::{
    rendezvous::{Reply, Request},
    webview_windows,
    wiring::Material,
};
use std::sync::Mutex;
use tauri::{AppHandle, Manager, Webview};
use tokio::sync::oneshot;

/// The challenge answers that are registered, by window label. A label is
/// used once in a run of the app.
static ANSWERING: Mutex<Vec<(String, i64)>> = Mutex::new(Vec::new());

pub fn ask(
    view: &Webview,
    request: Request,
    current: impl Fn() -> bool + Send + 'static,
    done: impl FnOnce(Reply) + Send + 'static,
) {
    let _ = view.with_webview(move |platform| {
        // SAFETY: Tauri runs this closure on the WebView2 apartment.
        let Ok(core) = (unsafe { platform.controller().CoreWebView2() }) else {
            return;
        };
        webview_windows::fetch_rendezvous(&core, &request, current, done);
    });
}

/// Routing here is given at creation: nothing is installed later.
pub fn install(_: &AppHandle, _: String, _: String, _: Material) {}

/// Registered when the window is made, before it is sent to its instance:
/// the route applies to it from its first request, and an unanswered
/// challenge would bring up WebView2's own sign-in prompt.
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
            let registered = webview_windows::answer_listener_challenge(
                &core,
                material.proxy,
                &material.username,
                &material.password,
            );
            if let (Ok(registration), Ok(mut answering)) = (registered, ANSWERING.lock()) {
                answering.push((window, registration));
            }
        });
    });
}

pub fn arguments(host: &str, material: &Material) -> Option<String> {
    Some(super::window_arguments(&webview_windows::route_argument(
        host,
        material.proxy,
    )?))
}

/// The route of an open window cannot be taken off: when its client is
/// dropped the listener's port closes, and the route's own `DIRECT` carries
/// the window on the tunnel. What is taken off, before that, is the answer
/// to the listener's challenge, so the credential leaves the window with
/// the session.
pub async fn detach(app: &AppHandle, window: String) {
    let registration = ANSWERING.lock().ok().and_then(|mut answering| {
        let at = answering.iter().position(|(label, _)| *label == window)?;
        Some(answering.swap_remove(at).1)
    });
    let Some(registration) = registration else { return };
    let (done, stopped) = oneshot::channel();
    let handle = app.clone();
    let sent = app.run_on_main_thread(move || {
        let Some(view) = handle.get_webview(&window) else {
            return;
        };
        let _ = view.with_webview(move |platform| {
            // SAFETY: Tauri runs this closure on the WebView2 apartment.
            if let Ok(core) = unsafe { platform.controller().CoreWebView2() } {
                let _ = webview_windows::stop_answering_listener_challenge(&core, registration);
            }
            let _ = done.send(());
        });
    });
    if sent.is_ok() {
        // A window that closed meanwhile never answers.
        let _ = stopped.await;
    }
}
