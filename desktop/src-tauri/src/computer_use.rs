//! macOS adapter for the desktop-owned computer-use lifecycle.

use std::ffi::c_void;
use std::fs::File;
use std::io::{self, Read};
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::ptr;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use avibe_runtime_host::computer_use::{
    CapabilityCache, CapabilityProbe, CapabilityVerdict, ComputerUseLifecycle, ComputerUsePhase, ComputerUseRecord,
    ComputerUseShellLock, ComputerUseStateStore, Grants, HealthResult, LifecycleDirective, RuntimeSupport,
    StoredComputerUseState, ToolSnapshot, COMPUTER_USE_DRIVER_VERSION, COMPUTER_USE_LOCK_FILE,
    COMPUTER_USE_SCHEMA_VERSION, COMPUTER_USE_SOCKET_FILE, COMPUTER_USE_STATE_FILE, COMPUTER_USE_TOOL_SNAPSHOT_SHA256,
};
use avibe_runtime_host::{BootstrapLog, LoopbackOrigin, BOOTSTRAP_LOG_NAME};
use block2::RcBlock;
use objc2::rc::{Allocated, Retained};
use objc2::runtime::{AnyClass, AnyObject, NSObjectProtocol, ProtocolObject};
use objc2::{msg_send, sel, MainThreadMarker};
use objc2_app_kit::NSApplicationDidBecomeActiveNotification;
use objc2_foundation::{
    NSArray, NSError, NSNotification, NSNotificationCenter, NSOperationQueue, NSPoint, NSRect, NSSize,
};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use tauri::plugin::{Builder as PluginBuilder, TauriPlugin};
use tauri::{AppHandle, Manager, RunEvent};
use tauri_plugin_dialog::{DialogExt, MessageDialogKind};
use tokio::io::{AsyncBufReadExt, AsyncWrite, AsyncWriteExt, BufReader};
use tokio::process::{Child, ChildStdin, Command};
use tokio::sync::{mpsc, oneshot};
use tokio::time::{sleep, timeout, timeout_at};

const STATE_DIR_ENV: &str = "AVIBE_COMPUTER_USE_STATE_DIR";
const DRIVER_PATH_ENV: &str = "AVIBE_COMPUTER_USE_DRIVER_PATH";
const POLICY_PATH_ENV: &str = "AVIBE_COMPUTER_USE_POLICY_PATH";
const SNAPSHOT_PATH_ENV: &str = "AVIBE_COMPUTER_USE_SNAPSHOT_PATH";
const TICK_INTERVAL: Duration = Duration::from_secs(5);
const CAPABILITY_TIMEOUT: Duration = Duration::from_secs(2);
const ENDPOINT_RECLAIM_TIMEOUT: Duration = Duration::from_secs(2);
const PROCESS_STOP_TIMEOUT: Duration = Duration::from_secs(3);
const HEALTH_TIMEOUT: Duration = Duration::from_secs(5);
const PERMISSION_REQUEST_TIMEOUT: Duration = Duration::from_secs(5);
const PERMISSION_CHILD_TIMEOUT: Duration = Duration::from_secs(10);
const START_BACKOFF: Duration = Duration::from_secs(1);
const COMPUTER_USE_SCHEMA: u64 = COMPUTER_USE_SCHEMA_VERSION as u64;

#[link(name = "ApplicationServices", kind = "framework")]
unsafe extern "C" {
    fn AXIsProcessTrusted() -> u8;
    fn AXIsProcessTrustedWithOptions(options: *const c_void) -> u8;
    static kAXTrustedCheckOptionPrompt: *const c_void;
}

#[link(name = "CoreFoundation", kind = "framework")]
unsafe extern "C" {
    fn CFDictionaryCreate(
        allocator: *const c_void,
        keys: *const *const c_void,
        values: *const *const c_void,
        count: isize,
        key_callbacks: *const c_void,
        value_callbacks: *const c_void,
    ) -> *const c_void;
    fn CFRelease(value: *const c_void);
    static kCFBooleanTrue: *const c_void;
}

#[link(name = "CoreGraphics", kind = "framework")]
unsafe extern "C" {
    fn CGPreflightScreenCaptureAccess() -> bool;
    fn CGRequestScreenCaptureAccess() -> bool;
}

#[link(name = "ScreenCaptureKit", kind = "framework")]
unsafe extern "C" {}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct MenuView {
    pub enabled: bool,
    pub phase: ComputerUsePhase,
    pub reason: Option<String>,
    pub invalid_state: bool,
    pub asset_error: bool,
    pub initialization_error: bool,
    pub save_failed: bool,
    pub runtime_refused: bool,
}

impl Default for MenuView {
    fn default() -> Self {
        Self {
            enabled: false,
            phase: ComputerUsePhase::Off,
            reason: None,
            invalid_state: false,
            asset_error: false,
            initialization_error: false,
            save_failed: false,
            runtime_refused: false,
        }
    }
}

enum Event {
    Toggle,
    RuntimeReady { origin: LoopbackOrigin, adoption: bool },
    RuntimeLost,
    Activated,
    RetryStart { id: u64 },
    PermissionProbeFinished { id: u64, result: PermissionChildRun },
    Shutdown(oneshot::Sender<()>),
}

pub(crate) struct Controller {
    sender: mpsc::UnboundedSender<Event>,
    view: Arc<Mutex<MenuView>>,
    shutdown_requested: Arc<AtomicBool>,
    shutdown_started: AtomicBool,
}

impl Controller {
    pub(crate) fn start(app: &AppHandle) -> Self {
        let (sender, receiver) = mpsc::unbounded_channel();
        let view = Arc::new(Mutex::new(MenuView::default()));
        let shutdown_requested = Arc::new(AtomicBool::new(false));
        match RuntimeState::new(app.clone(), view.clone(), sender.clone(), shutdown_requested.clone()) {
            Ok(runtime) => {
                tauri::async_runtime::spawn(runtime.run(receiver));
            }
            Err(error) => {
                eprintln!("warning: Computer Use initialization degraded: {error}");
                if let Ok(mut current) = view.lock() {
                    current.initialization_error = true;
                }
            }
        }
        Self {
            sender,
            view,
            shutdown_requested,
            shutdown_started: AtomicBool::new(false),
        }
    }

    pub(crate) fn view(&self) -> MenuView {
        self.view.lock().expect("computer-use menu state").clone()
    }

    pub(crate) fn toggle(&self) {
        let _ = self.sender.send(Event::Toggle);
    }

    pub(crate) fn runtime_ready(&self, origin: LoopbackOrigin, adoption: bool) {
        let _ = self.sender.send(Event::RuntimeReady { origin, adoption });
    }

    pub(crate) fn runtime_lost(&self) {
        let _ = self.sender.send(Event::RuntimeLost);
    }

    pub(crate) fn activated(&self) {
        let _ = self.sender.send(Event::Activated);
    }

    pub(crate) async fn shutdown(&self) {
        if self.shutdown_started.swap(true, Ordering::SeqCst) {
            return;
        }
        self.shutdown_requested.store(true, Ordering::SeqCst);
        let (sender, receiver) = oneshot::channel();
        if self.sender.send(Event::Shutdown(sender)).is_ok() {
            let _ = receiver.await;
        }
    }
}

struct Paths {
    state_dir: PathBuf,
    state_file: PathBuf,
    socket: PathBuf,
    driver: PathBuf,
    policy: PathBuf,
    snapshot: PathBuf,
}

impl Paths {
    fn resolve(app: &AppHandle) -> Result<Self, Box<dyn std::error::Error>> {
        let state_dir = configured_path(app.path().app_data_dir()?, STATE_DIR_ENV);
        let executable = std::env::current_exe()?;
        let resource_dir = app.path().resource_dir()?;
        let driver = configured_path(bundled_driver_path(&executable), DRIVER_PATH_ENV);
        let policy = configured_path(resource_dir.join("computer-use/policy.yaml"), POLICY_PATH_ENV);
        let snapshot = configured_path(resource_dir.join("computer-use/tools-v0.31.0.json"), SNAPSHOT_PATH_ENV);
        Ok(Self {
            state_file: state_dir.join(COMPUTER_USE_STATE_FILE),
            socket: state_dir.join(COMPUTER_USE_SOCKET_FILE),
            state_dir,
            driver,
            policy,
            snapshot,
        })
    }

    fn assets_valid(&self) -> bool {
        self.driver.is_file()
            && self.policy.is_file()
            && sha256_file(&self.snapshot).as_deref() == Some(COMPUTER_USE_TOOL_SNAPSHOT_SHA256)
    }
}

fn configured_path(default: PathBuf, variable: &str) -> PathBuf {
    configured_path_with_value(default, std::env::var_os(variable), test_path_overrides_enabled())
}

fn configured_path_with_value(
    default: PathBuf,
    override_value: Option<std::ffi::OsString>,
    allow_override: bool,
) -> PathBuf {
    if allow_override {
        override_value.map(PathBuf::from).unwrap_or(default)
    } else {
        default
    }
}

const fn test_path_overrides_enabled() -> bool {
    cfg!(any(debug_assertions, test, feature = "computer-use-test-overrides"))
}

fn bundled_driver_path(executable: &Path) -> PathBuf {
    executable
        .parent()
        .and_then(Path::parent)
        .map(|contents| contents.join("Helpers/cua-driver"))
        .unwrap_or_else(|| PathBuf::from("cua-driver"))
}

struct Daemon {
    child: Child,
    stdin: Option<ChildStdin>,
}

struct PermissionProbe {
    id: u64,
    cancel: oneshot::Sender<()>,
    task: tauri::async_runtime::JoinHandle<()>,
}

