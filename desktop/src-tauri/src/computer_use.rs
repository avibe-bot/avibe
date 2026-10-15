//! macOS adapter for the desktop-owned computer-use lifecycle.

use std::collections::{BTreeMap, HashMap};
use std::ffi::{c_char, c_void, CString};
use std::fs::File;
use std::io::{self, Read};
use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::{MetadataExt, PermissionsExt};
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
use objc2_app_kit::{NSApplicationActivationOptions, NSApplicationDidBecomeActiveNotification, NSRunningApplication};
use objc2_foundation::{
    NSArray, NSError, NSNotification, NSNotificationCenter, NSOperationQueue, NSPoint, NSRect, NSSize,
};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use tauri::plugin::{Builder as PluginBuilder, TauriPlugin};
use tauri::{AppHandle, Manager, RunEvent};
use tauri_plugin_dialog::{DialogExt, MessageDialogKind};
use tokio::io::{split, AsyncBufReadExt, AsyncWrite, AsyncWriteExt, BufReader};
use tokio::net::{UnixListener, UnixStream};
use tokio::process::{Child, ChildStderr, ChildStdin, Command};
use tokio::sync::{mpsc, oneshot, watch};
use tokio::task::JoinHandle;
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
const DARWIN_SOCKET_PATH_MAX_BYTES: usize = 103;
const SOCKET_DIRECTORY_MODE: u32 = 0o700;
const SOCKET_FLAVOR_HEX_BYTES: usize = 12;
const DAEMON_STDERR_TAIL_BYTES: usize = 8 * 1024;
const FOREGROUND_CONTROL_SOCKET_FILE: &str = "foreground-control.sock";
const FOREGROUND_CONTROL_PREFERENCE_FILE: &str = "foreground-control.json";
const FOREGROUND_TITLE_LIMIT: usize = 256;
const FOREGROUND_STOP_TIMEOUT: Duration = Duration::from_secs(5);
const AX_MESSAGING_TIMEOUT_SECONDS: f32 = 1.0;
const K_AX_ERROR_SUCCESS: i32 = 0;
const K_CF_STRING_ENCODING_UTF8: u32 = 0x0800_0100;

type AXUIElementRef = *mut c_void;
fn driver_command(driver: &Path) -> Command {
    let mut command = Command::new(driver);
    command
        .env_clear()
        .env("CUA_DRIVER_EMBEDDED", "1")
        .env("CUA_DRIVER_RS_TELEMETRY_ENABLED", "0")
        .env("CUA_DRIVER_RS_UPDATE_CHECK", "0");
    command
}

#[link(name = "ApplicationServices", kind = "framework")]
unsafe extern "C" {
    fn AXIsProcessTrusted() -> u8;
    fn AXIsProcessTrustedWithOptions(options: *const c_void) -> u8;
    fn AXUIElementCreateApplication(pid: i32) -> AXUIElementRef;
    fn AXUIElementCopyAttributeValue(
        element: AXUIElementRef,
        attribute: *const c_void,
        value: *mut *const c_void,
    ) -> i32;
    fn AXUIElementPerformAction(element: AXUIElementRef, action: *const c_void) -> i32;
    fn AXUIElementSetAttributeValue(element: AXUIElementRef, attribute: *const c_void, value: *const c_void) -> i32;
    fn AXUIElementSetMessagingTimeout(element: AXUIElementRef, timeout_in_seconds: f32) -> i32;
    fn _AXUIElementGetWindow(element: AXUIElementRef, window_id: *mut u32) -> i32;
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
    fn CFStringCreateWithCString(allocator: *const c_void, c_string: *const c_char, encoding: u32) -> *const c_void;
    fn CFArrayGetCount(array: *const c_void) -> isize;
    fn CFArrayGetValueAtIndex(array: *const c_void, index: isize) -> *const c_void;
    static kCFBooleanFalse: *const c_void;
    static kCFBooleanTrue: *const c_void;
}

#[link(name = "CoreGraphics", kind = "framework")]
unsafe extern "C" {
    fn CGPreflightScreenCaptureAccess() -> bool;
    fn CGRequestScreenCaptureAccess() -> bool;
    fn CGWarpMouseCursorPosition(point: NSPoint) -> i32;
}

