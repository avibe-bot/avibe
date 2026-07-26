//! The Avibe desktop shell.
//!
//! The shell is deliberately thin. It owns one window, runs the bootstrap state
//! machine from [`avibe_runtime_host`], and navigates that window to the
//! Workbench once the Runtime answers the combined readiness endpoint.
//!
//! Two boundaries are load-bearing:
//!
//! * **The Runtime is not ours to stop.** The shell may start one; it never stops
//!   one, whether it adopted it or launched it.
//! * **The Workbench is not privileged.** `capabilities/bootstrap.json` grants
//!   the two bootstrap commands to the shell's own local page only. Once the
//!   window navigates to the Workbench origin the capability no longer matches,
//!   and [`ensure_shell_ui`] rejects the call a second time regardless.

use std::sync::atomic::{AtomicU8, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use avibe_runtime_host::{
    default_runtime_host, is_shell_ui_url, BootstrapPhase, BootstrapStatus, LoopbackOrigin, RuntimeHost, StatusSink,
};
use tauri::plugin::Builder as PluginBuilder;
use tauri::{AppHandle, Emitter, Manager, RunEvent, WebviewWindow, WebviewWindowBuilder};
use url::Url;

/// The shell's only window. Matches `app.windows[0].label` in `tauri.conf.json`
/// and the `windows` list in `capabilities/bootstrap.json`.
const MAIN_WINDOW: &str = "main";

/// Event carrying a [`BootstrapStatus`] to the bootstrap page.
const STATUS_EVENT: &str = "bootstrap-status";

/// Fixed destination for the bootstrap's missing-Runtime help action.
const INSTALL_DOCS_URL: &str = "https://docs.avibe.bot/get-started/install";

/// How often the shell checks a Runtime after handing the window to it.
const MONITOR_INTERVAL: Duration = Duration::from_secs(2);

/// A single missed probe may be a reload or a short scheduler pause. Requiring
/// three misses avoids replacing a healthy Workbench on transient failure.
const READINESS_FAILURE_THRESHOLD: u8 = 3;

const ACTIVITY_IDLE: u8 = 0;
const ACTIVITY_BOOTSTRAP: u8 = 1;
const ACTIVITY_MONITOR: u8 = 2;

/// Shared shell state. Everything is an `Arc` so a bootstrap run can hold what it
/// needs without borrowing from the managed state across an await point.
struct Shell {
    host: Arc<RuntimeHost>,
    latest: Arc<Mutex<Option<BootstrapStatus>>>,
    activity: Arc<AtomicU8>,
    bootstrap_url: Url,
}

impl Shell {
    fn new(host: RuntimeHost, bootstrap_url: Url) -> Self {
        Self {
            host: Arc::new(host),
            latest: Arc::new(Mutex::new(None)),
            activity: Arc::new(AtomicU8::new(ACTIVITY_IDLE)),
            bootstrap_url,
        }
    }
}

#[derive(Default)]
struct ReadinessLoss {
    consecutive_failures: u8,
}

impl ReadinessLoss {
    fn observe(&mut self, ready: bool) -> bool {
        if ready {
            self.consecutive_failures = 0;
            return false;
        }
        self.consecutive_failures = self.consecutive_failures.saturating_add(1);
        self.consecutive_failures >= READINESS_FAILURE_THRESHOLD
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

/// Opens installation guidance in the system browser.
///
/// The URL is deliberately not an argument: even the privileged bootstrap page
/// cannot turn this into an arbitrary protocol or URL opener.
#[tauri::command]
fn open_install_docs(window: WebviewWindow) -> Result<(), String> {
    ensure_shell_ui(&window)?;
    tauri_plugin_opener::open_url(INSTALL_DOCS_URL, None::<&str>)
        .map_err(|_| "Installation help could not be opened.".to_owned())
}

/// Runs the bootstrap state machine once, unless one is already running.
fn spawn_bootstrap(app: AppHandle) {
    let activity = app.state::<Shell>().activity.clone();
    if activity
        .compare_exchange(ACTIVITY_IDLE, ACTIVITY_BOOTSTRAP, Ordering::SeqCst, Ordering::SeqCst)
        .is_err()
    {
        return;
    }
    spawn_owned_bootstrap(app);
}

/// Runs bootstrap after the caller has atomically acquired bootstrap activity.
fn spawn_owned_bootstrap(app: AppHandle) {
    let (host, latest, activity) = {
        let shell = app.state::<Shell>();
        (shell.host.clone(), shell.latest.clone(), shell.activity.clone())
    };

    tauri::async_runtime::spawn(async move {
        let sink = WindowSink {
            app: app.clone(),
            latest,
        };
        let status = host.bootstrap(&sink).await;

        if status.phase == BootstrapPhase::Ready {
            open_workbench(&app, &status.origin, activity);
        } else {
            activity.store(ACTIVITY_IDLE, Ordering::SeqCst);
        }
    });
}

/// Hands the window to the Workbench. Reached only from a `Ready` status, so the
/// Runtime has already proved both UI and Controller readiness at this origin.
fn open_workbench(app: &AppHandle, origin: &str, activity: Arc<AtomicU8>) {
    let Some(window) = app.get_webview_window(MAIN_WINDOW) else {
        activity.store(ACTIVITY_IDLE, Ordering::SeqCst);
        return;
    };
    // Validated once more at the point of use: navigation is the one irreversible
    // step, and it must never be reachable with an unvalidated string.
    let Ok(origin) = LoopbackOrigin::parse(origin) else {
        activity.store(ACTIVITY_IDLE, Ordering::SeqCst);
        return;
    };
    if activity
        .compare_exchange(ACTIVITY_BOOTSTRAP, ACTIVITY_MONITOR, Ordering::SeqCst, Ordering::SeqCst)
        .is_err()
    {
        return;
    }
    if window.navigate(origin.navigation_url()).is_err() {
        activity.store(ACTIVITY_IDLE, Ordering::SeqCst);
        return;
    }
    start_runtime_monitor(app.clone(), origin, activity);
}

/// Watches the exact origin that bootstrap proved ready. The caller owns the
/// shell's single monitor activity until this task exits or begins recovery.
fn start_runtime_monitor(app: AppHandle, origin: LoopbackOrigin, activity: Arc<AtomicU8>) {
    let host = app.state::<Shell>().host.clone();

    tauri::async_runtime::spawn(async move {
        let mut readiness_loss = ReadinessLoss::default();

        loop {
            tokio::time::sleep(MONITOR_INTERVAL).await;
            if app.get_webview_window(MAIN_WINDOW).is_none() {
                activity.store(ACTIVITY_IDLE, Ordering::SeqCst);
                break;
            }
            if readiness_loss.observe(host.is_ready(&origin).await) {
                // This only releases retained launch ownership. The desktop
                // shell never sends a stop signal to the old Runtime.
                host.reset_after_confirmed_runtime_loss();
                if return_to_bootstrap(&app) {
                    if activity
                        .compare_exchange(ACTIVITY_MONITOR, ACTIVITY_BOOTSTRAP, Ordering::SeqCst, Ordering::SeqCst)
                        .is_ok()
                    {
                        spawn_owned_bootstrap(app);
                    } else {
                        activity.store(ACTIVITY_IDLE, Ordering::SeqCst);
                    }
                    break;
                }
                // A transient native navigation failure must not silently
                // abandon recovery. Keep the monitor ownership and try again.
                if app.get_webview_window(MAIN_WINDOW).is_none() {
                    activity.store(ACTIVITY_IDLE, Ordering::SeqCst);
                    break;
                }
            }
        }
    });
}

/// Restores the exact bootstrap URL captured before the first navigation. It is
/// a bundled Tauri page in production and the fixed Vite dev URL in development.
fn return_to_bootstrap(app: &AppHandle) -> bool {
    let (bootstrap_url, latest) = {
        let shell = app.state::<Shell>();
        (shell.bootstrap_url.clone(), shell.latest.clone())
    };
    if !is_shell_ui_url(&bootstrap_url) {
        return false;
    }
    let Some(window) = app.get_webview_window(MAIN_WINDOW) else {
        return false;
    };
    if let Ok(mut latest) = latest.lock() {
        *latest = None;
    }
    window.navigate(bootstrap_url).is_ok()
}

/// Brings the existing window forward when a second instance is launched.
fn ensure_main_window(app: &AppHandle) -> Option<WebviewWindow> {
    if let Some(window) = app.get_webview_window(MAIN_WINDOW) {
        return Some(window);
    }
    let config = app
        .config()
        .app
        .windows
        .iter()
        .find(|config| config.label == MAIN_WINDOW)?
        .clone();
    WebviewWindowBuilder::from_config(app, &config).ok()?.build().ok()
}

/// Brings the existing window forward, or recreates it and re-enters bootstrap.
fn focus_or_restore_main_window(app: &AppHandle) {
    let created = app.get_webview_window(MAIN_WINDOW).is_none();
    let Some(window) = ensure_main_window(app) else {
        return;
    };
    let _ = window.unminimize();
    let _ = window.show();
    let _ = window.set_focus();
    if created {
        spawn_bootstrap(app.clone());
    }
}

pub fn run() {
    tauri::Builder::default()
        // Registered first, as the plugin documents: a second launch is handed to
        // the running shell instead of starting a competing Runtime.
        .plugin(tauri_plugin_single_instance::init(|app, _argv, _cwd| {
            focus_or_restore_main_window(app);
        }))
        .plugin(
            PluginBuilder::<_, ()>::new("shell-run-events")
                .on_event(|app, event| {
                    #[cfg(target_os = "macos")]
                    if let RunEvent::Reopen {
                        has_visible_windows: false,
                        ..
                    } = event
                    {
                        focus_or_restore_main_window(app);
                    }
                })
                .build(),
        )
        .invoke_handler(tauri::generate_handler![
            bootstrap_status,
            bootstrap_retry,
            open_install_docs
        ])
        .setup(|app| {
            let window = app.get_webview_window(MAIN_WINDOW).ok_or_else(|| {
                std::io::Error::new(std::io::ErrorKind::NotFound, "the Avibe desktop window is missing")
            })?;
            let bootstrap_url = window.url()?;
            if !is_shell_ui_url(&bootstrap_url) {
                return Err(std::io::Error::new(
                    std::io::ErrorKind::PermissionDenied,
                    "the Avibe desktop window did not load its bundled bootstrap page",
                )
                .into());
            }
            app.manage(Shell::new(default_runtime_host()?, bootstrap_url));
            spawn_bootstrap(app.handle().clone());
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("failed to start the Avibe desktop shell");
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn readiness_recovers_only_after_consecutive_failures() {
        let mut loss = ReadinessLoss::default();
        assert!(!loss.observe(false));
        assert!(!loss.observe(false));
        assert!(loss.observe(false));
    }

    #[test]
    fn a_successful_probe_resets_the_failure_streak() {
        let mut loss = ReadinessLoss::default();
        assert!(!loss.observe(false));
        assert!(!loss.observe(false));
        assert!(!loss.observe(true));
        assert!(!loss.observe(false));
        assert!(!loss.observe(false));
        assert!(loss.observe(false));
    }
}