struct RuntimeState {
    app: AppHandle,
    events: mpsc::UnboundedSender<Event>,
    view: Arc<Mutex<MenuView>>,
    diagnostics: BootstrapLog,
    paths: Paths,
    store: ComputerUseStateStore,
    _shell_lock: ComputerUseShellLock,
    shutdown_requested: Arc<AtomicBool>,
    lifecycle: ComputerUseLifecycle,
    capabilities: CapabilityCache,
    origin: Option<LoopbackOrigin>,
    daemon: Option<Daemon>,
    permission_probe: Option<PermissionProbe>,
    next_permission_probe_id: u64,
    start_retry_id: u64,
    instance_id: String,
    generation: u64,
    host_bundle_id: String,
    assets_valid: bool,
    pending_write: Option<ComputerUseRecord>,
    started_at: Instant,
}

impl RuntimeState {
    fn new(
        app: AppHandle,
        view: Arc<Mutex<MenuView>>,
        events: mpsc::UnboundedSender<Event>,
        shutdown_requested: Arc<AtomicBool>,
    ) -> Result<Self, Box<dyn std::error::Error>> {
        let paths = Paths::resolve(&app)?;
        let diagnostics = BootstrapLog::at(app.path().app_local_data_dir()?.join(BOOTSTRAP_LOG_NAME));
        std::fs::create_dir_all(&paths.state_dir)?;
        let shell_lock = ComputerUseShellLock::acquire(&paths.state_dir.join(COMPUTER_USE_LOCK_FILE))?;
        let store = ComputerUseStateStore::new(paths.state_file.clone());
        let stored = store.read();
        let lifecycle = match &stored {
            StoredComputerUseState::Current(record) => ComputerUseLifecycle::from_record(record),
            StoredComputerUseState::Missing | StoredComputerUseState::Invalid(_) => ComputerUseLifecycle::off(),
        };
        let assets_valid = paths.assets_valid();
        let mut runtime = Self {
            app,
            events,
            view,
            diagnostics,
            paths,
            store,
            _shell_lock: shell_lock,
            shutdown_requested,
            lifecycle,
            capabilities: CapabilityCache::default(),
            origin: None,
            daemon: None,
            permission_probe: None,
            next_permission_probe_id: 0,
            start_retry_id: 0,
            instance_id: random_instance_id()?,
            generation: 0,
            host_bundle_id: String::new(),
            assets_valid,
            pending_write: None,
            started_at: Instant::now(),
        };
        runtime.host_bundle_id = runtime.app.config().identifier.clone();
        match stored {
            StoredComputerUseState::Missing => {}
            StoredComputerUseState::Invalid(_) => {
                runtime.update_view(|view| view.invalid_state = true);
            }
            StoredComputerUseState::Current(record) if !record.enabled => {
                let _ = runtime.write_current_state();
            }
            StoredComputerUseState::Current(record) if record.state == ComputerUsePhase::Error => {}
            StoredComputerUseState::Current(_) => {
                let _ = runtime.write_current_state();
            }
        }
        runtime.publish_view();
        Ok(runtime)
    }

    async fn run(mut self, mut receiver: mpsc::UnboundedReceiver<Event>) {
        let mut tick = tokio::time::interval(TICK_INTERVAL);
        tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
        tick.tick().await;
        loop {
            tokio::select! {
                _ = tick.tick() => self.tick().await,
                event = receiver.recv() => {
                    let Some(event) = event else {
                        self.shutdown().await;
                        break;
                    };
                    match event {
                        Event::Toggle => self.toggle().await,
                        Event::RuntimeReady { origin, adoption } => {
                            self.runtime_ready(origin, adoption).await;
                        }
                        Event::RuntimeLost => self.runtime_lost().await,
                        Event::Activated => self.activated().await,
                        Event::RetryStart { id } => self.retry_start(id).await,
                        Event::PermissionProbeFinished { id, result } => {
                            self.permission_probe_finished(id, result);
                        }
                        Event::Shutdown(reply) => {
                            self.shutdown().await;
                            let RuntimeState {
                                _shell_lock: shell_lock,
                                ..
                            } = self;
                            drop(shell_lock);
                            let _ = reply.send(());
                            return;
                        }
                    }
                }
            }
        }
    }

    async fn toggle(&mut self) {
        self.update_view(|view| {
            view.runtime_refused = false;
            view.invalid_state = false;
        });
        if self.lifecycle.enabled() {
            self.invalidate_start_retry();
            let directive = self.lifecycle.toggle_off();
            self.apply(directive).await;
            return;
        }
        self.invalidate_start_retry();
        if !self.assets_valid {
            self.update_view(|view| view.asset_error = true);
            self.publish_view();
            return;
        }
        if self.capabilities.current() != RuntimeSupport::Supported {
            self.update_view(|view| view.runtime_refused = true);
            self.publish_view();
            return;
        }
        let grants = request_permissions(&self.app, &self.diagnostics).await;
        if grants.missing_reason().is_some() {
            open_missing_permission_settings(&self.app, grants);
        }
        let directive = self.lifecycle.toggle_on(self.capabilities.current(), grants);
        self.apply(directive).await;
    }

    async fn runtime_ready(&mut self, origin: LoopbackOrigin, adoption: bool) {
        if adoption {
            self.invalidate_start_retry();
            self.capabilities.begin_adoption();
            let directive = self.lifecycle.capabilities(RuntimeSupport::Unknown, silent_grants());
            self.apply(directive).await;
        }
        self.origin = Some(origin.clone());
        let probe = probe_capabilities(&origin).await;
        let support = self.capabilities.observe(probe);
        if support == RuntimeSupport::Supported {
            self.update_view(|view| view.runtime_refused = false);
        }
        let grants = if self.lifecycle.phase() == ComputerUsePhase::NeedsRuntime && support == RuntimeSupport::Supported
        {
            silent_grants()
        } else {
            Grants {
                accessibility: false,
                screen_recording: false,
            }
        };
        let directive = self.lifecycle.capabilities(support, grants);
        self.apply(directive).await;
    }

    async fn runtime_lost(&mut self) {
        self.invalidate_start_retry();
        self.origin = None;
        self.capabilities.begin_adoption();
        let directive = self.lifecycle.capabilities(RuntimeSupport::Unknown, silent_grants());
        self.apply(directive).await;
    }

    async fn activated(&mut self) {
        if self.capabilities.current() != RuntimeSupport::Supported {
            return;
        }
        let directive = self.lifecycle.observe_permissions(silent_grants());
        self.apply(directive).await;
    }

    async fn tick(&mut self) {
        if self.shutdown_requested.load(Ordering::SeqCst) {
            return;
        }
        if let Some(record) = self.pending_write.clone() {
            if self.store.write(&record).is_ok() {
                self.pending_write = None;
                self.update_view(|view| view.save_failed = false);
                self.publish_view();
            }
        }
        if !self.lifecycle.enabled()
            || self.lifecycle.phase() == ComputerUsePhase::Error
            || self.capabilities.current() != RuntimeSupport::Supported
        {
            return;
        }
        match self.lifecycle.phase() {
            ComputerUsePhase::NeedsPermission => {
                let directive = self.lifecycle.observe_permissions(silent_grants());
                self.apply(directive).await;
            }
            ComputerUsePhase::Ready => self.ready_tick().await,
            _ => {}
        }
    }

    async fn retry_start(&mut self, id: u64) {
        if self.shutdown_requested.load(Ordering::SeqCst) || !retry_is_current(id, self.start_retry_id, &self.lifecycle)
        {
            return;
        }
        self.apply(LifecycleDirective {
            spawn_daemon: true,
            ..LifecycleDirective::default()
        })
        .await;
    }

    async fn ready_tick(&mut self) {
        let exited = match self.daemon.as_mut() {
            Some(daemon) => daemon.child.try_wait().ok().flatten().is_some(),
            None => true,
        };
        if exited {
            let directive = self.lifecycle.daemon_failed("daemon_exited", self.elapsed());
            self.apply(directive).await;
            return;
        }
        if !socket_accepts(&self.paths.socket).await {
            let directive = self
                .lifecycle
                .ready_heartbeat(HealthResult::Unhealthy("socket_unreachable".to_owned()), self.elapsed());
            self.apply(directive).await;
            return;
        }
        let health = health_check(&self.paths.driver, &self.paths.socket, &self.host_bundle_id, false).await;
        let directive = self.lifecycle.ready_heartbeat(health, self.elapsed());
        self.apply(directive).await;
    }

    async fn apply(&mut self, mut directive: LifecycleDirective) {
        let spawn_daemon = directive.spawn_daemon;
        directive.spawn_daemon = false;
        if spawn_daemon {
            self.generation = self.generation.saturating_add(1);
        }
        if !self.apply_state(directive).await {
            return;
        }
        if !spawn_daemon {
            return;
        }

        let health = self.start_once().await;
        if self.shutdown_requested.load(Ordering::SeqCst) {
            self.stop_daemon().await;
            return;
        }
        let mut follow_up = self.lifecycle.startup_health(health, self.elapsed());
        let retry = follow_up.spawn_daemon;
        follow_up.spawn_daemon = false;
        if !self.apply_state(follow_up).await {
            return;
        }
        if retry {
            self.schedule_start_retry();
        }
    }

    async fn apply_state(&mut self, directive: LifecycleDirective) -> bool {
        let state_written = if directive.write_state {
            self.write_current_state()
        } else {
            true
        };
        if directive.stop_daemon {
            self.stop_daemon().await;
        }
        if directive.stop_permission_probe {
            self.stop_permission_probe().await;
        }
        if !state_written {
            if self.lifecycle.enabled()
                && matches!(
                    self.lifecycle.phase(),
                    ComputerUsePhase::Starting | ComputerUsePhase::Ready
                )
            {
                self.lifecycle.set_error("state_unwritable");
                self.pending_write = Some(self.current_record());
                self.stop_permission_probe().await;
                self.stop_daemon().await;
            }
            self.publish_view();
            return false;
        }
        self.publish_view();
        if directive.spawn_permission_probe {
            self.start_permission_probe().await;
        }
        true
    }

    fn invalidate_start_retry(&mut self) {
        self.start_retry_id = self.start_retry_id.wrapping_add(1);
    }

