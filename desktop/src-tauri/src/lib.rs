//! The Avibe desktop shell.
//!
//! The shell is deliberately thin. It owns one window, runs the bootstrap state
//! machine from [`avibe_runtime_host`], and navigates that window to the
//! Workbench once the Runtime answers the combined readiness endpoint.
//!
//! Two boundaries are load-bearing:
//!
//! * **Normal lifecycle does not stop the Runtime.** Closing or recreating a
//!   window leaves it running. Only explicit replacement or uninstall invokes
//!   the Runtime's own graceful stop command.
//! * **The Workbench is not privileged.** `capabilities/bootstrap.json` grants
//!   the two bootstrap commands to the shell's own local page only. Once the
//!   window navigates to the Workbench origin the capability no longer matches,
//!   and [`ensure_shell_ui`] rejects the call a second time regardless.

use std::sync::atomic::{AtomicU64, AtomicU8, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

#[cfg(feature = "bundled-runtime")]
use avibe_runtime_host::bundled_runtime_host;
#[cfg(not(feature = "bundled-runtime"))]
use avibe_runtime_host::default_runtime_host;
use avibe_runtime_host::{
    is_shell_ui_url, BootstrapNotice, BootstrapNoticeCode, BootstrapPhase, BootstrapStatus, LoopbackOrigin,
    RuntimeHost, StatusSink,
};
#[cfg(feature = "bundled-runtime")]
use serde::Deserialize;
use tauri::menu::Menu;
#[cfg(feature = "bundled-runtime")]
use tauri::menu::MenuItemKind;
use tauri::plugin::Builder as PluginBuilder;
#[cfg(target_os = "macos")]
use tauri::RunEvent;
use tauri::{AppHandle, Emitter, Manager, WebviewWindow, WebviewWindowBuilder};
#[cfg(feature = "bundled-runtime")]
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons, MessageDialogKind};
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
#[cfg(feature = "bundled-runtime")]
const ACTIVITY_UNINSTALL: u8 = 3;

#[cfg(feature = "bundled-runtime")]
const UNINSTALL_MENU_ID: &str = "uninstall-private-runtime";

#[cfg(feature = "bundled-runtime")]
const EN_PRODUCT_CATALOG: &str = include_str!("../../../ui/src/i18n/en.json");
#[cfg(feature = "bundled-runtime")]
const ZH_PRODUCT_CATALOG: &str = include_str!("../../../ui/src/i18n/zh.json");

#[cfg(feature = "bundled-runtime")]
#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct ProductCatalog {
    desktop_bootstrap: DesktopBootstrapCatalog,
}

#[cfg(feature = "bundled-runtime")]
#[derive(Deserialize)]
struct DesktopBootstrapCatalog {
    uninstall: NativeUninstallCatalog,
}

#[cfg(feature = "bundled-runtime")]
#[derive(Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
struct NativeUninstallCatalog {
    menu_label: String,
    confirm_title: String,
    confirm_message: String,
    confirm_action: String,
    cancel_action: String,
    busy_title: String,
    busy_message: String,
    success_title: String,
    success_message: String,
    failure_title: String,
    failure_message: String,
}

#[cfg(feature = "bundled-runtime")]
fn native_uninstall_catalog_for_locales(locales: impl IntoIterator<Item = String>) -> NativeUninstallCatalog {
    let use_chinese = locales
        .into_iter()
        .find_map(|locale| {
            let normalized = locale.to_lowercase();
            if normalized.starts_with("zh") {
                Some(true)
            } else if normalized.starts_with("en") {
                Some(false)
            } else {
                None
            }
        })
        .unwrap_or(false);
    let source = if use_chinese {
        ZH_PRODUCT_CATALOG
    } else {
        EN_PRODUCT_CATALOG
    };
    serde_json::from_str::<ProductCatalog>(source)
        .expect("the checked product locale catalog must be valid")
        .desktop_bootstrap
        .uninstall
}

#[cfg(feature = "bundled-runtime")]
fn native_uninstall_catalog() -> NativeUninstallCatalog {
    native_uninstall_catalog_for_locales(sys_locale::get_locales())
}

/// Shared shell state. Everything is an `Arc` so a bootstrap run can hold what it
/// needs without borrowing from the managed state across an await point.
struct Shell {
    host: Arc<RuntimeHost>,
    latest: Arc<Mutex<Option<BootstrapStatus>>>,
    activity: Arc<AtomicU8>,
    active_origin: Arc<Mutex<Option<LoopbackOrigin>>>,
    window_generation: Arc<AtomicU64>,
    bootstrap_url: Url,
}

