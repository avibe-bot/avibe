use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use avibe_runtime_host::notifications::{
    run_notifications, HttpNotificationTransport, NotificationFilter, NotificationGate, NotificationIntent,
    NotificationReceipt, NotificationRegistry, NotificationSink, NotificationSnapshot,
};
use avibe_runtime_host::LoopbackOrigin;
use serde::{Deserialize, Serialize};
use tauri::{AppHandle, Manager, WebviewWindow};

use crate::{native_catalog_for_locales, pet, updater, MAIN_WINDOW};

mod native;

pub const MENU_ID: &str = "notifications";
pub const SETTINGS_EVENT: &str = "avibe:desktop-notifications";
// The Runtime expires an unacknowledged request after ten seconds. This
// fallback stays below that deadline while SSE remains the normal path.
const REMOTE_REQUEST_POLL_INTERVAL: Duration = Duration::from_secs(5);

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct NativeNotificationCatalog {
    pub toggle: String,
    approval_requested: NotificationCopy,
    run_succeeded: NotificationCopy,
    run_failed: NotificationCopy,
    run_canceled: NotificationCopy,
}

#[derive(Clone, Deserialize)]
struct NotificationCopy {
    title: String,
    body: String,
}

impl NativeNotificationCatalog {
    fn copy(&self, intent: NotificationIntent) -> NotificationCopy {
        match intent {
            NotificationIntent::ApprovalRequested => &self.approval_requested,
            NotificationIntent::RunSucceeded => &self.run_succeeded,
            NotificationIntent::RunFailed => &self.run_failed,
            NotificationIntent::RunCanceled => &self.run_canceled,
        }
        .clone()
    }

    /// With a known source the banner reads like a mail notification: where it
    /// happened as the title, the outcome as the body. Without one it keeps the
    /// generic sentence, which is all the shell can honestly say.
    fn render(&self, intent: NotificationIntent, source: Option<String>) -> NotificationCopy {
        let copy = self.copy(intent);
        match source {
            Some(source) => NotificationCopy {
                title: source,
                body: copy.title,
            },
            None => copy,
        }
    }
}

#[derive(Deserialize, Serialize)]
struct Preference {
    enabled: bool,
}

fn read_preference(path: &Path) -> std::io::Result<bool> {
    match std::fs::read(path) {
        Ok(bytes) => serde_json::from_slice::<Preference>(&bytes)
            .map(|preference| preference.enabled)
            .map_err(std::io::Error::other),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(true),
        Err(error) => Err(error),
    }
}

fn write_preference(path: &Path, enabled: bool) -> std::io::Result<()> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let bytes = serde_json::to_vec(&Preference { enabled })?;
    let temporary = path.with_extension("tmp");
    let result = std::fs::write(&temporary, bytes).and_then(|()| std::fs::rename(&temporary, path));
    if result.is_err() {
        let _ = std::fs::remove_file(&temporary);
    }
    result
}

struct Connection {
    origin: LoopbackOrigin,
    task: tauri::async_runtime::JoinHandle<()>,
    remote_request_poll: tauri::async_runtime::JoinHandle<()>,
}

pub struct Notifications {
    preference_path: PathBuf,
    enabled: Arc<AtomicBool>,
    generation: Arc<AtomicU64>,
    connection: Mutex<Option<Connection>>,
    filter: Arc<Mutex<NotificationFilter>>,
    registry: Arc<Mutex<NotificationRegistry>>,
}

impl Notifications {
    pub fn new(preference_path: PathBuf) -> Self {
        let enabled = read_preference(&preference_path).unwrap_or_else(|_| {
            eprintln!("Desktop notifications disabled: the notification preference could not be read.");
            false
        });
        Self {
            preference_path,
            enabled: Arc::new(AtomicBool::new(enabled)),
            generation: Arc::new(AtomicU64::new(0)),
            connection: Mutex::new(None),
            filter: Arc::new(Mutex::new(NotificationFilter::default())),
            registry: Arc::new(Mutex::new(NotificationRegistry::default())),
        }
    }

    pub fn enabled(&self) -> bool {
        self.enabled.load(Ordering::SeqCst)
    }

    pub fn set_enabled(&self, enabled: bool) -> Result<bool, String> {
        write_preference(&self.preference_path, enabled)
            .map_err(|error| format!("The desktop notification preference could not be saved: {error}"))?;
        self.enabled.store(enabled, Ordering::SeqCst);
        Ok(enabled)
    }