    fn schedule_start_retry(&mut self) {
        let id = self.start_retry_id.wrapping_add(1);
        self.start_retry_id = id;
        let events = self.events.clone();
        tauri::async_runtime::spawn(async move {
            sleep(START_BACKOFF).await;
            let _ = events.send(Event::RetryStart { id });
        });
    }

    async fn start_once(&mut self) -> HealthResult {
        if self.shutdown_requested.load(Ordering::SeqCst) {
            return HealthResult::Unhealthy("shutdown_requested".to_owned());
        }
        self.stop_daemon().await;
        if let Err(reason) = self.reclaim_endpoint().await {
            return HealthResult::Unhealthy(reason);
        }
        let mut command = Command::new(&self.paths.driver);
        command
            .env_clear()
            .args([
                "serve",
                "--embedded",
                "--parent-liveness-stdio",
                "--no-permissions-gate",
                "--socket",
            ])
            .arg(&self.paths.socket)
            .args([
                "--host-bundle-id",
                &self.host_bundle_id,
                "--permission-mode",
                "standard",
            ])
            .env("CUA_DRIVER_EMBEDDED", "1")
            .env("CUA_DRIVER_EMBEDDED_HOST_PID", std::process::id().to_string())
            .env("CUA_DRIVER_MANAGED_POLICY_FILE", &self.paths.policy)
            .env("CUA_DRIVER_RS_TELEMETRY_ENABLED", "0")
            .env("CUA_DRIVER_RS_UPDATE_CHECK", "0")
            .stdin(Stdio::piped())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .kill_on_drop(true);
        let mut child = match command.spawn() {
            Ok(child) => child,
            Err(_) => {
                return HealthResult::Unhealthy("spawn_failed".to_owned());
            }
        };
        let stdin = child.stdin.take();
        self.daemon = Some(Daemon { child, stdin });

        timeout(HEALTH_TIMEOUT, async {
            loop {
                if self.shutdown_requested.load(Ordering::SeqCst) {
                    return HealthResult::Unhealthy("shutdown_requested".to_owned());
                }
                if self
                    .daemon
                    .as_mut()
                    .and_then(|daemon| daemon.child.try_wait().ok().flatten())
                    .is_some()
                {
                    return HealthResult::Unhealthy("daemon_exited".to_owned());
                }
                if socket_accepts(&self.paths.socket).await {
                    break;
                }
                sleep(Duration::from_millis(100)).await;
            }
            health_check_inner(&self.paths.driver, &self.paths.socket, &self.host_bundle_id, true).await
        })
        .await
        .unwrap_or_else(|_| HealthResult::Unhealthy("health_timeout".to_owned()))
    }

    async fn reclaim_endpoint(&self) -> Result<(), String> {
        if socket_accepts(&self.paths.socket).await {
            let deadline = Instant::now() + ENDPOINT_RECLAIM_TIMEOUT;
            while Instant::now() < deadline {
                sleep(Duration::from_millis(100)).await;
                if !socket_accepts(&self.paths.socket).await {
                    break;
                }
            }
        }
        if socket_accepts(&self.paths.socket).await {
            return Err("endpoint_busy".to_owned());
        }
        match std::fs::remove_file(&self.paths.socket) {
            Ok(()) => {}
            Err(error) if error.kind() == io::ErrorKind::NotFound => {}
            Err(_) => return Err("endpoint_unremovable".to_owned()),
        }
        Ok(())
    }

    async fn stop_daemon(&mut self) {
        let Some(mut daemon) = self.daemon.take() else {
            return;
        };
        drop(daemon.stdin.take());
        if timeout(PROCESS_STOP_TIMEOUT, daemon.child.wait()).await.is_err() {
            let _ = daemon.child.kill().await;
            let _ = daemon.child.wait().await;
        }
        let _ = std::fs::remove_file(&self.paths.socket);
    }

    async fn start_permission_probe(&mut self) {
        self.stop_permission_probe().await;
        self.next_permission_probe_id = self.next_permission_probe_id.saturating_add(1);
        let id = self.next_permission_probe_id;
        let (cancel, cancelled) = oneshot::channel();
        let events = self.events.clone();
        let driver = self.paths.driver.clone();
        let state_dir = self.paths.state_dir.clone();
        let policy = self.paths.policy.clone();
        let host_bundle_id = self.host_bundle_id.clone();
        let task = tauri::async_runtime::spawn(async move {
            let result = run_permission_child(
                &driver,
                &state_dir,
                &policy,
                &host_bundle_id,
                PERMISSION_CHILD_TIMEOUT,
                cancelled,
            )
            .await;
            let _ = events.send(Event::PermissionProbeFinished { id, result });
        });
        self.permission_probe = Some(PermissionProbe { id, cancel, task });
    }

    async fn stop_permission_probe(&mut self) {
        if let Some(probe) = self.permission_probe.take() {
            let _ = probe.cancel.send(());
            let _ = probe.task.await;
        }
    }

    fn permission_probe_finished(&mut self, id: u64, result: PermissionChildRun) {
        if self.permission_probe.as_ref().map(|probe| probe.id) != Some(id) {
            return;
        }
        self.permission_probe = None;
        let mut fields = vec![
            ("outcome", result.outcome.code().to_owned()),
            (
                "pid",
                result
                    .pid
                    .map(|pid| pid.to_string())
                    .unwrap_or_else(|| "not_started".to_owned()),
            ),
        ];
        if let Some(detail) = result.outcome.detail() {
            fields.push(("detail", detail));
        }
        self.diagnostics.record("computer-use.permission-child", &fields);
    }

    async fn shutdown(&mut self) {
        self.invalidate_start_retry();
        let directive = self.lifecycle.quit();
        if directive.write_state {
            let _ = self.write_current_state();
        }
        self.stop_permission_probe().await;
        self.stop_daemon().await;
    }

    fn current_record(&self) -> ComputerUseRecord {
        let ready = self.lifecycle.phase() == ComputerUsePhase::Ready;
        ComputerUseRecord {
            schema_version: COMPUTER_USE_SCHEMA_VERSION,
            enabled: self.lifecycle.enabled(),
            state: self.lifecycle.phase(),
            reason: self.lifecycle.reason().map(str::to_owned),
            shell_pid: std::process::id(),
            instance_id: self.instance_id.clone(),
            generation: self.generation,
            driver_version: COMPUTER_USE_DRIVER_VERSION.to_owned(),
            tool_snapshot: ToolSnapshot {
                path: self.paths.snapshot.clone(),
                sha256: COMPUTER_USE_TOOL_SNAPSHOT_SHA256.to_owned(),
            },
            socket_path: ready.then(|| self.paths.socket.clone()),
            proxy_executable: ready.then(|| self.paths.driver.clone()),
            host_bundle_id: ready.then(|| self.host_bundle_id.clone()),
        }
    }

    fn write_current_state(&mut self) -> bool {
        let record = self.current_record();
        match self.store.write(&record) {
            Ok(()) => {
                self.pending_write = None;
                self.update_view(|view| view.save_failed = false);
                true
            }
            Err(_) => {
                self.pending_write = Some(record);
                self.update_view(|view| view.save_failed = true);
                false
            }
        }
    }

    fn elapsed(&self) -> Duration {
        self.started_at.elapsed()
    }

    fn update_view(&self, change: impl FnOnce(&mut MenuView)) {
        if let Ok(mut view) = self.view.lock() {
            change(&mut view);
        }
    }

    fn publish_view(&self) {
        self.update_view(|view| {
            view.enabled = self.lifecycle.enabled();
            view.phase = self.lifecycle.phase();
            view.reason = self.lifecycle.reason().map(str::to_owned);
            view.asset_error = !self.assets_valid;
        });
        crate::refresh_computer_use_control(&self.app);
    }
}

fn retry_is_current(id: u64, current_id: u64, lifecycle: &ComputerUseLifecycle) -> bool {
    id == current_id && lifecycle.enabled() && lifecycle.phase() == ComputerUsePhase::Starting
}

async fn probe_capabilities(origin: &LoopbackOrigin) -> CapabilityProbe {
    let url = format!("{}/api/desktop/capabilities", origin.as_str().trim_end_matches('/'));
    let client = match reqwest::Client::builder()
        .timeout(CAPABILITY_TIMEOUT)
        .redirect(reqwest::redirect::Policy::none())
        .no_proxy()
        .build()
    {
        Ok(client) => client,
        Err(_) => {
            return CapabilityProbe {
                controller_id: None,
                verdict: CapabilityVerdict::Transient,
            };
        }
    };
    let response = match client.get(url).send().await {
        Ok(response) => response,
        Err(_) => {
            return CapabilityProbe {
                controller_id: None,
                verdict: CapabilityVerdict::Transient,
            };
        }
    };
    if response.status() == reqwest::StatusCode::NOT_FOUND {
        return CapabilityProbe {
            controller_id: Some("legacy".to_owned()),
            verdict: CapabilityVerdict::Unsupported,
        };
    }
    let successful = response.status().is_success();
    let payload = response.json::<Value>().await.ok();
    let controller_id = payload
        .as_ref()
        .and_then(|value| value.get("controller_id"))
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
        .map(str::to_owned);
    if !successful {
        return CapabilityProbe {
            controller_id,
            verdict: CapabilityVerdict::Transient,
        };
    }
    let schema = payload
        .as_ref()
        .and_then(|value| value.get("computer_use_schema"))
        .and_then(Value::as_u64);
    let verdict = classify_capability_payload(controller_id.as_deref(), schema);
    CapabilityProbe { controller_id, verdict }
}

fn classify_capability_payload(controller_id: Option<&str>, schema: Option<u64>) -> CapabilityVerdict {
    if controller_id.is_none() {
        return CapabilityVerdict::Transient;
    }
    match schema {
        Some(schema) if schema >= COMPUTER_USE_SCHEMA => CapabilityVerdict::Supported,
        Some(_) => CapabilityVerdict::Unsupported,
        None => CapabilityVerdict::Transient,
    }
}

