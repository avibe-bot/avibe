use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};

use avibe_runtime_host::notifications::{
    run_notifications, HttpNotificationTransport, NotificationFilter, NotificationGate, NotificationIntent,
    NotificationSink,
};
use avibe_runtime_host::LoopbackOrigin;
use serde::{Deserialize, Serialize};
use tauri::{AppHandle, Manager};
use tauri_plugin_notification::NotificationExt;

use crate::{native_catalog_for_locales, MAIN_WINDOW};

pub const MENU_ID: &str = "notifications";

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
}

pub struct Notifications {
    preference_path: PathBuf,
    enabled: Arc<AtomicBool>,
    generation: Arc<AtomicU64>,
    connection: Mutex<Option<Connection>>,
    filter: Arc<Mutex<NotificationFilter>>,
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
        }
    }

    pub fn enabled(&self) -> bool {
        self.enabled.load(Ordering::SeqCst)
    }

    pub fn toggle(&self) {
        let enabled = !self.enabled();
        if write_preference(&self.preference_path, enabled).is_ok() {
            self.enabled.store(enabled, Ordering::SeqCst);
        } else {
            eprintln!("The desktop notification preference could not be saved.");
        }
    }

    pub fn start(&self, app: &AppHandle, origin: LoopbackOrigin) {
        let mut connection = self.connection.lock().expect("notification connection lock");
        if connection.as_ref().is_some_and(|current| current.origin == origin) {
            return;
        }
        let observed_generation = self.generation.fetch_add(1, Ordering::SeqCst) + 1;
        if let Some(previous) = connection.take() {
            previous.task.abort();
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
        };
        let filter = self.filter.clone();
        *connection = Some(Connection {
            origin,
            task: tauri::async_runtime::spawn(async move {
                run_notifications(&transport, filter, &sink).await;
            }),
        });
    }

    pub fn stop(&self) {
        let mut connection = self.connection.lock().expect("notification connection lock");
        self.generation.fetch_add(1, Ordering::SeqCst);
        if let Some(connection) = connection.take() {
            connection.task.abort();
        }
    }
}

#[derive(Clone)]
struct NativeSink {
    app: AppHandle,
    enabled: Arc<AtomicBool>,
    generation: Arc<AtomicU64>,
    observed_generation: u64,
}

impl NotificationSink for NativeSink {
    fn gate(&self) -> NotificationGate {
        let window = self.app.get_webview_window(MAIN_WINDOW);
        NotificationGate {
            enabled: self.enabled.load(Ordering::SeqCst)
                && self.generation.load(Ordering::SeqCst) == self.observed_generation,
            focused: window
                .as_ref()
                .is_some_and(|window| window.is_focused().unwrap_or(true)),
            visible: window
                .as_ref()
                .is_some_and(|window| window.is_visible().unwrap_or(true)),
        }
    }

    fn deliver(&self, intent: NotificationIntent) {
        let sink = self.clone();
        let _ = self.app.run_on_main_thread(move || {
            if !sink.gate().allows() {
                return;
            }
            let copy = native_catalog_for_locales(sys_locale::get_locales())
                .notifications
                .copy(intent);
            if sink
                .app
                .notification()
                .builder()
                .title(copy.title)
                .body(copy.body)
                .show()
                .is_err()
            {
                eprintln!("The desktop notification could not be submitted to the operating system.");
            }
        });
    }
}

pub fn start(app: &AppHandle, origin: LoopbackOrigin) {
    app.state::<Notifications>().start(app, origin);
}

pub fn stop(app: &AppHandle) {
    app.state::<Notifications>().stop();
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
    fn preference_defaults_on_and_persists_the_tray_choice_across_restarts() {
        let directory = TestDirectory::new();
        let path = directory.0.join("notifications.json");
        let state = Notifications::new(path.clone());
        assert!(state.enabled());
        state.toggle();
        assert!(!state.enabled());
        let restarted = Notifications::new(path.clone());
        assert!(!restarted.enabled());
        restarted.toggle();
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
        state.toggle();
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
