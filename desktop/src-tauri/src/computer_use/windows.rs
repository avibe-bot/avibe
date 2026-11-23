//! Windows x64 adapter for the existing desktop-owned lifecycle.
//! Settings/tray are opt-in. Only the shell starts the driver; the Runtime
//! independently admits each published generation through its private pipe.

use std::ffi::OsString;
use std::io;
use std::os::windows::io::AsHandle;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use avibe_runtime_host::computer_use::{
    CapabilityCache, CapabilityProbe, CapabilityVerdict, ComputerUseLifecycle, ComputerUsePhase, ComputerUseRecord,
    ComputerUseShellLock, ComputerUseStateStore, Grants, HealthResult, LifecycleDirective, RuntimeSupport,
    StoredComputerUseState, ToolSnapshot, COMPUTER_USE_DRIVER_VERSION, COMPUTER_USE_LOCK_FILE,
    COMPUTER_USE_SCHEMA_VERSION, COMPUTER_USE_STATE_FILE, COMPUTER_USE_WINDOWS_TOOL_SNAPSHOT_SHA256,
};
use avibe_runtime_host::computer_use_windows::{interactive_session, require_ordinary_user, verify_owned_pipe};
use avibe_runtime_host::hashing::lower_hex;
use avibe_runtime_host::LoopbackOrigin;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use tauri::{AppHandle, Manager};
use tokio::io::{AsyncBufReadExt, AsyncReadExt, AsyncWriteExt, BufReader};
use tokio::net::windows::named_pipe::ClientOptions;
use tokio::sync::{mpsc, oneshot};
use tokio::time::{sleep, timeout};

use crate::computer_use_process::OwnedDriver;

const TICK: Duration = Duration::from_secs(5);
const STOP_GRACE: Duration = Duration::from_secs(3);
const HEALTH_LIMIT: Duration = Duration::from_secs(5);
const MAX_RESPONSE: u64 = 1024 * 1024;

#[cfg(test)]
#[path = "disable_tests.rs"]
mod disable_tests;

#[cfg(test)]
#[path = "windows_tests.rs"]
mod tests;

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
    pub available: bool,
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
            available: false,
        }
    }
}

enum Event {
    Toggle,
    Enable,
    Disable(oneshot::Sender<Result<(), String>>),
    RuntimeReady { origin: LoopbackOrigin, adoption: bool },
    RuntimeLost,
    RuntimeStopped,
    Shutdown(oneshot::Sender<()>),
}

pub(crate) struct Controller {
    sender: mpsc::UnboundedSender<Event>,
    view: Arc<Mutex<MenuView>>,
    shutdown_requested: Arc<AtomicBool>,
}
impl Controller {
    pub(crate) fn start(app: &AppHandle) -> Self {
        let (sender, receiver) = mpsc::unbounded_channel();
        let view = Arc::new(Mutex::new(MenuView::default()));
        let shutdown_requested = Arc::new(AtomicBool::new(false));
        match RuntimeState::new(app.clone(), view.clone(), shutdown_requested.clone()) {
            Ok(runtime) => {
                tauri::async_runtime::spawn(runtime.run(receiver));
            }
            Err(error) => {
                eprintln!("warning: Computer Use initialization degraded: {error}");
                view.lock().expect("computer-use view").initialization_error = true;
            }
        }
        Self {
            sender,
            view,
            shutdown_requested,
        }
    }
    pub(crate) fn view(&self) -> MenuView {
        self.view.lock().expect("computer-use view").clone()
    }
    pub(crate) fn toggle(&self) {
        let _ = self.sender.send(Event::Toggle);
    }
    pub(crate) fn turn_on(&self) -> Result<(), String> {
        if !crate::computer_use_control_enabled(&self.view()) {
            return Err("Computer Use is unavailable.".to_owned());
        }
        self.sender
            .send(Event::Enable)
            .map_err(|_| "Computer Use is unavailable.".to_owned())
    }
    pub(crate) async fn disable(&self) -> Result<(), String> {
        let (sender, receiver) = oneshot::channel();
        self.sender
            .send(Event::Disable(sender))
            .map_err(|_| "Computer Use is unavailable.".to_owned())?;
        receiver.await.map_err(|_| "Computer Use is unavailable.".to_owned())?
    }
    pub(crate) fn runtime_ready(&self, origin: LoopbackOrigin, adoption: bool) {
        let _ = self.sender.send(Event::RuntimeReady { origin, adoption });
    }
    pub(crate) fn runtime_lost(&self) {
        let _ = self.sender.send(Event::RuntimeLost);
    }
    pub(crate) fn runtime_stopped(&self) {
        let _ = self.sender.send(Event::RuntimeStopped);
    }
    pub(crate) async fn shutdown(&self) {
        if self.shutdown_requested.swap(true, Ordering::SeqCst) {
            return;
        }
        let (sender, receiver) = oneshot::channel();
        if self.sender.send(Event::Shutdown(sender)).is_ok() {
            let _ = receiver.await;
        }
    }
}