async fn socket_accepts(path: &Path) -> bool {
    timeout(Duration::from_millis(300), tokio::net::UnixStream::connect(path))
        .await
        .is_ok_and(|result| result.is_ok())
}

#[derive(Clone, Debug, Eq, PartialEq)]
enum PermissionChildOutcome {
    Image,
    CaptureRejected,
    CaptureCompleted,
    SpawnFailed(String),
    TransportFailed,
    TimedOut,
    Cancelled,
}

impl PermissionChildOutcome {
    fn code(&self) -> &'static str {
        match self {
            Self::Image => "image",
            Self::CaptureRejected => "capture_rejected",
            Self::CaptureCompleted => "capture_completed",
            Self::SpawnFailed(_) => "spawn_failed",
            Self::TransportFailed => "transport_failed",
            Self::TimedOut => "timed_out",
            Self::Cancelled => "cancelled",
        }
    }

    fn detail(&self) -> Option<String> {
        match self {
            Self::SpawnFailed(error) => Some(error.clone()),
            _ => None,
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct PermissionChildRun {
    pid: Option<u32>,
    outcome: PermissionChildOutcome,
}

async fn run_permission_child(
    driver: &Path,
    state_dir: &Path,
    policy: &Path,
    host_bundle_id: &str,
    child_timeout: Duration,
    mut cancelled: oneshot::Receiver<()>,
) -> PermissionChildRun {
    run_permission_child_with_stop_timeout(
        driver,
        state_dir,
        policy,
        host_bundle_id,
        child_timeout,
        PROCESS_STOP_TIMEOUT,
        &mut cancelled,
    )
    .await
}

async fn run_permission_child_with_stop_timeout(
    driver: &Path,
    state_dir: &Path,
    policy: &Path,
    host_bundle_id: &str,
    child_timeout: Duration,
    stop_timeout: Duration,
    cancelled: &mut oneshot::Receiver<()>,
) -> PermissionChildRun {
    let private_root = state_dir.join("permission-probe");
    for directory in [
        private_root.clone(),
        private_root.join("config"),
        private_root.join("data"),
        private_root.join("state"),
        private_root.join("cache"),
        private_root.join("tmp"),
    ] {
        if let Err(error) = std::fs::create_dir_all(directory) {
            return PermissionChildRun {
                pid: None,
                outcome: PermissionChildOutcome::SpawnFailed(error.to_string()),
            };
        }
    }

    let mut command = Command::new(driver);
    command
        .env_clear()
        .args(["mcp", "--direct", "--embedded", "--no-overlay", "--host-bundle-id"])
        .arg(host_bundle_id)
        .env("HOME", &private_root)
        .env("USERPROFILE", &private_root)
        .env("XDG_CONFIG_HOME", private_root.join("config"))
        .env("XDG_DATA_HOME", private_root.join("data"))
        .env("XDG_STATE_HOME", private_root.join("state"))
        .env("XDG_CACHE_HOME", private_root.join("cache"))
        .env("TMPDIR", private_root.join("tmp"))
        .env("CUA_DRIVER_RS_HOME", private_root.join("driver"))
        .env("CUA_DRIVER_TELEMETRY_HOME", private_root.join("telemetry"))
        .env("CUA_DRIVER_EMBEDDED", "1")
        .env("CUA_DRIVER_EMBEDDED_HOST_PID", std::process::id().to_string())
        .env("CUA_DRIVER_MANAGED_POLICY_FILE", policy)
        .env("CUA_DRIVER_PERMISSION_MODE", "standard")
        .env("CUA_DRIVER_RS_TELEMETRY_ENABLED", "0")
        .env("CUA_DRIVER_RS_UPDATE_CHECK", "0")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .kill_on_drop(true);
    let mut child = match command.spawn() {
        Ok(child) => child,
        Err(error) => {
            return PermissionChildRun {
                pid: None,
                outcome: PermissionChildOutcome::SpawnFailed(error.to_string()),
            };
        }
    };
    let pid = child.id();
    let mut stdin = match child.stdin.take() {
        Some(stdin) => stdin,
        None => {
            stop_child(&mut child, stop_timeout).await;
            return PermissionChildRun {
                pid,
                outcome: PermissionChildOutcome::TransportFailed,
            };
        }
    };
    let stdout = match child.stdout.take() {
        Some(stdout) => stdout,
        None => {
            drop(stdin);
            stop_child(&mut child, stop_timeout).await;
            return PermissionChildRun {
                pid,
                outcome: PermissionChildOutcome::TransportFailed,
            };
        }
    };
    let mut stdout = BufReader::new(stdout);
    let outcome = {
        let operation = timeout(child_timeout, permission_child_capture(&mut stdin, &mut stdout));
        tokio::pin!(operation);
        tokio::select! {
            _ = cancelled => PermissionChildOutcome::Cancelled,
            result = &mut operation => match result {
                Ok(outcome) => outcome,
                Err(_) => PermissionChildOutcome::TimedOut,
            },
        }
    };
    drop(stdin);
    stop_child(&mut child, stop_timeout).await;
    PermissionChildRun { pid, outcome }
}

async fn permission_child_capture<W, R>(writer: &mut W, reader: &mut BufReader<R>) -> PermissionChildOutcome
where
    W: AsyncWrite + Unpin,
    R: tokio::io::AsyncRead + Unpin,
{
    let initialize = match json_rpc_request(
        writer,
        reader,
        1,
        "initialize",
        json!({
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "avibe-permission-bootstrap", "version": "1"},
        }),
    )
    .await
    {
        Ok(message) if message.get("error").is_none() => message,
        _ => return PermissionChildOutcome::TransportFailed,
    };
    let _ = initialize;
    if write_json_line(
        writer,
        &json!({
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
            "params": {},
        }),
    )
    .await
    .is_err()
    {
        return PermissionChildOutcome::TransportFailed;
    }
    let capture = match json_rpc_request(
        writer,
        reader,
        2,
        "tools/call",
        json!({
            "name": "get_desktop_state",
            "arguments": {
                "session": "avibe-permission-bootstrap",
                "max_image_dimension": 1
            }
        }),
    )
    .await
    {
        Ok(message) => message,
        Err(_) => return PermissionChildOutcome::TransportFailed,
    };
    if capture.get("error").is_some()
        || capture
            .get("result")
            .and_then(|result| result.get("isError"))
            .and_then(Value::as_bool)
            == Some(true)
    {
        return PermissionChildOutcome::CaptureRejected;
    }
    if validate_capture(&capture).is_ok() {
        PermissionChildOutcome::Image
    } else if capture.get("result").is_some() {
        PermissionChildOutcome::CaptureCompleted
    } else {
        PermissionChildOutcome::TransportFailed
    }
}

async fn stop_child(child: &mut Child, stop_timeout: Duration) {
    if timeout(stop_timeout, child.wait()).await.is_err() {
        let _ = child.kill().await;
        let _ = child.wait().await;
    }
}

async fn health_check(driver: &Path, socket: &Path, host_bundle_id: &str, full: bool) -> HealthResult {
    timeout(HEALTH_TIMEOUT, health_check_inner(driver, socket, host_bundle_id, full))
        .await
        .unwrap_or_else(|_| HealthResult::Unhealthy("health_timeout".to_owned()))
}

async fn health_check_inner(driver: &Path, socket: &Path, host_bundle_id: &str, full: bool) -> HealthResult {
    let mut command = Command::new(driver);
    command
        .args(["mcp", "--embedded", "--socket"])
        .arg(socket)
        .args(["--host-bundle-id", host_bundle_id])
        .env("CUA_DRIVER_EMBEDDED", "1")
        .env("CUA_DRIVER_RS_TELEMETRY_ENABLED", "0")
        .env("CUA_DRIVER_RS_UPDATE_CHECK", "0")
        .env_remove("CUA_DRIVER_WINDOW_CHANGE_TIMEOUT_MS")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .kill_on_drop(true);
    let mut child = match command.spawn() {
        Ok(child) => child,
        Err(_) => return HealthResult::Unhealthy("health_proxy_spawn".to_owned()),
    };
    let mut stdin = match child.stdin.take() {
        Some(stdin) => stdin,
        None => return HealthResult::Unhealthy("health_proxy_stdin".to_owned()),
    };
    let stdout = match child.stdout.take() {
        Some(stdout) => stdout,
        None => return HealthResult::Unhealthy("health_proxy_stdout".to_owned()),
    };
    let mut stdout = BufReader::new(stdout);
    let result = async {
        let initialize = json_rpc_request(
            &mut stdin,
            &mut stdout,
            1,
            "initialize",
            json!({
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "avibe-shell-health", "version": "1"},
            }),
        )
        .await?;
        if initialize.get("error").is_some() {
            return Err("health_initialize");
        }
        write_json_line(
            &mut stdin,
            &json!({
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
                "params": {},
            }),
        )
        .await
        .map_err(|_| "health_initialize")?;
        let report = json_rpc_request(
            &mut stdin,
            &mut stdout,
            2,
            "tools/call",
            json!({
                "name": "health_report",
                "arguments": {
                    "include": [
                        "bundle_identity",
                        "tcc_accessibility",
                        "tcc_screen_recording",
                        "ax_capability"
                    ]
                }
            }),
        )
        .await?;
        validate_health_report(&report)?;
        if full {
            let capture = json_rpc_request(
                &mut stdin,
                &mut stdout,
                3,
                "tools/call",
                json!({
                    "name": "get_desktop_state",
                    "arguments": {"max_image_dimension": 64}
                }),
            )
            .await?;
            validate_capture(&capture)?;
        }
        Ok::<(), &'static str>(())
    }
    .await;
    drop(stdin);
    if timeout(PROCESS_STOP_TIMEOUT, child.wait()).await.is_err() {
        let _ = child.kill().await;
        let _ = child.wait().await;
    }
    match result {
        Ok(()) => HealthResult::Pass,
        Err("accessibility") => HealthResult::MissingGrant("accessibility".to_owned()),
        Err("screen_recording") => HealthResult::MissingGrant("screen_recording".to_owned()),
        Err(reason) => HealthResult::Unhealthy(reason.to_owned()),
    }
}