impl Shell {
    fn new(host: RuntimeHost, bootstrap_url: Url) -> Self {
        Self {
            host: Arc::new(host),
            latest: Arc::new(Mutex::new(None)),
            activity: Arc::new(AtomicU8::new(ACTIVITY_IDLE)),
            active_origin: Arc::new(Mutex::new(None)),
            window_generation: Arc::new(AtomicU64::new(0)),
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

/// The bundled bootstrap may always navigate within its own origin. Once the
/// Runtime is ready, unprivileged pages may route only below the exact listener
/// that proved readiness; a settings rebind or hostile link cannot move the
/// shell onto another local or remote service.
fn navigation_is_allowed(url: &Url, active_origin: Option<&LoopbackOrigin>) -> bool {
    is_shell_ui_url(url) || active_origin.is_some_and(|origin| origin.matches_url_origin(url))
}

/// The Console settings page expresses a UI rebind as a navigation to a bare
/// HTTP origin. The desktop shell must rediscover the Python-owned companion
/// listener instead of following that configured LAN address.
fn is_runtime_rebind_target(url: &Url) -> bool {
    url.scheme() == "http"
        && url.host().is_some()
        && url.port().is_some()
        && url.username().is_empty()
        && url.password().is_none()
        && matches!(url.path(), "" | "/")
        && url.query().is_none()
        && url.fragment().is_none()
}

fn set_active_origin(app: &AppHandle, origin: Option<LoopbackOrigin>) -> bool {
    app.state::<Shell>()
        .active_origin
        .lock()
        .map(|mut active| *active = origin)
        .is_ok()
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
fn bootstrap_retry(window: WebviewWindow, app: AppHandle) -> Result<bool, String> {
    ensure_shell_ui(&window)?;
    Ok(spawn_bootstrap(app))
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
///
/// The return value is part of the retry contract: the bootstrap page must not
/// hide its Retry action when another run still owns the activity.
fn spawn_bootstrap(app: AppHandle) -> bool {
    let activity = app.state::<Shell>().activity.clone();
    if activity
        .compare_exchange(ACTIVITY_IDLE, ACTIVITY_BOOTSTRAP, Ordering::SeqCst, Ordering::SeqCst)
        .is_err()
    {
        return false;
    }
    spawn_owned_bootstrap(app);
    true
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
            open_workbench(&app, &status, activity);
        } else {
            let _ = activity.compare_exchange(ACTIVITY_BOOTSTRAP, ACTIVITY_IDLE, Ordering::SeqCst, Ordering::SeqCst);
        }
    });
}

#[derive(Debug, PartialEq, Eq)]
enum WorkbenchHandoff {
    Monitor,
    RetryCurrentWindow,
    LostOwnership,
}

/// Promotes a ready bootstrap to monitoring only if the window it navigated is
/// still the current generation. A recreation that happened during the handoff
/// returns ownership to bootstrap so the caller can navigate the replacement.
fn complete_workbench_handoff(
    activity: &AtomicU8,
    window_generation: &AtomicU64,
    observed_generation: u64,
) -> WorkbenchHandoff {
    if activity
        .compare_exchange(ACTIVITY_BOOTSTRAP, ACTIVITY_MONITOR, Ordering::SeqCst, Ordering::SeqCst)
        .is_err()
    {
        return WorkbenchHandoff::LostOwnership;
    }
    if window_generation.load(Ordering::SeqCst) == observed_generation {
        return WorkbenchHandoff::Monitor;
    }
    if activity
        .compare_exchange(ACTIVITY_MONITOR, ACTIVITY_BOOTSTRAP, Ordering::SeqCst, Ordering::SeqCst)
        .is_ok()
    {
        WorkbenchHandoff::RetryCurrentWindow
    } else {
        WorkbenchHandoff::LostOwnership
    }
}

/// Hands the window to the Workbench. Reached only from a `Ready` status, so the
/// Runtime has already proved both UI and Controller readiness at this origin.
fn open_workbench(app: &AppHandle, ready: &BootstrapStatus, activity: Arc<AtomicU8>) {
    // Validated once more at the point of use: navigation is the one irreversible
    // step, and it must never be reachable with an unvalidated string.
    let Ok(origin) = LoopbackOrigin::parse(&ready.origin) else {
        let _ = activity.compare_exchange(ACTIVITY_BOOTSTRAP, ACTIVITY_IDLE, Ordering::SeqCst, Ordering::SeqCst);
        return;
    };
    let window_generation = app.state::<Shell>().window_generation.clone();

    loop {
        let observed_generation = window_generation.load(Ordering::SeqCst);
        let Some(window) = app.get_webview_window(MAIN_WINDOW) else {
            let _ = activity.compare_exchange(ACTIVITY_BOOTSTRAP, ACTIVITY_IDLE, Ordering::SeqCst, Ordering::SeqCst);
            if app.get_webview_window(MAIN_WINDOW).is_some() {
                let _ = spawn_bootstrap(app.clone());
            }
            return;
        };
        if window_generation.load(Ordering::SeqCst) != observed_generation {
            continue;
        }
        // A recreated window clears the previous navigation grant. Restore the
        // exact ready origin for each generation immediately before navigating
        // that generation's window.
        if !set_active_origin(app, Some(origin.clone())) {
            let _ = activity.compare_exchange(ACTIVITY_BOOTSTRAP, ACTIVITY_IDLE, Ordering::SeqCst, Ordering::SeqCst);
            return;
        }
        if window.navigate(origin.navigation_url()).is_err() {
            if window_generation.load(Ordering::SeqCst) != observed_generation {
                continue;
            }
            let _ = set_active_origin(app, None);
            let _ = activity.compare_exchange(ACTIVITY_BOOTSTRAP, ACTIVITY_IDLE, Ordering::SeqCst, Ordering::SeqCst);
            WindowSink {
                app: app.clone(),
                latest: app.state::<Shell>().latest.clone(),
            }
            .publish(workbench_navigation_failure_status(ready, &origin));
            return;
        }
        match complete_workbench_handoff(&activity, &window_generation, observed_generation) {
            WorkbenchHandoff::Monitor => {
                start_runtime_monitor(app.clone(), origin, activity);
                return;
            }
            WorkbenchHandoff::RetryCurrentWindow => continue,
            WorkbenchHandoff::LostOwnership => return,
        }
    }
}

fn workbench_navigation_failure_status(ready: &BootstrapStatus, origin: &LoopbackOrigin) -> BootstrapStatus {
    BootstrapStatus::failed(
        origin,
        ready.attempt,
        BootstrapNotice::new(BootstrapNoticeCode::WorkbenchNavigationFailed),
        true,
    )
}

/// Watches the exact origin that bootstrap proved ready. The caller owns the
/// shell's single monitor activity until this task exits or begins recovery.
fn start_runtime_monitor(app: AppHandle, origin: LoopbackOrigin, activity: Arc<AtomicU8>) {
    let host = app.state::<Shell>().host.clone();

    tauri::async_runtime::spawn(async move {
        let mut readiness_loss = ReadinessLoss::default();

        loop {
            tokio::time::sleep(MONITOR_INTERVAL).await;
            if activity.load(Ordering::SeqCst) != ACTIVITY_MONITOR {
                break;
            }
            if app.get_webview_window(MAIN_WINDOW).is_none() {
                let _ = activity.compare_exchange(ACTIVITY_MONITOR, ACTIVITY_IDLE, Ordering::SeqCst, Ordering::SeqCst);
                break;
            }
            let ready = host.is_ready(&origin).await;
            // Window recreation can transfer ownership while the network probe
            // is pending. The superseded monitor must not mutate the new
            // bootstrap run's launch ownership after the await point.
            if activity.load(Ordering::SeqCst) != ACTIVITY_MONITOR {
                break;
            }
            if readiness_loss.observe(ready) {
                // This only releases retained launch ownership. The desktop
                // shell never sends a stop signal to the old Runtime.
                host.reset_after_confirmed_runtime_loss();
                if return_to_bootstrap(&app) {
                    if activity
                        .compare_exchange(ACTIVITY_MONITOR, ACTIVITY_BOOTSTRAP, Ordering::SeqCst, Ordering::SeqCst)
                        .is_ok()
                    {
                        spawn_owned_bootstrap(app);
                    }
                    break;
                }
                // A transient native navigation failure must not silently
                // abandon recovery. Keep the monitor ownership and try again.
                if app.get_webview_window(MAIN_WINDOW).is_none() {
                    let _ =
                        activity.compare_exchange(ACTIVITY_MONITOR, ACTIVITY_IDLE, Ordering::SeqCst, Ordering::SeqCst);
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
    if window.navigate(bootstrap_url).is_err() {
        return false;
    }
    let _ = set_active_origin(app, None);
    true
}

/// Re-enters endpoint discovery after the Workbench asks to move to a newly
/// configured UI origin. The requested URL itself is never loaded.
fn rediscover_after_runtime_rebind(app: AppHandle, origin: LoopbackOrigin) {
    let activity = app.state::<Shell>().activity.clone();
    if activity
        .compare_exchange(ACTIVITY_MONITOR, ACTIVITY_BOOTSTRAP, Ordering::SeqCst, Ordering::SeqCst)
        .is_err()
    {
        return;
    }

    tauri::async_runtime::spawn(async move {
        if return_to_bootstrap(&app) {
            spawn_owned_bootstrap(app);
            return;
        }
        if activity
            .compare_exchange(ACTIVITY_BOOTSTRAP, ACTIVITY_MONITOR, Ordering::SeqCst, Ordering::SeqCst)
            .is_ok()
        {
            start_runtime_monitor(app, origin, activity);
        }
    });
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

/// Transfers a recreated window from an idle or stale monitor owner to a fresh
/// bootstrap run. An already-running bootstrap will publish into the new page.
fn claim_recreated_window_bootstrap(activity: &AtomicU8) -> bool {
    loop {
        let current = activity.load(Ordering::SeqCst);
        match current {
            ACTIVITY_BOOTSTRAP => return false,
            ACTIVITY_IDLE | ACTIVITY_MONITOR => {
                if activity
                    .compare_exchange(current, ACTIVITY_BOOTSTRAP, Ordering::SeqCst, Ordering::SeqCst)
                    .is_ok()
                {
                    return true;
                }
            }
            _ => return false,
        }
    }
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
        let (activity, latest, window_generation) = {
            let shell = app.state::<Shell>();
            (
                shell.activity.clone(),
                shell.latest.clone(),
                shell.window_generation.clone(),
            )
        };
        // Increment before inspecting activity. A bootstrap-to-monitor handoff
        // that races this recreation will observe the generation change and
        // navigate this replacement window before it starts monitoring.
        window_generation.fetch_add(1, Ordering::SeqCst);
        let _ = set_active_origin(app, None);
        if claim_recreated_window_bootstrap(&activity) {
            if let Ok(mut latest) = latest.lock() {
                *latest = None;
            }
            spawn_owned_bootstrap(app.clone());
        }
    }
}

fn application_menu(app: &AppHandle) -> tauri::Result<Menu<tauri::Wry>> {
    let menu = Menu::default(app)?;
    #[cfg(feature = "bundled-runtime")]
    {
        use tauri::menu::{MenuItem, PredefinedMenuItem};

        let first_submenu = menu.items()?.into_iter().find_map(|item| match item {
            MenuItemKind::Submenu(submenu) => Some(submenu),
            _ => None,
        });
        if let Some(submenu) = first_submenu {
            let catalog = native_uninstall_catalog();
            let separator = PredefinedMenuItem::separator(app)?;
            let uninstall = MenuItem::with_id(app, UNINSTALL_MENU_ID, catalog.menu_label, true, None::<&str>)?;
            let position = submenu.items()?.len().saturating_sub(1);
            submenu.insert_items(&[&separator, &uninstall], position)?;
        }
    }
    Ok(menu)
}

#[cfg(feature = "bundled-runtime")]
fn claim_runtime_removal(activity: &AtomicU8) -> bool {
    loop {
        let current = activity.load(Ordering::SeqCst);
        match current {
            ACTIVITY_IDLE | ACTIVITY_MONITOR => {
                if activity
                    .compare_exchange(current, ACTIVITY_UNINSTALL, Ordering::SeqCst, Ordering::SeqCst)
                    .is_ok()
                {
                    return true;
                }
            }
            ACTIVITY_BOOTSTRAP | ACTIVITY_UNINSTALL => return false,
            _ => return false,
        }
    }
}

#[cfg(feature = "bundled-runtime")]
fn recover_after_runtime_removal_failure(app: &AppHandle, activity: Arc<AtomicU8>) {
    let _ = activity.compare_exchange(ACTIVITY_UNINSTALL, ACTIVITY_IDLE, Ordering::SeqCst, Ordering::SeqCst);
    if return_to_bootstrap(app) {
        let _ = spawn_bootstrap(app.clone());
    }
}

#[cfg(feature = "bundled-runtime")]
fn request_private_runtime_removal(app: AppHandle) {
    let catalog = native_uninstall_catalog();
    let confirmation_app = app.clone();
    app.dialog()
        .message(catalog.confirm_message.clone())
        .title(catalog.confirm_title.clone())
        .kind(MessageDialogKind::Warning)
        .buttons(MessageDialogButtons::OkCancelCustom(
            catalog.confirm_action.clone(),
            catalog.cancel_action.clone(),
        ))
        .show(move |confirmed| {
            if !confirmed {
                return;
            }
            let (host, activity, active_origin) = {
                let shell = confirmation_app.state::<Shell>();
                (
                    shell.host.clone(),
                    shell.activity.clone(),
                    shell.active_origin.lock().ok().and_then(|origin| origin.clone()),
                )
            };
            if !claim_runtime_removal(&activity) {
                confirmation_app
                    .dialog()
                    .message(catalog.busy_message.clone())
                    .title(catalog.busy_title.clone())
                    .kind(MessageDialogKind::Info)
                    .show(|_| {});
                return;
            }

            tauri::async_runtime::spawn(async move {
                match host.remove_private_runtime(active_origin.as_ref()).await {
                    Ok(true) => {
                        let exit_app = confirmation_app.clone();
                        confirmation_app
                            .dialog()
                            .message(catalog.success_message.clone())
                            .title(catalog.success_title.clone())
                            .kind(MessageDialogKind::Info)
                            .show(move |_| exit_app.exit(0));
                    }
                    Ok(false) | Err(_) => {
                        recover_after_runtime_removal_failure(&confirmation_app, activity);
                        confirmation_app
                            .dialog()
                            .message(catalog.failure_message.clone())
                            .title(catalog.failure_title.clone())
                            .kind(MessageDialogKind::Error)
                            .show(|_| {});
                    }
                }
            });
        });
}

pub fn run() {
    tauri::Builder::default()
        // Registered first, as the plugin documents: a second launch is handed to
        // the running shell instead of starting a competing Runtime.
        .plugin(tauri_plugin_single_instance::init(|app, _argv, _cwd| {
            focus_or_restore_main_window(app);
        }))
        .plugin(tauri_plugin_dialog::init())
        .menu(application_menu)
        .on_menu_event(|_app, _event| {
            #[cfg(feature = "bundled-runtime")]
            if _event.id() == UNINSTALL_MENU_ID {
                request_private_runtime_removal(_app.clone());
            }
        })
        .plugin(
            PluginBuilder::<_, ()>::new("shell-run-events")
                .on_navigation(|webview, url| {
                    let active_origin = webview
                        .try_state::<Shell>()
                        .and_then(|shell| shell.active_origin.lock().ok().and_then(|active| active.clone()));
                    if navigation_is_allowed(url, active_origin.as_ref()) {
                        return true;
                    }
                    if is_runtime_rebind_target(url) {
                        if let Some(origin) = active_origin {
                            rediscover_after_runtime_rebind(webview.app_handle().clone(), origin);
                        }
                    }
                    false
                })
                .on_event(|_app, _event| {
                    #[cfg(target_os = "macos")]
                    if let RunEvent::Reopen {
                        has_visible_windows: false,
                        ..
                    } = _event
                    {
                        focus_or_restore_main_window(_app);
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
            let host = {
                #[cfg(feature = "bundled-runtime")]
                {
                    bundled_runtime_host(
                        app.path().resource_dir()?.join("runtime"),
                        app.path().app_local_data_dir()?.join("runtime"),
                    )?
                }
                #[cfg(not(feature = "bundled-runtime"))]
                {
                    default_runtime_host()?
                }
            };
            app.manage(Shell::new(host, bootstrap_url));
            let _ = spawn_bootstrap(app.handle().clone());
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

    #[test]
    fn a_recreated_window_transfers_monitor_ownership_to_bootstrap() {
        let activity = AtomicU8::new(ACTIVITY_MONITOR);

        assert!(claim_recreated_window_bootstrap(&activity));
        assert_eq!(activity.load(Ordering::SeqCst), ACTIVITY_BOOTSTRAP);
    }

    #[test]
    fn a_recreated_window_does_not_duplicate_an_active_bootstrap() {
        let activity = AtomicU8::new(ACTIVITY_BOOTSTRAP);

        assert!(!claim_recreated_window_bootstrap(&activity));
        assert_eq!(activity.load(Ordering::SeqCst), ACTIVITY_BOOTSTRAP);
    }

    #[cfg(feature = "bundled-runtime")]
    #[test]
    fn uninstall_claims_idle_or_monitor_activity_exclusively() {
        for current in [ACTIVITY_IDLE, ACTIVITY_MONITOR] {
            let activity = AtomicU8::new(current);
            assert!(claim_runtime_removal(&activity));
            assert_eq!(activity.load(Ordering::SeqCst), ACTIVITY_UNINSTALL);
            assert!(!claim_runtime_removal(&activity));
        }
    }

    #[cfg(feature = "bundled-runtime")]
    #[test]
    fn uninstall_waits_for_an_active_bootstrap() {
        let activity = AtomicU8::new(ACTIVITY_BOOTSTRAP);

        assert!(!claim_runtime_removal(&activity));
        assert_eq!(activity.load(Ordering::SeqCst), ACTIVITY_BOOTSTRAP);
    }

    #[cfg(feature = "bundled-runtime")]
    #[test]
    fn native_uninstall_copy_uses_the_first_supported_system_locale() {
        let chinese =
            native_uninstall_catalog_for_locales(["fr-FR", "zh-Hant-TW", "en-US"].into_iter().map(str::to_owned));
        let english =
            native_uninstall_catalog_for_locales(["fr-FR", "en-US", "zh-Hans-CN"].into_iter().map(str::to_owned));

        assert_ne!(chinese.menu_label, english.menu_label);
        assert_ne!(chinese.confirm_message, english.confirm_message);
        assert!(!chinese.failure_message.is_empty());
        assert!(!english.failure_message.is_empty());
    }

    #[test]
    fn a_navigation_failure_is_retryable_without_losing_the_ready_origin() {
        let origin = LoopbackOrigin::parse("http://127.0.0.1:5123").expect("a loopback origin");
        let ready = BootstrapStatus::ready(&origin, 4, BootstrapNoticeCode::Ready);

        let failed = workbench_navigation_failure_status(&ready, &origin);

        assert_eq!(failed.phase, BootstrapPhase::Failed);
        assert_eq!(failed.origin, origin.as_str());
        assert_eq!(failed.attempt, ready.attempt);
        assert_eq!(failed.notice.code, BootstrapNoticeCode::WorkbenchNavigationFailed);
        assert!(failed.retryable);
    }

    #[test]
    fn a_recreated_window_keeps_bootstrap_ownership_during_handoff() {
        let activity = AtomicU8::new(ACTIVITY_BOOTSTRAP);
        let generation = AtomicU64::new(2);

        assert_eq!(
            complete_workbench_handoff(&activity, &generation, 1),
            WorkbenchHandoff::RetryCurrentWindow
        );
        assert_eq!(activity.load(Ordering::SeqCst), ACTIVITY_BOOTSTRAP);
    }

    #[test]
    fn an_unchanged_window_enters_monitoring_after_handoff() {
        let activity = AtomicU8::new(ACTIVITY_BOOTSTRAP);
        let generation = AtomicU64::new(2);

        assert_eq!(
            complete_workbench_handoff(&activity, &generation, 2),
            WorkbenchHandoff::Monitor
        );
        assert_eq!(activity.load(Ordering::SeqCst), ACTIVITY_MONITOR);
    }

    #[test]
    fn navigation_is_confined_to_the_shell_or_the_active_runtime_listener() {
        let origin = LoopbackOrigin::parse("http://127.0.0.1:5123").expect("a loopback origin");

        assert!(navigation_is_allowed(
            &Url::parse("tauri://localhost/index.html").expect("shell URL"),
            None
        ));
        assert!(navigation_is_allowed(
            &Url::parse("http://127.0.0.1:5123/show/session/").expect("Workbench URL"),
            Some(&origin)
        ));
        for raw in [
            "http://127.0.0.1:5124/",
            "http://192.168.1.10:5123/",
            "https://avibe.bot/",
        ] {
            assert!(
                !navigation_is_allowed(&Url::parse(raw).expect("test URL"), Some(&origin)),
                "{raw} must not leave the active listener"
            );
        }
    }

    #[test]
    fn only_bare_http_origins_are_treated_as_runtime_rebinds() {
        for raw in [
            "http://192.168.1.10:5123/",
            "http://localhost:5123",
            "http://[2001:db8::1]:5123/",
        ] {
            assert!(is_runtime_rebind_target(&Url::parse(raw).expect("test URL")), "{raw}");
        }
        for raw in [
            "https://192.168.1.10:5123/",
            "http://192.168.1.10/",
            "http://192.168.1.10:5123/settings",
            "http://user@192.168.1.10:5123/",
            "http://192.168.1.10:5123/?next=remote",
        ] {
            assert!(!is_runtime_rebind_target(&Url::parse(raw).expect("test URL")), "{raw}");
        }
    }
}