    pub fn toggle(&self) -> Result<bool, String> {
        self.set_enabled(!self.enabled())
    }

    pub fn start(&self, app: &AppHandle, origin: LoopbackOrigin) {
        let mut connection = self.connection.lock().expect("notification connection lock");
        if connection.as_ref().is_some_and(|current| current.origin == origin) {
            return;
        }
        let observed_generation = self.generation.fetch_add(1, Ordering::SeqCst) + 1;
        if let Some(previous) = connection.take() {
            previous.task.abort();
            previous.remote_request_poll.abort();
        }
        let Ok(transport) = HttpNotificationTransport::new(origin.clone()) else {
            eprintln!("The desktop notification connection could not be initialized.");
            return;
        };
        let sink = NativeSink {
            app: app.clone(),
            enabled: self.enabled.clone(),
            generation: self.generation.clone(),
            observed_generation,
            registry: self.registry.clone(),
        };
        let filter = self.filter.clone();
        let poll_sink = sink.clone();
        *connection = Some(Connection {
            origin,
            task: tauri::async_runtime::spawn(async move {
                run_notifications(Arc::new(transport), filter, Arc::new(sink)).await;
            }),
            remote_request_poll: tauri::async_runtime::spawn(async move {
                loop {
                    poll_sink.desktop_update_requested();
                    tokio::time::sleep(REMOTE_REQUEST_POLL_INTERVAL).await;
                }
            }),
        });
    }

    pub fn stop(&self) {
        let mut connection = self.connection.lock().expect("notification connection lock");
        self.generation.fetch_add(1, Ordering::SeqCst);
        if let Some(connection) = connection.take() {
            connection.task.abort();
            connection.remote_request_poll.abort();
        }
    }
}

#[derive(Clone)]
struct NativeSink {
    app: AppHandle,
    enabled: Arc<AtomicBool>,
    generation: Arc<AtomicU64>,
    observed_generation: u64,
    registry: Arc<Mutex<NotificationRegistry>>,
}

impl NotificationSink for NativeSink {
    fn registry(&self) -> &Mutex<NotificationRegistry> {
        &self.registry
    }

    fn presentation_generation(&self) -> u64 {
        crate::instance_client::epoch(&self.app).unwrap_or(0)
    }

    fn gate(&self) -> NotificationGate {
        let window = self.app.get_webview_window(MAIN_WINDOW);
        NotificationGate {
            enabled: self.enabled.load(Ordering::SeqCst)
                && self.generation.load(Ordering::SeqCst) == self.observed_generation
                && crate::instance_client::local_selected(&self.app)
                && crate::instance_client::foreground_local(&self.app),
            focused: window
                .as_ref()
                .is_some_and(|window| window.is_focused().unwrap_or(true)),
            visible: window
                .as_ref()
                .is_some_and(|window| window.is_visible().unwrap_or(true)),
        }
    }

    fn deliver(
        &self,
        intent: NotificationIntent,
        source: Option<String>,
        presentation_generation: u64,
        receipt: Arc<NotificationReceipt>,
    ) {
        let sink = self.clone();
        let cleanup = receipt.clone();
        let queued = self.app.run_on_main_thread(move || {
            if receipt.retired() || sink.presentation_generation() != presentation_generation || !sink.gate().allows() {
                sink.registry
                    .lock()
                    .expect("notification registry lock")
                    .forget(receipt.id());
                return;
            }
            let copy = native_catalog_for_locales(sys_locale::get_locales())
                .notifications
                .render(intent, source);
            if native::show(&sink.app, &receipt, copy, sink.registry.clone()).is_err() {
                sink.registry
                    .lock()
                    .expect("notification registry lock")
                    .forget(receipt.id());
                eprintln!("The desktop notification could not be submitted to the operating system.");
            } else {
                receipt.posted();
            }
        });
        if queued.is_err() {
            self.registry
                .lock()
                .expect("notification registry lock")
                .forget(cleanup.id());
        }
    }