#[link(name = "ScreenCaptureKit", kind = "framework")]
unsafe extern "C" {}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct ForegroundAttention {
    pub target: String,
    pub restoration: Value,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum ForegroundAttentionEvent {
    RequestConsent,
    SetActive,
    ComputerUseToggle,
    ForegroundSettingChange,
    ConsentChange,
    SessionChange,
    ComputerUseStopStart,
    OwnerDismiss,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum ForegroundAttentionOutcome {
    NoAttention,
    Refused,
    Kept,
    Dismissed,
}

#[derive(Default)]
struct ForegroundAttentionLatch {
    attention: Option<ForegroundAttention>,
}

impl ForegroundAttentionLatch {
    fn latch(&mut self, attention: ForegroundAttention) {
        self.attention = Some(attention);
    }

    fn current(&self) -> Option<&ForegroundAttention> {
        self.attention.as_ref()
    }

    fn transition(&mut self, event: ForegroundAttentionEvent) -> ForegroundAttentionOutcome {
        if self.attention.is_none() {
            return ForegroundAttentionOutcome::NoAttention;
        }
        match event {
            ForegroundAttentionEvent::RequestConsent | ForegroundAttentionEvent::SetActive => {
                ForegroundAttentionOutcome::Refused
            }
            ForegroundAttentionEvent::OwnerDismiss => {
                self.attention = None;
                ForegroundAttentionOutcome::Dismissed
            }
            ForegroundAttentionEvent::ComputerUseToggle
            | ForegroundAttentionEvent::ForegroundSettingChange
            | ForegroundAttentionEvent::ConsentChange
            | ForegroundAttentionEvent::SessionChange
            | ForegroundAttentionEvent::ComputerUseStopStart => ForegroundAttentionOutcome::Kept,
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct MenuView {
    pub enabled: bool,
    pub foreground_enabled: bool,
    pub phase: ComputerUsePhase,
    pub reason: Option<String>,
    pub invalid_state: bool,
    pub asset_error: bool,
    pub initialization_error: bool,
    pub save_failed: bool,
    pub runtime_refused: bool,
    pub foreground_pending: Option<String>,
    pub foreground_granted: Option<String>,
    pub foreground_active: Option<String>,
    pub foreground_attention: Option<ForegroundAttention>,
}

impl Default for MenuView {
    fn default() -> Self {
        Self {
            enabled: false,
            foreground_enabled: false,
            phase: ComputerUsePhase::Off,
            reason: None,
            invalid_state: false,
            asset_error: false,
            initialization_error: false,
            save_failed: false,
            runtime_refused: false,
            foreground_pending: None,
            foreground_granted: None,
            foreground_active: None,
            foreground_attention: None,
        }
    }
}

#[derive(Debug, Deserialize)]
struct ForegroundControlRequest {
    operation: String,
    session: String,
    title: String,
    target: Option<Value>,
    restoration: Option<Value>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum ForegroundExitReason {
    ToggleOff,
    ForegroundDisabled,
    ComputerUseStop,
    ConsentRevoke,
    SessionEndOrArchive,
    AppQuit,
    MacOsSessionEnd,
    ControllerDisconnect,
    DriverDisconnect,
}

impl ForegroundExitReason {
    #[allow(dead_code)]
    const ALL: [Self; 9] = [
        Self::ToggleOff,
        Self::ForegroundDisabled,
        Self::ComputerUseStop,
        Self::ConsentRevoke,
        Self::SessionEndOrArchive,
        Self::AppQuit,
        Self::MacOsSessionEnd,
        Self::ControllerDisconnect,
        Self::DriverDisconnect,
    ];

    const fn code(self) -> &'static str {
        match self {
            Self::ToggleOff => "toggle_off",
            Self::ForegroundDisabled => "foreground_disabled",
            Self::ComputerUseStop => "computer_use_stop",
            Self::ConsentRevoke => "consent_revoke",
            Self::SessionEndOrArchive => "session_end_or_archive",
            Self::AppQuit => "app_quit",
            Self::MacOsSessionEnd => "macos_session_end",
            Self::ControllerDisconnect => "controller_disconnect",
            Self::DriverDisconnect => "driver_disconnect",
        }
    }
}

enum ForegroundStopAction {
    ApplyDirective {
        directive: LifecycleDirective,
        completion: Option<oneshot::Sender<()>>,
    },
    RevokeConsent {
        session: String,
        reply: Option<oneshot::Sender<Value>>,
    },
    DisableForeground,
    Shutdown {
        reason: ForegroundExitReason,
        reply: oneshot::Sender<()>,
    },
}

enum Event {
    Toggle,
    RuntimeReady {
        origin: LoopbackOrigin,
        adoption: bool,
    },
    RuntimeLost,
    RuntimeStopped(oneshot::Sender<()>),
    Activated,
    RetryStart {
        id: u64,
    },
    PermissionProbeFinished {
        id: u64,
        result: PermissionChildRun,
    },
    ForegroundControl {
        request: ForegroundControlRequest,
        reply: oneshot::Sender<Value>,
    },
    ForegroundStopReady {
        call_id: u64,
        reached_terminal: bool,
        reason: ForegroundExitReason,
    },
    ToggleForegroundConsent,
    ToggleForegroundEnabled,
    DismissForegroundAttention,
    Shutdown {
        reason: ForegroundExitReason,
        reply: oneshot::Sender<()>,
    },
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

    pub(crate) async fn runtime_stopped(&self) {
        let (sender, receiver) = oneshot::channel();
        if self.sender.send(Event::RuntimeStopped(sender)).is_ok() {
            let _ = receiver.await;
        }
    }

    pub(crate) fn activated(&self) {
        let _ = self.sender.send(Event::Activated);
    }

    pub(crate) fn toggle_foreground_consent(&self) {
        let _ = self.sender.send(Event::ToggleForegroundConsent);
    }

    pub(crate) fn toggle_foreground_enabled(&self) {
        let _ = self.sender.send(Event::ToggleForegroundEnabled);
    }

    pub(crate) fn dismiss_foreground_attention(&self) {
        let _ = self.sender.send(Event::DismissForegroundAttention);
    }

    pub(crate) async fn shutdown(&self, reason: ForegroundExitReason) {
        if self.shutdown_started.swap(true, Ordering::SeqCst) {
            return;
        }
        self.shutdown_requested.store(true, Ordering::SeqCst);
        let (sender, receiver) = oneshot::channel();
        if self.sender.send(Event::Shutdown { reason, reply: sender }).is_ok() {
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
    foreground_control_socket: PathBuf,
    foreground_control_preference: PathBuf,
}

impl Paths {
    fn resolve(app: &AppHandle) -> Result<Self, Box<dyn std::error::Error>> {
        let state_dir = configured_path(app.path().app_data_dir()?, STATE_DIR_ENV);
        std::fs::create_dir_all(&state_dir)?;
        let owner_uid = std::fs::metadata(&state_dir)?.uid();
        let socket = short_socket_path(&state_dir, owner_uid);
        let socket_directory = socket.parent().expect("socket directory");
        ensure_private_socket_directory(socket_directory, owner_uid)?;
        let foreground_control_socket = short_foreground_control_socket_path(&state_dir, owner_uid);
        socket_path_preflight(&foreground_control_socket).map_err(io::Error::other)?;
        let executable = std::env::current_exe()?;
        let resource_dir = app.path().resource_dir()?;
        let driver = configured_path(bundled_driver_path(&executable), DRIVER_PATH_ENV);
        let policy = configured_path(resource_dir.join("computer-use/policy.yaml"), POLICY_PATH_ENV);
        let snapshot = configured_path(resource_dir.join("computer-use/tools-v0.31.0.json"), SNAPSHOT_PATH_ENV);
        let foreground_control_preference = state_dir.join(FOREGROUND_CONTROL_PREFERENCE_FILE);
        Ok(Self {
            state_file: state_dir.join(COMPUTER_USE_STATE_FILE),
            socket,
            state_dir,
            driver,
            policy,
            snapshot,
            foreground_control_socket,
            foreground_control_preference,
        })
    }

    fn assets_valid(&self) -> bool {
        self.driver.is_file()
            && self.policy.is_file()
            && sha256_file(&self.snapshot).as_deref() == Some(COMPUTER_USE_TOOL_SNAPSHOT_SHA256)
    }
}

#[derive(Deserialize, Serialize)]
struct ForegroundPreference {
    enabled: bool,
}

fn read_foreground_preference(path: &Path) -> bool {
    std::fs::read(path)
        .ok()
        .and_then(|bytes| serde_json::from_slice::<ForegroundPreference>(&bytes).ok())
        .is_some_and(|preference| preference.enabled)
}

fn write_foreground_preference(path: &Path, enabled: bool) -> io::Result<()> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let bytes = serde_json::to_vec(&ForegroundPreference { enabled })?;
    let temporary = path.with_extension("tmp");
    let result = std::fs::write(&temporary, bytes).and_then(|()| std::fs::rename(&temporary, path));
    if result.is_err() {
        let _ = std::fs::remove_file(&temporary);
    }
    result
}

fn short_socket_path(state_dir: &Path, owner_uid: u32) -> PathBuf {
    let mut digest = Sha256::new();
    digest.update(state_dir.as_os_str().as_bytes());
    let flavor = format!("{:x}", digest.finalize());
    let flavor = &flavor[..SOCKET_FLAVOR_HEX_BYTES];
    PathBuf::from("/tmp")
        .join(format!("avibe-cua-{owner_uid}-{flavor}"))
        .join(COMPUTER_USE_SOCKET_FILE)
}

fn short_foreground_control_socket_path(state_dir: &Path, owner_uid: u32) -> PathBuf {
    short_socket_path(state_dir, owner_uid)
        .parent()
        .expect("short Computer Use socket directory")
        .join(FOREGROUND_CONTROL_SOCKET_FILE)
}

fn ensure_private_socket_directory(path: &Path, owner_uid: u32) -> io::Result<()> {
    match std::fs::create_dir(path) {
        Ok(()) => {}
        Err(error) if error.kind() == io::ErrorKind::AlreadyExists => {}
        Err(error) => return Err(error),
    }
    let metadata = std::fs::symlink_metadata(path)?;
    if !metadata.file_type().is_dir() {
        return Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "computer-use socket directory is not a real directory",
        ));
    }
    if metadata.uid() != owner_uid {
        return Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "computer-use socket directory is not owned by the desktop user",
        ));
    }
    let mut permissions = metadata.permissions();
    permissions.set_mode(SOCKET_DIRECTORY_MODE);
    std::fs::set_permissions(path, permissions)
}

fn socket_path_preflight(path: &Path) -> Result<(), &'static str> {
    if path.as_os_str().as_bytes().len() > DARWIN_SOCKET_PATH_MAX_BYTES {
        return Err("socket_path_too_long");
    }
    Ok(())
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
    stderr_tail: JoinHandle<Vec<u8>>,
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
    foreground_control_listener: UnixListener,
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
    foreground_enabled: bool,
    foreground_consent_sessions: HashMap<String, String>,
    pending_foreground: Option<(String, String)>,
    foreground_activity: Option<ForegroundActivity>,
    foreground_attention: ForegroundAttentionLatch,
    next_foreground_transaction_id: u64,
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
        let assets_valid = paths.assets_valid();
        let lifecycle = restored_lifecycle(&stored, assets_valid);
        match std::fs::remove_file(&paths.foreground_control_socket) {
            Ok(()) => {}
            Err(error) if error.kind() == io::ErrorKind::NotFound => {}
            Err(error) => return Err(error.into()),
        }
        let foreground_control_listener = UnixListener::bind(&paths.foreground_control_socket)?;
        std::fs::set_permissions(&paths.foreground_control_socket, std::fs::Permissions::from_mode(0o600))?;
        let foreground_enabled = read_foreground_preference(&paths.foreground_control_preference);
        if let Ok(mut current) = view.lock() {
            current.foreground_enabled = foreground_enabled;
        }
        let mut runtime = Self {
            app,
            events,
            view,
            diagnostics,
            paths,
            foreground_control_listener,
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
            foreground_enabled,
            foreground_consent_sessions: HashMap::new(),
            pending_foreground: None,
            foreground_activity: None,
            foreground_attention: ForegroundAttentionLatch::default(),
            next_foreground_transaction_id: 0,
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
                        self.shutdown_after_controller_disconnect().await;
                        break;
                    };
                    match event {
                        Event::Toggle => self.toggle().await,
                        Event::RuntimeReady { origin, adoption } => {
                            self.runtime_ready(origin, adoption).await;
                        }
                        Event::RuntimeLost => self.runtime_lost().await,
                        Event::RuntimeStopped(reply) => self.runtime_stopped(reply).await,
                        Event::Activated => self.activated().await,
                        Event::RetryStart { id } => self.retry_start(id).await,
                        Event::PermissionProbeFinished { id, result } => {
                            self.permission_probe_finished(id, result);
                        }
                        Event::ForegroundControl { request, reply } => {
                            self.dispatch_foreground_control(request, reply).await;
                        }
                        Event::ForegroundStopReady {
                            call_id,
                            reached_terminal,
                            reason,
                        } => {
                            if self
                                .complete_foreground_stop(call_id, reached_terminal, reason)
                                .await
                            {
                                let RuntimeState {
                                    _shell_lock: shell_lock,
                                    ..
                                } = self;
                                drop(shell_lock);
                                return;
                            }
                        }
                        Event::ToggleForegroundConsent => self.toggle_foreground_consent(),
                        Event::ToggleForegroundEnabled => self.toggle_foreground_enabled(),
                        Event::DismissForegroundAttention => self.dismiss_foreground_attention(),
                        Event::Shutdown { reason, reply } => {
                            if self.foreground_activity.is_some() {
                                self.await_foreground_stop(
                                    reason,
                                    ForegroundStopAction::Shutdown { reason, reply },
                                );
                                continue;
                            }
                            self.shutdown_now(reason).await;
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
                accepted = self.foreground_control_listener.accept() => {
                    if let Ok((stream, _)) = accepted {
                        let events = self.events.clone();
                        tauri::async_runtime::spawn(serve_foreground_control(stream, events));
                    }
                }
            }
        }
    }

    async fn toggle(&mut self) {
        let _ = self
            .foreground_attention
            .transition(ForegroundAttentionEvent::ComputerUseToggle);
        self.update_view(|view| {
            view.runtime_refused = false;
            view.invalid_state = false;
        });
        if self.lifecycle.enabled() {
            self.invalidate_start_retry();
            let directive = self.lifecycle.toggle_off();
            if self.foreground_activity.is_some() {
                self.await_foreground_stop(
                    ForegroundExitReason::ToggleOff,
                    ForegroundStopAction::ApplyDirective {
                        directive,
                        completion: None,
                    },
                );
                self.publish_view();
                return;
            }
            self.expire_foreground_authorization();
            self.apply(directive).await;
            return;
        }
        self.invalidate_start_retry();
        if !self.assets_valid {
            self.update_view(|view| view.asset_error = true);
            self.publish_view();
            return;
        }
        if enable_support(&mut self.capabilities, self.origin.as_ref()).await != RuntimeSupport::Supported {
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

    fn toggle_foreground_enabled(&mut self) {
        let _ = self
            .foreground_attention
            .transition(ForegroundAttentionEvent::ForegroundSettingChange);
        if !self.foreground_enabled && self.foreground_activity.is_some() {
            return;
        }
        let enabled = !self.foreground_enabled;
        if let Err(error) = write_foreground_preference(&self.paths.foreground_control_preference, enabled) {
            eprintln!("The foreground control preference could not be saved: {error}");
            return;
        }
        self.foreground_enabled = enabled;
        self.update_view(|view| view.foreground_enabled = enabled);
        if enabled {
            self.publish_view();
            return;
        }

        self.expire_foreground_grants();
        if self.foreground_activity.is_some() {
            self.await_foreground_stop(
                ForegroundExitReason::ForegroundDisabled,
                ForegroundStopAction::DisableForeground,
            );
        }
        self.publish_view();
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
        self.set_runtime_lost(false, ForegroundExitReason::DriverDisconnect, None)
            .await;
    }

    async fn runtime_stopped(&mut self, completion: oneshot::Sender<()>) {
        self.set_runtime_lost(true, ForegroundExitReason::ComputerUseStop, Some(completion))
            .await;
    }

    async fn set_runtime_lost(
        &mut self,
        clear_origin: bool,
        reason: ForegroundExitReason,
        completion: Option<oneshot::Sender<()>>,
    ) {
        let _ = self
            .foreground_attention
            .transition(ForegroundAttentionEvent::ComputerUseStopStart);
        self.invalidate_start_retry();
        if clear_origin {
            self.origin = None;
        }
        self.capabilities.begin_adoption();
        let directive = self.lifecycle.capabilities(RuntimeSupport::Unknown, silent_grants());
        if self.foreground_activity.is_some() {
            self.await_foreground_stop(reason, ForegroundStopAction::ApplyDirective { directive, completion });
            self.publish_view();
            return;
        }
        self.expire_foreground_authorization();
        self.apply(directive).await;
        if let Some(completion) = completion {
            let _ = completion.send(());
        }
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
        if !self.lifecycle.enabled() || self.lifecycle.phase() == ComputerUsePhase::Error {
            return;
        }
        if self.lifecycle.phase() == ComputerUsePhase::NeedsRuntime {
            if let Some(directive) = runtime_reprobe_directive(
                &mut self.lifecycle,
                &mut self.capabilities,
                self.origin.as_ref(),
                silent_grants(),
            )
            .await
            {
                self.apply(directive).await;
            }
        }
        if self.capabilities.current() != RuntimeSupport::Supported {
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
        if let Err(reason) = socket_path_preflight(&self.paths.socket) {
            self.diagnostics.record(
                "computer-use.socket-preflight",
                &[
                    ("reason", reason.to_owned()),
                    ("path_bytes", self.paths.socket.as_os_str().as_bytes().len().to_string()),
                ],
            );
            return HealthResult::Unhealthy(reason.to_owned());
        }
        if let Err(reason) = self.reclaim_endpoint().await {
            return HealthResult::Unhealthy(reason);
        }
        let daemon = match spawn_daemon(
            &self.paths.driver,
            &self.paths.socket,
            &self.host_bundle_id,
            &self.paths.policy,
        ) {
            Ok(daemon) => daemon,
            Err(_) => {
                return HealthResult::Unhealthy("spawn_failed".to_owned());
            }
        };
        self.daemon = Some(daemon);

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
        let Some(daemon) = self.daemon.take() else {
            return;
        };
        stop_daemon_process(daemon, &self.paths.socket, &self.diagnostics, PROCESS_STOP_TIMEOUT).await;
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

    async fn shutdown_now(&mut self, reason: ForegroundExitReason) {
        self.invalidate_start_retry();
        self.expire_foreground_grants();
        self.diagnostics.record(
            "computer-use.foreground-shutdown",
            &[("reason", reason.code().to_owned())],
        );
        let directive = self.lifecycle.quit();
        if directive.write_state {
            let _ = self.write_current_state();
        }
        self.stop_permission_probe().await;
        self.stop_daemon().await;
        let _ = std::fs::remove_file(&self.paths.foreground_control_socket);
    }

    async fn shutdown_after_controller_disconnect(&mut self) {
        if let Some(call) = self.foreground_activity.as_ref() {
            let call_id = call.id;
            let reached_terminal = call.finish_and_await_terminal(FOREGROUND_STOP_TIMEOUT).await;
            let _ = self
                .complete_foreground_stop(call_id, reached_terminal, ForegroundExitReason::ControllerDisconnect)
                .await;
        }
        self.shutdown_now(ForegroundExitReason::ControllerDisconnect).await;
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

    async fn publish_view_and_wait(&self) -> Result<(), String> {
        self.update_view(|view| {
            view.enabled = self.lifecycle.enabled();
            view.phase = self.lifecycle.phase();
            view.reason = self.lifecycle.reason().map(str::to_owned);
            view.asset_error = !self.assets_valid;
        });
        crate::refresh_computer_use_control_and_wait(&self.app).await
    }

    async fn dispatch_foreground_control(&mut self, request: ForegroundControlRequest, reply: oneshot::Sender<Value>) {
        let session = request.session.trim().to_owned();
        if request.operation == "revoke_session" {
            let _ = self
                .foreground_attention
                .transition(ForegroundAttentionEvent::SessionChange);
        }
        if request.operation == "revoke_session"
            && self
                .foreground_activity
                .as_ref()
                .is_some_and(|call| call.session == session)
        {
            self.await_foreground_stop(
                ForegroundExitReason::SessionEndOrArchive,
                ForegroundStopAction::RevokeConsent {
                    session,
                    reply: Some(reply),
                },
            );
            return;
        }
        let _ = reply.send(self.handle_foreground_control(request).await);
    }

    fn await_foreground_stop(&mut self, reason: ForegroundExitReason, action: ForegroundStopAction) {
        let Some(call) = self.foreground_activity.as_mut() else {
            return;
        };
        call.finish_actions.push(action);
        if call.exit_wait_started {
            return;
        }
        call.exit_wait_started = true;
        let call_id = call.id;
        let wait = call.finish_and_await_terminal(FOREGROUND_STOP_TIMEOUT);
        let events = self.events.clone();
        tauri::async_runtime::spawn(async move {
            let reached_terminal = wait.await;
            let _ = events.send(Event::ForegroundStopReady {
                call_id,
                reached_terminal,
                reason,
            });
        });
    }

    async fn complete_foreground_stop(
        &mut self,
        call_id: u64,
        reached_terminal: bool,
        reason: ForegroundExitReason,
    ) -> bool {
        if self.foreground_activity.as_ref().is_none_or(|call| call.id != call_id) {
            return false;
        }
        if !reached_terminal {
            self.mark_foreground_attention_incomplete(reason);
        }

        let Some(call) = self.foreground_activity.take() else {
            return false;
        };

        let actions = call.finish_actions;
        for action in actions {
            match action {
                ForegroundStopAction::ApplyDirective { directive, completion } => {
                    self.expire_foreground_grants();
                    self.apply(directive).await;
                    if let Some(completion) = completion {
                        let _ = completion.send(());
                    }
                }
                ForegroundStopAction::RevokeConsent { session, reply } => {
                    self.foreground_consent_sessions.remove(&session);
                    if self
                        .pending_foreground
                        .as_ref()
                        .is_some_and(|(pending_session, _)| pending_session == &session)
                    {
                        self.pending_foreground = None;
                        self.update_view(|view| view.foreground_pending = None);
                    }
                    self.refresh_granted_view();
                    self.publish_view();
                    if let Some(reply) = reply {
                        let _ = reply.send(json!({"ok": true}));
                    }
                }
                ForegroundStopAction::DisableForeground => {
                    self.expire_foreground_grants();
                    self.publish_view();
                }
                ForegroundStopAction::Shutdown { reason, reply } => {
                    self.shutdown_now(reason).await;
                    let _ = reply.send(());
                    return true;
                }
            }
        }
        false
    }

    fn mark_foreground_attention_incomplete(&mut self, reason: ForegroundExitReason) {
        let (attention, target_pid, target_window_id) = {
            let Some(call) = self.foreground_activity.as_mut() else {
                return;
            };
            let mut restoration = call.restoration.clone();
            let detail = format!("foreground restoration was incomplete before {}", reason.code());
            fail_restoration(&mut restoration, &detail);
            call.restoration = restoration.clone();
            call.mark_terminal();
            let target = call.target_label.clone();
            let target_pid = call.target.pid;
            let target_window_id = call.target.window_id;
            let restoration = serde_json::to_value(&restoration).unwrap_or_else(|_| json!({"restoration_ok": false}));
            (
                ForegroundAttention { target, restoration },
                target_pid,
                target_window_id,
            )
        };
        self.update_view(|view| view.foreground_active = None);
        self.latch_foreground_attention(attention);
        self.diagnostics.record(
            "computer-use.foreground-terminal-failure",
            &[
                ("reason", reason.code().to_owned()),
                ("target_pid", target_pid.to_string()),
                ("target_window_id", target_window_id.to_string()),
            ],
        );
        self.publish_view();
    }

    async fn handle_foreground_control(&mut self, request: ForegroundControlRequest) -> Value {
        let session = request.session.trim();
        if session.is_empty() || session.len() > FOREGROUND_TITLE_LIMIT {
            return json!({"ok": false, "error": "invalid_session"});
        }
        let title = foreground_display_title(session, &request.title);
        match request.operation.as_str() {
            "request_consent" => {
                if !self.foreground_enabled {
                    return json!({"ok": false, "error": "foreground_disabled"});
                }
                if self
                    .foreground_attention
                    .transition(ForegroundAttentionEvent::RequestConsent)
                    == ForegroundAttentionOutcome::Refused
                {
                    return json!({"ok": false, "error": "foreground_attention_pending"});
                }
                if !self.lifecycle.enabled() || self.lifecycle.phase() != ComputerUsePhase::Ready {
                    return json!({"ok": false, "error": "computer_use_unavailable"});
                }
                if self.foreground_consent_sessions.contains_key(session) {
                    self.update_view(|view| view.foreground_granted = Some(title.clone()));
                    self.publish_view();
                    return json!({"ok": true, "allowed": true});
                }
                self.pending_foreground = Some((session.to_owned(), title.clone()));
                self.update_view(|view| view.foreground_pending = Some(title.clone()));
                self.publish_view();
                json!({"ok": true, "allowed": false})
            }
            "set_active" => {
                if !self.foreground_enabled {
                    return json!({"ok": false, "error": "foreground_disabled"});
                }
                if self
                    .foreground_attention
                    .transition(ForegroundAttentionEvent::SetActive)
                    == ForegroundAttentionOutcome::Refused
                {
                    return json!({"ok": false, "error": "foreground_attention_pending"});
                }
                if !self.lifecycle.enabled() || self.lifecycle.phase() != ComputerUsePhase::Ready {
                    return json!({"ok": false, "error": "computer_use_unavailable"});
                }
                if !self.foreground_consent_sessions.contains_key(session) {
                    return json!({"ok": false, "error": "foreground_consent_required"});
                }
                if self.foreground_activity.is_some() {
                    return json!({"ok": false, "error": "foreground_transaction_active"});
                }
                let target_identity = match parse_window_identity(request.target.as_ref()) {
                    Ok(identity) => identity,
                    Err(error) => return json!({"ok": false, "error": error}),
                };
                if let Err(error) = verify_process_identity(&target_identity, process_start_stamp) {
                    return json!({"ok": false, "error": "target_not_admissible", "detail": error});
                }
                let restoration = match parse_foreground_restoration(request.restoration.as_ref()) {
                    Ok(restoration) => restoration,
                    Err(error) => return json!({"ok": false, "error": error}),
                };
                let target = request.target.as_ref().and_then(foreground_target_label);
                let target_label = target
                    .map(|target| format!("{title}: {target}"))
                    .unwrap_or_else(|| title.clone());
                self.next_foreground_transaction_id = self.next_foreground_transaction_id.wrapping_add(1);
                let activity = ForegroundActivity::new(
                    self.next_foreground_transaction_id,
                    session.to_owned(),
                    target_identity,
                    target_label,
                    restoration,
                );
                self.foreground_activity = Some(activity);
                self.update_view(|view| {
                    view.foreground_pending = None;
                    view.foreground_active = self
                        .foreground_activity
                        .as_ref()
                        .map(|transaction| transaction.target_label.clone());
                });
                if let Err(error) = self.publish_view_and_wait().await {
                    self.foreground_activity = None;
                    self.update_view(|view| view.foreground_active = None);
                    self.publish_view();
                    return json!({"ok": false, "error": error});
                }
                json!({"ok": true})
            }
            "restore" => {
                let requested = match parse_foreground_restoration(request.restoration.as_ref()) {
                    Ok(restoration) => restoration,
                    Err(error) => return json!({"ok": false, "error": error}),
                };
                {
                    let Some(call) = self.foreground_activity.as_ref() else {
                        return json!({"ok": false, "error": "foreground_transaction_mismatch"});
                    };
                    if call.session != session || !same_restoration_contract(&call.restoration, &requested) {
                        return json!({"ok": false, "error": "foreground_transaction_mismatch"});
                    }
                }
                let restoration = attempt_native_foreground_restoration(requested);
                if let Some(call) = self.foreground_activity.as_mut() {
                    call.restoration = restoration.clone();
                }
                json!({"ok": true, "restoration": restoration})
            }
            "terminal" => {
                let attention = {
                    let Some(call) = self.foreground_activity.as_mut() else {
                        return json!({"ok": false, "error": "foreground_transaction_mismatch"});
                    };
                    if call.session != session {
                        return json!({"ok": false, "error": "foreground_transaction_mismatch"});
                    }
                    let restoration = match parse_foreground_restoration(request.restoration.as_ref()) {
                        Ok(restoration) => restoration,
                        Err(error) => return json!({"ok": false, "error": error}),
                    };
                    if !same_restoration_contract(&call.restoration, &restoration) {
                        return json!({"ok": false, "error": "foreground_transaction_mismatch"});
                    }
                    call.restoration = restoration;
                    call.mark_terminal();
                    if call.restoration.restoration_ok {
                        None
                    } else {
                        let target = call.target_label.clone();
                        let restoration = serde_json::to_value(&call.restoration)
                            .unwrap_or_else(|_| json!({"restoration_ok": false}));
                        Some(ForegroundAttention { target, restoration })
                    }
                };
                self.update_view(|view| view.foreground_active = None);
                if let Some(attention) = attention {
                    self.latch_foreground_attention(attention);
                }
                self.publish_view();
                self.remove_terminal_foreground_transaction_if_unblocked();
                json!({"ok": true})
            }
            "revoke_session" => {
                self.foreground_consent_sessions.remove(session);
                if self
                    .pending_foreground
                    .as_ref()
                    .is_some_and(|(pending_session, _)| pending_session == session)
                {
                    self.pending_foreground = None;
                    self.update_view(|view| view.foreground_pending = None);
                }
                self.refresh_granted_view();
                self.publish_view();
                json!({"ok": true})
            }
            _ => json!({"ok": false, "error": "unknown_operation"}),
        }
    }

    fn dismiss_foreground_attention(&mut self) {
        if self
            .foreground_attention
            .transition(ForegroundAttentionEvent::OwnerDismiss)
            == ForegroundAttentionOutcome::Dismissed
        {
            self.sync_foreground_attention_view();
            self.publish_view();
        }
    }

    fn toggle_foreground_consent(&mut self) {
        if !self.foreground_enabled {
            return;
        }
        if self
            .foreground_attention
            .transition(ForegroundAttentionEvent::ConsentChange)
            == ForegroundAttentionOutcome::Kept
        {
            return;
        }
        if !self.lifecycle.enabled() || self.lifecycle.phase() != ComputerUsePhase::Ready {
            return;
        }
        if let Some((session, title)) = self.pending_foreground.take() {
            self.foreground_consent_sessions.insert(session, title.clone());
            self.update_view(|view| {
                view.foreground_pending = None;
                view.foreground_granted = Some(title);
            });
            self.publish_view();
            return;
        }
        let selected = self
            .foreground_activity
            .as_ref()
            .map(|transaction| &transaction.session)
            .filter(|session| self.foreground_consent_sessions.contains_key(*session))
            .cloned()
            .or_else(|| self.foreground_consent_sessions.keys().min().cloned());
        if let Some(session) = selected {
            if self
                .foreground_activity
                .as_ref()
                .is_some_and(|transaction| transaction.session == session)
            {
                self.await_foreground_stop(
                    ForegroundExitReason::ConsentRevoke,
                    ForegroundStopAction::RevokeConsent { session, reply: None },
                );
                return;
            }
            self.foreground_consent_sessions.remove(&session);
            self.refresh_granted_view();
            self.publish_view();
        }
    }

    fn refresh_granted_view(&self) {
        let title = self
            .foreground_consent_sessions
            .iter()
            .min_by_key(|(session, _)| *session)
            .map(|(_, title)| title.clone());
        self.update_view(|view| view.foreground_granted = title);
    }

    fn expire_foreground_grants(&mut self) {
        self.foreground_consent_sessions.clear();
        self.pending_foreground = None;
        self.update_view(|view| {
            view.foreground_pending = None;
            view.foreground_granted = None;
        });
    }

    fn remove_terminal_foreground_transaction_if_unblocked(&mut self) {
        let remove = self
            .foreground_activity
            .as_ref()
            .is_some_and(|activity| *activity.terminal.borrow() && !activity.exit_wait_started);
        if remove {
            self.foreground_activity = None;
        }
    }

    fn latch_foreground_attention(&mut self, attention: ForegroundAttention) {
        self.foreground_attention.latch(attention);
        self.sync_foreground_attention_view();
    }

    fn sync_foreground_attention_view(&self) {
        let attention = self.foreground_attention.current().cloned();
        self.update_view(move |view| view.foreground_attention = attention);
    }

    fn expire_foreground_authorization(&mut self) {
        self.expire_foreground_grants();
        self.publish_view();
    }
}

fn foreground_target_label(target: &Value) -> Option<String> {
    let object = target.as_object()?;
    let app = object.get("app_name").and_then(Value::as_str).unwrap_or("");
    let title = object.get("title").and_then(Value::as_str).unwrap_or("");
    let label = if app.is_empty() { title } else { app };
    (!label.is_empty()).then(|| label.to_owned())
}

fn foreground_display_title(session: &str, title: &str) -> String {
    let candidate = match title.trim() {
        "" => session,
        title => title,
    };
    if candidate.len() <= FOREGROUND_TITLE_LIMIT {
        return candidate.to_owned();
    }
    let mut end = FOREGROUND_TITLE_LIMIT;
    while !candidate.is_char_boundary(end) {
        end -= 1;
    }
    let truncated = candidate[..end].trim_end();
    if truncated.is_empty() {
        session.to_owned()
    } else {
        truncated.to_owned()
    }
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
struct WindowIdentity {
    pid: i32,
    process_start_stamp: String,
    window_id: u32,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
struct ForegroundPointer {
    x: f64,
    y: f64,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
struct ForegroundRestorationComponent<T> {
    recorded: T,
    attempted: bool,
    verified: bool,
    failure_detail: Option<String>,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
struct ForegroundRestorationComponents {
    prior_frontmost: ForegroundRestorationComponent<WindowIdentity>,
    pointer: ForegroundRestorationComponent<ForegroundPointer>,
    active_space: ForegroundRestorationComponent<BTreeMap<String, u64>>,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
struct ForegroundRestoration {
    components: ForegroundRestorationComponents,
    restoration_ok: bool,
}

struct ForegroundActivity {
    id: u64,
    session: String,
    target: WindowIdentity,
    target_label: String,
    restoration: ForegroundRestoration,
    terminal: watch::Sender<bool>,
    exit_wait_started: bool,
    finish_actions: Vec<ForegroundStopAction>,
}

impl ForegroundActivity {
    fn new(
        id: u64,
        session: String,
        target: WindowIdentity,
        target_label: String,
        restoration: ForegroundRestoration,
    ) -> Self {
        let (terminal, _receiver) = watch::channel(false);
        Self {
            id,
            session,
            target,
            target_label,
            restoration,
            terminal,
            exit_wait_started: false,
            finish_actions: Vec::new(),
        }
    }

    fn mark_terminal(&mut self) {
        let _ = self.terminal.send(true);
    }

    fn finish_and_await_terminal(&self, limit: Duration) -> impl std::future::Future<Output = bool> + Send + 'static {
        let mut terminal = self.terminal.subscribe();
        async move {
            if *terminal.borrow() {
                return true;
            }
            timeout(limit, async {
                loop {
                    if terminal.changed().await.is_err() {
                        return false;
                    }
                    if *terminal.borrow() {
                        return true;
                    }
                }
            })
            .await
            .unwrap_or(false)
        }
    }
}

fn process_start_stamp(pid: i32) -> Option<String> {
    if pid <= 0 {
        return None;
    }
    let mut info = unsafe { std::mem::zeroed::<libc::proc_bsdinfo>() };
    let expected_size = std::mem::size_of::<libc::proc_bsdinfo>();
    let read = unsafe {
        libc::proc_pidinfo(
            pid,
            libc::PROC_PIDTBSDINFO,
            0,
            (&raw mut info).cast(),
            expected_size as i32,
        )
    };
    if read != expected_size as i32 {
        return None;
    }
    info.pbi_start_tvsec
        .checked_mul(1_000_000)
        .and_then(|seconds| seconds.checked_add(info.pbi_start_tvusec))
        .filter(|stamp| *stamp > 0)
        .map(|stamp| stamp.to_string())
}

fn verify_process_identity(
    target: &WindowIdentity,
    read_start_stamp: impl FnOnce(i32) -> Option<String>,
) -> Result<(), String> {
    match read_start_stamp(target.pid) {
        Some(stamp) if stamp == target.process_start_stamp => Ok(()),
        Some(_) => Err(format!(
            "process identity changed before restoring window {}:{}",
            target.pid, target.window_id
        )),
        None => Err(format!(
            "process identity could not be verified for window {}:{}",
            target.pid, target.window_id
        )),
    }
}

fn cf_string(value: &str) -> Result<*const c_void, String> {
    let value = CString::new(value).map_err(|_| "invalid AX string".to_owned())?;
    let string = unsafe { CFStringCreateWithCString(ptr::null(), value.as_ptr(), K_CF_STRING_ENCODING_UTF8) };
    if string.is_null() {
        Err("could not allocate AX string".to_owned())
    } else {
        Ok(string)
    }
}

fn with_exact_ax_window(
    target: &WindowIdentity,
    operation: impl FnOnce(AXUIElementRef) -> Result<(), String>,
) -> Result<(), String> {
    if target.pid <= 0 || target.window_id == 0 {
        return Err("foreground window identity is invalid".to_owned());
    }
    verify_process_identity(target, process_start_stamp)?;
    unsafe {
        let windows_attribute = cf_string("AXWindows")?;
        let application = AXUIElementCreateApplication(target.pid);
        if application.is_null() {
            CFRelease(windows_attribute);
            return Err(format!("application {} has no accessibility object", target.pid));
        }
        let _ = AXUIElementSetMessagingTimeout(application, AX_MESSAGING_TIMEOUT_SECONDS);
        let mut windows: *const c_void = ptr::null();
        let copy_result = AXUIElementCopyAttributeValue(application, windows_attribute, &mut windows);
        CFRelease(windows_attribute);
        if copy_result != K_AX_ERROR_SUCCESS || windows.is_null() {
            CFRelease(application);
            return Err(format!("could not enumerate windows for application {}", target.pid));
        }
        let mut matching_window = ptr::null_mut();
        for index in 0..CFArrayGetCount(windows) {
            let window = CFArrayGetValueAtIndex(windows, index) as AXUIElementRef;
            if window.is_null() {
                continue;
            }
            let mut window_id = 0_u32;
            if _AXUIElementGetWindow(window, &mut window_id) == K_AX_ERROR_SUCCESS && window_id == target.window_id {
                matching_window = window;
                break;
            }
        }
        let result = if matching_window.is_null() {
            Err(format!(
                "exact window {}:{} is no longer accessible",
                target.pid, target.window_id
            ))
        } else {
            verify_process_identity(target, process_start_stamp)?;
            let _ = AXUIElementSetMessagingTimeout(matching_window, AX_MESSAGING_TIMEOUT_SECONDS);
            operation(matching_window)
        };
        CFRelease(windows);
        CFRelease(application);
        result
    }
}

fn set_exact_window_minimized(target: &WindowIdentity, minimized: bool) -> Result<(), String> {
    with_exact_ax_window(target, |window| {
        let attribute = cf_string("AXMinimized")?;
        let value = unsafe {
            if minimized {
                kCFBooleanTrue
            } else {
                kCFBooleanFalse
            }
        };
        let result = unsafe { AXUIElementSetAttributeValue(window, attribute, value) };
        unsafe { CFRelease(attribute) };
        if result == K_AX_ERROR_SUCCESS {
            Ok(())
        } else {
            Err(format!(
                "could not restore minimized state for {}:{} ({result})",
                target.pid, target.window_id
            ))
        }
    })
}

fn raise_exact_window(target: &WindowIdentity) -> Result<(), String> {
    with_exact_ax_window(target, |window| {
        let action = cf_string("AXRaise")?;
        let result = unsafe { AXUIElementPerformAction(window, action) };
        unsafe { CFRelease(action) };
        if result == K_AX_ERROR_SUCCESS {
            Ok(())
        } else {
            Err(format!(
                "could not restore exact frontmost window {}:{} ({result})",
                target.pid, target.window_id
            ))
        }
    })
}

fn running_application(target: &WindowIdentity) -> Result<Retained<NSRunningApplication>, String> {
    verify_process_identity(target, process_start_stamp)?;
    NSRunningApplication::runningApplicationWithProcessIdentifier(target.pid)
        .ok_or_else(|| format!("application {} is no longer running", target.pid))
}

fn combine_restoration_failures(failures: Vec<String>) -> Result<(), String> {
    if failures.is_empty() {
        Ok(())
    } else {
        Err(failures.join("; "))
    }
}

fn restore_prior_frontmost(target: &WindowIdentity) -> Result<(), String> {
    let mut failures = Vec::new();
    match running_application(target) {
        Ok(application) => {
            if let Err(error) = set_exact_window_minimized(target, false) {
                failures.push(error);
            }
            if !application.unhide() {
                failures.push("could not reveal the prior frontmost application".to_owned());
            }
            if !application.activateWithOptions(NSApplicationActivationOptions::empty()) {
                failures.push("could not restore prior frontmost application".to_owned());
            }
        }
        Err(error) => failures.push(error),
    }
    if let Err(error) = raise_exact_window(target) {
        failures.push(error);
    }
    combine_restoration_failures(failures)
}

fn restore_pointer(pointer: &ForegroundPointer) -> Result<(), String> {
    if !pointer.x.is_finite() || !pointer.y.is_finite() {
        return Err("hardware pointer contract is invalid".to_owned());
    }
    let pointer_result = unsafe {
        CGWarpMouseCursorPosition(NSPoint {
            x: pointer.x,
            y: pointer.y,
        })
    };
    if pointer_result != 0 {
        Err(format!("could not restore hardware pointer ({pointer_result})"))
    } else {
        Ok(())
    }
}

fn record_restoration_attempt<T>(component: &mut ForegroundRestorationComponent<T>, result: Result<(), String>) {
    component.attempted = true;
    component.verified = false;
    component.failure_detail = result.err();
}

fn attempt_native_foreground_restoration(mut restoration: ForegroundRestoration) -> ForegroundRestoration {
    let prior_result = restore_prior_frontmost(&restoration.components.prior_frontmost.recorded);
    record_restoration_attempt(&mut restoration.components.prior_frontmost, prior_result);

    let pointer_result = restore_pointer(&restoration.components.pointer.recorded);
    record_restoration_attempt(&mut restoration.components.pointer, pointer_result);

    restoration.components.active_space.attempted = restoration.components.prior_frontmost.attempted;
    restoration.components.active_space.verified = false;
    restoration.components.active_space.failure_detail = None;
    restoration.restoration_ok = false;
    restoration
}

fn parse_foreground_restoration(restoration: Option<&Value>) -> Result<ForegroundRestoration, String> {
    restoration
        .cloned()
        .ok_or_else(|| "missing restoration contract".to_owned())
        .and_then(|value| {
            serde_json::from_value::<ForegroundRestoration>(value)
                .map_err(|_| "invalid restoration contract".to_owned())
        })
}

fn parse_window_identity(target: Option<&Value>) -> Result<WindowIdentity, String> {
    let target = target
        .cloned()
        .ok_or_else(|| "missing foreground target identity".to_owned())?;
    let identity = serde_json::from_value::<WindowIdentity>(target)
        .map_err(|_| "invalid foreground target identity".to_owned())?;
    if identity.pid <= 0 || identity.window_id == 0 || identity.process_start_stamp.is_empty() {
        return Err("invalid foreground target identity".to_owned());
    }
    Ok(identity)
}

fn same_restoration_contract(left: &ForegroundRestoration, right: &ForegroundRestoration) -> bool {
    left.components.prior_frontmost.recorded == right.components.prior_frontmost.recorded
        && left.components.pointer.recorded == right.components.pointer.recorded
        && left.components.active_space.recorded == right.components.active_space.recorded
}

fn fail_restoration(restoration: &mut ForegroundRestoration, detail: &str) {
    fail_restoration_component(&mut restoration.components.prior_frontmost, detail);
    fail_restoration_component(&mut restoration.components.pointer, detail);
    fail_restoration_component(&mut restoration.components.active_space, detail);
    restoration.restoration_ok = false;
}

fn fail_restoration_component<T>(component: &mut ForegroundRestorationComponent<T>, detail: &str) {
    component.attempted = true;
    component.verified = false;
    if component.failure_detail.is_none() {
        component.failure_detail = Some(detail.to_owned());
    } else if component
        .failure_detail
        .as_deref()
        .is_some_and(|existing| !existing.contains(detail))
    {
        let existing = component.failure_detail.take().unwrap_or_default();
        component.failure_detail = Some(format!("{existing}; {detail}"));
    }
}

async fn serve_foreground_control(stream: UnixStream, events: mpsc::UnboundedSender<Event>) {
    let (reader, mut writer) = split(stream);
    let mut reader = BufReader::new(reader);
    let mut line = String::new();
    let response = match reader.read_line(&mut line).await {
        Ok(size) if size > 0 && size <= 32 * 1024 => match serde_json::from_str::<ForegroundControlRequest>(&line) {
            Ok(request) => {
                let (reply, result) = oneshot::channel();
                if events.send(Event::ForegroundControl { request, reply }).is_err() {
                    json!({"ok": false, "error": "shell_stopped"})
                } else {
                    result
                        .await
                        .unwrap_or_else(|_| json!({"ok": false, "error": "shell_stopped"}))
                }
            }
            Err(_) => json!({"ok": false, "error": "invalid_request"}),
        },
        _ => json!({"ok": false, "error": "invalid_request"}),
    };
    let encoded = serde_json::to_string(&response).unwrap_or_else(|_| "{\"ok\":false}".to_owned());
    let _ = writer.write_all(format!("{encoded}\n").as_bytes()).await;
}

fn restored_lifecycle(stored: &StoredComputerUseState, assets_valid: bool) -> ComputerUseLifecycle {
    let mut lifecycle = match stored {
        StoredComputerUseState::Current(record) => ComputerUseLifecycle::from_record(record),
        StoredComputerUseState::Missing | StoredComputerUseState::Invalid(_) => ComputerUseLifecycle::off(),
    };
    if !assets_valid && lifecycle.enabled() {
        // A persisted enablement must never turn an incomplete package into an
        // automatic daemon launch. Keep the feature enabled so the user can
        // turn it off, but fail closed with the same visible asset error.
        lifecycle.set_error("assets_invalid");
    }
    lifecycle
}

async fn runtime_reprobe_directive(
    lifecycle: &mut ComputerUseLifecycle,
    capabilities: &mut CapabilityCache,
    origin: Option<&LoopbackOrigin>,
    grants: Grants,
) -> Option<LifecycleDirective> {
    if !lifecycle.enabled() || lifecycle.phase() != ComputerUsePhase::NeedsRuntime {
        return None;
    }
    let origin = origin?;
    let support = capabilities.observe(probe_capabilities(origin).await);
    let grants = if support == RuntimeSupport::Supported {
        grants
    } else {
        Grants {
            accessibility: false,
            screen_recording: false,
        }
    };
    Some(lifecycle.capabilities(support, grants))
}

/// The Runtime support an enable request acts on.
///
/// A transient capability probe leaves the cache unknown, and no tick probes
/// while the feature is off. The request therefore asks the current Runtime
/// again instead of refusing until the next Runtime-ready event or a restart.
/// A definite answer, supported or not, stands until the Runtime changes.
async fn enable_support(capabilities: &mut CapabilityCache, origin: Option<&LoopbackOrigin>) -> RuntimeSupport {
    match (capabilities.current(), origin) {
        (RuntimeSupport::Unknown, Some(origin)) => capabilities.observe(probe_capabilities(origin).await),
        (support, _) => support,
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

async fn request_daemon_shutdown(socket: &Path, pid: u32) -> io::Result<()> {
    let stream = tokio::net::UnixStream::connect(socket).await?;
    let (reader, mut writer) = stream.into_split();
    write_json_line(
        &mut writer,
        &json!({
            "method": "shutdown_if_pid",
            "args": {"expected_pid": pid},
        }),
    )
    .await?;
    let mut reader = BufReader::new(reader);
    let mut line = String::new();
    if reader.read_line(&mut line).await? == 0 {
        return Err(io::Error::new(
            io::ErrorKind::UnexpectedEof,
            "computer-use daemon closed the shutdown connection without a response",
        ));
    }
    let response = serde_json::from_str::<Value>(&line).map_err(io::Error::other)?;
    if response.get("ok").and_then(Value::as_bool) != Some(true) {
        return Err(io::Error::other(
            "computer-use daemon rejected the pid-bound shutdown request",
        ));
    }
    Ok(())
}

async fn stop_daemon_process(mut daemon: Daemon, socket: &Path, diagnostics: &BootstrapLog, stop_timeout: Duration) {
    let deadline = tokio::time::Instant::from_std(Instant::now() + stop_timeout);
    let graceful_shutdown_requested = match daemon.child.id() {
        Some(pid) => timeout_at(deadline, request_daemon_shutdown(socket, pid))
            .await
            .is_ok_and(|result| result.is_ok()),
        None => false,
    };
    if !graceful_shutdown_requested {
        drop(daemon.stdin.take());
    }
    if timeout_at(deadline, daemon.child.wait()).await.is_err() {
        let _ = daemon.child.kill().await;
        let _ = daemon.child.wait().await;
    }
    drop(daemon.stdin.take());
    if let Ok(Ok(stderr_tail)) = timeout(stop_timeout, daemon.stderr_tail).await {
        record_daemon_stderr(diagnostics, &stderr_tail);
    }
    let _ = std::fs::remove_file(socket);
}

fn spawn_daemon(driver: &Path, socket: &Path, host_bundle_id: &str, policy: &Path) -> io::Result<Daemon> {
    let mut command = driver_command(driver);
    command
        .args([
            "serve",
            "--embedded",
            "--parent-liveness-stdio",
            "--no-permissions-gate",
            "--socket",
        ])
        .arg(socket)
        .args(["--host-bundle-id", host_bundle_id, "--permission-mode", "standard"])
        .env("CUA_DRIVER_EMBEDDED_HOST_PID", std::process::id().to_string())
        .env("CUA_DRIVER_MANAGED_POLICY_FILE", policy)
        .stdin(Stdio::piped())
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .kill_on_drop(true);
    let mut child = command.spawn()?;
    let stdin = child.stdin.take();
    let stderr = child
        .stderr
        .take()
        .ok_or_else(|| io::Error::other("computer-use daemon stderr pipe was not available"))?;
    let stderr_tail = tokio::spawn(capture_stderr_tail(stderr));
    Ok(Daemon {
        child,
        stdin,
        stderr_tail,
    })
}

async fn capture_stderr_tail(mut stderr: ChildStderr) -> Vec<u8> {
    let mut tail = Vec::with_capacity(DAEMON_STDERR_TAIL_BYTES);
    let mut chunk = [0_u8; 4096];
    loop {
        let read = match tokio::io::AsyncReadExt::read(&mut stderr, &mut chunk).await {
            Ok(0) | Err(_) => break,
            Ok(read) => read,
        };
        if read >= DAEMON_STDERR_TAIL_BYTES {
            tail.clear();
            tail.extend_from_slice(&chunk[read - DAEMON_STDERR_TAIL_BYTES..read]);
            continue;
        }
        let overflow = (tail.len() + read).saturating_sub(DAEMON_STDERR_TAIL_BYTES);
        if overflow > 0 {
            tail.drain(..overflow);
        }
        tail.extend_from_slice(&chunk[..read]);
    }
    tail
}

fn record_daemon_stderr(diagnostics: &BootstrapLog, stderr_tail: &[u8]) {
    if stderr_tail.is_empty() {
        return;
    }
    diagnostics.record(
        "computer-use.daemon-stderr",
        &[("tail", String::from_utf8_lossy(stderr_tail).into_owned())],
    );
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

    let mut command = driver_command(driver);
    command
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
        .env("CUA_DRIVER_EMBEDDED_HOST_PID", std::process::id().to_string())
        .env("CUA_DRIVER_MANAGED_POLICY_FILE", policy)
        .env("CUA_DRIVER_PERMISSION_MODE", "standard")
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
    let mut command = driver_command(driver);
    command
        .args(["mcp", "--embedded", "--socket"])
        .arg(socket)
        .args(["--host-bundle-id", host_bundle_id])
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
    use std::collections::BTreeMap;
    use std::os::unix::fs::PermissionsExt;

    #[tokio::test]
    async fn all_driver_launch_boundaries_clear_parent_credentials() {
        const CHILD: &str = "AVIBE_TEST_DRIVER_BOUNDARY_CHILD";
        const ROOT: &str = "AVIBE_TEST_DRIVER_BOUNDARY_ROOT";
        const MARKER: &str = "AVIBE_TEST_PROVIDER_CREDENTIAL_DO_NOT_FORWARD";
        if std::env::var_os(CHILD).is_none() {
            let temporary = tempfile::tempdir().expect("temporary directory");
            for directory in ["home", "config", "data", "state", "cache", "tmp"] {
                std::fs::create_dir_all(temporary.path().join(directory)).expect("isolated test directory");
            }
            let status = std::process::Command::new(std::env::current_exe().expect("current test executable"))
                .args([
                    "--exact",
                    "computer_use::tests::all_driver_launch_boundaries_clear_parent_credentials",
                    "--nocapture",
                ])
                .env(CHILD, "1")
                .env(ROOT, temporary.path())
                .env(MARKER, "fixture-secret")
                .env("HOME", temporary.path().join("home"))
                .env("USERPROFILE", temporary.path().join("home"))
                .env("XDG_CONFIG_HOME", temporary.path().join("config"))
                .env("XDG_DATA_HOME", temporary.path().join("data"))
                .env("XDG_STATE_HOME", temporary.path().join("state"))
                .env("XDG_CACHE_HOME", temporary.path().join("cache"))
                .env("TMPDIR", temporary.path().join("tmp"))
                .status()
                .expect("run isolated launch-boundary test");
            assert!(status.success());
            return;
        }

        let root = PathBuf::from(std::env::var_os(ROOT).expect("isolated root"));
        let state_dir = root.join("computer-use-state");
        let policy = root.join("policy.yaml");
        let socket = root.join("computer-use.sock");
        std::fs::write(&policy, "version: 1\n").expect("policy fixture");
        let driver = write_environment_driver(&root);

        let mut daemon =
            spawn_daemon(&driver, &socket, "bot.avibe.desktop.test", &policy).expect("spawn daemon fixture");
        assert!(daemon.child.wait().await.expect("daemon fixture exit").success());

        let (cancel, cancelled) = oneshot::channel();
        let permission = run_permission_child(
            &driver,
            &state_dir,
            &policy,
            "bot.avibe.desktop.test",
            Duration::from_secs(1),
            cancelled,
        )
        .await;
        drop(cancel);
        assert_eq!(permission.outcome, PermissionChildOutcome::CaptureRejected);

        assert_eq!(
            health_check(&driver, &socket, "bot.avibe.desktop.test", false).await,
            HealthResult::Pass
        );

        let daemon_environment = read_environment(&root.join("daemon.env"));
        let permission_environment = read_environment(&root.join("permission.env"));
        let health_environment = read_environment(&root.join("health.env"));
        for environment in [&daemon_environment, &permission_environment, &health_environment] {
            assert!(!environment.contains_key(MARKER));
            assert_shared_driver_environment(environment);
        }

        assert_eq!(
            daemon_environment.get("CUA_DRIVER_EMBEDDED_HOST_PID"),
            Some(&std::process::id().to_string())
        );
        assert_eq!(
            daemon_environment.get("CUA_DRIVER_MANAGED_POLICY_FILE"),
            Some(&policy.display().to_string())
        );
        assert!(!daemon_environment.contains_key("HOME"));

        let permission_root = state_dir.join("permission-probe");
        for (name, expected) in [
            ("HOME", permission_root.clone()),
            ("USERPROFILE", permission_root.clone()),
            ("XDG_CONFIG_HOME", permission_root.join("config")),
            ("XDG_DATA_HOME", permission_root.join("data")),
            ("XDG_STATE_HOME", permission_root.join("state")),
            ("XDG_CACHE_HOME", permission_root.join("cache")),
            ("TMPDIR", permission_root.join("tmp")),
            ("CUA_DRIVER_RS_HOME", permission_root.join("driver")),
            ("CUA_DRIVER_TELEMETRY_HOME", permission_root.join("telemetry")),
        ] {
            assert_eq!(
                permission_environment.get(name),
                Some(&expected.display().to_string()),
                "{name} stays inside the test-owned permission root"
            );
        }
        assert_eq!(
            permission_environment.get("CUA_DRIVER_EMBEDDED_HOST_PID"),
            Some(&std::process::id().to_string())
        );
        assert_eq!(
            permission_environment.get("CUA_DRIVER_MANAGED_POLICY_FILE"),
            Some(&policy.display().to_string())
        );
        assert_eq!(
            permission_environment
                .get("CUA_DRIVER_PERMISSION_MODE")
                .map(String::as_str),
            Some("standard")
        );
        assert!(!health_environment.contains_key("HOME"));
    }

    fn shell_quote(value: &Path) -> String {
        format!("'{}'", value.display().to_string().replace('\'', "'\"'\"'"))
    }

    fn write_environment_driver(root: &Path) -> PathBuf {
        let path = root.join("fake-cua-driver");
        let body = r#"#!/bin/sh
set -eu
case "${1:-}" in
  serve)
    /usr/bin/env > __DAEMON_ENV__
    exit 0
    ;;
  mcp)
    if [ "${2:-}" = "--direct" ]; then
      /usr/bin/env > __PERMISSION_ENV__
      while IFS= read -r line; do
        case "$line" in
          *'"id":1'*)
            printf '%s\n' '{"jsonrpc":"2.0","id":1,"result":{}}'
            ;;
          *'"id":2'*)
            printf '%s\n' '{"jsonrpc":"2.0","id":2,"result":{"isError":true,"content":[{"type":"text","text":"permission fixture"}]}}'
            break
            ;;
        esac
      done
    else
      /usr/bin/env > __HEALTH_ENV__
      while IFS= read -r line; do
        case "$line" in
          *'"id":1'*)
            printf '%s\n' '{"jsonrpc":"2.0","id":1,"result":{}}'
            ;;
          *'"id":2'*)
            printf '%s\n' '{"jsonrpc":"2.0","id":2,"result":{"structuredContent":{"checks":[{"name":"tcc_accessibility","status":"pass"},{"name":"tcc_screen_recording","status":"pass"},{"name":"bundle_identity","status":"pass","data":{"identity_source":"parent_application"}},{"name":"ax_capability","status":"pass"}]}}}'
            break
            ;;
        esac
      done
    fi
    ;;
  *)
    exit 64
    ;;
esac
"#
        .replace("__DAEMON_ENV__", &shell_quote(&root.join("daemon.env")))
        .replace(
            "__PERMISSION_ENV__",
            &shell_quote(&root.join("permission.env")),
        )
        .replace("__HEALTH_ENV__", &shell_quote(&root.join("health.env")));
        std::fs::write(&path, body).expect("fake driver");
        let mut permissions = std::fs::metadata(&path).expect("fake driver metadata").permissions();
        permissions.set_mode(0o755);
        std::fs::set_permissions(&path, permissions).expect("fake driver executable");
        path
    }

    fn write_graceful_daemon_driver(root: &Path, socket: &Path, request_path: &Path) -> PathBuf {
        let path = root.join("fake-graceful-cua-driver");
        let executable = std::env::current_exe().expect("current test executable");
        let body = format!(
            "#!/bin/sh\n\
             AVIBE_TEST_GRACEFUL_DAEMON_CHILD=1 \\\n\
             AVIBE_TEST_GRACEFUL_DAEMON_SOCKET={} \\\n\
             AVIBE_TEST_GRACEFUL_DAEMON_REQUEST={} \\\n\
             exec {} --exact computer_use::tests::intentional_stop_daemon_child --nocapture\n",
            shell_quote(socket),
            shell_quote(request_path),
            shell_quote(&executable),
        );
        std::fs::write(&path, body).expect("fake graceful driver");
        let mut permissions = std::fs::metadata(&path)
            .expect("fake graceful driver metadata")
            .permissions();
        permissions.set_mode(0o755);
        std::fs::set_permissions(&path, permissions).expect("fake graceful driver executable");
        path
    }

    fn read_environment(path: &Path) -> BTreeMap<String, String> {
        std::fs::read_to_string(path)
            .unwrap_or_else(|error| panic!("{} is readable: {error}", path.display()))
            .lines()
            .map(|line| {
                let (name, value) = line.split_once('=').expect("environment entry");
                (name.to_owned(), value.to_owned())
            })
            .collect()
    }

    fn pinned_shutdown_response(request: &Value, actual_pid: u32) -> (Value, bool) {
        let expected_pid = request
            .pointer("/args/expected_pid")
            .and_then(Value::as_u64)
            .and_then(|pid| u32::try_from(pid).ok())
            .filter(|pid| *pid != 0);
        let Some(expected_pid) = expected_pid else {
            return (
                json!({
                    "ok": false,
                    "error": "shutdown_if_pid requires a positive integer expected_pid",
                    "exit_code": 64,
                }),
                false,
            );
        };
        if expected_pid != actual_pid {
            return (
                json!({
                    "ok": false,
                    "error": format!(
                        "daemon pid mismatch (expected {expected_pid}, found {actual_pid})"
                    ),
                    "exit_code": 1,
                }),
                false,
            );
        }
        (json!({"ok": true, "result": {"shutdown": true}}), true)
    }

    #[test]
    fn intentional_stop_pinned_shutdown_contract_rejects_legacy_pid_field() {
        let (response, should_shutdown) = pinned_shutdown_response(
            &json!({
                "method": "shutdown_if_pid",
                "args": {"pid": std::process::id()},
            }),
            std::process::id(),
        );

        assert!(!should_shutdown);
        assert_eq!(response.get("ok").and_then(Value::as_bool), Some(false));
        assert_eq!(response.get("exit_code").and_then(Value::as_u64), Some(64));
        assert_eq!(
            response.get("error").and_then(Value::as_str),
            Some("shutdown_if_pid requires a positive integer expected_pid")
        );
    }

    #[tokio::test]
    async fn intentional_stop_daemon_child() {
        if std::env::var_os("AVIBE_TEST_GRACEFUL_DAEMON_CHILD").is_none() {
            return;
        }
        let socket = PathBuf::from(std::env::var_os("AVIBE_TEST_GRACEFUL_DAEMON_SOCKET").expect("socket path"));
        let request_path = PathBuf::from(std::env::var_os("AVIBE_TEST_GRACEFUL_DAEMON_REQUEST").expect("request path"));
        let listener = tokio::net::UnixListener::bind(&socket).expect("bind fake daemon");
        let (stream, _) = listener.accept().await.expect("accept shutdown request");
        let (reader, mut writer) = stream.into_split();
        let mut reader = BufReader::new(reader);
        let mut line = String::new();
        assert_ne!(reader.read_line(&mut line).await.expect("read shutdown request"), 0);
        let request: Value = serde_json::from_str(&line).expect("parse shutdown request");
        assert_eq!(request.get("method").and_then(Value::as_str), Some("shutdown_if_pid"));
        std::fs::write(&request_path, &line).expect("record shutdown request");
        let (response, should_shutdown) = pinned_shutdown_response(&request, std::process::id());
        write_json_line(&mut writer, &response)
            .await
            .expect("write pinned shutdown response");
        if !should_shutdown {
            let mut stdin = std::io::stdin();
            let mut byte = [0_u8; 1];
            let _ = stdin.read(&mut byte);
        }
    }

    #[tokio::test]
    async fn intentional_stop_requests_graceful_shutdown_before_closing_parent_liveness() {
        let temporary = tempfile::tempdir().expect("temporary directory");
        let socket = temporary.path().join("driver.sock");
        let request_path = temporary.path().join("shutdown-request.json");
        let policy = temporary.path().join("policy.yaml");
        std::fs::write(&policy, "version: 1\n").expect("policy fixture");
        let driver = write_graceful_daemon_driver(temporary.path(), &socket, &request_path);
        let daemon =
            spawn_daemon(&driver, &socket, "bot.avibe.desktop.test", &policy).expect("spawn graceful daemon fixture");
        let daemon_pid = daemon.child.id().expect("spawned daemon pid");
        let deadline = Instant::now() + Duration::from_secs(5);
        while !socket.exists() {
            assert!(Instant::now() < deadline, "timed out waiting for fake daemon socket");
            sleep(Duration::from_millis(20)).await;
        }

        let diagnostics = BootstrapLog::at(temporary.path().join(BOOTSTRAP_LOG_NAME));
        stop_daemon_process(daemon, &socket, &diagnostics, Duration::from_secs(3)).await;

        let request: Value = serde_json::from_slice(&std::fs::read(&request_path).expect("graceful shutdown request"))
            .expect("recorded request");
        assert_eq!(request.get("method").and_then(Value::as_str), Some("shutdown_if_pid"));
        assert_eq!(
            request.pointer("/args/expected_pid").and_then(Value::as_u64),
            Some(daemon_pid.into())
        );
        assert!(
            request.pointer("/args/pid").is_none(),
            "legacy pid field bypassed the pinned shutdown parser contract"
        );
        assert!(!socket.exists(), "stopped daemon socket survived cleanup");
    }

    fn assert_shared_driver_environment(environment: &BTreeMap<String, String>) {
        for (name, expected) in [
            ("CUA_DRIVER_EMBEDDED", "1"),
            ("CUA_DRIVER_RS_TELEMETRY_ENABLED", "0"),
            ("CUA_DRIVER_RS_UPDATE_CHECK", "0"),
        ] {
            assert_eq!(environment.get(name).map(String::as_str), Some(expected));
        }
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
    fn socket_path_is_short_and_isolated_by_state_directory_flavor() {
        let first = short_socket_path(Path::new("/tmp/first-computer-use-state"), 501);
        let second = short_socket_path(Path::new("/tmp/second-computer-use-state"), 501);
        let foreground = short_foreground_control_socket_path(Path::new("/tmp/first-computer-use-state"), 501);

        assert_ne!(first, second);
        assert_eq!(
            first.file_name().and_then(|name| name.to_str()),
            Some(COMPUTER_USE_SOCKET_FILE)
        );
        assert!(first.as_os_str().as_bytes().len() <= DARWIN_SOCKET_PATH_MAX_BYTES);
        assert!(first
            .parent()
            .and_then(Path::to_str)
            .unwrap()
            .starts_with("/tmp/avibe-cua-"));
        assert_eq!(foreground.parent(), first.parent());
        assert_eq!(
            foreground.file_name().and_then(|name| name.to_str()),
            Some(FOREGROUND_CONTROL_SOCKET_FILE)
        );
        assert!(foreground.as_os_str().as_bytes().len() <= DARWIN_SOCKET_PATH_MAX_BYTES);
    }

    #[test]
    fn socket_directory_is_private_and_owned_by_the_state_directory_owner() {
        let temporary = tempfile::tempdir().expect("temporary directory");
        let state_dir = temporary.path().join("state");
        std::fs::create_dir_all(&state_dir).expect("state directory");
        let owner_uid = std::fs::metadata(&state_dir).expect("state metadata").uid();
        let socket_dir = short_socket_path(&state_dir, owner_uid)
            .parent()
            .expect("socket directory")
            .to_owned();

        ensure_private_socket_directory(&socket_dir, owner_uid).expect("private socket directory");

        let metadata = std::fs::metadata(socket_dir).expect("socket directory metadata");
        assert_eq!(metadata.uid(), owner_uid);
        assert_eq!(metadata.permissions().mode() & 0o777, SOCKET_DIRECTORY_MODE);
        std::fs::remove_dir(short_socket_path(&state_dir, owner_uid).parent().unwrap())
            .expect("remove socket directory");
    }

    #[test]
    fn over_limit_socket_paths_are_classified_before_spawn() {
        let path = PathBuf::from("/tmp").join("x".repeat(DARWIN_SOCKET_PATH_MAX_BYTES));
        assert_eq!(socket_path_preflight(&path), Err("socket_path_too_long"));
    }

    #[test]
    fn foreground_display_titles_are_utf8_safe_and_never_block_admission() {
        assert_eq!(foreground_display_title("ses-a", "  "), "ses-a");
        let title = "前台控制".repeat(80);
        let truncated = foreground_display_title("ses-a", &title);
        assert!(truncated.len() <= FOREGROUND_TITLE_LIMIT);
        assert!(title.starts_with(&truncated));
        assert!(std::str::from_utf8(truncated.as_bytes()).is_ok());
    }

    #[test]
    fn foreground_restoration_contract_requires_three_component_entries() {
        let contract = json!({
            "components": {
                "prior_frontmost": {
                    "recorded": {"pid": 3, "process_start_stamp": "start-3", "window_id": 30},
                    "attempted": false,
                    "verified": false,
                    "failure_detail": null
                },
                "pointer": {
                    "recorded": {"x": 10.0, "y": 20.0},
                    "attempted": false,
                    "verified": false,
                    "failure_detail": null
                },
                "active_space": {
                    "recorded": {
                        "prior_frontmost_display": 1,
                        "target_display": 1
                    },
                    "attempted": false,
                    "verified": false,
                    "failure_detail": null
                }
            },
            "restoration_ok": false
        });
        let parsed = parse_foreground_restoration(Some(&contract)).expect("complete restoration contract");
        assert_eq!(
            (
                parsed.components.prior_frontmost.recorded.pid,
                parsed.components.prior_frontmost.recorded.window_id
            ),
            (3, 30)
        );
        assert_eq!(
            (
                parsed.components.pointer.recorded.x,
                parsed.components.pointer.recorded.y
            ),
            (10.0, 20.0)
        );
        assert_eq!(
            parsed.components.active_space.recorded,
            BTreeMap::from([
                ("prior_frontmost_display".to_owned(), 1),
                ("target_display".to_owned(), 1),
            ])
        );
        assert!(!parsed.restoration_ok);

        for missing in ["prior_frontmost", "pointer", "active_space"] {
            let mut incomplete = contract.clone();
            incomplete["components"]
                .as_object_mut()
                .expect("component object")
                .remove(missing);
            assert!(parse_foreground_restoration(Some(&incomplete)).is_err());
        }
        let mut incomplete = contract.clone();
        incomplete
            .as_object_mut()
            .expect("contract object")
            .remove("restoration_ok");
        assert!(parse_foreground_restoration(Some(&incomplete)).is_err());
    }

    #[test]
    fn foreground_exit_inventory_routes_every_reason_through_bounded_stop_wait() {
        let codes: Vec<_> = ForegroundExitReason::ALL
            .into_iter()
            .map(ForegroundExitReason::code)
            .collect();
        let mut unique = codes.clone();
        unique.sort_unstable();
        unique.dedup();

        assert_eq!(codes.len(), 9);
        assert_eq!(unique.len(), codes.len());
        assert!(codes.iter().all(|code| !code.is_empty()));
    }

    #[test]
    fn foreground_preference_defaults_off_and_persists_only_explicit_owner_choices() {
        let directory = tempfile::tempdir().expect("foreground preference directory");
        let path = directory.path().join(FOREGROUND_CONTROL_PREFERENCE_FILE);

        assert!(!read_foreground_preference(&path));
        write_foreground_preference(&path, true).expect("enable foreground preference");
        assert!(read_foreground_preference(&path));
        write_foreground_preference(&path, false).expect("disable foreground preference");
        assert!(!read_foreground_preference(&path));

        std::fs::write(&path, "invalid").expect("invalid foreground preference");
        assert!(!read_foreground_preference(&path));
    }

    #[test]
    fn foreground_attention_is_latched_until_owner_dismissal() {
        let cases = [
            (
                ForegroundAttentionEvent::RequestConsent,
                ForegroundAttentionOutcome::Refused,
                true,
            ),
            (
                ForegroundAttentionEvent::SetActive,
                ForegroundAttentionOutcome::Refused,
                true,
            ),
            (
                ForegroundAttentionEvent::ComputerUseToggle,
                ForegroundAttentionOutcome::Kept,
                true,
            ),
            (
                ForegroundAttentionEvent::ForegroundSettingChange,
                ForegroundAttentionOutcome::Kept,
                true,
            ),
            (
                ForegroundAttentionEvent::ConsentChange,
                ForegroundAttentionOutcome::Kept,
                true,
            ),
            (
                ForegroundAttentionEvent::SessionChange,
                ForegroundAttentionOutcome::Kept,
                true,
            ),
            (
                ForegroundAttentionEvent::ComputerUseStopStart,
                ForegroundAttentionOutcome::Kept,
                true,
            ),
            (
                ForegroundAttentionEvent::OwnerDismiss,
                ForegroundAttentionOutcome::Dismissed,
                false,
            ),
        ];

        for (event, expected_outcome, remains_latched) in cases {
            let mut latch = ForegroundAttentionLatch::default();
            latch.latch(ForegroundAttention {
                target: "QQ conversation: QQ".to_owned(),
                restoration: json!({"restoration_ok": false}),
            });

            assert_eq!(latch.transition(event), expected_outcome);
            assert_eq!(latch.current().is_some(), remains_latched);
        }
    }

    #[test]
    fn window_identity_verification_rejects_process_reuse() {
        let identity = WindowIdentity {
            pid: 7,
            process_start_stamp: "start-7".to_owned(),
            window_id: 9,
        };
        for (stamp, expected_ok) in [(Some("start-7"), true), (Some("replacement-7"), false), (None, false)] {
            let result = verify_process_identity(&identity, |_| stamp.map(str::to_owned));
            assert_eq!(result.is_ok(), expected_ok);
        }
    }

    #[test]
    fn terminal_failure_preserves_existing_component_details_and_marks_all_components() {
        let contract = json!({
            "components": {
                "prior_frontmost": {
                    "recorded": {
                        "pid": 3,
                        "process_start_stamp": "start-3",
                        "window_id": 30
                    },
                    "attempted": true,
                    "verified": false,
                    "failure_detail": "identity changed"
                },
                "pointer": {
                    "recorded": {"x": 10.0, "y": 20.0},
                    "attempted": false,
                    "verified": false,
                    "failure_detail": null
                },
                "active_space": {
                    "recorded": {
                        "prior_frontmost_display": 1,
                        "target_display": 1
                    },
                    "attempted": false,
                    "verified": false,
                    "failure_detail": null
                }
            },
            "restoration_ok": false
        });
        let mut restoration = parse_foreground_restoration(Some(&contract)).expect("restoration contract");

        fail_restoration(&mut restoration, "teardown timed out");

        assert_eq!(
            restoration.components.prior_frontmost.failure_detail.as_deref(),
            Some("identity changed; teardown timed out")
        );
        assert!(restoration.components.pointer.attempted);
        assert!(restoration.components.active_space.attempted);
        assert!(!restoration.restoration_ok);
    }

    #[tokio::test]
    async fn foreground_transaction_waiter_observes_terminal_transition() {
        let target = WindowIdentity {
            pid: 7,
            process_start_stamp: "start-7".to_owned(),
            window_id: 9,
        };
        let restoration = parse_foreground_restoration(Some(&json!({
            "components": {
                "prior_frontmost": {
                    "recorded": {
                        "pid": 3,
                        "process_start_stamp": "start-3",
                        "window_id": 30
                    },
                    "attempted": false,
                    "verified": false,
                    "failure_detail": null
                },
                "pointer": {
                    "recorded": {"x": 10.0, "y": 20.0},
                    "attempted": false,
                    "verified": false,
                    "failure_detail": null
                },
                "active_space": {
                    "recorded": {
                        "prior_frontmost_display": 1,
                        "target_display": 1
                    },
                    "attempted": false,
                    "verified": false,
                    "failure_detail": null
                }
            },
            "restoration_ok": false
        })))
        .expect("restoration contract");
        let mut transaction =
            ForegroundActivity::new(1, "session".to_owned(), target, "session: App".to_owned(), restoration);
        let wait = transaction.finish_and_await_terminal(Duration::from_secs(1));

        transaction.mark_terminal();

        assert!(wait.await);
    }

    #[tokio::test]
    async fn daemon_stderr_tail_is_bounded_and_reaches_shell_diagnostics() {
        let temporary = tempfile::tempdir().expect("temporary directory");
        let driver = temporary.path().join("fake-cua-driver");
        std::fs::write(
            &driver,
            "#!/bin/sh\nprintf 'daemon failed: %s\\n' 'socket unavailable' >&2\nexit 1\n",
        )
        .expect("stderr fixture");
        let mut permissions = std::fs::metadata(&driver)
            .expect("stderr fixture metadata")
            .permissions();
        permissions.set_mode(0o755);
        std::fs::set_permissions(&driver, permissions).expect("stderr fixture executable");
        let policy = temporary.path().join("policy.yaml");
        std::fs::write(&policy, "version: 1\n").expect("policy fixture");
        let mut daemon = spawn_daemon(
            &driver,
            &temporary.path().join("driver.sock"),
            "bot.avibe.desktop.test",
            &policy,
        )
        .expect("spawn stderr fixture");
        daemon.child.wait().await.expect("stderr fixture exit");
        let tail = daemon.stderr_tail.await.expect("stderr reader");

        let path = temporary.path().join(BOOTSTRAP_LOG_NAME);
        let diagnostics = BootstrapLog::at(path.clone());
        record_daemon_stderr(&diagnostics, &tail);

        let written = std::fs::read_to_string(path).expect("diagnostic log");
        assert!(tail.len() <= DAEMON_STDERR_TAIL_BYTES);
        assert!(written.contains("computer-use.daemon-stderr"));
        assert!(written.contains("socket unavailable"));
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
    fn restored_enablement_with_invalid_assets_cannot_spawn() {
        let record = ComputerUseRecord {
            schema_version: COMPUTER_USE_SCHEMA_VERSION,
            enabled: true,
            state: ComputerUsePhase::NeedsRuntime,
            reason: Some("runtime_unavailable".to_owned()),
            shell_pid: 1,
            instance_id: "restored-shell".to_owned(),
            generation: 4,
            driver_version: COMPUTER_USE_DRIVER_VERSION.to_owned(),
            tool_snapshot: ToolSnapshot {
                path: PathBuf::from("/tmp/tools.json"),
                sha256: COMPUTER_USE_TOOL_SNAPSHOT_SHA256.to_owned(),
            },
            socket_path: None,
            proxy_executable: None,
            host_bundle_id: None,
        };
        let mut lifecycle = restored_lifecycle(&StoredComputerUseState::Current(Box::new(record)), false);

        assert!(lifecycle.enabled());
        assert_eq!(lifecycle.phase(), ComputerUsePhase::Error);
        assert_eq!(lifecycle.reason(), Some("assets_invalid"));
        assert!(
            !lifecycle
                .capabilities(RuntimeSupport::Supported, Grants::all())
                .spawn_daemon
        );
    }

    #[tokio::test]
    async fn normal_runtime_reprobe_relaunches_persisted_enablement_on_the_same_origin() {
        let record = ComputerUseRecord {
            schema_version: COMPUTER_USE_SCHEMA_VERSION,
            enabled: true,
            state: ComputerUsePhase::NeedsRuntime,
            reason: Some("runtime_unavailable".to_owned()),
            shell_pid: 1,
            instance_id: "reprobe-shell".to_owned(),
            generation: 4,
            driver_version: COMPUTER_USE_DRIVER_VERSION.to_owned(),
            tool_snapshot: ToolSnapshot {
                path: PathBuf::from("/tmp/tools.json"),
                sha256: COMPUTER_USE_TOOL_SNAPSHOT_SHA256.to_owned(),
            },
            socket_path: None,
            proxy_executable: None,
            host_bundle_id: None,
        };
        let mut lifecycle = ComputerUseLifecycle::from_record(&record);
        let mut capabilities = CapabilityCache::default();
        capabilities.begin_adoption();

        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("capability listener");
        let address = listener.local_addr().expect("capability listener address");
        let origin = LoopbackOrigin::parse(&format!("http://{address}")).expect("loopback origin");
        let server = tokio::spawn(async move {
            use tokio::io::{AsyncReadExt as _, AsyncWriteExt as _};

            let (mut stream, _) = listener.accept().await.expect("capability request");
            let mut request = [0_u8; 1024];
            let read = stream.read(&mut request).await.expect("capability request bytes");
            assert!(String::from_utf8_lossy(&request[..read]).starts_with("GET /api/desktop/capabilities "));
            let body =
                format!(r#"{{"controller_id":"restarted-runtime","computer_use_schema":{COMPUTER_USE_SCHEMA}}}"#);
            let response = format!(
                "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                body.len()
            );
            stream
                .write_all(response.as_bytes())
                .await
                .expect("capability response");
        });
        let directive = runtime_reprobe_directive(&mut lifecycle, &mut capabilities, Some(&origin), Grants::all())
            .await
            .expect("normal tick reprobe");
        server.await.expect("capability server");

        assert!(directive.spawn_daemon);
        assert!(directive.write_state);
        assert_eq!(capabilities.controller_id(), Some("restarted-runtime"));
        assert_eq!(lifecycle.phase(), ComputerUsePhase::Starting);
        assert_eq!(
            runtime_reprobe_directive(&mut lifecycle, &mut capabilities, Some(&origin), Grants::all(),).await,
            None
        );
    }

    #[tokio::test]
    async fn enabling_reprobes_a_runtime_whose_last_probe_was_transient() {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("capability listener");
        let address = listener.local_addr().expect("capability listener address");
        let origin = LoopbackOrigin::parse(&format!("http://{address}")).expect("loopback origin");
        // The Runtime-ready probe hits a 503; the user's later enable request
        // finds the Runtime ready.
        let server = tokio::spawn(async move {
            use tokio::io::{AsyncReadExt as _, AsyncWriteExt as _};

            let ready = format!(r#"{{"controller_id":"ready-runtime","computer_use_schema":{COMPUTER_USE_SCHEMA}}}"#);
            for (status, body) in [("503 Service Unavailable", "{}".to_owned()), ("200 OK", ready)] {
                let (mut stream, _) = listener.accept().await.expect("capability request");
                let mut request = [0_u8; 1024];
                let read = stream.read(&mut request).await.expect("capability request bytes");
                assert!(String::from_utf8_lossy(&request[..read]).starts_with("GET /api/desktop/capabilities "));
                let response = format!(
                    "HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                    body.len()
                );
                stream
                    .write_all(response.as_bytes())
                    .await
                    .expect("capability response");
            }
        });
        let mut capabilities = CapabilityCache::default();
        capabilities.begin_adoption();

        assert_eq!(
            capabilities.observe(probe_capabilities(&origin).await),
            RuntimeSupport::Unknown
        );
        assert_eq!(
            enable_support(&mut capabilities, Some(&origin)).await,
            RuntimeSupport::Supported
        );
        server.await.expect("capability server");
        assert_eq!(capabilities.controller_id(), Some("ready-runtime"));

        // Without a Runtime there is nothing to ask.
        let mut unknown = CapabilityCache::default();
        assert_eq!(enable_support(&mut unknown, None).await, RuntimeSupport::Unknown);
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

        let first = controller.shutdown(ForegroundExitReason::AppQuit);
        tokio::pin!(first);
        let event = tokio::select! {
            event = receiver.recv() => event.expect("shutdown event"),
            _ = &mut first => panic!("shutdown completed before the actor acknowledged it"),
        };
        let Event::Shutdown { reply, .. } = event else {
            panic!("unexpected computer-use event");
        };
        reply.send(()).expect("shutdown acknowledgement receiver");
        first.await;

        controller.shutdown(ForegroundExitReason::AppQuit).await;
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
            Duration::from_secs(3),
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