struct Paths {
    state_dir: PathBuf,
    driver: PathBuf,
    policy: PathBuf,
    snapshot: PathBuf,
}
impl Paths {
    fn resolve(app: &AppHandle) -> Result<Self, Box<dyn std::error::Error>> {
        let state_dir = app.path().app_data_dir()?;
        let resources = app.path().resource_dir()?.join("computer-use");
        Ok(Self {
            state_dir,
            driver: resources.join("cua-driver.exe"),
            policy: resources.join("policy.yaml"),
            snapshot: resources.join("tools-windows-v0.31.0.json"),
        })
    }
    fn assets_valid(&self) -> bool {
        self.driver.is_file()
            && self.policy.is_file()
            && std::fs::read(&self.snapshot)
                .is_ok_and(|bytes| lower_hex(&Sha256::digest(bytes)) == COMPUTER_USE_WINDOWS_TOOL_SNAPSHOT_SHA256)
    }
}

struct RuntimeState {
    app: AppHandle,
    view: Arc<Mutex<MenuView>>,
    shutdown_requested: Arc<AtomicBool>,
    paths: Paths,
    store: ComputerUseStateStore,
    _shell_lock: ComputerUseShellLock,
    lifecycle: ComputerUseLifecycle,
    capabilities: CapabilityCache,
    origin: Option<LoopbackOrigin>,
    daemon: Option<OwnedDriver>,
    instance_id: String,
    generation: u64,
    endpoint: PathBuf,
    assets_valid: bool,
    retry_start: bool,
    pending_off: bool,
    started: Instant,
}
impl RuntimeState {
    fn new(
        app: AppHandle,
        view: Arc<Mutex<MenuView>>,
        shutdown_requested: Arc<AtomicBool>,
    ) -> Result<Self, Box<dyn std::error::Error>> {
        let paths = Paths::resolve(&app)?;
        std::fs::create_dir_all(&paths.state_dir)?;
        let shell_lock = ComputerUseShellLock::acquire(&paths.state_dir.join(COMPUTER_USE_LOCK_FILE))?;
        let store = ComputerUseStateStore::new(paths.state_dir.join(COMPUTER_USE_STATE_FILE));
        let stored = store.read();
        let assets_valid = paths.assets_valid();
        let mut lifecycle = match &stored {
            StoredComputerUseState::Current(record) => ComputerUseLifecycle::from_record(record),
            _ => ComputerUseLifecycle::off(),
        };
        lifecycle.recover_on_launch();
        if lifecycle.enabled() && !assets_valid {
            lifecycle.set_error("assets_invalid");
        }
        view.lock().expect("computer-use view").invalid_state = matches!(stored, StoredComputerUseState::Invalid(_));
        let state = Self {
            app,
            view,
            shutdown_requested,
            paths,
            store,
            _shell_lock: shell_lock,
            lifecycle,
            capabilities: CapabilityCache::default(),
            origin: None,
            daemon: None,
            instance_id: uuid::Uuid::new_v4().to_string(),
            generation: 0,
            endpoint: PathBuf::new(),
            assets_valid,
            retry_start: false,
            pending_off: false,
            started: Instant::now(),
        };
        // A stale ready record must not survive a new shell lifetime. A first
        // installation remains off, never borrowing another process's daemon.
        state.write_state()?;
        state.publish_view();
        Ok(state)
    }

    async fn run(mut self, mut receiver: mpsc::UnboundedReceiver<Event>) {
        let mut tick = tokio::time::interval(TICK);
        tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
        tick.tick().await;
        loop {
            tokio::select! {
                _ = tick.tick() => self.tick().await,
                event = receiver.recv() => match event {
                    Some(Event::Toggle) => self.toggle().await,
                    Some(Event::Enable) => if !self.lifecycle.enabled() { self.toggle().await },
                    Some(Event::Disable(reply)) => {
                        let _ = reply.send(self.disable().await);
                    }
                    Some(Event::RuntimeReady { origin, adoption }) => {
                        if adoption { self.runtime_lost(false).await; }
                        self.origin = Some(origin);
                        self.reprobe().await;
                    }
                    Some(Event::RuntimeLost) => self.runtime_lost(false).await,
                    Some(Event::RuntimeStopped) => self.runtime_lost(true).await,
                    Some(Event::Shutdown(reply)) => {
                        self.shutdown().await;
                        drop(self);
                        let _ = reply.send(());
                        return;
                    }
                    None => { self.shutdown().await; return; }
                }
            }
        }
    }

