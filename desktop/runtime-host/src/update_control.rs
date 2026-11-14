//! Bounded discovery/channel commands. This mailbox can never request an install.
use serde::{Deserialize, Serialize};
use std::{fs::File, io::Read, path::Path, time::SystemTime};

pub const CONTROL_REQUEST_FILE: &str = "desktop-update-control.json";
pub const CONTROL_MAX_BYTES: u64 = 4096;
pub const CONTROL_TTL_SECONDS: i64 = 120;
pub const CONTROL_CHECK_TIMEOUT_SECONDS: u64 = 90;

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum ControlAction {
    Check,
    Prereleases,
    #[serde(rename = "auto_update")]
    AutoUpdate,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq)]
#[serde(rename_all = "lowercase")]
enum RequestState {
    Pending,
    Expired,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct ControlRequest {
    schema_version: u32,
    pub request_id: String,
    pub action: ControlAction,
    pub enabled: Option<bool>,
    state: RequestState,
    pub created_at: String,
    pub expires_at: String,
}

#[derive(Debug)]
pub enum ControlRequestError {
    Invalid,
    Expired(ControlRequest),
}

fn time(value: &str) -> Option<chrono::DateTime<chrono::Utc>> {
    (value.len() <= 64)
        .then(|| value.parse::<chrono::DateTime<chrono::FixedOffset>>().ok())
        .flatten()
        .map(|date| date.with_timezone(&chrono::Utc))
}

fn valid_id(value: &str) -> bool {
    uuid::Uuid::parse_str(value).is_ok_and(|id| id.to_string() == value)
}

fn valid_lifetime(created: &str, expires: &str) -> bool {
    match (time(created), time(expires)) {
        (Some(created), Some(expires)) => {
            expires > created && expires - created <= chrono::Duration::seconds(CONTROL_TTL_SECONDS)
        }
        _ => false,
    }
}

fn timestamp(now: SystemTime) -> String {
    chrono::DateTime::<chrono::Utc>::from(now).to_rfc3339_opts(chrono::SecondsFormat::Secs, true)
}

impl ControlRequest {
    pub fn parse(bytes: &[u8], now: SystemTime) -> Result<Self, ControlRequestError> {
        if bytes.len() as u64 > CONTROL_MAX_BYTES {
            return Err(ControlRequestError::Invalid);
        }
        let value: serde_json::Value = serde_json::from_slice(bytes).map_err(|_| ControlRequestError::Invalid)?;
        let request: Self = serde_json::from_value(value.clone()).map_err(|_| ControlRequestError::Invalid)?;
        if request.schema_version != 1
            || !valid_id(&request.request_id)
            || !valid_lifetime(&request.created_at, &request.expires_at)
            || match request.action {
                ControlAction::Check => value.get("enabled").is_some(),
                ControlAction::Prereleases | ControlAction::AutoUpdate => request.enabled.is_none(),
            }
            || time(&request.created_at).is_none_or(|created| created > chrono::DateTime::<chrono::Utc>::from(now))
        {
            return Err(ControlRequestError::Invalid);
        }
        if !request.pending(now) {
            return Err(ControlRequestError::Expired(request));
        }
        Ok(request)
    }

    pub fn pending(&self, now: SystemTime) -> bool {
        self.state == RequestState::Pending
            && time(&self.expires_at).is_some_and(|expires| expires > chrono::DateTime::<chrono::Utc>::from(now))
    }

