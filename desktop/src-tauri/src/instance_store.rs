//! Terminal retirement of a disposable instance WebView.
//!
//! Call off the UI thread, after invalidating the store's selection generation.
//! The caller must never project cookies into, navigate, or reuse this window
//! after retirement starts. Native completion, cookie readback and destruction
//! are separate boundaries; Tauri's queued clear API is not completion.
use std::sync::mpsc;
use std::time::{Duration, Instant};

use tauri::{Manager, WebviewWindow};

const ERASE_TIMEOUT: Duration = Duration::from_secs(10);

pub fn retire(window: &WebviewWindow) -> Result<(), String> {
    #[cfg(target_os = "macos")]
    if objc2::MainThreadMarker::new().is_some() {
        return Err("instance store retirement requires a worker thread".into());
    }
    let _ = window.hide();
    let erased = erase_completed(window);
    // Even a failed clear retires this disposable store permanently. Never
    // return it to a signed-out page or another account.
    let destroyed = destroy_completed(window);
    erased.and(destroyed)
}

fn erase_completed(window: &WebviewWindow) -> Result<(), String> {
    let (sender, receiver) = mpsc::channel();
    window
        .with_webview(move |platform| clear_platform(platform, sender))
        .map_err(|_| "instance store unavailable")?;
    receiver
        .recv_timeout(ERASE_TIMEOUT)
        .map_err(|_| "instance store erasure timed out")??;
    if !window
        .cookies()
        .map_err(|_| "instance cookie readback failed")?
        .is_empty()
    {
        return Err("instance cookies remain after native erasure completion".into());
    }
    Ok(())
}

fn destroy_completed(window: &WebviewWindow) -> Result<(), String> {
    let label = window.label().to_owned();
    let app = window.app_handle().clone();
    window.destroy().map_err(|_| "instance window destruction failed")?;
    let deadline = Instant::now() + ERASE_TIMEOUT;
    while app.get_webview_window(&label).is_some() {
        if Instant::now() >= deadline {
            return Err("instance window destruction timed out".into());
        }
        // Wait for an observable registry transition, not a guessed erase delay.
        std::thread::sleep(Duration::from_millis(10));
    }
    Ok(())
}

#[cfg(target_os = "macos")]
fn clear_platform(platform: tauri::webview::PlatformWebview, sender: mpsc::Sender<Result<(), String>>) {
    use objc2::{rc::Retained, MainThreadMarker};
    use objc2_foundation::NSDate;
    use objc2_web_kit::{WKWebView, WKWebsiteDataStore};
    let Some(main) = MainThreadMarker::new() else {
        let _ = sender.send(Err("instance store callback is not on main thread".into()));
        return;
    };
    // SAFETY: Tauri supplies a live WKWebView on the UI thread for this closure.
    let Some(view) = (unsafe { Retained::retain(platform.inner().cast::<WKWebView>()) }) else {
        let _ = sender.send(Err("instance store unavailable".into()));
        return;
    };
    unsafe {
        view.stopLoading();
        let store = view.configuration().websiteDataStore();
        let types = WKWebsiteDataStore::allWebsiteDataTypes(main);
        let since = NSDate::dateWithTimeIntervalSince1970(0.0);
        let completed = block2::RcBlock::new(move || {
            let _ = sender.send(Ok(()));
        });
        store.removeDataOfTypes_modifiedSince_completionHandler(&types, &since, &completed);
    }
}

#[cfg(windows)]
fn clear_platform(platform: tauri::webview::PlatformWebview, sender: mpsc::Sender<Result<(), String>>) {
    use webview2_com::{
        ClearBrowsingDataCompletedHandler,
        Microsoft::Web::WebView2::Win32::{ICoreWebView2Profile2, ICoreWebView2_13},
    };
    use windows::core::Interface;
    let failure = sender.clone();
    // SAFETY: Tauri runs this closure on the WebView2 COM apartment.
    let result = unsafe {
        (|| -> windows::core::Result<()> {
            let view = platform.controller().CoreWebView2()?;
            view.Stop()?;
            view.cast::<ICoreWebView2_13>()?
                .Profile()?
                .cast::<ICoreWebView2Profile2>()?
                .ClearBrowsingDataAll(&ClearBrowsingDataCompletedHandler::create(Box::new(move |result| {
                    let _ = sender.send(result.map_err(|_| "instance store erasure failed".to_owned()));
                    Ok(())
                })))
        })()
    };
    if result.is_err() {
        let _ = failure.send(Err("instance store erasure failed".into()));
    }
}

#[cfg(not(any(target_os = "macos", windows)))]
fn clear_platform(_: tauri::webview::PlatformWebview, sender: mpsc::Sender<Result<(), String>>) {
    let _ = sender.send(Err("instance stores are unsupported on this platform".into()));
}