    async fn disable(&mut self) -> Result<(), String> {
        self.retry_start = false;
        let directive = self.lifecycle.toggle_off();
        if self.apply_state(directive).await {
            Ok(())
        } else {
            Err("Computer Use could not be disabled. Check its status and try again.".to_owned())
        }
    }

    async fn toggle(&mut self) {
        if let Ok(mut view) = self.view.lock() {
            view.invalid_state = false;
        }
        if self.lifecycle.enabled() {
            let _ = self.disable().await;
            return;
        }
        self.reprobe().await;
        if !self.assets_valid
            || self.capabilities.current() != RuntimeSupport::Supported
            || self.shutdown_requested.load(Ordering::SeqCst)
        {
            return;
        }
        let directive = self.lifecycle.toggle_on(RuntimeSupport::Supported, Grants::all());
        self.apply(directive).await;
    }

    async fn runtime_lost(&mut self, clear_origin: bool) {
        self.retry_start = false;
        if clear_origin {
            self.origin = None;
        }
        self.capabilities.begin_adoption();
        let directive = self.lifecycle.capabilities(RuntimeSupport::Unknown, Grants::all());
        self.apply(directive).await;
    }

    async fn reprobe(&mut self) {
        let Some(origin) = &self.origin else {
            return;
        };
        let support = self.capabilities.observe(probe_capabilities(origin).await);
        if support != RuntimeSupport::Supported {
            self.retry_start = false;
        }
        let directive = self.lifecycle.capabilities(support, Grants::all());
        self.apply(directive).await;
    }

    async fn tick(&mut self) {
        if self.shutdown_requested.load(Ordering::SeqCst) {
            return;
        }
        // Like macOS pending_write, this is process-local retry state, not a
        // second persisted opt-in. Retry Off before any capability work and
        // never turn a failed Off into an automatic On.
        if self.pending_off {
            let _ = self.disable().await;
        }
        // Re-probe even while ready: a Runtime rollback must retire an already
        // running driver, not merely hide the next toggle.
        self.reprobe().await;
        if self.capabilities.current() != RuntimeSupport::Supported || !self.lifecycle.enabled() {
            return;
        }
        if self.retry_start && self.lifecycle.phase() == ComputerUsePhase::Starting {
            self.retry_start = false;
            self.apply(LifecycleDirective {
                spawn_daemon: true,
                ..LifecycleDirective::default()
            })
            .await;
        } else if self.lifecycle.phase() == ComputerUsePhase::Ready {
            let health = self.health(false).await;
            let directive = self.lifecycle.ready_heartbeat(health, self.started.elapsed());
            self.apply(directive).await;
        }
        if self.lifecycle.recovering() {
            let visible = self
                .app
                .get_window(crate::MAIN_WINDOW)
                .is_some_and(|window| window.is_visible().unwrap_or(false));
            let directive = self.lifecycle.retry_recovery(
                self.capabilities.current(),
                Grants::all(),
                self.started.elapsed(),
                visible,
            );
            self.apply(directive).await;
        }
    }

    async fn apply(&mut self, mut directive: LifecycleDirective) {
        let spawn = directive.spawn_daemon;
        directive.spawn_daemon = false;
        if spawn {
            self.generation = self.generation.saturating_add(1);
            self.endpoint = PathBuf::from(format!(r"\\.\pipe\avibe-cua-{}-{}", self.instance_id, self.generation));
        }
        if !self.apply_state(directive).await || !spawn {
            return;
        }
        if self.shutdown_requested.load(Ordering::SeqCst) {
            return;
        }
        let health = match self.start_driver().await {
            Ok(()) => self.health(true).await,
            Err(error) => HealthResult::Unhealthy(stable_error(&error, "spawn_failed").to_owned()),
        };
        if self.shutdown_requested.load(Ordering::SeqCst) {
            self.stop_driver().await;
            return;
        }
        let mut directive = self.lifecycle.startup_health(health, self.started.elapsed());
        self.retry_start = directive.spawn_daemon;
        directive.spawn_daemon = false;
        self.apply_state(directive).await;
    }