    fn withdraw(&self, snapshot: NotificationSnapshot, unread: HashMap<String, u64>, presentation_generation: u64) {
        let sink = self.clone();
        let _ = self.app.run_on_main_thread(move || {
            if sink.generation.load(Ordering::SeqCst) != sink.observed_generation
                || sink.presentation_generation() != presentation_generation
            {
                return;
            }
            let receipts = {
                let mut registry = sink.registry.lock().expect("notification registry lock");
                registry.reconcile(snapshot, &unread);
                registry.posted_snapshot()
            };
            if receipts.is_empty() {
                return;
            }
            match native::withdraw(&sink.app, receipts) {
                Ok(removed) => {
                    let mut registry = sink.registry.lock().expect("notification registry lock");
                    for id in removed {
                        registry.forget(id);
                    }
                }
                Err(_) => eprintln!("The desktop notifications could not be withdrawn from the operating system."),
            }
        });
    }

    fn show_unread(&self, total: u64, presentation_generation: u64) {
        let sink = self.clone();
        let _ = self.app.run_on_main_thread(move || {
            // A total read for a connection that has since stopped or moved to
            // another Runtime describes nothing on screen.
            if sink.generation.load(Ordering::SeqCst) == sink.observed_generation
                && sink.presentation_generation() == presentation_generation
                && crate::instance_client::local_selected(&sink.app)
                && crate::instance_client::foreground_local(&sink.app)
            {
                paint_badge(&sink.app, total);
            }
        });
    }

    fn desktop_update_requested(&self) {
        if self.generation.load(Ordering::SeqCst) == self.observed_generation {
            updater::handle_remote_update_request(&self.app);
            updater::handle_control_request(&self.app);
        }
    }
}

/// The Badging API's rule: zero clears. macOS would otherwise draw a literal
/// "0". Windows has no badge count, so the call is a no-op error there.
fn badge_count(total: u64) -> Option<i64> {
    (total > 0).then(|| i64::try_from(total).unwrap_or(i64::MAX))
}

fn paint_badge(app: &AppHandle, total: u64) {
    if let Some(window) = app.get_webview_window(MAIN_WINDOW) {
        let _ = window.set_badge_count(badge_count(total));
    }
}

pub fn start(app: &AppHandle, origin: LoopbackOrigin) {
    app.state::<Notifications>().start(app, origin);
}

/// Presentation changes retire queued output, not the local control transport.
pub fn clear_badge(app: &AppHandle) {
    let handle = app.clone();
    let _ = app.run_on_main_thread(move || paint_badge(&handle, 0));
}

fn dispatch(window: &WebviewWindow, enabled: bool) {
    let script = format!(
        "window.dispatchEvent(new CustomEvent({}, {{ detail: {} }}));",
        serde_json::to_string(SETTINGS_EVENT).expect("event name JSON"),
        serde_json::json!({ "enabled": enabled }),
    );
    let _ = window.eval(script);
}

fn notify_workbench(app: &AppHandle, enabled: bool) {
    if let Some(window) = app.get_webview_window(MAIN_WINDOW).filter(pet::is_workbench) {
        dispatch(&window, enabled);
    }
}

fn sync_native_menu(app: &AppHandle, enabled: bool) {
    if let Some(menus) = app.try_state::<crate::NativeMenus>() {
        if menus.notifications.set_checked(enabled).is_err() {
            eprintln!("failed to refresh the desktop notification menu");
        }
    }
}

fn publish_enabled(app: &AppHandle, enabled: bool) {
    sync_native_menu(app, enabled);
    notify_workbench(app, enabled);
}

pub fn toggle(app: &AppHandle) {
    let notifications = app.state::<Notifications>();
    match notifications.toggle() {
        Ok(enabled) => publish_enabled(app, enabled),
        Err(error) => {
            eprintln!("{error}");
            sync_native_menu(app, notifications.enabled());
        }
    }
}

pub fn set_enabled(app: &AppHandle, enabled: bool) -> Result<bool, String> {
    let saved = app.state::<Notifications>().set_enabled(enabled)?;
    publish_enabled(app, saved);
    Ok(saved)
}

fn ensure_workbench(window: &WebviewWindow) -> Result<(), String> {
    if pet::is_workbench(window) {
        Ok(())
    } else {
        Err("This command is only available to the Avibe Workbench.".to_owned())
    }
}

#[tauri::command]
pub fn desktop_notifications_state(window: WebviewWindow, app: AppHandle) -> Result<bool, String> {
    ensure_workbench(&window)?;
    Ok(app.state::<Notifications>().enabled())
}

