//! The frozen desktop bootstrap contract.
//!
//! Every field here is produced by this crate and consumed by the bootstrap UI.
//! The desktop vertical-slice plan freezes this shape; changing a field name or
//! phase value is a contract change.

use serde::{Deserialize, Serialize};

use crate::origin::LoopbackOrigin;

/// `probing -> ready`, `probing -> starting -> ready | failed`, `probing -> failed`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum BootstrapPhase {
    Probing,
    Starting,
    Ready,
    Failed,
}

impl BootstrapPhase {
    /// Terminal phases end a bootstrap run; the shell only navigates on `Ready`.
    pub fn is_terminal(self) -> bool {
        matches!(self, Self::Ready | Self::Failed)
    }
}

/// Stable bootstrap copy identifiers understood by the bundled UI.
///
/// Rust emits codes, never display prose. The offline desktop bundle maps every
/// value to the central English and Chinese catalogs.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum BootstrapNoticeCode {
    Probing,
    Adopted,
    Starting,
    Ready,
    InvalidOrigin,
    RuntimeNotFound,
    RuntimeDiscoveryFailed,
    RuntimeSpawnFailed,
    LauncherExited,
    ReadyTimeout,
}

/// Typed, bounded arguments for one localized bootstrap notice.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct BootstrapNotice {
    pub code: BootstrapNoticeCode,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub seconds: Option<u64>,
}

impl BootstrapNotice {
    pub const fn new(code: BootstrapNoticeCode) -> Self {
        Self { code, seconds: None }
    }

    pub const fn timeout(seconds: u64) -> Self {
        Self {
            code: BootstrapNoticeCode::ReadyTimeout,
            seconds: Some(seconds),
        }
    }
}

/// One observation of the bootstrap state machine.
///
/// `notice` contains no display prose, command string, environment variable,
/// path, or Runtime process output.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct BootstrapStatus {
    pub phase: BootstrapPhase,
    /// A validated loopback origin, or empty. Never unvalidated input — see
    /// [`BootstrapStatus::rejected`].
    pub origin: String,
    /// Current readiness probe attempt; `0` before the first probe is possible.
    pub attempt: u32,
    pub notice: BootstrapNotice,
    /// Whether the user may safely ask the shell to try again.
    pub retryable: bool,
}

impl BootstrapStatus {
    pub fn probing(origin: &LoopbackOrigin, attempt: u32) -> Self {
        Self {
            phase: BootstrapPhase::Probing,
            origin: origin.as_str().to_owned(),
            attempt,
            notice: BootstrapNotice::new(BootstrapNoticeCode::Probing),
            retryable: false,
        }
    }

    pub fn starting(origin: &LoopbackOrigin, attempt: u32) -> Self {
        Self {
            phase: BootstrapPhase::Starting,
            origin: origin.as_str().to_owned(),
            attempt,
            notice: BootstrapNotice::new(BootstrapNoticeCode::Starting),
            retryable: false,
        }
    }

    pub fn ready(origin: &LoopbackOrigin, attempt: u32, code: BootstrapNoticeCode) -> Self {
        debug_assert!(matches!(
            code,
            BootstrapNoticeCode::Adopted | BootstrapNoticeCode::Ready
        ));
        Self {
            phase: BootstrapPhase::Ready,
            origin: origin.as_str().to_owned(),
            attempt,
            notice: BootstrapNotice::new(code),
            retryable: false,
        }
    }

    pub fn failed(origin: &LoopbackOrigin, attempt: u32, notice: BootstrapNotice, retryable: bool) -> Self {
        Self {
            phase: BootstrapPhase::Failed,
            origin: origin.as_str().to_owned(),
            attempt,
            notice,
            retryable,
        }
    }

    /// A run that failed before it had an origin at all.
    ///
    /// The rejected value is dropped rather than echoed. It is unvalidated
    /// configuration input, it would reach the WebView as rendered text, and the
    /// error already describes what an acceptable origin looks like without
    /// quoting the bad one. Taking `&LoopbackOrigin` everywhere else is what
    /// makes this the only way to report the failure.
    pub fn rejected(code: BootstrapNoticeCode, retryable: bool) -> Self {
        Self {
            phase: BootstrapPhase::Failed,
            origin: String::new(),
            attempt: 0,
            notice: BootstrapNotice::new(code),
            retryable,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn phases_serialize_as_the_contract_spells_them() {
        let origin = LoopbackOrigin::parse("http://127.0.0.1:5123").expect("a loopback origin");
        let status = BootstrapStatus::failed(&origin, 3, BootstrapNotice::timeout(120), true);
        let json = serde_json::to_value(&status).expect("status serializes");

        assert_eq!(json["phase"], "failed");
        assert_eq!(json["origin"], "http://127.0.0.1:5123");
        assert_eq!(json["attempt"], 3);
        assert_eq!(json["notice"]["code"], "ready_timeout");
        assert_eq!(json["notice"]["seconds"], 120);
        assert_eq!(json["retryable"], true);

        let object = json.as_object().expect("status is a JSON object");
        let mut keys: Vec<&str> = object.keys().map(String::as_str).collect();
        keys.sort_unstable();
        // The contract has exactly five fields; extra fields are a contract change.
        assert_eq!(keys, ["attempt", "notice", "origin", "phase", "retryable"]);
    }

    #[test]
    fn every_phase_uses_its_contract_spelling() {
        let cases = [
            (BootstrapPhase::Probing, "probing"),
            (BootstrapPhase::Starting, "starting"),
            (BootstrapPhase::Ready, "ready"),
            (BootstrapPhase::Failed, "failed"),
        ];
        for (phase, expected) in cases {
            assert_eq!(serde_json::to_value(phase).expect("phase serializes"), expected);
        }
    }

    /// The one status produced from a value that was never validated.
    #[test]
    fn a_rejected_origin_is_not_carried_into_the_status() {
        let status = BootstrapStatus::rejected(BootstrapNoticeCode::InvalidOrigin, false);
        assert_eq!(status.phase, BootstrapPhase::Failed);
        assert!(status.origin.is_empty(), "got {:?}", status.origin);
        assert!(!status.retryable);
        assert_eq!(status.attempt, 0);
        assert_eq!(status.notice.code, BootstrapNoticeCode::InvalidOrigin);
    }

    #[test]
    fn every_notice_code_uses_its_contract_spelling() {
        let cases = [
            (BootstrapNoticeCode::Probing, "probing"),
            (BootstrapNoticeCode::Adopted, "adopted"),
            (BootstrapNoticeCode::Starting, "starting"),
            (BootstrapNoticeCode::Ready, "ready"),
            (BootstrapNoticeCode::InvalidOrigin, "invalid_origin"),
            (BootstrapNoticeCode::RuntimeNotFound, "runtime_not_found"),
            (BootstrapNoticeCode::RuntimeDiscoveryFailed, "runtime_discovery_failed"),
            (BootstrapNoticeCode::RuntimeSpawnFailed, "runtime_spawn_failed"),
            (BootstrapNoticeCode::LauncherExited, "launcher_exited"),
            (BootstrapNoticeCode::ReadyTimeout, "ready_timeout"),
        ];
        for (code, expected) in cases {
            assert_eq!(serde_json::to_value(code).expect("code serializes"), expected);
        }
    }

    #[test]
    fn only_ready_and_failed_end_a_run() {
        assert!(!BootstrapPhase::Probing.is_terminal());
        assert!(!BootstrapPhase::Starting.is_terminal());
        assert!(BootstrapPhase::Ready.is_terminal());
        assert!(BootstrapPhase::Failed.is_terminal());
    }
}