    async fn apply_state(&mut self, directive: LifecycleDirective) -> bool {
        let record = self.current_record();
        let write_state = directive.write_state;
        let result = apply_transition(
            &self.store,
            &record,
            directive,
            &mut self.daemon,
            &mut self.lifecycle,
            &mut self.pending_off,
        )
        .await;
        if result.is_err() {
            if self.lifecycle.enabled() {
                let _ = self.write_state();
            }
            self.retry_start = false;
        }
        if write_state {
            if let Ok(mut view) = self.view.lock() {
                view.save_failed = result.is_err();
            }
        }
        self.publish_view();
        result.is_ok()
    }

    async fn start_driver(&mut self) -> io::Result<()> {
        if !self.stop_driver().await {
            return Err(io::Error::other("daemon_stop_failed"));
        }
        require_ordinary_user()?;
        interactive_session()?;
        let args: Vec<OsString> = [
            "serve",
            "--embedded",
            "--parent-liveness-stdio",
            "--no-permissions-gate",
            "--socket",
        ]
        .into_iter()
        .map(OsString::from)
        .chain([self.endpoint.as_os_str().to_owned()])
        .chain(
            [
                "--host-bundle-id",
                self.app.config().identifier.as_str(),
                "--permission-mode",
                "standard",
                "--glide-ms",
                "200",
            ]
            .into_iter()
            .map(OsString::from),
        )
        .collect();
        let environment = driver_environment(&self.paths.policy);
        let driver =
            OwnedDriver::spawn_suspended(&self.paths.driver, &args, &environment, &self.paths.state_dir)?.resume()?;
        self.daemon = Some(driver);
        Ok(())
    }

    async fn health(&self, full: bool) -> HealthResult {
        let Some(driver) = &self.daemon else {
            return HealthResult::Unhealthy("daemon_exited".to_owned());
        };
        let result = timeout(HEALTH_LIMIT, async {
            loop {
                if self.shutdown_requested.load(Ordering::SeqCst) {
                    return Err(io::Error::other("shutdown_requested"));
                }
                if driver.has_exited()? {
                    return Err(io::Error::other("daemon_exited"));
                }
                match ClientOptions::new().open(&self.endpoint) {
                    Ok(pipe) => {
                        verify_owned_pipe(pipe.as_handle(), driver.id())?;
                        let report = call(pipe, "health_report", json!({})).await?;
                        validate_health(&report)?;
                        if full {
                            let pipe = ClientOptions::new().open(&self.endpoint)?;
                            verify_owned_pipe(pipe.as_handle(), driver.id())?;
                            let capture = call(pipe, "get_desktop_state", json!({"max_image_dimension":64})).await?;
                            validate_capture(&capture)?;
                        }
                        // Bind the publication to a fresh connected pipe and
                        // the still-owned child after all asynchronous probes.
                        let pipe = ClientOptions::new().open(&self.endpoint)?;
                        verify_owned_pipe(pipe.as_handle(), driver.id())?;
                        if driver.has_exited()? {
                            return Err(io::Error::other("daemon_exited"));
                        }
                        return Ok(());
                    }
                    Err(error) if full && matches!(error.raw_os_error(), Some(2 | 231)) => {
                        sleep(Duration::from_millis(100)).await
                    }
                    Err(error) => return Err(error),
                }
            }
        })
        .await;
        match result {
            Ok(Ok(())) => HealthResult::Pass,
            Ok(Err(error)) => HealthResult::Unhealthy(stable_error(&error, "health_failed").to_owned()),
            Err(_) => HealthResult::Unhealthy("health_timeout".to_owned()),
        }
    }

    async fn stop_driver(&mut self) -> bool {
        match self.daemon.as_mut() {
            Some(driver) => {
                if driver.stop(STOP_GRACE).await.is_err() {
                    return false;
                }
                self.daemon = None;
                true
            }
            None => true,
        }
    }
    async fn shutdown(&mut self) {
        self.retry_start = false;
        let directive = self.lifecycle.quit();
        if directive.write_state {
            let _ = self.write_state();
        }
        self.stop_driver().await;
    }
    fn write_state(&self) -> io::Result<()> {
        self.store.write(&self.current_record())
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
                sha256: COMPUTER_USE_WINDOWS_TOOL_SNAPSHOT_SHA256.to_owned(),
            },
            socket_path: ready.then(|| self.endpoint.clone()),
            proxy_executable: ready.then(|| self.paths.driver.clone()),
            host_bundle_id: ready.then(|| self.app.config().identifier.clone()),
        }
    }
    fn publish_view(&self) {
        if let Ok(mut view) = self.view.lock() {
            view.enabled = self.lifecycle.enabled();
            view.phase = self.lifecycle.phase();
            view.reason = self.lifecycle.reason().map(str::to_owned);
            view.asset_error = !self.assets_valid;
            view.available = self.capabilities.current() == RuntimeSupport::Supported;
            view.runtime_refused = !view.available;
        }
        crate::refresh_computer_use_control(&self.app);
    }
}