async fn json_rpc_request<W, R>(
    writer: &mut W,
    reader: &mut BufReader<R>,
    id: u64,
    method: &str,
    params: Value,
) -> Result<Value, &'static str>
where
    W: AsyncWrite + Unpin,
    R: tokio::io::AsyncRead + Unpin,
{
    write_json_line(
        writer,
        &json!({
            "jsonrpc": "2.0",
            "id": id,
            "method": method,
            "params": params,
        }),
    )
    .await
    .map_err(|_| "health_transport")?;
    let mut line = String::new();
    loop {
        line.clear();
        let read = reader.read_line(&mut line).await.map_err(|_| "health_transport")?;
        if read == 0 {
            return Err("health_transport");
        }
        let Ok(message) = serde_json::from_str::<Value>(&line) else {
            continue;
        };
        if message.get("id").and_then(Value::as_u64) == Some(id) {
            return Ok(message);
        }
    }
}

async fn write_json_line(writer: &mut (impl AsyncWrite + Unpin), payload: &Value) -> io::Result<()> {
    let mut bytes = serde_json::to_vec(payload).map_err(io::Error::other)?;
    bytes.push(b'\n');
    writer.write_all(&bytes).await?;
    writer.flush().await
}

fn validate_health_report(message: &Value) -> Result<(), &'static str> {
    let result = tool_result(message)?;
    let structured = structured_tool_content(result).ok_or("health_protocol")?;
    let checks = structured
        .get("checks")
        .and_then(Value::as_array)
        .ok_or("health_protocol")?;
    for (name, failure) in [
        ("tcc_accessibility", "accessibility"),
        ("tcc_screen_recording", "screen_recording"),
    ] {
        let check = named_check(checks, name).ok_or("health_protocol")?;
        if check.get("status").and_then(Value::as_str) != Some("pass") {
            return Err(failure);
        }
    }
    let bundle = named_check(checks, "bundle_identity").ok_or("health_protocol")?;
    if bundle.get("status").and_then(Value::as_str) != Some("pass")
        || bundle
            .get("data")
            .and_then(|data| data.get("identity_source"))
            .and_then(Value::as_str)
            != Some("parent_application")
    {
        return Err("bundle_identity");
    }
    let accessibility = named_check(checks, "ax_capability").ok_or("health_protocol")?;
    if accessibility.get("status").and_then(Value::as_str) != Some("pass") {
        return Err("ax_capability");
    }
    Ok(())
}

fn validate_capture(message: &Value) -> Result<(), &'static str> {
    let result = tool_result(message)?;
    let content = result
        .get("content")
        .and_then(Value::as_array)
        .ok_or("capture_protocol")?;
    if content.iter().any(|block| {
        block.get("type").and_then(Value::as_str) == Some("image")
            && block.get("data").and_then(Value::as_str).is_some()
    }) {
        Ok(())
    } else {
        Err("capture_missing")
    }
}

fn tool_result(message: &Value) -> Result<&Value, &'static str> {
    if message.get("error").is_some() {
        return Err("health_tool_error");
    }
    let result = message.get("result").ok_or("health_protocol")?;
    if result.get("isError").and_then(Value::as_bool) == Some(true) {
        return Err("health_tool_error");
    }
    Ok(result)
}

fn structured_tool_content(result: &Value) -> Option<Value> {
    if let Some(structured) = result.get("structuredContent") {
        return Some(structured.clone());
    }
    result
        .get("content")
        .and_then(Value::as_array)?
        .iter()
        .filter(|block| block.get("type").and_then(Value::as_str) == Some("text"))
        .filter_map(|block| block.get("text").and_then(Value::as_str))
        .find_map(|text| serde_json::from_str(text).ok())
}

fn named_check<'a>(checks: &'a [Value], name: &str) -> Option<&'a Value> {
    checks
        .iter()
        .find(|check| check.get("name").and_then(Value::as_str) == Some(name))
}

