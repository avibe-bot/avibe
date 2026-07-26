//! The frozen desktop bootstrap contract.
//!
//! Every field here is produced by this crate and consumed by the bootstrap UI.
//! See `docs/plans/tauri-desktop-vertical-slice.md` ("Frozen Desktop Bootstrap
//! Contract"). Changing a field name or phase value is a contract change.

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

/// One observation of the bootstrap state machine.
///
/// `message` is a non-secret diagnostic summary: it never carries command
/// strings, environment variables, or Runtime process output.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct BootstrapStatus {
    pub phase: BootstrapPhase,
    /// Validated loopback origin, or the rejected raw value when validation failed.
    pub origin: String,
    /// Current readiness probe attempt; `0` before the first probe is possible.
    pub attempt: u32,
    pub message: String,
    /// Whether the user may safely ask the shell to try again.
    pub retryable: bool,
}

impl BootstrapStatus {
    pub fn probing(origin: &LoopbackOrigin, attempt: u32, message: impl Into<String>) -> Self {
        Self {
            phase: BootstrapPhase::Probing,
            origin: origin.as_str().to_owned(),
            attempt,
            message: message.into(),
            retryable: false,
        }
    }

    pub fn starting(origin: &LoopbackOrigin, attempt: u32, message: impl Into<String>) -> Self {
        Self {
            phase: BootstrapPhase::Starting,
            origin: origin.as_str().to_owned(),
            attempt,
            message: message.into(),
            retryable: false,
        }
    }

    pub fn ready(origin: &LoopbackOrigin, attempt: u32, message: impl Into<String>) -> Self {
        Self {
            phase: BootstrapPhase::Ready,
            origin: origin.as_str().to_owned(),
            attempt,
            message: message.into(),
            retryable: false,
        }
    }

    pub fn failed(origin: impl Into<String>, attempt: u32, message: impl Into<String>, retryable: bool) -> Self {
        Self {
            phase: BootstrapPhase::Failed,
            origin: origin.into(),
            attempt,
            message: message.into(),
            retryable,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn phases_serialize_as_the_contract_spells_them() {
        let status = BootstrapStatus::failed("http://127.0.0.1:5123", 3, "nope", true);
        let json = serde_json::to_value(&status).expect("status serializes");

        assert_eq!(json["phase"], "failed");
        assert_eq!(json["origin"], "http://127.0.0.1:5123");
        assert_eq!(json["attempt"], 3);
        assert_eq!(json["message"], "nope");
        assert_eq!(json["retryable"], true);

        let object = json.as_object().expect("status is a JSON object");
        let mut keys: Vec<&str> = object.keys().map(String::as_str).collect();
        keys.sort_unstable();
        // The contract has exactly five fields; extra fields are a contract change.
        assert_eq!(keys, ["attempt", "message", "origin", "phase", "retryable"]);
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

    #[test]
    fn only_ready_and_failed_end_a_run() {
        assert!(!BootstrapPhase::Probing.is_terminal());
        assert!(!BootstrapPhase::Starting.is_terminal());
        assert!(BootstrapPhase::Ready.is_terminal());
        assert!(BootstrapPhase::Failed.is_terminal());
    }
}