async fn apply_transition(
    store: &ComputerUseStateStore,
    record: &ComputerUseRecord,
    directive: LifecycleDirective,
    daemon: &mut Option<OwnedDriver>,
    lifecycle: &mut ComputerUseLifecycle,
    pending_off: &mut bool,
) -> io::Result<()> {
    // Publish non-ready BEFORE stopping. A failed ready write also retires the
    // child; no unadvertised generation or stale ready record may stay live.
    let written = if directive.write_state {
        store.write(record)
    } else {
        Ok(())
    };
    let stopped = if directive.stop_daemon || written.is_err() {
        match daemon.as_mut() {
            Some(driver) => match driver.stop(STOP_GRACE).await {
                Ok(()) => {
                    *daemon = None;
                    Ok(())
                }
                Err(_) => Err(io::Error::other("daemon_stop_failed")),
            },
            None => Ok(()),
        }
    } else {
        Ok(())
    };
    let result = stopped.and(written.map_err(|_| io::Error::other("state_unwritable")));
    if let Err(error) = &result {
        // set_error restores ON by design. Off must remain off in memory even
        // when publication fails, or a later tick could restart its driver.
        if lifecycle.enabled() {
            lifecycle.set_error(stable_error(error, "state_unwritable"));
        }
    }
    if directive.write_state {
        *pending_off = !lifecycle.enabled() && result.is_err();
    }
    result
}