    pub fn same_intent(&self, other: &Self) -> bool {
        self.request_id == other.request_id
            && self.action == other.action
            && self.enabled == other.enabled
            && self.created_at == other.created_at
            && self.expires_at == other.expires_at
    }
}

pub fn read_control_request(path: &Path, now: SystemTime) -> Result<ControlRequest, ControlRequestError> {
    let mut bytes = Vec::new();
    File::open(path)
        .and_then(|file| file.take(CONTROL_MAX_BYTES + 1).read_to_end(&mut bytes))
        .map_err(|_| ControlRequestError::Invalid)?;
    ControlRequest::parse(&bytes, now)
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum ControlPhase {
    Checking,
    Completed,
    Failed,
    Expired,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub struct ControlStatus {
    pub request_id: String,
    pub action: ControlAction,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub enabled: Option<bool>,
    pub phase: ControlPhase,
    pub error_code: Option<String>,
    pub created_at: String,
    pub expires_at: String,
    pub updated_at: String,
}

impl ControlStatus {
    pub fn new(request: &ControlRequest, phase: ControlPhase, error: Option<&str>, now: SystemTime) -> Self {
        Self {
            request_id: request.request_id.clone(),
            action: request.action,
            enabled: request.enabled,
            phase,
            error_code: error.map(str::to_owned),
            created_at: request.created_at.clone(),
            expires_at: request.expires_at.clone(),
            updated_at: timestamp(now),
        }
    }

    pub fn valid(&self) -> bool {
        valid_id(&self.request_id)
            && valid_lifetime(&self.created_at, &self.expires_at)
            && time(&self.updated_at).is_some()
            && self.enabled.is_some() == matches!(self.action, ControlAction::Prereleases | ControlAction::AutoUpdate)
            && self
                .error_code
                .as_ref()
                .is_none_or(|error| !error.trim().is_empty() && error.len() <= 128)
    }

    pub fn interrupted(mut self, now: SystemTime) -> Self {
        if self.phase == ControlPhase::Checking {
            self.phase = ControlPhase::Failed;
            self.error_code = Some("shell_interrupted".into());
            self.updated_at = timestamp(now);
        }
        self
    }
}

pub fn deserialize_control_status<'de, D>(deserializer: D) -> Result<Option<ControlStatus>, D::Error>
where
    D: serde::Deserializer<'de>,
{
    let value = Option::<serde_json::Value>::deserialize(deserializer)?;
    Ok(value.and_then(|value| {
        serde_json::from_value::<ControlStatus>(value)
            .ok()
            .filter(ControlStatus::valid)
    }))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::{Duration, UNIX_EPOCH};

    fn now() -> SystemTime {
        UNIX_EPOCH + Duration::from_secs(1000)
    }
    fn request() -> serde_json::Value {
        serde_json::json!({
            "schema_version": 1, "request_id": "00000000-0000-4000-8000-000000000001",
            "action": "check", "state": "pending", "created_at": timestamp(now()),
            "expires_at": timestamp(now() + Duration::from_secs(120))
        })
    }
    fn parse(value: &serde_json::Value) -> Result<ControlRequest, ControlRequestError> {
        ControlRequest::parse(&serde_json::to_vec(value).unwrap(), now())
    }

    #[test]
    fn check_and_channel_commands_are_not_legacy_installs() {
        for action in ["check", "prereleases", "auto_update"] {
            let enabled = (action != "check").then_some(true);
            let mut value = request();
            value["action"] = action.into();
            if let Some(enabled) = enabled {
                value["enabled"] = enabled.into();
            }
            let raw = serde_json::to_vec(&value).unwrap();
            let command = ControlRequest::parse(&raw, now()).unwrap();
            assert_eq!(command.enabled, enabled);
            assert!(crate::update_status::DesktopUpdateRequest::parse(&raw, now()).is_err());
            assert!(ControlStatus::new(&command, ControlPhase::Checking, None, now()).valid());
        }
    }

    #[test]
    fn invalid_or_unbounded_commands_fail_closed() {
        for (key, value) in [
            ("schema_version", serde_json::json!(true)),
            ("schema_version", serde_json::json!(2)),
            ("request_id", serde_json::json!("opaque")),
            ("action", serde_json::json!("install")),
            ("enabled", serde_json::Value::Null),
            ("target", serde_json::json!("3.2.7")),
            ("state", serde_json::json!("completed")),
            (
                "expires_at",
                serde_json::json!(timestamp(now() + Duration::from_secs(121))),
            ),
            (
                "created_at",
                serde_json::json!(timestamp(now() + Duration::from_secs(1))),
            ),
        ] {
            let mut mutated = request();
            mutated[key] = value;
            assert!(matches!(parse(&mutated), Err(ControlRequestError::Invalid)), "{key}");
        }
        for enabled in [serde_json::Value::Null, serde_json::json!(1), serde_json::json!("true")] {
            let mut mutated = request();
            mutated["action"] = "prereleases".into();
            mutated["enabled"] = enabled;
            assert!(parse(&mutated).is_err());
        }
        assert!(ControlRequest::parse(&vec![b' '; CONTROL_MAX_BYTES as usize + 1], now()).is_err());
    }

    #[test]
    fn expiry_and_restart_never_replay_a_request() {
        let value = request();
        let raw = serde_json::to_vec(&value).unwrap();
        assert!(matches!(
            ControlRequest::parse(&raw, now() + Duration::from_secs(120)),
            Err(ControlRequestError::Expired(_))
        ));
        let mut expired = value.clone();
        expired["state"] = "expired".into();
        assert!(matches!(parse(&expired), Err(ControlRequestError::Expired(_))));
        let parsed = parse(&value).unwrap();
        let interrupted = ControlStatus::new(&parsed, ControlPhase::Checking, None, now()).interrupted(now());
        assert_eq!(interrupted.phase, ControlPhase::Failed);
        assert_eq!(interrupted.error_code.as_deref(), Some("shell_interrupted"));
        let completed = ControlStatus::new(&parsed, ControlPhase::Completed, None, now());
        assert_eq!(completed.clone().interrupted(now()), completed);
    }

    #[test]
    fn python_control_fixture_produces_the_shared_status_wire_format() {
        use crate::update::Channel;
        use crate::update_status::{UpdateStatusPhase, UpdateStatusRecord};
        let fixture: serde_json::Value =
            serde_json::from_str(include_str!("../../../tests/fixtures/desktop_update_control.json")).unwrap();
        let request = parse(&fixture["request"]).unwrap();
        let status = UpdateStatusRecord::new(
            UpdateStatusPhase::Available,
            "3.2.6",
            Some("3.2.7".into()),
            Channel::Test,
            now(),
        )
        .with_auto_update(Some(true))
        .with_control(Some(ControlStatus::new(&request, ControlPhase::Checking, None, now())));
        assert_eq!(serde_json::to_value(&status).unwrap(), fixture["status"]);
        for field in [
            serde_json::json!({"phase": []}),
            serde_json::json!({"request_id": "bad"}),
        ] {
            let mut corrupt = fixture["status"].clone();
            corrupt["control_request"]
                .as_object_mut()
                .unwrap()
                .extend(field.as_object().unwrap().clone());
            let parsed: UpdateStatusRecord = serde_json::from_value(corrupt).unwrap();
            assert!(parsed.control_request.is_none());
            assert!(parsed.has_update);
        }
    }

    #[test]
    fn matching_covers_action_value_identity_and_deadline() {
        let original = parse(&request()).unwrap();
        assert!(original.same_intent(&original));
        for key in ["request_id", "action", "expires_at"] {
            let mut other = request();
            match key {
                "request_id" => other[key] = "00000000-0000-4000-8000-000000000002".into(),
                "action" => {
                    other[key] = "prereleases".into();
                    other["enabled"] = true.into();
                }
                _ => other[key] = timestamp(now() + Duration::from_secs(119)).into(),
            }
            assert!(!original.same_intent(&parse(&other).unwrap()));
        }
    }
}
