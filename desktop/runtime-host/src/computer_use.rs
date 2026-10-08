//! Desktop-owned state and lifecycle rules for macOS computer use.
//!
//! The Tauri shell supplies native permission checks, HTTP probing, and child
//! processes. This module owns the durable `computer-use.json` shape and the
//! transition rules so tests can exercise them without a window server or TCC.

use std::collections::VecDeque;
use std::fs::{self, File, OpenOptions};
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Duration;

use serde::{Deserialize, Serialize};

pub const COMPUTER_USE_SCHEMA_VERSION: u32 = 1;
pub const COMPUTER_USE_STATE_FILE: &str = "computer-use.json";
pub const COMPUTER_USE_LOCK_FILE: &str = "computer-use.lock";
pub const COMPUTER_USE_SOCKET_FILE: &str = "cua-driver.sock";
pub const COMPUTER_USE_DRIVER_VERSION: &str = "0.31.0";
pub const COMPUTER_USE_TOOL_SNAPSHOT_SHA256: &str = "b03c3e48d1b00c8fe7c0e8b9813eb5ea104ad38313672fc4b68af203a827f43b";
pub const RUNTIME_TOO_OLD_REASON: &str = "runtime_too_old";
pub const RUNTIME_UNAVAILABLE_REASON: &str = "runtime_unavailable";
pub const FAILURE_LIMIT: usize = 3;
pub const FAILURE_WINDOW: Duration = Duration::from_secs(5 * 60);

