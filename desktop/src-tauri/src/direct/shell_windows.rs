//! What `shell.rs` asks of a WebView2. Every call ends on its apartment.
use crate::direct::{
    rendezvous::{Reply, Request},
    webview_windows,
    wiring::Material,
    WindowRegistration,
};
use std::sync::Mutex;
use tauri::{AppHandle, Manager, Webview};
use tokio::sync::oneshot;

/// The challenge answers that are registered, by window label: at most one
/// for a label, the newest window's.
struct Answering(Vec<(String, i64)>);

impl Answering {
    const fn new() -> Self {
        Self(Vec::new())
    }

    /// `window` answers under `registration` from now on. An app window's
    /// label is used again after its window closed: what an earlier window
    /// of that label had is forgotten.
    fn put(&mut self, window: String, registration: i64) {
        self.0.retain(|(label, _)| *label != window);
        self.0.push((window, registration));
    }

    /// The registration to take off `window`, once.
    fn take(&mut self, window: &str) -> Option<i64> {
        let at = self.0.iter().position(|(label, _)| label == window)?;
        Some(self.0.swap_remove(at).1)
    }

    /// Forgets the windows that are no longer open: their answer went with
    /// their WebView.
    fn keep(&mut self, open: impl Fn(&str) -> bool) {
        self.0.retain(|(label, _)| open(label));
    }
}

/// Read and changed on the main thread, where a window's answer is put on
/// and taken off, so the two are in order with each other; `forget_closed`
/// alone comes from elsewhere, and only drops windows that are gone.
static ANSWERING: Mutex<Answering> = Mutex::new(Answering::new());

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
pub fn install(
    _: &AppHandle,
    _: String,
    _: String,
    _: Material,
    _: WindowRegistration,
    _: impl Fn() -> bool + Send + 'static,
) {
}

/// Registered when the window is made, before it is sent to its instance:
/// the route applies to it from its first request, and an unanswered
/// challenge would bring up WebView2's own sign-in prompt.
///
/// `still` is asked on the main thread, at this work's turn: an answer
/// queued for a session that has ended meanwhile is not registered, so the
/// credential of an ended session is never put on a window.
///
/// The answer is the window's own: the credential of `registration`, which
/// no other window presents.
pub fn answer(
    app: &AppHandle,
    window: String,
    material: Material,
    registration: WindowRegistration,
    still: impl Fn() -> bool + Send + 'static,
) {
    let handle = app.clone();
    let _ = app.run_on_main_thread(move || {
        let Some(view) = handle.get_webview(&window) else {
            return;
        };
        let _ = view.with_webview(move |platform| {
            if !still() {
                return;
            }
            // SAFETY: Tauri runs this closure on the WebView2 apartment.
            let Ok(core) = (unsafe { platform.controller().CoreWebView2() }) else {
                return;
            };
            let (username, password) = registration.native_login();
            let registered = webview_windows::answer_listener_challenge(&core, material.proxy, username, password);
            if let (Ok(registration), Ok(mut answering)) = (registered, ANSWERING.lock()) {
                answering.put(window, registration);
            }
        });
    });
}

/// What was kept for a window that has closed is dropped.
pub fn forget_closed(open: impl Fn(&str) -> bool) {
    if let Ok(mut answering) = ANSWERING.lock() {
        answering.keep(open);
    }
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
///
/// Which answer the window has is looked up on the main thread, at this
/// work's turn, not when it is asked for: an answer that was queued before
/// the session ended has then either been registered, and is taken off
/// here, or finds its session gone and registers nothing.
pub async fn detach(app: &AppHandle, window: String) {
    let (done, stopped) = oneshot::channel();
    let handle = app.clone();
    let sent = app.run_on_main_thread(move || {
        let registration = ANSWERING.lock().ok().and_then(|mut answering| answering.take(&window));
        let (Some(registration), Some(view)) = (registration, handle.get_webview(&window)) else {
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
        // A window that closed meanwhile, or had no answer, never answers.
        let _ = stopped.await;
    }
}

// This module is the shell's `native` on Windows alone, so its tests run
// there alone; they sit inside it, where what it keeps is private.
#[cfg(test)]
#[path = "../../tests/direct/answering.rs"]
mod tests;
