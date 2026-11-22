//! What `shell.rs` asks of a WKWebView. Every call ends on the main thread.
use crate::direct::{
    rendezvous::{Reply, Request},
    webview_macos,
    wiring::Material,
};
use objc2::{rc::Retained, MainThreadMarker};
use objc2_web_kit::WKWebView;
use tauri::{AppHandle, Manager, Webview};
use tokio::sync::oneshot;

fn with(view: &Webview, then: impl FnOnce(&WKWebView, MainThreadMarker) + Send + 'static) {
    let _ = view.with_webview(move |platform| {
        let Some(main) = MainThreadMarker::new() else { return };
        // SAFETY: Tauri hands out the live WKWebView, on the main thread,
        // for the duration of this closure.
        let Some(webview) = (unsafe { Retained::retain(platform.inner().cast::<WKWebView>()) }) else {
            return;
        };
        then(&webview, main);
    });
}

pub fn ask(
    view: &Webview,
    request: Request,
    current: impl Fn() -> bool + Send + 'static,
    done: impl FnOnce(Reply) + Send + 'static,
) {
    with(view, move |webview, main| {
        webview_macos::fetch_rendezvous(webview, main, &request, current, done);
    });
}

/// `still` is asked on the main thread, at this work's turn: a route queued
/// for a session that has ended meanwhile is not installed.
pub fn install(
    app: &AppHandle,
    window: String,
    host: String,
    material: Material,
    still: impl Fn() -> bool + Send + 'static,
) {
    let handle = app.clone();
    let _ = app.run_on_main_thread(move || {
        let Some(view) = handle.get_webview(&window) else {
            return;
        };
        with(&view, move |webview, main| {
            if !still() {
                return;
            }
            let _ = webview_macos::install_route(
                webview,
                main,
                &host,
                material.proxy,
                &material.username,
                &material.password,
            );
        });
    });
}

/// Routing here is live: nothing is answered per window.
pub fn answer(_: &AppHandle, _: String, _: Material, _: impl Fn() -> bool + Send + 'static) {}

/// Routing here is live: nothing is kept per window.
pub fn forget_closed(_: impl Fn(&str) -> bool) {}

/// Routing here is live: a window is created without arguments.
pub fn arguments(_: &str, _: &Material) -> Option<String> {
    None
}

/// Takes the route off the window's browsing data, for connections opened
/// from now on. A connection that is alive keeps its proxy until the
/// session's client is dropped, which the caller does next.
pub async fn detach(app: &AppHandle, window: String) {
    let (done, detached) = oneshot::channel();
    let handle = app.clone();
    let sent = app.run_on_main_thread(move || {
        let Some(view) = handle.get_webview(&window) else {
            return;
        };
        with(&view, move |webview, main| {
            let _ = webview_macos::remove_route(webview, main);
            let _ = done.send(());
        });
    });
    if sent.is_ok() {
        // A window that closed meanwhile never answers.
        let _ = detached.await;
    }
}
