//! The Avibe desktop shell.
//!
//! The shell is deliberately thin. It owns one window, runs the bootstrap state
//! machine from [`avibe_runtime_host`], and navigates that window to the
//! Workbench once — and only once — the Runtime answers `/health`.
//!
//! Two boundaries are load-bearing:
//!
//! * **The Runtime is not ours to stop.** The shell may start one; it never stops
//!   one, whether it adopted it or launched it.
//! * **The Workbench is not privileged.** `capabilities/bootstrap.json` grants
//!   the two bootstrap commands to the shell's own local page only. Once the
//!   window navigates to the Workbench origin the capability no longer matches,
//!   and [`ensure_shell_ui`] rejects the call a second time regardless.

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};

use avibe_runtime_host::{
    default_runtime_host, is_shell_ui_url, BootstrapPhase, BootstrapStatus, LoopbackOrigin, RuntimeHost, StatusSink,
};
use tauri::{AppHandle, Emitter, Manager, WebviewWindow};

/// The shell's only window. Matches `app.windows[0].label` in `tauri.conf.json`
/// and the `windows` list in `capabilities/bootstrap.json`.
const MAIN_WINDOW: &str = "main";

/// Event carrying a [`BootstrapStatus`] to the bootstrap page.
const STATUS_EVENT: &str = "bootstrap-status";

/// Shared shell state. Everything is an `Arc` so a bootstrap run can hold what it
/// needs without borrowing from the managed state across an await point.
struct Shell {
    host: Arc<RuntimeHost>,
    latest: Arc<Mutex<Option<BootstrapStatus>>>,
    run_in_flight: Arc<AtomicBool>,
}

impl Shell {
    fn new(host: RuntimeHost) -> Self {
        Self {
            host: Arc::new(host),
            latest: Arc::new(Mutex::new(None)),
            run_in_flight: Arc::new(AtomicBool::new(false)),
        }
    }
}

/// Publishes bootstrap progress to the bootstrap page, and keeps the latest value
/// so a page that loads mid-run can catch up.
struct WindowSink {
    app: AppHandle,
    latest: Arc<Mutex<Option<BootstrapStatus>>>,
}

impl StatusSink for WindowSink {
    fn publish(&self, status: BootstrapStatus) {
        if let Ok(mut latest) = self.latest.lock() {
            *latest = Some(status.clone());
        }
        // Addressed to the shell's window specifically: a broadcast would also
        // reach the Workbench after navigation.
        let _ = self.app.emit_to(MAIN_WINDOW, STATUS_EVENT, status);
    }
}

/// Rejects any caller that is not the shell's own bootstrap page.
///
/// The capability file is the enforcing boundary. This is the second layer, so
/// that widening a capability by mistake cannot alone expose the shell to a page
/// served by the Runtime.
fn ensure_shell_ui(window: &WebviewWindow) -> Result<(), String> {
    match window.url() {
        Ok(url) if is_shell_ui_url(&url) => Ok(()),
        _ => Err("This command is only available to the Avibe desktop shell.".to_owned()),
    }
}

/// The current bootstrap status, or `null` before the first one is published.
#[tauri::command]
fn bootstrap_status(window: WebviewWindow, app: AppHandle) -> Result<Option<BootstrapStatus>, String> {
    ensure_shell_ui(&window)?;
    let latest = app.state::<Shell>().latest.clone();
    let status = latest
        .lock()
        .map_err(|_| "Bootstrap state is unavailable.".to_owned())?;
    Ok(status.clone())
}

/// Starts another bootstrap run after a failure. Returns as soon as the run is
/// scheduled; progress arrives through [`STATUS_EVENT`].
#[tauri::command]
fn bootstrap_retry(window: WebviewWindow, app: AppHandle) -> Result<(), String> {
    ensure_shell_ui(&window)?;
    spawn_bootstrap(app);
    Ok(())
}

/// Runs the bootstrap state machine once, unless one is already running.
fn spawn_bootstrap(app: AppHandle) {
    let (host, latest, run_in_flight) = {
        let shell = app.state::<Shell>();
        (shell.host.clone(), shell.latest.clone(), shell.run_in_flight.clone())
    };

    // A second concurrent run would race the first over the same window and,
    // worse, would reach the launch decision twice.
    if run_in_flight.swap(true, Ordering::SeqCst) {
        return;
    }

    tauri::async_runtime::spawn(async move {
        let sink = WindowSink {
            app: app.clone(),
            latest,
        };
        let status = host.bootstrap(&sink).await;
        run_in_flight.store(false, Ordering::SeqCst);

        if status.phase == BootstrapPhase::Ready {
            open_workbench(&app, &status.origin);
        }
    });
}

/// Hands the window to the Workbench. Reached only from a `Ready` status, so the
/// Runtime has already answered `/health` at this origin.
fn open_workbench(app: &AppHandle, origin: &str) {
    let Some(window) = app.get_webview_window(MAIN_WINDOW) else {
        return;
    };
    // Validated once more at the point of use: navigation is the one irreversible
    // step, and it must never be reachable with an unvalidated string.
    let Ok(origin) = LoopbackOrigin::parse(origin) else {
        return;
    };
    let _ = window.navigate(origin.navigation_url());
}

/// Brings the existing window forward when a second instance is launched.
fn focus_main_window(app: &AppHandle) {
    let Some(window) = app.get_webview_window(MAIN_WINDOW) else {
        return;
    };
    let _ = window.unminimize();
    let _ = window.show();
    let _ = window.set_focus();
}

pub fn run() {
    tauri::Builder::default()
        // Registered first, as the plugin documents: a second launch is handed to
        // the running shell instead of starting a competing Runtime.
        .plugin(tauri_plugin_single_instance::init(|app, _argv, _cwd| {
            focus_main_window(app);
        }))
        .invoke_handler(tauri::generate_handler![bootstrap_status, bootstrap_retry])
        .setup(|app| {
            app.manage(Shell::new(default_runtime_host()?));
            spawn_bootstrap(app.handle().clone());
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("failed to start the Avibe desktop shell");
}