static TEMP_FILE_SEQUENCE: AtomicU64 = AtomicU64::new(0);

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ComputerUsePhase {
    Off,
    NeedsPermission,
    Starting,
    Ready,
    Error,
    Stopped,
    NeedsRuntime,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub struct ToolSnapshot {
    pub path: PathBuf,
    pub sha256: String,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub struct ComputerUseRecord {
    pub schema_version: u32,
    pub enabled: bool,
    pub state: ComputerUsePhase,
    pub reason: Option<String>,
    pub shell_pid: u32,
    pub instance_id: String,
    pub generation: u64,
    pub driver_version: String,
    pub tool_snapshot: ToolSnapshot,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub socket_path: Option<PathBuf>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub proxy_executable: Option<PathBuf>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub host_bundle_id: Option<String>,
}

impl ComputerUseRecord {
    pub fn validate(&self) -> Result<(), String> {
        if self.schema_version != COMPUTER_USE_SCHEMA_VERSION {
            return Err(format!("unsupported schema_version: {}", self.schema_version));
        }
        if self.shell_pid == 0 {
            return Err("shell_pid must be positive".to_owned());
        }
        if self.instance_id.trim().is_empty() {
            return Err("instance_id must be non-empty".to_owned());
        }
        if self.driver_version.trim().is_empty() {
            return Err("driver_version must be non-empty".to_owned());
        }
        if !self.tool_snapshot.path.is_absolute() {
            return Err("tool_snapshot.path must be absolute".to_owned());
        }
        if self.tool_snapshot.sha256.len() != 64
            || !self.tool_snapshot.sha256.bytes().all(|byte| byte.is_ascii_hexdigit())
        {
            return Err("tool_snapshot.sha256 must be a SHA-256 hex digest".to_owned());
        }
        if self.state == ComputerUsePhase::Ready {
            let required_path = |value: &Option<PathBuf>, name: &str| -> Result<(), String> {
                if value.as_ref().is_some_and(|path| path.is_absolute()) {
                    Ok(())
                } else {
                    Err(format!("{name} must be an absolute path in ready state"))
                }
            };
            required_path(&self.socket_path, "socket_path")?;
            required_path(&self.proxy_executable, "proxy_executable")?;
            if self.host_bundle_id.as_deref().is_none_or(str::is_empty) {
                return Err("host_bundle_id must be non-empty in ready state".to_owned());
            }
        }
        Ok(())
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum StoredComputerUseState {
    Missing,
    Current(Box<ComputerUseRecord>),
    Invalid(String),
}

#[derive(Clone, Debug)]
pub struct ComputerUseStateStore {
    path: PathBuf,
}

impl ComputerUseStateStore {
    pub fn new(path: PathBuf) -> Self {
        Self { path }
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    pub fn read(&self) -> StoredComputerUseState {
        let bytes = match fs::read(&self.path) {
            Ok(bytes) => bytes,
            Err(error) if error.kind() == io::ErrorKind::NotFound => {
                return StoredComputerUseState::Missing;
            }
            Err(error) => {
                return StoredComputerUseState::Invalid(format!("state file is unreadable: {error}"));
            }
        };
        let record = match serde_json::from_slice::<ComputerUseRecord>(&bytes) {
            Ok(record) => record,
            Err(error) => {
                return StoredComputerUseState::Invalid(format!("state file is malformed: {error}"));
            }
        };
        match record.validate() {
            Ok(()) => StoredComputerUseState::Current(Box::new(record)),
            Err(error) => StoredComputerUseState::Invalid(error),
        }
    }

    pub fn write(&self, record: &ComputerUseRecord) -> io::Result<()> {
        record
            .validate()
            .map_err(|error| io::Error::new(io::ErrorKind::InvalidInput, error))?;
        let parent = self
            .path
            .parent()
            .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "state path has no parent"))?;
        fs::create_dir_all(parent)?;
        let suffix = TEMP_FILE_SEQUENCE.fetch_add(1, Ordering::Relaxed);
        let temporary = parent.join(format!(
            ".{COMPUTER_USE_STATE_FILE}.{}.{}.tmp",
            std::process::id(),
            suffix
        ));
        let mut file = OpenOptions::new().create_new(true).write(true).open(&temporary)?;
        let mut payload = serde_json::to_vec(record).map_err(io::Error::other)?;
        payload.push(b'\n');
        if let Err(error) = (|| {
            file.write_all(&payload)?;
            file.sync_all()?;
            drop(file);
            fs::rename(&temporary, &self.path)
        })() {
            let _ = fs::remove_file(&temporary);
            return Err(error);
        }
        Ok(())
    }
}

/// The shell holds this file lock for its whole lifetime.
///
/// `std::fs::File` maps this operation to `flock` on Unix and `LockFileEx` on
/// Windows. Phase 1 uses it on macOS; the cross-platform implementation keeps
/// the state contract ready for the deferred Windows lane.
pub struct ComputerUseShellLock {
    _file: File,
}

impl ComputerUseShellLock {
    pub fn acquire(path: &Path) -> io::Result<Self> {
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent)?;
        }
        let file = OpenOptions::new()
            .create(true)
            .truncate(false)
            .read(true)
            .write(true)
            .open(path)?;
        file.try_lock()?;
        Ok(Self { _file: file })
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum RuntimeSupport {
    Unknown,
    Supported,
    Unsupported,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum CapabilityVerdict {
    Supported,
    Unsupported,
    Transient,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct CapabilityProbe {
    pub controller_id: Option<String>,
    pub verdict: CapabilityVerdict,
}

#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct CapabilityCache {
    controller_id: Option<String>,
    support: Option<bool>,
}

impl CapabilityCache {
    pub fn begin_adoption(&mut self) {
        self.controller_id = None;
        self.support = None;
    }

    pub fn observe(&mut self, probe: CapabilityProbe) -> RuntimeSupport {
        if probe.controller_id.is_none() && probe.verdict != CapabilityVerdict::Transient {
            return self.current();
        }
        if let Some(controller_id) = probe.controller_id {
            if self.controller_id.as_deref() != Some(&controller_id) {
                self.controller_id = Some(controller_id);
                self.support = None;
            }
        }
        match probe.verdict {
            CapabilityVerdict::Supported => self.support = Some(true),
            CapabilityVerdict::Unsupported => self.support = Some(false),
            CapabilityVerdict::Transient => {}
        }
        self.current()
    }

    pub fn current(&self) -> RuntimeSupport {
        match self.support {
            Some(true) => RuntimeSupport::Supported,
            Some(false) => RuntimeSupport::Unsupported,
            None => RuntimeSupport::Unknown,
        }
    }

    pub fn controller_id(&self) -> Option<&str> {
        self.controller_id.as_deref()
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct Grants {
    pub accessibility: bool,
    pub screen_recording: bool,
}

impl Grants {
    pub const fn all() -> Self {
        Self {
            accessibility: true,
            screen_recording: true,
        }
    }

    pub fn missing_reason(self) -> Option<&'static str> {
        if !self.accessibility {
            Some("accessibility")
        } else if !self.screen_recording {
            Some("screen_recording")
        } else {
            None
        }
    }
}

#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct LifecycleDirective {
    pub write_state: bool,
    pub prompt_permissions: bool,
    pub spawn_permission_probe: bool,
    pub stop_permission_probe: bool,
    pub spawn_daemon: bool,
    pub stop_daemon: bool,
    pub runtime_refused: bool,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum HealthResult {
    Pass,
    MissingGrant(String),
    Unhealthy(String),
}

#[derive(Clone, Debug)]
pub struct FailureBudget {
    failures: VecDeque<Duration>,
}

impl Default for FailureBudget {
    fn default() -> Self {
        Self {
            failures: VecDeque::with_capacity(FAILURE_LIMIT),
        }
    }
}

impl FailureBudget {
    pub fn record(&mut self, now: Duration) -> bool {
        while self
            .failures
            .front()
            .is_some_and(|failure| now.saturating_sub(*failure) > FAILURE_WINDOW)
        {
            self.failures.pop_front();
        }
        self.failures.push_back(now);
        self.failures.len() >= FAILURE_LIMIT
    }

    pub fn clear(&mut self) {
        self.failures.clear();
    }
}

#[derive(Clone, Debug)]
pub struct ComputerUseLifecycle {
    enabled: bool,
    phase: ComputerUsePhase,
    reason: Option<String>,
    permission_edge_armed: bool,
    failure_budget: FailureBudget,
    consecutive_ready_failures: u8,
}

impl ComputerUseLifecycle {
    pub fn off() -> Self {
        Self {
            enabled: false,
            phase: ComputerUsePhase::Off,
            reason: None,
            permission_edge_armed: false,
            failure_budget: FailureBudget::default(),
            consecutive_ready_failures: 0,
        }
    }

    pub fn from_record(record: &ComputerUseRecord) -> Self {
        let phase = if !record.enabled {
            ComputerUsePhase::Off
        } else if record.state == ComputerUsePhase::Error {
            ComputerUsePhase::Error
        } else {
            ComputerUsePhase::NeedsRuntime
        };
        Self {
            enabled: record.enabled,
            phase,
            reason: if phase == ComputerUsePhase::Error {
                record.reason.clone()
            } else if phase == ComputerUsePhase::NeedsRuntime {
                Some(RUNTIME_UNAVAILABLE_REASON.to_owned())
            } else {
                None
            },
            permission_edge_armed: false,
            failure_budget: FailureBudget::default(),
            consecutive_ready_failures: 0,
        }
    }

    pub fn enabled(&self) -> bool {
        self.enabled
    }

    pub fn phase(&self) -> ComputerUsePhase {
        self.phase
    }

    pub fn reason(&self) -> Option<&str> {
        self.reason.as_deref()
    }

    pub fn set_error(&mut self, reason: impl Into<String>) {
        self.enabled = true;
        self.phase = ComputerUsePhase::Error;
        self.reason = Some(reason.into());
        self.permission_edge_armed = false;
    }

    pub fn toggle_off(&mut self) -> LifecycleDirective {
        self.enabled = false;
        self.phase = ComputerUsePhase::Off;
        self.reason = None;
        self.permission_edge_armed = false;
        self.failure_budget.clear();
        self.consecutive_ready_failures = 0;
        LifecycleDirective {
            write_state: true,
            stop_permission_probe: true,
            stop_daemon: true,
            ..LifecycleDirective::default()
        }
    }

    pub fn toggle_on(&mut self, support: RuntimeSupport, grants: Grants) -> LifecycleDirective {
        if support != RuntimeSupport::Supported {
            return LifecycleDirective {
                runtime_refused: true,
                ..LifecycleDirective::default()
            };
        }
        self.enabled = true;
        if let Some(reason) = grants.missing_reason() {
            self.phase = ComputerUsePhase::NeedsPermission;
            self.reason = Some(reason.to_owned());
            self.permission_edge_armed = true;
            return LifecycleDirective {
                write_state: true,
                prompt_permissions: true,
                spawn_permission_probe: !grants.screen_recording,
                ..LifecycleDirective::default()
            };
        }
        self.phase = ComputerUsePhase::Starting;
        self.reason = None;
        self.permission_edge_armed = false;
        LifecycleDirective {
            write_state: true,
            prompt_permissions: true,
            spawn_daemon: true,
            ..LifecycleDirective::default()
        }
    }

    pub fn capabilities(&mut self, support: RuntimeSupport, grants: Grants) -> LifecycleDirective {
        if !self.enabled || self.phase == ComputerUsePhase::Error {
            return LifecycleDirective::default();
        }
        if support != RuntimeSupport::Supported {
            let reason = match support {
                RuntimeSupport::Unknown => RUNTIME_UNAVAILABLE_REASON,
                RuntimeSupport::Unsupported => RUNTIME_TOO_OLD_REASON,
                RuntimeSupport::Supported => unreachable!(),
            };
            let changed = self.phase != ComputerUsePhase::NeedsRuntime || self.reason.as_deref() != Some(reason);
            let stop_daemon = matches!(self.phase, ComputerUsePhase::Starting | ComputerUsePhase::Ready);
            self.phase = ComputerUsePhase::NeedsRuntime;
            self.reason = Some(reason.to_owned());
            self.permission_edge_armed = false;
            return LifecycleDirective {
                write_state: changed,
                stop_daemon,
                stop_permission_probe: true,
                ..LifecycleDirective::default()
            };
        }
        if self.phase != ComputerUsePhase::NeedsRuntime {
            return LifecycleDirective::default();
        }
        if let Some(reason) = grants.missing_reason() {
            self.phase = ComputerUsePhase::NeedsPermission;
            self.reason = Some(reason.to_owned());
            self.permission_edge_armed = true;
            return LifecycleDirective {
                write_state: true,
                ..LifecycleDirective::default()
            };
        }
        self.phase = ComputerUsePhase::Starting;
        self.reason = None;
        self.permission_edge_armed = false;
        LifecycleDirective {
            write_state: true,
            spawn_daemon: true,
            ..LifecycleDirective::default()
        }
    }

    pub fn observe_permissions(&mut self, grants: Grants) -> LifecycleDirective {
        if !self.enabled || self.phase != ComputerUsePhase::NeedsPermission {
            return LifecycleDirective::default();
        }
        let Some(reason) = grants.missing_reason() else {
            if !self.permission_edge_armed {
                return LifecycleDirective::default();
            }
            self.permission_edge_armed = false;
            self.phase = ComputerUsePhase::Starting;
            self.reason = None;
            return LifecycleDirective {
                write_state: true,
                stop_permission_probe: true,
                spawn_daemon: true,
                ..LifecycleDirective::default()
            };
        };
        self.permission_edge_armed = true;
        if self.reason.as_deref() != Some(reason) {
            self.reason = Some(reason.to_owned());
            return LifecycleDirective {
                write_state: true,
                ..LifecycleDirective::default()
            };
        }
        LifecycleDirective::default()
    }

    pub fn startup_health(&mut self, result: HealthResult, now: Duration) -> LifecycleDirective {
        if !self.enabled || self.phase != ComputerUsePhase::Starting {
            return LifecycleDirective::default();
        }
        match result {
            HealthResult::Pass => {
                self.phase = ComputerUsePhase::Ready;
                self.reason = None;
                self.permission_edge_armed = false;
                self.consecutive_ready_failures = 0;
                LifecycleDirective {
                    write_state: true,
                    ..LifecycleDirective::default()
                }
            }
            HealthResult::MissingGrant(reason) => {
                self.phase = ComputerUsePhase::NeedsPermission;
                self.reason = Some(reason);
                self.permission_edge_armed = false;
                LifecycleDirective {
                    write_state: true,
                    stop_daemon: true,
                    ..LifecycleDirective::default()
                }
            }
            HealthResult::Unhealthy(reason) => self.failed(reason, now),
        }
    }

    pub fn ready_heartbeat(&mut self, result: HealthResult, now: Duration) -> LifecycleDirective {
        if !self.enabled || self.phase != ComputerUsePhase::Ready {
            return LifecycleDirective::default();
        }
        match result {
            HealthResult::Pass => {
                self.consecutive_ready_failures = 0;
                LifecycleDirective::default()
            }
            HealthResult::MissingGrant(reason) => {
                self.consecutive_ready_failures = 0;
                self.phase = ComputerUsePhase::NeedsPermission;
                self.reason = Some(reason);
                self.permission_edge_armed = false;
                LifecycleDirective {
                    write_state: true,
                    stop_daemon: true,
                    ..LifecycleDirective::default()
                }
            }
            HealthResult::Unhealthy(reason) => {
                self.consecutive_ready_failures += 1;
                if self.consecutive_ready_failures < 2 {
                    LifecycleDirective::default()
                } else {
                    self.consecutive_ready_failures = 0;
                    self.failed(reason, now)
                }
            }
        }
    }

    pub fn daemon_failed(&mut self, reason: impl Into<String>, now: Duration) -> LifecycleDirective {
        if !self.enabled || !matches!(self.phase, ComputerUsePhase::Starting | ComputerUsePhase::Ready) {
            return LifecycleDirective::default();
        }
        self.failed(reason.into(), now)
    }

    pub fn quit(&mut self) -> LifecycleDirective {
        if !self.enabled || self.phase == ComputerUsePhase::Error {
            return LifecycleDirective::default();
        }
        self.phase = ComputerUsePhase::Stopped;
        self.reason = None;
        self.permission_edge_armed = false;
        LifecycleDirective {
            write_state: true,
            stop_permission_probe: true,
            stop_daemon: true,
            ..LifecycleDirective::default()
        }
    }

    fn failed(&mut self, reason: String, now: Duration) -> LifecycleDirective {
        let exhausted = self.failure_budget.record(now);
        self.reason = Some(stable_failure_reason(&reason).to_owned());
        self.permission_edge_armed = false;
        self.phase = if exhausted {
            ComputerUsePhase::Error
        } else {
            ComputerUsePhase::Starting
        };
        LifecycleDirective {
            write_state: true,
            spawn_daemon: !exhausted,
            stop_daemon: true,
            ..LifecycleDirective::default()
        }
    }
}

fn stable_failure_reason(reason: &str) -> &'static str {
    match reason {
        "state_unwritable" => "state_unwritable",
        "assets_invalid" => "assets_invalid",
        "spawn_failed" => "spawn_failed",
        "daemon_exited" => "daemon_exited",
        "health_timeout" => "health_timeout",
        "endpoint_busy" => "endpoint_busy",
        "endpoint_unremovable" => "endpoint_unremovable",
        "socket_unreachable" => "socket_unreachable",
        "bundle_identity" => "bundle_identity",
        "ax_capability" => "ax_capability",
        "capture_missing" | "capture_protocol" => "capture_failed",
        "health_initialize"
        | "health_transport"
        | "health_protocol"
        | "health_tool_error"
        | "health_proxy_spawn"
        | "health_proxy_stdin"
        | "health_proxy_stdout"
        | "shutdown_requested" => "driver_health_failed",
        _ => "driver_health_failed",
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn record(state: ComputerUsePhase, enabled: bool) -> ComputerUseRecord {
        ComputerUseRecord {
            schema_version: COMPUTER_USE_SCHEMA_VERSION,
            enabled,
            state,
            reason: None,
            shell_pid: 12,
            instance_id: "shell-test".to_owned(),
            generation: 1,
            driver_version: COMPUTER_USE_DRIVER_VERSION.to_owned(),
            tool_snapshot: ToolSnapshot {
                path: PathBuf::from("/tmp/tools.json"),
                sha256: COMPUTER_USE_TOOL_SNAPSHOT_SHA256.to_owned(),
            },
            socket_path: (state == ComputerUsePhase::Ready).then(|| PathBuf::from("/tmp/cua.sock")),
            proxy_executable: (state == ComputerUsePhase::Ready).then(|| PathBuf::from("/tmp/cua-driver")),
            host_bundle_id: (state == ComputerUsePhase::Ready).then(|| "bot.avibe.desktop.test".to_owned()),
        }
    }

    #[test]
    fn state_round_trip_and_validation_keep_the_python_contract() {
        let record = record(ComputerUsePhase::Ready, true);
        record.validate().expect("record");
        let payload = serde_json::to_value(&record).expect("serialize");
        assert_eq!(payload["state"], "ready");
        assert_eq!(payload["reason"], serde_json::Value::Null);
        assert_eq!(
            serde_json::from_value::<ComputerUseRecord>(payload).expect("parse"),
            record
        );
    }

    #[test]
    fn shell_lock_reports_a_same_user_startup_race_without_replacing_the_holder() {
        let directory = std::env::temp_dir().join(format!(
            "avibe-computer-use-lock-test-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .expect("clock")
                .as_nanos()
        ));
        std::fs::create_dir_all(&directory).expect("temporary directory");
        let path = directory.join(COMPUTER_USE_LOCK_FILE);
        let first = ComputerUseShellLock::acquire(&path).expect("first shell lock");
        assert!(ComputerUseShellLock::acquire(&path).is_err());
        drop(first);
        ComputerUseShellLock::acquire(&path).expect("lock after holder exits");
        std::fs::remove_dir_all(directory).expect("remove temporary directory");
    }

    #[test]
    fn capability_cache_keeps_transient_answers_only_within_one_adoption() {
        let mut cache = CapabilityCache::default();
        assert_eq!(
            cache.observe(CapabilityProbe {
                controller_id: None,
                verdict: CapabilityVerdict::Supported,
            }),
            RuntimeSupport::Unknown
        );
        assert_eq!(
            cache.observe(CapabilityProbe {
                controller_id: Some("a".to_owned()),
                verdict: CapabilityVerdict::Supported,
            }),
            RuntimeSupport::Supported
        );
        assert_eq!(
            cache.observe(CapabilityProbe {
                controller_id: None,
                verdict: CapabilityVerdict::Transient,
            }),
            RuntimeSupport::Supported
        );
        assert_eq!(
            cache.observe(CapabilityProbe {
                controller_id: Some("b".to_owned()),
                verdict: CapabilityVerdict::Transient,
            }),
            RuntimeSupport::Unknown
        );
        cache.begin_adoption();
        assert_eq!(cache.current(), RuntimeSupport::Unknown);
    }

    #[test]
    fn missing_to_granted_edge_spawns_once_and_synthetic_activation_cannot_spawn() {
        let mut lifecycle = ComputerUseLifecycle::off();
        let missing = Grants {
            accessibility: true,
            screen_recording: false,
        };
        let initial = lifecycle.toggle_on(RuntimeSupport::Supported, missing);
        assert!(initial.spawn_permission_probe);
        assert!(!initial.spawn_daemon);
        assert_eq!(lifecycle.phase(), ComputerUsePhase::NeedsPermission);

        for _ in 0..32 {
            assert_eq!(
                lifecycle.observe_permissions(missing),
                LifecycleDirective::default(),
                "driver exit -> host activation feedback must not authorize another child"
            );
        }

        let granted = lifecycle.observe_permissions(Grants::all());
        assert!(granted.stop_permission_probe);
        assert!(granted.spawn_daemon);
        assert_eq!(lifecycle.phase(), ComputerUsePhase::Starting);

        let denied = lifecycle.startup_health(
            HealthResult::MissingGrant("screen_recording".to_owned()),
            Duration::ZERO,
        );
        assert!(denied.stop_daemon);
        assert_eq!(lifecycle.phase(), ComputerUsePhase::NeedsPermission);
        for _ in 0..32 {
            assert_eq!(
                lifecycle.observe_permissions(Grants::all()),
                LifecycleDirective::default(),
                "a stale all-granted shell preflight cannot reauthorize the daemon"
            );
        }
        assert_eq!(lifecycle.phase(), ComputerUsePhase::NeedsPermission);
        assert_eq!(lifecycle.reason(), Some("screen_recording"));
        assert!(lifecycle.failure_budget.failures.is_empty());

        assert_eq!(lifecycle.observe_permissions(missing), LifecycleDirective::default());
        let rearmed = lifecycle.observe_permissions(Grants::all());
        assert!(rearmed.spawn_daemon);
        assert_eq!(lifecycle.phase(), ComputerUsePhase::Starting);
    }

    #[test]
    fn ready_permission_disagreement_also_requires_a_new_edge_or_explicit_retry() {
        let mut lifecycle = ComputerUseLifecycle::off();
        assert!(
            lifecycle
                .toggle_on(RuntimeSupport::Supported, Grants::all())
                .spawn_daemon
        );
        assert!(lifecycle.startup_health(HealthResult::Pass, Duration::ZERO).write_state);
        assert_eq!(lifecycle.phase(), ComputerUsePhase::Ready);

        let missing_from_driver = lifecycle.ready_heartbeat(
            HealthResult::MissingGrant("accessibility".to_owned()),
            Duration::from_secs(5),
        );
        assert!(missing_from_driver.stop_daemon);
        assert_eq!(lifecycle.phase(), ComputerUsePhase::NeedsPermission);
        for _ in 0..10 {
            assert_eq!(
                lifecycle.observe_permissions(Grants::all()),
                LifecycleDirective::default()
            );
        }
        assert!(lifecycle.failure_budget.failures.is_empty());

        let shell_missing = Grants {
            accessibility: false,
            screen_recording: true,
        };
        assert_eq!(
            lifecycle.observe_permissions(shell_missing),
            LifecycleDirective::default()
        );
        assert!(lifecycle.observe_permissions(Grants::all()).spawn_daemon);

        assert!(
            lifecycle
                .startup_health(
                    HealthResult::MissingGrant("accessibility".to_owned()),
                    Duration::from_secs(6),
                )
                .stop_daemon
        );
        assert!(!lifecycle.observe_permissions(Grants::all()).spawn_daemon);
        assert!(lifecycle.toggle_off().stop_daemon);
        assert!(
            lifecycle
                .toggle_on(RuntimeSupport::Supported, Grants::all())
                .spawn_daemon,
            "explicit off/on remains an authorized retry"
        );
    }

    #[test]
    fn restored_runtime_support_arms_only_after_observing_a_missing_grant() {
        let stored = record(ComputerUsePhase::NeedsPermission, true);
        let mut lifecycle = ComputerUseLifecycle::from_record(&stored);
        assert_eq!(lifecycle.phase(), ComputerUsePhase::NeedsRuntime);

        let missing = Grants {
            accessibility: true,
            screen_recording: false,
        };
        let supported = lifecycle.capabilities(RuntimeSupport::Supported, missing);
        assert!(supported.write_state);
        assert!(!supported.spawn_daemon);
        assert_eq!(lifecycle.phase(), ComputerUsePhase::NeedsPermission);
        assert!(
            lifecycle.observe_permissions(Grants::all()).spawn_daemon,
            "the restored missing -> granted edge starts once"
        );

        let mut all_granted = ComputerUseLifecycle::from_record(&stored);
        assert!(
            all_granted
                .capabilities(RuntimeSupport::Supported, Grants::all())
                .spawn_daemon,
            "restore may make one initial attempt when the shell already sees every grant"
        );
        assert!(
            all_granted
                .startup_health(
                    HealthResult::MissingGrant("screen_recording".to_owned()),
                    Duration::ZERO,
                )
                .stop_daemon
        );
        assert_eq!(
            all_granted.observe_permissions(Grants::all()),
            LifecycleDirective::default(),
            "the driver's disagreement consumes restore eligibility"
        );
    }

    #[test]
    fn runtime_adoption_reason_distinguishes_unavailable_from_unsupported() {
        let stored = record(ComputerUsePhase::Ready, true);
        let mut lifecycle = ComputerUseLifecycle::from_record(&stored);
        assert_eq!(lifecycle.reason(), Some(RUNTIME_UNAVAILABLE_REASON));
        assert!(
            lifecycle
                .capabilities(RuntimeSupport::Unsupported, Grants::all())
                .write_state
        );
        assert_eq!(lifecycle.reason(), Some(RUNTIME_TOO_OLD_REASON));

        let mut restarting = ComputerUseLifecycle::off();
        restarting.enabled = true;
        restarting.phase = ComputerUsePhase::Ready;
        let directive = restarting.capabilities(RuntimeSupport::Unknown, Grants::all());
        assert!(directive.stop_daemon);
        assert_eq!(restarting.phase(), ComputerUsePhase::NeedsRuntime);
        assert_eq!(restarting.reason(), Some(RUNTIME_UNAVAILABLE_REASON));
    }

    #[test]
    fn only_toggle_on_marks_a_permission_request() {
        let missing = Grants {
            accessibility: true,
            screen_recording: false,
        };
        let mut lifecycle = ComputerUseLifecycle::off();
        assert!(
            lifecycle
                .toggle_on(RuntimeSupport::Supported, missing)
                .prompt_permissions
        );
        assert!(!lifecycle.observe_permissions(missing).prompt_permissions);
        assert!(!lifecycle.toggle_off().prompt_permissions);

        lifecycle.enabled = true;
        lifecycle.phase = ComputerUsePhase::NeedsRuntime;
        assert!(
            !lifecycle
                .capabilities(RuntimeSupport::Supported, missing)
                .prompt_permissions
        );
    }

    #[test]
    fn denied_permission_probe_stays_in_needs_permission_without_retrying() {
        let missing = Grants {
            accessibility: true,
            screen_recording: false,
        };
        let mut lifecycle = ComputerUseLifecycle::off();
        let toggle = lifecycle.toggle_on(RuntimeSupport::Supported, missing);
        assert!(toggle.write_state);
        assert!(toggle.prompt_permissions);
        assert!(toggle.spawn_permission_probe);
        assert!(!toggle.spawn_daemon);
        assert_eq!(lifecycle.phase(), ComputerUsePhase::NeedsPermission);
        assert_eq!(lifecycle.reason(), Some("screen_recording"));

        for _ in 0..3 {
            let tick = lifecycle.observe_permissions(missing);
            assert_eq!(tick, LifecycleDirective::default());
            assert_eq!(lifecycle.phase(), ComputerUsePhase::NeedsPermission);
            assert_eq!(lifecycle.reason(), Some("screen_recording"));
        }
    }

    #[test]
    fn only_each_explicit_toggle_on_authorizes_one_permission_child() {
        let missing = Grants {
            accessibility: false,
            screen_recording: false,
        };
        let mut lifecycle = ComputerUseLifecycle::off();

        let first = lifecycle.toggle_on(RuntimeSupport::Supported, missing);
        assert!(first.spawn_permission_probe);
        assert!(!first.stop_permission_probe);
        for _ in 0..10 {
            assert!(!lifecycle.observe_permissions(missing).spawn_permission_probe);
        }

        let off = lifecycle.toggle_off();
        assert!(off.stop_permission_probe);
        let second = lifecycle.toggle_on(RuntimeSupport::Supported, missing);
        assert!(second.spawn_permission_probe);
        assert!(!second.spawn_daemon);

        lifecycle.toggle_off();
        let accessibility_only = lifecycle.toggle_on(
            RuntimeSupport::Supported,
            Grants {
                accessibility: false,
                screen_recording: true,
            },
        );
        assert!(accessibility_only.prompt_permissions);
        assert!(!accessibility_only.spawn_permission_probe);

        let unsupported = lifecycle.capabilities(RuntimeSupport::Unsupported, Grants::all());
        assert!(unsupported.stop_permission_probe);
    }

    #[test]
    fn third_failure_in_five_minutes_enters_sticky_error() {
        let mut lifecycle = ComputerUseLifecycle::off();
        lifecycle.enabled = true;
        lifecycle.phase = ComputerUsePhase::Starting;
        for second in [0, 10] {
            let directive = lifecycle.startup_health(
                HealthResult::Unhealthy("spawn_failed".to_owned()),
                Duration::from_secs(second),
            );
            assert!(directive.spawn_daemon);
            assert_eq!(lifecycle.phase(), ComputerUsePhase::Starting);
        }
        let directive = lifecycle.startup_health(
            HealthResult::Unhealthy("spawn_failed".to_owned()),
            Duration::from_secs(20),
        );
        assert!(!directive.spawn_daemon);
        assert_eq!(lifecycle.phase(), ComputerUsePhase::Error);
        assert_eq!(lifecycle.reason(), Some("spawn_failed"));
        assert_eq!(
            lifecycle.capabilities(RuntimeSupport::Supported, Grants::all()),
            LifecycleDirective::default()
        );
        assert!(lifecycle.toggle_off().stop_daemon);
        assert_eq!(lifecycle.phase(), ComputerUsePhase::Off);
    }

    #[test]
    fn health_failures_persist_only_the_stable_reason_set() {
        for (raw, expected) in [
            ("spawn_failed", "spawn_failed"),
            ("daemon_exited", "daemon_exited"),
            ("health_timeout", "health_timeout"),
            ("endpoint_busy", "endpoint_busy"),
            ("endpoint_unremovable", "endpoint_unremovable"),
            ("socket_unreachable", "socket_unreachable"),
            ("bundle_identity", "bundle_identity"),
            ("ax_capability", "ax_capability"),
            ("capture_missing", "capture_failed"),
            ("capture_protocol", "capture_failed"),
            ("health_initialize", "driver_health_failed"),
            ("health_transport", "driver_health_failed"),
            ("health_protocol", "driver_health_failed"),
            ("health_tool_error", "driver_health_failed"),
            ("health_proxy_spawn", "driver_health_failed"),
            ("health_proxy_stdin", "driver_health_failed"),
            ("health_proxy_stdout", "driver_health_failed"),
            ("shutdown_requested", "driver_health_failed"),
            ("future_driver_failure", "driver_health_failed"),
        ] {
            let mut lifecycle = ComputerUseLifecycle::off();
            lifecycle.enabled = true;
            lifecycle.phase = ComputerUsePhase::Starting;

            lifecycle.startup_health(HealthResult::Unhealthy(raw.to_owned()), Duration::ZERO);

            assert_eq!(lifecycle.reason(), Some(expected), "raw reason: {raw}");
        }
    }

    #[test]
    fn ready_requires_two_consecutive_unhealthy_ticks_but_grants_fail_once() {
        let mut lifecycle = ComputerUseLifecycle::off();
        lifecycle.enabled = true;
        lifecycle.phase = ComputerUsePhase::Ready;
        assert_eq!(
            lifecycle.ready_heartbeat(HealthResult::Unhealthy("socket_unreachable".to_owned()), Duration::ZERO,),
            LifecycleDirective::default()
        );
        let directive = lifecycle.ready_heartbeat(
            HealthResult::Unhealthy("socket_unreachable".to_owned()),
            Duration::from_secs(5),
        );
        assert!(directive.stop_daemon);
        assert!(directive.spawn_daemon);

        lifecycle.phase = ComputerUsePhase::Ready;
        let directive = lifecycle.ready_heartbeat(
            HealthResult::MissingGrant("accessibility".to_owned()),
            Duration::from_secs(10),
        );
        assert!(directive.stop_daemon);
        assert!(!directive.spawn_daemon);
        assert_eq!(lifecycle.phase(), ComputerUsePhase::NeedsPermission);
    }
}