#[tauri::command]
pub fn desktop_notifications_set_enabled(window: WebviewWindow, app: AppHandle, enabled: bool) -> Result<bool, String> {
    ensure_workbench(&window)?;
    set_enabled(&app, enabled)
}

/// A stopped connection leaves no count it can vouch for. The clear queues
/// behind any paint already dispatched, which the generation bump voids.
pub fn stop(app: &AppHandle) {
    app.state::<Notifications>().stop();
    clear_badge(app);
}

#[cfg(test)]
mod tests {
    use super::*;

    struct TestDirectory(PathBuf);

    impl TestDirectory {
        fn new() -> Self {
            static NEXT: AtomicU64 = AtomicU64::new(0);
            let directory = std::env::temp_dir().join(format!(
                "avibe-notification-preference-{}-{}",
                std::process::id(),
                NEXT.fetch_add(1, Ordering::SeqCst)
            ));
            std::fs::create_dir(&directory).unwrap();
            Self(directory)
        }
    }

    impl Drop for TestDirectory {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    #[test]
    fn zero_clears_the_badge_and_totals_saturate() {
        assert_eq!(badge_count(0), None);
        assert_eq!(badge_count(1), Some(1));
        assert_eq!(badge_count(42), Some(42));
        assert_eq!(badge_count(u64::MAX), Some(i64::MAX));
    }

    #[test]
    fn preference_defaults_on_and_persists_the_tray_choice_across_restarts() {
        let directory = TestDirectory::new();
        let path = directory.0.join("notifications.json");
        let state = Notifications::new(path.clone());
        assert!(state.enabled());
        state.set_enabled(false).unwrap();
        assert!(!state.enabled());
        let restarted = Notifications::new(path.clone());
        assert!(!restarted.enabled());
        restarted.set_enabled(true).unwrap();
        assert!(Notifications::new(path).enabled());
    }

    #[test]
    fn unreadable_preferences_fail_closed_and_failed_writes_preserve_the_choice() {
        let directory = TestDirectory::new();
        let path = directory.0.join("notifications.json");
        std::fs::write(&path, "invalid").unwrap();
        assert!(!Notifications::new(path.clone()).enabled());
        std::fs::remove_file(&path).unwrap();
        let state = Notifications::new(path.clone());
        std::fs::create_dir(&path).unwrap();
        assert!(state.set_enabled(false).is_err());
        assert!(state.enabled());
        assert!(!path.with_extension("tmp").exists());
    }

    #[test]
    fn every_intent_uses_only_native_catalog_copy_in_both_locales() {
        for locale in ["en-US", "zh-CN"] {
            let catalog = native_catalog_for_locales([locale.to_owned()]).notifications;
            assert!(!catalog.toggle.is_empty());
            for intent in [
                NotificationIntent::ApprovalRequested,
                NotificationIntent::RunSucceeded,
                NotificationIntent::RunFailed,
                NotificationIntent::RunCanceled,
            ] {
                let copy = catalog.copy(intent);
                assert!(!copy.title.is_empty() && !copy.body.is_empty());
                assert!(!copy.body.contains("{{"));
                let sourced = catalog.render(intent, Some("avibe-app · 发布检查".into()));
                assert_eq!(sourced.title, "avibe-app · 发布检查");
                assert_eq!(sourced.body, copy.title);
                let generic = catalog.render(intent, None);
                assert_eq!((generic.title, generic.body), (copy.title, copy.body));
            }
        }
    }

    #[test]
    fn stopping_invalidates_dispatched_intents_and_aborts_the_connection_task() {
        let directory = TestDirectory::new();
        let state = Notifications::new(directory.0.join("notifications.json"));
        let task = tauri::async_runtime::spawn(std::future::pending());
        *state.connection.lock().unwrap() = Some(Connection {
            origin: LoopbackOrigin::parse("http://127.0.0.1:5123").unwrap(),
            task,
            remote_request_poll: tauri::async_runtime::spawn(std::future::pending()),
        });
        let previous = state.generation.load(Ordering::SeqCst);
        state.stop();
        assert!(state.connection.lock().unwrap().is_none());
        assert_ne!(state.generation.load(Ordering::SeqCst), previous);
        assert!(state.enabled());
        state.stop();
        assert!(state.connection.lock().unwrap().is_none());
    }
}