fn stable_error(error: &io::Error, fallback: &'static str) -> &'static str {
    match error.to_string().as_str() {
        "elevated_shell" => "elevated_runtime",
        "session_locked" => "session_locked",
        "session_unavailable" => "session_unavailable",
        "session_mismatch" => "session_mismatch",
        "pipe_unverified" | "object_unverified" | "token_unverified" => "pipe_unverified",
        "daemon_exited" => "daemon_exited",
        "daemon_stop_failed" => "daemon_stop_failed",
        _ => fallback,
    }
}

fn driver_environment(policy: &std::path::Path) -> Vec<(OsString, OsString)> {
    // No PATH, credentials, arbitrary CUA switches or parent environment leaks.
    let mut env: Vec<_> = [
        "SystemRoot",
        "WINDIR",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "TEMP",
        "TMP",
    ]
    .into_iter()
    .filter_map(|key| std::env::var_os(key).map(|value| (key.into(), value)))
    .collect();
    env.extend([
        ("CUA_DRIVER_EMBEDDED".into(), "1".into()),
        ("CUA_DRIVER_RS_TELEMETRY_ENABLED".into(), "0".into()),
        ("CUA_DRIVER_RS_UPDATE_CHECK".into(), "0".into()),
        (
            "CUA_DRIVER_EMBEDDED_HOST_PID".into(),
            std::process::id().to_string().into(),
        ),
        ("CUA_DRIVER_MANAGED_POLICY_FILE".into(), policy.as_os_str().to_owned()),
        ("CUA_DRIVER_WINDOW_CHANGE_TIMEOUT_MS".into(), "500".into()),
    ]);
    env
}

async fn call(
    mut pipe: tokio::net::windows::named_pipe::NamedPipeClient,
    name: &str,
    args: Value,
) -> io::Result<Value> {
    let mut bytes =
        serde_json::to_vec(&json!({"method":"call", "name":name, "args":args})).map_err(io::Error::other)?;
    bytes.push(b'\n');
    pipe.write_all(&bytes).await?;
    let mut response = Vec::new();
    let read = BufReader::new(pipe)
        .take(MAX_RESPONSE + 1)
        .read_until(b'\n', &mut response)
        .await?;
    if read == 0 || read as u64 > MAX_RESPONSE {
        return Err(io::Error::other("health_protocol"));
    }
    let value: Value = serde_json::from_slice(&response).map_err(io::Error::other)?;
    if value.get("ok").and_then(Value::as_bool) != Some(true)
        || value.pointer("/result/isError").and_then(Value::as_bool) == Some(true)
    {
        return Err(io::Error::other("health_tool_error"));
    }
    value
        .get("result")
        .cloned()
        .ok_or_else(|| io::Error::other("health_protocol"))
}
fn validate_health(result: &Value) -> io::Result<()> {
    let structured = result
        .get("structuredContent")
        .cloned()
        .or_else(|| {
            result
                .get("content")?
                .as_array()?
                .iter()
                .filter_map(|block| block.get("text")?.as_str())
                .find_map(|text| serde_json::from_str::<Value>(text).ok())
        })
        .ok_or_else(|| io::Error::other("health_protocol"))?;
    if structured.get("schema_version").and_then(Value::as_str) != Some("1")
        || structured.get("platform").and_then(Value::as_str) != Some("win32")
        || structured.get("driver_version").and_then(Value::as_str) != Some(COMPUTER_USE_DRIVER_VERSION)
        || structured.get("overall").and_then(Value::as_str) != Some("ok")
    {
        return Err(io::Error::other("health_protocol"));
    }
    let checks = structured
        .get("checks")
        .and_then(Value::as_array)
        .ok_or_else(|| io::Error::other("health_protocol"))?;
    // Pin the complete provider map: Windows-inapplicable checks are explicit
    // skips, never absent or falsely reported as permission passes.
    let expected = [
        ("binary_version", "pass"),
        ("platform_supported", "pass"),
        ("session_active", "pass"),
        ("bundle_identity", "skip"),
        ("tcc_accessibility", "skip"),
        ("tcc_screen_recording", "skip"),
        ("ax_capability", "pass"),
        ("screen_capture_capability", "pass"),
    ];
    if checks.len() != expected.len() {
        return Err(io::Error::other("health_protocol"));
    }
    for (name, status) in expected {
        let mut matching = checks
            .iter()
            .filter(|check| check.get("name").and_then(Value::as_str) == Some(name));
        let check = matching.next().ok_or_else(|| io::Error::other("health_protocol"))?;
        if check.get("status").and_then(Value::as_str) != Some(status)
            || check.get("message").and_then(Value::as_str).is_none()
            || (status == "skip" && check.get("message").and_then(Value::as_str) != Some("not applicable on Windows"))
            || matching.next().is_some()
        {
            return Err(io::Error::other("health_failed"));
        }
    }
    Ok(())
}
fn validate_capture(result: &Value) -> io::Result<()> {
    if result.get("content").and_then(Value::as_array).is_some_and(|blocks| {
        blocks.iter().any(|block| {
            block.get("type").and_then(Value::as_str) == Some("image")
                && block
                    .get("data")
                    .and_then(Value::as_str)
                    .is_some_and(|data| !data.is_empty())
        })
    }) {
        Ok(())
    } else {
        Err(io::Error::other("capture_missing"))
    }
}
async fn probe_capabilities(origin: &LoopbackOrigin) -> CapabilityProbe {
    let transient = || CapabilityProbe {
        controller_id: None,
        verdict: CapabilityVerdict::Transient,
    };
    let Ok(client) = reqwest::Client::builder()
        .timeout(Duration::from_secs(2))
        .redirect(reqwest::redirect::Policy::none())
        .no_proxy()
        .build()
    else {
        return transient();
    };
    let Ok(response) = client
        .get(format!(
            "{}/api/desktop/capabilities",
            origin.as_str().trim_end_matches('/')
        ))
        .send()
        .await
    else {
        return transient();
    };
    if response.status() == reqwest::StatusCode::NOT_FOUND {
        return CapabilityProbe {
            controller_id: Some("legacy".to_owned()),
            verdict: CapabilityVerdict::Unsupported,
        };
    }
    if !response.status().is_success() {
        return transient();
    }
    let Ok(payload) = response.json::<Value>().await else {
        return transient();
    };
    classify_capabilities(&payload)
}

fn classify_capabilities(payload: &Value) -> CapabilityProbe {
    let controller_id = payload
        .get("controller_id")
        .and_then(Value::as_str)
        .filter(|id| !id.is_empty())
        .map(str::to_owned);
    let verdict = if controller_id.is_none() {
        CapabilityVerdict::Transient
    } else if payload.get("computer_use_windows_schema").and_then(Value::as_u64) == Some(1) {
        CapabilityVerdict::Supported
    } else {
        CapabilityVerdict::Unsupported
    };
    CapabilityProbe { controller_id, verdict }
}
