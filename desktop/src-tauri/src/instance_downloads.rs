//! Fence WebView2's existing native download UI without replacing its owner.
//!
//! Wry's on_download handler sets Handled=true on Windows, which suppresses the
//! normal download UI. Subscribe at the native edge instead. macOS has no
//! download handler in this shell; do not introduce a new download workflow.

#[cfg(windows)]
mod windows_native {
    use std::cell::RefCell;

    use avibe_runtime_host::instance_client::transport::Epoch;
    use tauri::{AppHandle, Manager, WebviewWindow};
    use webview2_com::{
        take_pwstr, DownloadStartingEventHandler,
        Microsoft::Web::WebView2::Win32::{
            ICoreWebView2DownloadOperation, ICoreWebView2_4, ICoreWebView2_9, COREWEBVIEW2_DOWNLOAD_STATE,
            COREWEBVIEW2_DOWNLOAD_STATE_IN_PROGRESS,
        },
        StateChangedEventHandler,
    };
    use windows::core::{Interface, PWSTR};

    struct Download {
        operation: ICoreWebView2DownloadOperation,
        view: ICoreWebView2_9,
        epoch: Epoch,
    }
    // These COM objects never leave their WebView2 UI apartment.
    thread_local! {
        static ACTIVE: RefCell<Vec<Download>> = const { RefCell::new(Vec::new()) };
    }

    pub fn install(window: &WebviewWindow) -> tauri::Result<()> {
        let app = window.app_handle().clone();
        window.with_webview(move |platform| {
            // SAFETY: Tauri runs this closure and all subscribed callbacks on
            // the WebView2 apartment. The interface owns each event argument.
            let result = unsafe {
                (|| -> windows::core::Result<()> {
                    platform
                        .controller()
                        .CoreWebView2()?
                        .cast::<ICoreWebView2_4>()?
                        .add_DownloadStarting(
                            &DownloadStartingEventHandler::create(Box::new({
                                let app = app.clone();
                                move |sender, args| {
                                    let Some(args) = args else {
                                        return Ok(());
                                    };
                                    if !crate::instance_client::enabled(&app) {
                                        return Ok(());
                                    }
                                    args.SetCancel(true)?;
                                    let operation = args.DownloadOperation()?;
                                    let mut uri = PWSTR::null();
                                    operation.Uri(&mut uri)?;
                                    let uri = take_pwstr(uri);
                                    let epoch = app
                                        .get_webview_window(crate::MAIN_WINDOW)
                                        .and_then(|window| window.url().ok())
                                        .zip(uri.parse().ok())
                                        .and_then(|(current, target)| {
                                            crate::instance_client::download_epoch(&app, &current, &target)
                                        });
                                    let Some(epoch) = epoch else {
                                        args.SetCancel(true)?;
                                        return Ok(());
                                    };
                                    let Some(sender) = sender else {
                                        args.SetCancel(true)?;
                                        return Ok(());
                                    };
                                    let view = sender.cast::<ICoreWebView2_9>()?;
                                    let pointer = operation.as_raw() as usize;
                                    operation.add_StateChanged(
                                        &StateChangedEventHandler::create(Box::new(move |operation, _| {
                                            let Some(operation) = operation else {
                                                return Ok(());
                                            };
                                            let mut state = COREWEBVIEW2_DOWNLOAD_STATE::default();
                                            operation.State(&mut state)?;
                                            if state != COREWEBVIEW2_DOWNLOAD_STATE_IN_PROGRESS {
                                                ACTIVE.with(|active| {
                                                    active
                                                        .borrow_mut()
                                                        .retain(|entry| entry.operation.as_raw() as usize != pointer)
                                                });
                                            }
                                            Ok(())
                                        })),
                                        &mut 0,
                                    )?;
                                    if epoch.check().is_err() {
                                        return Ok(());
                                    }
                                    ACTIVE.with(|active| active.borrow_mut().push(Download { operation, view, epoch }));
                                    // Leave Handled untouched: the existing WebView2
                                    // download UI remains responsible for presentation.
                                    args.SetCancel(false)
                                }
                            })),
                            &mut 0,
                        )
                })()
            };
            if let Err(error) = result {
                eprintln!("desktop download guard: {error}");
                if crate::instance_client::enabled(&app) {
                    if let Some(window) = app.get_webview_window(crate::MAIN_WINDOW) {
                        let _ = window.destroy();
                    }
                }
            }
        })
    }

    pub fn invalidate(app: &AppHandle) {
        let _ = app.run_on_main_thread(|| {
            let stale = ACTIVE.with(|active| {
                let mut active = active.borrow_mut();
                let mut stale = vec![];
                let mut current = vec![];
                for download in active.drain(..) {
                    if download.epoch.check().is_err() {
                        stale.push(download);
                    } else {
                        current.push(download);
                    }
                }
                *active = current;
                stale
            });
            for download in stale {
                // Release the RefCell before COM can reenter StateChanged.
                unsafe {
                    let _ = download.operation.Cancel();
                    let _ = download.view.CloseDefaultDownloadDialog();
                }
            }
        });
    }
}

#[cfg(windows)]
pub(crate) use windows_native::{install, invalidate};

#[cfg(not(windows))]
pub(crate) fn invalidate(_: &tauri::AppHandle) {}