fn silent_grants() -> Grants {
    // SAFETY: These public CoreGraphics/ApplicationServices calls take no
    // borrowed pointers and are documented as process-wide preflight checks.
    unsafe {
        Grants {
            accessibility: AXIsProcessTrusted() != 0,
            screen_recording: CGPreflightScreenCaptureAccess(),
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct PermissionRequestChecks {
    grants: Grants,
    main_thread: bool,
}

#[derive(Clone, Debug, Eq, PartialEq)]
enum CaptureProbeOutcome {
    Image,
    NativeError {
        stage: CaptureProbeStage,
        domain: String,
        code: isize,
        description: String,
    },
    EmptyResult(CaptureProbeStage),
    NoDisplay,
    ObjectCreationFailed(&'static str),
    ClassUnavailable(&'static str),
    SelectorUnavailable(&'static str),
    CallbackTimedOut,
    CallbackDropped,
    MainThreadDispatchFailed(String),
    MainThreadContextMismatch,
    MainThreadRequestTimedOut,
    MainThreadRequestDropped,
}

impl CaptureProbeOutcome {
    fn code(&self) -> &'static str {
        match self {
            Self::Image => "image",
            Self::NativeError {
                stage: CaptureProbeStage::ShareableContent,
                ..
            } => "shareable_content_error",
            Self::NativeError {
                stage: CaptureProbeStage::Capture,
                ..
            } => "capture_error",
            Self::EmptyResult(CaptureProbeStage::ShareableContent) => "shareable_content_empty",
            Self::EmptyResult(CaptureProbeStage::Capture) => "capture_empty",
            Self::NoDisplay => "display_unavailable",
            Self::ObjectCreationFailed(_) => "object_creation_failed",
            Self::ClassUnavailable(_) => "class_unavailable",
            Self::SelectorUnavailable(_) => "selector_unavailable",
            Self::CallbackTimedOut => "callback_timed_out",
            Self::CallbackDropped => "callback_dropped",
            Self::MainThreadDispatchFailed(_) => "main_thread_dispatch_failed",
            Self::MainThreadContextMismatch => "main_thread_context_mismatch",
            Self::MainThreadRequestTimedOut => "main_thread_request_timed_out",
            Self::MainThreadRequestDropped => "main_thread_request_dropped",
        }
    }

    fn detail(&self) -> Option<String> {
        match self {
            Self::NativeError {
                domain,
                code,
                description,
                ..
            } => Some(format!("{domain}({code}): {description}")),
            Self::ObjectCreationFailed(class_name) | Self::ClassUnavailable(class_name) => {
                Some((*class_name).to_owned())
            }
            Self::SelectorUnavailable(selector) => Some((*selector).to_owned()),
            Self::MainThreadDispatchFailed(error) => Some(error.clone()),
            _ => None,
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum CaptureProbeStage {
    ShareableContent,
    Capture,
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct PermissionRequestResult {
    checks: Option<PermissionRequestChecks>,
    capture: CaptureProbeOutcome,
}

impl PermissionRequestResult {
    fn grants(&self) -> Grants {
        self.checks
            .as_ref()
            .map(|checks| checks.grants)
            .unwrap_or_else(silent_grants)
    }
}

async fn run_permission_request<F>(request_timeout: Duration, dispatch: F) -> PermissionRequestResult
where
    F: FnOnce(
        oneshot::Sender<PermissionRequestChecks>,
        oneshot::Sender<CaptureProbeOutcome>,
        Instant,
    ) -> Result<(), String>,
{
    let (checks_sender, checks_receiver) = oneshot::channel();
    let (capture_sender, capture_receiver) = oneshot::channel();
    let deadline = Instant::now() + request_timeout;
    if let Err(error) = dispatch(checks_sender, capture_sender, deadline) {
        return PermissionRequestResult {
            checks: None,
            capture: CaptureProbeOutcome::MainThreadDispatchFailed(error),
        };
    }
    let deadline = tokio::time::Instant::from_std(deadline);
    let checks = match timeout_at(deadline, checks_receiver).await {
        Ok(Ok(checks)) => checks,
        Ok(Err(_)) => {
            return PermissionRequestResult {
                checks: None,
                capture: CaptureProbeOutcome::MainThreadRequestDropped,
            };
        }
        Err(_) => {
            return PermissionRequestResult {
                checks: None,
                capture: CaptureProbeOutcome::MainThreadRequestTimedOut,
            };
        }
    };
    let capture = match timeout_at(deadline, capture_receiver).await {
        Ok(Ok(outcome)) => outcome,
        Ok(Err(_)) => CaptureProbeOutcome::CallbackDropped,
        Err(_) => CaptureProbeOutcome::CallbackTimedOut,
    };
    PermissionRequestResult {
        checks: Some(checks),
        capture,
    }
}

async fn request_permissions(app: &AppHandle, diagnostics: &BootstrapLog) -> Grants {
    let app = app.clone();
    let result = run_permission_request(
        PERMISSION_REQUEST_TIMEOUT,
        move |checks_sender, capture_sender, deadline| {
            app.run_on_main_thread(move || {
                let main_thread = MainThreadMarker::new().is_some();
                if !main_thread {
                    let _ = checks_sender.send(PermissionRequestChecks {
                        grants: silent_grants(),
                        main_thread: false,
                    });
                    let _ = capture_sender.send(CaptureProbeOutcome::MainThreadContextMismatch);
                    return;
                }
                let accessibility = request_accessibility();
                // SAFETY: CoreGraphics owns the permission prompt and returns
                // the current process grant.
                let screen_recording = unsafe { CGRequestScreenCaptureAccess() };
                let _ = checks_sender.send(PermissionRequestChecks {
                    grants: Grants {
                        accessibility,
                        screen_recording,
                    },
                    main_thread,
                });
                one_pixel_capture_probe(capture_sender, deadline);
            })
            .map_err(|error| error.to_string())
        },
    )
    .await;
    record_permission_request(diagnostics, &result);
    result.grants()
}

fn request_accessibility() -> bool {
    // SAFETY: The dictionary contains two process-lifetime CoreFoundation
    // constants. The prompting call does not retain it after returning.
    unsafe {
        let keys = [kAXTrustedCheckOptionPrompt];
        let values = [kCFBooleanTrue];
        let options = CFDictionaryCreate(ptr::null(), keys.as_ptr(), values.as_ptr(), 1, ptr::null(), ptr::null());
        let trusted = AXIsProcessTrustedWithOptions(options) != 0;
        if !options.is_null() {
            CFRelease(options);
        }
        trusted
    }
}

#[derive(Clone)]
struct CaptureProbeGate {
    sender: Arc<Mutex<Option<oneshot::Sender<CaptureProbeOutcome>>>>,
    deadline: Instant,
}

impl CaptureProbeGate {
    fn new(sender: oneshot::Sender<CaptureProbeOutcome>, deadline: Instant) -> Self {
        Self {
            sender: Arc::new(Mutex::new(Some(sender))),
            deadline,
        }
    }

    fn run_if_active(&self, stage: impl FnOnce()) -> bool {
        if Instant::now() >= self.deadline {
            return false;
        }
        let active = self
            .sender
            .lock()
            .ok()
            .and_then(|sender| sender.as_ref().map(|sender| !sender.is_closed()))
            .unwrap_or(false);
        if !active {
            return false;
        }
        stage();
        true
    }

    fn finish(&self, outcome: CaptureProbeOutcome) {
        if let Ok(mut sender) = self.sender.lock() {
            if let Some(sender) = sender.take() {
                let _ = sender.send(outcome);
            }
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq)]
struct CaptureProbeGeometry {
    source: NSRect,
    destination: NSRect,
    width: isize,
    height: isize,
}

fn one_pixel_probe_geometry() -> CaptureProbeGeometry {
    // The content filter already selects one display, so (0, 0) is the origin
    // of that display's logical coordinate system rather than global desktop
    // space. Setting sourceRect is what keeps the input sample at one pixel;
    // width and height alone would downsample the whole display.
    CaptureProbeGeometry {
        source: NSRect::new(NSPoint::new(0.0, 0.0), NSSize::new(1.0, 1.0)),
        destination: NSRect::new(NSPoint::new(0.0, 0.0), NSSize::new(1.0, 1.0)),
        width: 1,
        height: 1,
    }
}

fn one_pixel_capture_probe(sender: oneshot::Sender<CaptureProbeOutcome>, deadline: Instant) {
    let gate = CaptureProbeGate::new(sender, deadline);
    let Some(shareable_content) = AnyClass::get(c"SCShareableContent") else {
        gate.finish(CaptureProbeOutcome::ClassUnavailable("SCShareableContent"));
        return;
    };
    let selector = sel!(getShareableContentWithCompletionHandler:);
    if shareable_content.class_method(selector).is_none() {
        gate.finish(CaptureProbeOutcome::SelectorUnavailable(
            "getShareableContentWithCompletionHandler:",
        ));
        return;
    }
    let completion_gate = gate.clone();
    let completion = RcBlock::new(move |content: *mut AnyObject, error: *mut NSError| {
        let completion_gate = completion_gate.clone();
        completion_gate.clone().run_if_active(move || {
            let error = unsafe { error.as_ref() }.map(capture_probe_error);
            continue_after_shareable_content(content.is_null(), error, || {
                // SAFETY: The ScreenCaptureKit completion supplied a non-null
                // object for the duration of this callback.
                let content = unsafe { content.as_ref() }.expect("shareable content checked non-null");
                begin_one_pixel_display_capture(content, completion_gate.clone());
            })
            .unwrap_or_else(|outcome| completion_gate.finish(outcome));
        });
    });
    // SAFETY: Availability and selector shape were checked above.
    // ScreenCaptureKit copies the block for asynchronous completion.
    unsafe {
        let _: () = msg_send![shareable_content, getShareableContentWithCompletionHandler: &*completion];
    }
}

fn continue_after_shareable_content(
    content_is_null: bool,
    error: Option<CaptureProbeError>,
    capture: impl FnOnce(),
) -> Result<(), CaptureProbeOutcome> {
    if let Some(error) = error {
        return Err(native_capture_error(CaptureProbeStage::ShareableContent, error));
    }
    if content_is_null {
        return Err(CaptureProbeOutcome::EmptyResult(CaptureProbeStage::ShareableContent));
    }
    capture();
    Ok(())
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct CaptureProbeError {
    domain: String,
    code: isize,
    description: String,
}

fn capture_probe_error(error: &NSError) -> CaptureProbeError {
    CaptureProbeError {
        domain: error.domain().to_string(),
        code: error.code(),
        description: error.localizedDescription().to_string(),
    }
}

fn native_capture_error(stage: CaptureProbeStage, error: CaptureProbeError) -> CaptureProbeOutcome {
    CaptureProbeOutcome::NativeError {
        stage,
        domain: error.domain,
        code: error.code,
        description: error.description,
    }
}

fn begin_one_pixel_display_capture(content: &AnyObject, gate: CaptureProbeGate) {
    let displays: Retained<NSArray<AnyObject>> = unsafe { msg_send![content, displays] };
    if displays.count() == 0 {
        gate.finish(CaptureProbeOutcome::NoDisplay);
        return;
    }
    let display = displays.objectAtIndex(0);
    let excluded_windows = NSArray::<AnyObject>::new();

    let Some(filter_class) = AnyClass::get(c"SCContentFilter") else {
        gate.finish(CaptureProbeOutcome::ClassUnavailable("SCContentFilter"));
        return;
    };
    let filter_selector = sel!(initWithDisplay:excludingWindows:);
    if filter_class.instance_method(filter_selector).is_none() {
        gate.finish(CaptureProbeOutcome::SelectorUnavailable(
            "initWithDisplay:excludingWindows:",
        ));
        return;
    }
    let allocated_filter: Allocated<AnyObject> = unsafe { msg_send![filter_class, alloc] };
    let filter: Option<Retained<AnyObject>> = unsafe {
        msg_send![
            allocated_filter,
            initWithDisplay: &*display,
            excludingWindows: &*excluded_windows
        ]
    };
    let Some(filter) = filter else {
        gate.finish(CaptureProbeOutcome::ObjectCreationFailed("SCContentFilter"));
        return;
    };

    let Some(configuration_class) = AnyClass::get(c"SCStreamConfiguration") else {
        gate.finish(CaptureProbeOutcome::ClassUnavailable("SCStreamConfiguration"));
        return;
    };
    let configuration: Retained<AnyObject> = unsafe { msg_send![configuration_class, new] };
    let geometry = one_pixel_probe_geometry();
    unsafe {
        let _: () = msg_send![&*configuration, setWidth: geometry.width];
        let _: () = msg_send![&*configuration, setHeight: geometry.height];
        let _: () = msg_send![&*configuration, setSourceRect: geometry.source];
        let _: () = msg_send![&*configuration, setDestinationRect: geometry.destination];
        let _: () = msg_send![&*configuration, setShowsCursor: false];
    }

    let Some(manager) = AnyClass::get(c"SCScreenshotManager") else {
        gate.finish(CaptureProbeOutcome::ClassUnavailable("SCScreenshotManager"));
        return;
    };
    let selector = sel!(captureImageWithFilter:configuration:completionHandler:);
    if manager.class_method(selector).is_none() {
        gate.finish(CaptureProbeOutcome::SelectorUnavailable(
            "captureImageWithFilter:configuration:completionHandler:",
        ));
        return;
    }

    let completion_gate = gate.clone();
    let completion_filter = filter.clone();
    let completion_configuration = configuration.clone();
    let completion = RcBlock::new(move |image: *mut AnyObject, error: *mut NSError| {
        let _keep_alive = (&completion_filter, &completion_configuration);
        if let Some(error) = unsafe { error.as_ref() } {
            completion_gate.finish(native_capture_error(
                CaptureProbeStage::Capture,
                capture_probe_error(error),
            ));
        } else if image.is_null() {
            completion_gate.finish(CaptureProbeOutcome::EmptyResult(CaptureProbeStage::Capture));
        } else {
            completion_gate.finish(CaptureProbeOutcome::Image);
        }
    });
    // SAFETY: The classes/selectors above are available, the retained filter
    // and configuration stay alive through the copied completion block, and
    // the callback only reports metadata; the one-pixel image is discarded.
    unsafe {
        let _: () = msg_send![
            manager,
            captureImageWithFilter: &*filter,
            configuration: &*configuration,
            completionHandler: &*completion
        ];
    }
}

fn record_permission_request(diagnostics: &BootstrapLog, result: &PermissionRequestResult) {
    let grants = result.grants();
    let thread = result
        .checks
        .as_ref()
        .map(|checks| if checks.main_thread { "main" } else { "non_main" })
        .unwrap_or("not_run");
    let capture = result.capture.code();
    let mut fields = vec![
        ("thread", thread.to_owned()),
        ("accessibility", grants.accessibility.to_string()),
        ("screen_recording", grants.screen_recording.to_string()),
        ("capture", capture.to_owned()),
    ];
    if let Some(detail) = result.capture.detail() {
        fields.push(("capture_detail", detail));
    }
    diagnostics.record("computer-use.permission-request", &fields);
    eprintln!(
        "computer-use permission request: thread={thread} accessibility={} screen_recording={} capture={capture}{}",
        grants.accessibility,
        grants.screen_recording,
        result
            .capture
            .detail()
            .map(|detail| format!(" detail={detail:?}"))
            .unwrap_or_default()
    );
}

fn open_missing_permission_settings(app: &AppHandle, grants: Grants) {
    let screen_recording_needs_guidance = grants.accessibility && !grants.screen_recording;
    let url = if !grants.accessibility {
        "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
    } else {
        "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"
    };
    if tauri_plugin_opener::open_url(url, None::<&str>).is_err() {
        let _ = app;
    }
    if screen_recording_needs_guidance {
        let catalog = crate::native_catalog_for_locales(sys_locale::get_locales()).computer_use;
        app.dialog()
            .message(catalog.screen_recording_guide)
            .title(catalog.screen_recording_title)
            .kind(MessageDialogKind::Info)
            .show(|_| {});
    }
}

fn sha256_file(path: &Path) -> Option<String> {
    let mut file = File::open(path).ok()?;
    let mut hasher = Sha256::new();
    let mut buffer = [0_u8; 64 * 1024];
    loop {
        let read = file.read(&mut buffer).ok()?;
        if read == 0 {
            break;
        }
        hasher.update(&buffer[..read]);
    }
    Some(format!("{:x}", hasher.finalize()))
}

fn random_instance_id() -> io::Result<String> {
    let mut bytes = [0_u8; 16];
    File::open("/dev/urandom")?.read_exact(&mut bytes)?;
    Ok(bytes.iter().map(|byte| format!("{byte:02x}")).collect())
}

type ActivationObserver = objc2::rc::Retained<ProtocolObject<dyn NSObjectProtocol>>;

thread_local! {
    static ACTIVATION_OBSERVER: std::cell::RefCell<Option<ActivationObserver>> =
        const { std::cell::RefCell::new(None) };
}

fn install_activation_observer(app: AppHandle) {
    let center = NSNotificationCenter::defaultCenter();
    let block = RcBlock::new(move |_notification: std::ptr::NonNull<NSNotification>| {
        if let Some(controller) = app.try_state::<Controller>() {
            controller.activated();
        }
    });
    // SAFETY: The notification name is an AppKit process-lifetime constant,
    // and NSNotificationCenter copies the sendable block.
    let observer = unsafe {
        center.addObserverForName_object_queue_usingBlock(
            Some(NSApplicationDidBecomeActiveNotification),
            None,
            None::<&NSOperationQueue>,
            &block,
        )
    };
    ACTIVATION_OBSERVER.with(|current| *current.borrow_mut() = Some(observer));
}

fn remove_activation_observer() {
    ACTIVATION_OBSERVER.with(|current| {
        let Some(observer) = current.borrow_mut().take() else {
            return;
        };
        let center = NSNotificationCenter::defaultCenter();
        // SAFETY: `observer` is the opaque token returned by this center.
        unsafe {
            center.removeObserver(observer.as_ref());
        }
    });
}

pub(crate) fn activation_plugin() -> TauriPlugin<tauri::Wry> {
    PluginBuilder::<_, ()>::new("computer-use-activation")
        .setup(|app, _| {
            install_activation_observer(app.clone());
            Ok(())
        })
        .on_event(|_, event| {
            if matches!(event, RunEvent::Exit) {
                remove_activation_observer();
            }
        })
        .build()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;

    fn shell_quote(value: &Path) -> String {
        format!("'{}'", value.display().to_string().replace('\'', "'\"'\"'"))
    }

    fn write_permission_child(root: &Path, state_dir: &Path, policy: &Path, behavior: &str) -> PathBuf {
        let path = root.join("fake-cua-driver");
        let launches = root.join("launches");
        let requests = root.join("requests");
        let private_root = state_dir.join("permission-probe");
        let body = format!(
            r#"#!/bin/sh
set -eu
printf '1\n' >> {launches}
[ "$1" = "mcp" ]
[ "$2" = "--direct" ]
[ "$3" = "--embedded" ]
[ "$4" = "--no-overlay" ]
[ "$5" = "--host-bundle-id" ]
[ "$6" = "bot.avibe.desktop.test" ]
[ "$HOME" = {home} ]
[ "$USERPROFILE" = {home} ]
[ "$XDG_CONFIG_HOME" = {config} ]
[ "$XDG_DATA_HOME" = {data} ]
[ "$XDG_STATE_HOME" = {state} ]
[ "$XDG_CACHE_HOME" = {cache} ]
[ "$TMPDIR" = {tmp} ]
[ "$CUA_DRIVER_RS_HOME" = {driver_home} ]
[ "$CUA_DRIVER_TELEMETRY_HOME" = {telemetry} ]
[ "$CUA_DRIVER_EMBEDDED" = "1" ]
[ "$CUA_DRIVER_MANAGED_POLICY_FILE" = {policy} ]
[ "$CUA_DRIVER_PERMISSION_MODE" = "standard" ]
[ "$CUA_DRIVER_RS_TELEMETRY_ENABLED" = "0" ]
[ "$CUA_DRIVER_RS_UPDATE_CHECK" = "0" ]
[ -z "${{CUA_DRIVER_WINDOW_CHANGE_TIMEOUT_MS+x}}" ]
while IFS= read -r line; do
  printf '%s\n' "$line" >> {requests}
  case "$line" in
    *'"id":1'*)
      printf '%s\n' '{{"jsonrpc":"2.0","id":1,"result":{{}}}}'
      ;;
    *'"id":2'*)
      {behavior}
      ;;
  esac
done
"#,
            launches = shell_quote(&launches),
            requests = shell_quote(&requests),
            home = shell_quote(&private_root),
            config = shell_quote(&private_root.join("config")),
            data = shell_quote(&private_root.join("data")),
            state = shell_quote(&private_root.join("state")),
            cache = shell_quote(&private_root.join("cache")),
            tmp = shell_quote(&private_root.join("tmp")),
            driver_home = shell_quote(&private_root.join("driver")),
            telemetry = shell_quote(&private_root.join("telemetry")),
            policy = shell_quote(policy),
        );
        std::fs::write(&path, body).expect("fake driver");
        let mut permissions = std::fs::metadata(&path).expect("fake driver metadata").permissions();
        permissions.set_mode(0o755);
        std::fs::set_permissions(&path, permissions).expect("fake driver executable");
        path
    }

    #[test]
    fn health_parser_requires_host_attribution_and_both_grants() {
        let message = json!({
            "result": {
                "structuredContent": {
                    "checks": [
                        {"name": "tcc_accessibility", "status": "pass"},
                        {"name": "tcc_screen_recording", "status": "pass"},
                        {
                            "name": "bundle_identity",
                            "status": "pass",
                            "data": {"identity_source": "parent_application"}
                        },
                        {"name": "ax_capability", "status": "pass"}
                    ]
                }
            }
        });
        assert_eq!(validate_health_report(&message), Ok(()));
        let mut missing = message.clone();
        missing["result"]["structuredContent"]["checks"][1]["status"] = Value::String("fail".to_owned());
        assert_eq!(validate_health_report(&missing), Err("screen_recording"));
    }

    #[test]
    fn full_health_requires_an_image_block() {
        assert_eq!(
            validate_capture(&json!({
                "result": {
                    "content": [{"type": "image", "data": "cG5n", "mimeType": "image/png"}]
                }
            })),
            Ok(())
        );
        assert_eq!(
            validate_capture(&json!({
                "result": {"content": [{"type": "text", "text": "no image"}]}
            })),
            Err("capture_missing")
        );
    }

    #[test]
    fn capability_payload_requires_controller_identity_for_a_definitive_answer() {
        assert_eq!(
            classify_capability_payload(None, Some(COMPUTER_USE_SCHEMA)),
            CapabilityVerdict::Transient
        );
        assert_eq!(
            classify_capability_payload(Some("controller-a"), Some(COMPUTER_USE_SCHEMA)),
            CapabilityVerdict::Supported
        );
        assert_eq!(
            classify_capability_payload(Some("controller-a"), Some(COMPUTER_USE_SCHEMA - 1)),
            CapabilityVerdict::Unsupported
        );
    }

    #[test]
    fn packaged_driver_resolves_from_contents_helpers() {
        assert_eq!(
            bundled_driver_path(Path::new("/Applications/Avibe.app/Contents/MacOS/avibe-desktop")),
            PathBuf::from("/Applications/Avibe.app/Contents/Helpers/cua-driver")
        );
    }

    #[test]
    fn release_path_resolution_ignores_test_environment_overrides() {
        let bundled = PathBuf::from("/Applications/Avibe.app/Contents/Helpers/cua-driver");
        let injected = PathBuf::from("/tmp/replaced-driver");
        assert_eq!(
            configured_path_with_value(bundled.clone(), Some(injected.clone().into_os_string()), false),
            bundled
        );
        assert_eq!(
            configured_path_with_value(bundled, Some(injected.clone().into_os_string()), true),
            injected
        );
    }

    #[test]
    fn delayed_start_retry_is_invalidated_by_toggle_off_and_runtime_loss() {
        let mut starting = ComputerUseLifecycle::off();
        assert!(
            starting
                .toggle_on(RuntimeSupport::Supported, Grants::all())
                .spawn_daemon
        );
        assert!(retry_is_current(7, 7, &starting));

        starting.toggle_off();
        assert!(!retry_is_current(7, 8, &starting));

        let mut restarting = ComputerUseLifecycle::off();
        assert!(
            restarting
                .toggle_on(RuntimeSupport::Supported, Grants::all())
                .spawn_daemon
        );
        let _ = restarting.capabilities(RuntimeSupport::Unknown, Grants::all());
        assert!(!retry_is_current(7, 8, &restarting));
    }

    #[tokio::test]
    async fn controller_shutdown_is_idempotent_and_acknowledges_once() {
        let (sender, mut receiver) = mpsc::unbounded_channel();
        let controller = Controller {
            sender,
            view: Arc::new(Mutex::new(MenuView::default())),
            shutdown_requested: Arc::new(AtomicBool::new(false)),
            shutdown_started: AtomicBool::new(false),
        };

        let first = controller.shutdown();
        tokio::pin!(first);
        let event = tokio::select! {
            event = receiver.recv() => event.expect("shutdown event"),
            _ = &mut first => panic!("shutdown completed before the actor acknowledged it"),
        };
        let Event::Shutdown(reply) = event else {
            panic!("unexpected computer-use event");
        };
        reply.send(()).expect("shutdown acknowledgement receiver");
        first.await;

        controller.shutdown().await;
        assert!(receiver.try_recv().is_err());
        assert!(controller.shutdown_requested.load(Ordering::SeqCst));
    }

    #[test]
    fn lifecycle_waits_have_explicit_bounds_for_shutdown_documentation() {
        assert_eq!(PERMISSION_REQUEST_TIMEOUT, Duration::from_secs(5));
        assert_eq!(HEALTH_TIMEOUT, Duration::from_secs(5));
        assert_eq!(PROCESS_STOP_TIMEOUT, Duration::from_secs(3));
        assert_eq!(START_BACKOFF, Duration::from_secs(1));
    }

    #[tokio::test]
    async fn permission_child_is_one_direct_bounded_capture_with_private_state() {
        let temporary = tempfile::tempdir().expect("temporary directory");
        let state_dir = temporary.path().join("state");
        let policy = temporary.path().join("policy.yaml");
        std::fs::write(&policy, "version: 1\n").expect("policy");
        let driver = write_permission_child(
            temporary.path(),
            &state_dir,
            &policy,
            r#"printf '%s\n' '{"jsonrpc":"2.0","id":2,"result":{"isError":true,"content":[{"type":"text","text":"screen recording denied"}]}}'
      break"#,
        );
        let (_cancel, mut cancelled) = oneshot::channel();

        let result = run_permission_child_with_stop_timeout(
            &driver,
            &state_dir,
            &policy,
            "bot.avibe.desktop.test",
            Duration::from_secs(1),
            Duration::from_millis(100),
            &mut cancelled,
        )
        .await;

        assert_eq!(result.outcome, PermissionChildOutcome::CaptureRejected);
        assert_eq!(
            std::fs::read_to_string(temporary.path().join("launches"))
                .expect("launch count")
                .lines()
                .count(),
            1
        );
        let requests = std::fs::read_to_string(temporary.path().join("requests")).expect("MCP requests");
        assert_eq!(requests.matches("\"id\":2").count(), 1);
        assert!(requests.contains("\"name\":\"get_desktop_state\""));
        assert!(requests.contains("\"session\":\"avibe-permission-bootstrap\""));
        assert!(requests.contains("\"max_image_dimension\":1"));
    }

    #[tokio::test]
    async fn permission_child_timeout_and_cancellation_reap_the_recorded_pid() {
        for cancel in [false, true] {
            let temporary = tempfile::tempdir().expect("temporary directory");
            let state_dir = temporary.path().join("state");
            let policy = temporary.path().join("policy.yaml");
            std::fs::write(&policy, "version: 1\n").expect("policy");
            let driver = write_permission_child(
                temporary.path(),
                &state_dir,
                &policy,
                "while IFS= read -r ignored; do :; done; exit 0",
            );
            let (cancel_sender, mut cancelled) = oneshot::channel();
            if cancel {
                tokio::spawn(async move {
                    sleep(Duration::from_millis(25)).await;
                    let _ = cancel_sender.send(());
                });
            }

            let result = run_permission_child_with_stop_timeout(
                &driver,
                &state_dir,
                &policy,
                "bot.avibe.desktop.test",
                Duration::from_millis(50),
                Duration::from_millis(50),
                &mut cancelled,
            )
            .await;
            assert_eq!(
                result.outcome,
                if cancel {
                    PermissionChildOutcome::Cancelled
                } else {
                    PermissionChildOutcome::TimedOut
                }
            );
            let pid = result.pid.expect("spawned permission child").to_string();
            let status = std::process::Command::new("ps")
                .args(["-p", &pid])
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .status()
                .expect("query recorded pid");
            assert!(!status.success(), "permission child {pid} remained alive");
        }
    }

    #[tokio::test]
    async fn permission_probe_preserves_failures_and_bounds_a_missing_callback() {
        let checks = PermissionRequestChecks {
            grants: Grants {
                accessibility: true,
                screen_recording: false,
            },
            main_thread: true,
        };
        for outcome in [
            CaptureProbeOutcome::ClassUnavailable("SCShareableContent"),
            CaptureProbeOutcome::SelectorUnavailable("getShareableContentWithCompletionHandler:"),
            CaptureProbeOutcome::MainThreadContextMismatch,
            CaptureProbeOutcome::NativeError {
                stage: CaptureProbeStage::ShareableContent,
                domain: "SCStreamErrorDomain".to_owned(),
                code: -3801,
                description: "permission denied".to_owned(),
            },
        ] {
            let expected = outcome.clone();
            let checks = checks.clone();
            let result = run_permission_request(Duration::from_millis(50), move |checks_sender, capture_sender, _| {
                checks_sender.send(checks).expect("request receiver");
                capture_sender.send(outcome).expect("capture receiver");
                Ok(())
            })
            .await;
            assert_eq!(result.capture, expected);
            assert_eq!(result.checks.as_ref().map(|value| value.main_thread), Some(true));
        }

        let dispatch_failure =
            run_permission_request(Duration::from_millis(50), |_, _, _| Err("event loop closed".to_owned())).await;
        assert_eq!(
            dispatch_failure.capture,
            CaptureProbeOutcome::MainThreadDispatchFailed("event loop closed".to_owned())
        );

        let timed_out = run_permission_request(Duration::from_millis(5), move |checks_sender, capture_sender, _| {
            checks_sender.send(checks).expect("request receiver");
            tokio::spawn(async move {
                sleep(Duration::from_millis(50)).await;
                drop(capture_sender);
            });
            Ok(())
        })
        .await;
        assert_eq!(timed_out.capture, CaptureProbeOutcome::CallbackTimedOut);
    }

    #[test]
    fn shareable_content_error_ends_before_the_capture_stage() {
        let capture_started = std::cell::Cell::new(false);
        let error = CaptureProbeError {
            domain: "SCStreamErrorDomain".to_owned(),
            code: -3801,
            description: "permission denied".to_owned(),
        };
        assert_eq!(
            continue_after_shareable_content(false, Some(error), || capture_started.set(true)),
            Err(CaptureProbeOutcome::NativeError {
                stage: CaptureProbeStage::ShareableContent,
                domain: "SCStreamErrorDomain".to_owned(),
                code: -3801,
                description: "permission denied".to_owned(),
            })
        );
        assert!(!capture_started.get());
        assert_eq!(
            continue_after_shareable_content(true, None, || capture_started.set(true)),
            Err(CaptureProbeOutcome::EmptyResult(CaptureProbeStage::ShareableContent))
        );
        assert!(!capture_started.get());
        assert_eq!(
            continue_after_shareable_content(false, None, || capture_started.set(true)),
            Ok(())
        );
        assert!(capture_started.get());
    }

    #[test]
    fn one_pixel_probe_bounds_source_and_output_without_downsampling_a_display() {
        let geometry = one_pixel_probe_geometry();
        assert_eq!(
            geometry.source,
            NSRect::new(NSPoint::new(0.0, 0.0), NSSize::new(1.0, 1.0))
        );
        assert_eq!(
            geometry.destination,
            NSRect::new(NSPoint::new(0.0, 0.0), NSSize::new(1.0, 1.0))
        );
        assert_eq!((geometry.width, geometry.height), (1, 1));
    }

    #[test]
    fn late_shareable_content_completion_cannot_start_the_capture_stage() {
        let (sender, _receiver) = oneshot::channel();
        let gate = CaptureProbeGate::new(sender, Instant::now() - Duration::from_millis(1));
        let started = std::cell::Cell::new(false);
        assert!(!gate.run_if_active(|| started.set(true)));
        assert!(!started.get());
    }

    #[test]
    fn closed_probe_receiver_cannot_start_the_capture_stage() {
        let (sender, receiver) = oneshot::channel();
        drop(receiver);
        let gate = CaptureProbeGate::new(sender, Instant::now() + Duration::from_secs(1));
        let started = std::cell::Cell::new(false);
        assert!(!gate.run_if_active(|| started.set(true)));
        assert!(!started.get());
    }

    #[tokio::test]
    async fn permission_probe_uses_one_deadline_for_request_and_capture() {
        let result = run_permission_request(Duration::from_millis(200), move |checks_sender, capture_sender, _| {
            tokio::spawn(async move {
                sleep(Duration::from_millis(75)).await;
                let _ = checks_sender.send(PermissionRequestChecks {
                    grants: Grants {
                        accessibility: true,
                        screen_recording: false,
                    },
                    main_thread: true,
                });
                sleep(Duration::from_millis(150)).await;
                let _ = capture_sender.send(CaptureProbeOutcome::Image);
            });
            Ok(())
        })
        .await;
        assert_eq!(result.capture, CaptureProbeOutcome::CallbackTimedOut);
    }

    #[test]
    fn permission_probe_result_is_written_to_the_existing_desktop_log() {
        let temporary = tempfile::tempdir().expect("temporary directory");
        let path = temporary.path().join(BOOTSTRAP_LOG_NAME);
        let diagnostics = BootstrapLog::at(path.clone());
        record_permission_request(
            &diagnostics,
            &PermissionRequestResult {
                checks: Some(PermissionRequestChecks {
                    grants: Grants {
                        accessibility: true,
                        screen_recording: false,
                    },
                    main_thread: true,
                }),
                capture: CaptureProbeOutcome::NativeError {
                    stage: CaptureProbeStage::ShareableContent,
                    domain: "SCStreamErrorDomain".to_owned(),
                    code: -3801,
                    description: "permission denied".to_owned(),
                },
            },
        );
        let written = std::fs::read_to_string(path).expect("diagnostic log");
        assert!(written.contains("computer-use.permission-request"));
        assert!(written.contains("thread=\"main\""));
        assert!(written.contains("capture=\"shareable_content_error\""));
        assert!(written.contains("SCStreamErrorDomain(-3801): permission denied"));
    }
}
