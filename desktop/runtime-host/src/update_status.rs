//! The desktop updater's last conclusion, published for the Runtime.
//!
//! Only the shell decides whether this computer can install a release: it owns
//! the update channel, skipped versions, signature checks, and updater
//! artifacts. After each completed check it records the result in
//! `update-status.json`, beside `computer-use.json`, and a desktop-managed
//! Runtime relays `latest` and `has_update` from that file to browsers
//! (`vibe/desktop_update_status.py`) without deciding anything itself.
//!
//! The Runtime ignores unknown fields, so a newer shell never breaks an older
//! Runtime. This module knows nothing about Tauri.

use std::fs::{self, OpenOptions};
use std::io::{self, Write};
use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

use semver::Version;
use serde::{Deserialize, Serialize};

use crate::update::Channel;

const UPDATE_STATUS_SCHEMA_VERSION: u32 = 1;
pub const UPDATE_STATUS_FILE: &str = "update-status.json";
pub const DESKTOP_UPDATE_REQUEST_FILE: &str = "desktop-update-request.json";
pub const REMOTE_REQUEST_MAX_AGE: chrono::Duration = chrono::Duration::minutes(30);

static TEMP_FILE_SEQUENCE: AtomicU64 = AtomicU64::new(0);

/// A completed check. In-flight phases (checking, downloading, installing) are
/// never persisted, so a recheck cannot make the web forget a known update. The
/// install handoff is not a result either: the next launch's check publishes it.
#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum UpdateStatusPhase {
    Current,
    Available,
    Disabled,
    Error,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum RemoteRequestPhase {
    Requested,
    Acknowledged,
    Checking,
    AwaitingHost,
    Downloading,
    Installing,
    Restarting,
    Completed,
    Failed,
    Declined,
    Expired,
}

impl RemoteRequestPhase {
    pub fn is_terminal(self) -> bool {
        matches!(self, Self::Completed | Self::Failed | Self::Declined | Self::Expired)
    }
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub struct RemoteRequestStatus {
    pub request_id: String,
    pub target: String,
    pub phase: RemoteRequestPhase,
    pub progress: Option<u8>,
    pub error_code: Option<String>,
    pub updated_at: String,
}

impl RemoteRequestStatus {
    pub fn new(
        request_id: impl Into<String>,
        target: impl Into<String>,
        phase: RemoteRequestPhase,
        progress: Option<u8>,
        error_code: Option<String>,
        updated_at: SystemTime,
    ) -> Self {
        let progress = (phase == RemoteRequestPhase::Downloading).then_some(progress.unwrap_or(0));
        Self {
            request_id: request_id.into(),
            target: target.into(),
            phase,
            progress,
            error_code: error_code.filter(|error| !error.trim().is_empty()),
            updated_at: rfc3339(updated_at),
        }
    }

    fn valid(&self) -> bool {
        !self.request_id.trim().is_empty()
            && !self.target.trim().is_empty()
            && self.updated_at.parse::<chrono::DateTime<chrono::FixedOffset>>().is_ok()
            && self.progress.is_none_or(|progress| progress <= 100)
            && (self.phase == RemoteRequestPhase::Downloading || self.progress.is_none())
            && self.error_code.as_deref().is_none_or(|error| !error.trim().is_empty())
    }
}

fn deserialize_remote_request<'de, D>(deserializer: D) -> Result<Option<RemoteRequestStatus>, D::Error>
where
    D: serde::Deserializer<'de>,
{
    let value = Option::<serde_json::Value>::deserialize(deserializer)?;
    Ok(value.and_then(|value| {
        serde_json::from_value::<RemoteRequestStatus>(value)
            .ok()
            .filter(RemoteRequestStatus::valid)
    }))
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub struct UpdateStatusRecord {
    pub schema_version: u32,
    pub phase: UpdateStatusPhase,
    /// The app's own SemVer, not the Runtime's PEP 440 version.
    pub current: String,
    pub latest: Option<String>,
    pub channel: Channel,
    /// The shell's conclusion; readers copy it and never infer it.
    pub has_update: bool,
    /// RFC 3339 UTC.
    pub checked_at: String,
    /// Whether a local Workbench has opted into unattended remote installs.
    #[serde(default)]
    pub remote_install_allowed: bool,
    /// The last remote request, including terminal outcomes.
    #[serde(default, deserialize_with = "deserialize_remote_request")]
    pub remote_request: Option<RemoteRequestStatus>,
}

impl UpdateStatusRecord {
    /// The record for one completed check.
    ///
    /// Only `Available` claims an update, and a skipped version is still
    /// available. `Disabled` and `Error` reached no conclusion, so they carry no
    /// version: a release the shell could not verify is not one it can install.
    pub fn new(
        phase: UpdateStatusPhase,
        current: impl Into<String>,
        latest: Option<String>,
        channel: Channel,
        checked_at: SystemTime,
    ) -> Self {
        let latest = match phase {
            UpdateStatusPhase::Disabled | UpdateStatusPhase::Error => None,
            _ => latest.filter(|version| !version.trim().is_empty()),
        };
        Self {
            schema_version: UPDATE_STATUS_SCHEMA_VERSION,
            phase,
            current: current.into(),
            latest,
            channel,
            has_update: phase == UpdateStatusPhase::Available,
            checked_at: rfc3339(checked_at),
            remote_install_allowed: false,
            remote_request: None,
        }
    }

    pub fn with_remote(mut self, remote_install_allowed: bool, remote_request: Option<RemoteRequestStatus>) -> Self {
        self.remote_install_allowed = remote_install_allowed;
        self.remote_request = remote_request;
        self
    }

    /// What the Runtime rejects (`vibe/desktop_update_status.py`), refused before writing.
    fn validate(&self) -> Result<(), String> {
        if self.schema_version != UPDATE_STATUS_SCHEMA_VERSION {
            return Err(format!("unsupported schema_version: {}", self.schema_version));
        }
        if self.has_update && self.latest.as_deref().is_none_or(|version| version.trim().is_empty()) {
            return Err("latest must be non-empty when has_update is true".to_owned());
        }
        Ok(())
    }
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq)]
#[serde(rename_all = "lowercase")]
enum DesktopUpdateRequestState {
    Pending,
    Expired,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct DesktopUpdateRequest {
    pub schema_version: u32,
    pub request_id: String,
    pub target: String,
    state: DesktopUpdateRequestState,
    pub created_at: String,
    pub expires_at: String,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum DesktopUpdateRequestError {
    Invalid(String),
    Expired(DesktopUpdateRequest),
}

impl std::fmt::Display for DesktopUpdateRequestError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Invalid(error) => formatter.write_str(error),
            Self::Expired(_) => formatter.write_str("desktop update request expired"),
        }
    }
}

impl std::error::Error for DesktopUpdateRequestError {}

fn uuid_shaped(value: &str) -> bool {
    let bytes = value.as_bytes();
    bytes.len() == 36
        && [8, 13, 18, 23].into_iter().all(|index| bytes[index] == b'-')
        && bytes.iter().enumerate().all(|(index, byte)| {
            index == 8
                || index == 13
                || index == 18
                || index == 23
                || (byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
        })
}

fn parse_timestamp(value: &str) -> Option<chrono::DateTime<chrono::Utc>> {
    value
        .parse::<chrono::DateTime<chrono::FixedOffset>>()
        .ok()
        .map(|moment| moment.with_timezone(&chrono::Utc))
}

impl DesktopUpdateRequest {
    pub fn is_pending(&self) -> bool {
        self.state == DesktopUpdateRequestState::Pending
    }

    pub fn parse(bytes: &[u8], now: SystemTime) -> Result<Self, DesktopUpdateRequestError> {
        let request: Self = serde_json::from_slice(bytes)
            .map_err(|_| DesktopUpdateRequestError::Invalid("invalid desktop update request".to_owned()))?;
        let parsed_target = Version::parse(&request.target);
        if request.schema_version != UPDATE_STATUS_SCHEMA_VERSION
            || !uuid_shaped(&request.request_id)
            || request.target.len() > 96
            || request.target.trim() != request.target
            || parsed_target.is_err()
            || parsed_target.is_ok_and(|version| version.to_string() != request.target)
        {
            return Err(DesktopUpdateRequestError::Invalid(
                "invalid desktop update request".to_owned(),
            ));
        }
        let now = chrono::DateTime::<chrono::Utc>::from(now);
        let Some(created_at) = parse_timestamp(&request.created_at) else {
            return Err(DesktopUpdateRequestError::Invalid(
                "invalid desktop update request timestamps".to_owned(),
            ));
        };
        let Some(expires_at) = parse_timestamp(&request.expires_at) else {
            return Err(DesktopUpdateRequestError::Invalid(
                "invalid desktop update request timestamps".to_owned(),
            ));
        };
        if created_at > now || expires_at <= created_at || expires_at - created_at > REMOTE_REQUEST_MAX_AGE {
            return Err(DesktopUpdateRequestError::Invalid(
                "invalid desktop update request timestamps".to_owned(),
            ));
        }
        if !request.is_pending() || expires_at <= now {
            return Err(DesktopUpdateRequestError::Expired(request));
        }
        Ok(request)
    }

    pub fn expires_at(&self) -> chrono::DateTime<chrono::Utc> {
        parse_timestamp(&self.expires_at).expect("validated desktop update request timestamp")
    }

    pub fn expires_in(&self, now: SystemTime) -> std::time::Duration {
        (self.expires_at() - chrono::DateTime::<chrono::Utc>::from(now))
            .to_std()
            .unwrap_or_default()
    }
}

pub fn parse_desktop_update_request(
    bytes: &[u8],
    now: SystemTime,
) -> Result<DesktopUpdateRequest, DesktopUpdateRequestError> {
    DesktopUpdateRequest::parse(bytes, now)
}

pub fn read_desktop_update_request(
    path: &std::path::Path,
    now: SystemTime,
) -> Result<DesktopUpdateRequest, DesktopUpdateRequestError> {
    let bytes = fs::read(path)
        .map_err(|_| DesktopUpdateRequestError::Invalid("desktop update request unavailable".to_owned()))?;
    parse_desktop_update_request(&bytes, now)
}

fn rfc3339(moment: SystemTime) -> String {
    let since_epoch = moment.duration_since(UNIX_EPOCH).unwrap_or_default();
    chrono::DateTime::from_timestamp(since_epoch.as_secs() as i64, 0)
        .map(|moment| moment.to_rfc3339_opts(chrono::SecondsFormat::Secs, true))
        .unwrap_or_default()
}

#[derive(Clone, Debug)]
pub struct UpdateStatusStore {
    path: PathBuf,
}

impl UpdateStatusStore {
    pub fn new(path: PathBuf) -> Self {
        Self { path }
    }

    pub fn read(&self) -> io::Result<Option<UpdateStatusRecord>> {
        match fs::read(&self.path) {
            Ok(bytes) => {
                let record: UpdateStatusRecord = serde_json::from_slice(&bytes).map_err(io::Error::other)?;
                record
                    .validate()
                    .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))?;
                Ok(Some(record))
            }
            Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(None),
            Err(error) => Err(error),
        }
    }

    /// Replaces the file atomically: a reader sees the previous record or this
    /// one, never a partial write.
    pub fn write(&self, record: &UpdateStatusRecord) -> io::Result<()> {
        record
            .validate()
            .map_err(|error| io::Error::new(io::ErrorKind::InvalidInput, error))?;
        let parent = self
            .path
            .parent()
            .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "status path has no parent"))?;
        fs::create_dir_all(parent)?;
        let suffix = TEMP_FILE_SEQUENCE.fetch_add(1, Ordering::Relaxed);
        let temporary = parent.join(format!(".{UPDATE_STATUS_FILE}.{}.{}.tmp", std::process::id(), suffix));
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

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::AtomicBool;
    use std::sync::Arc;
    use std::time::Duration;

    fn scratch_dir(label: &str) -> PathBuf {
        static COUNTER: AtomicU64 = AtomicU64::new(0);
        let unique = COUNTER.fetch_add(1, Ordering::SeqCst);
        let dir = std::env::temp_dir().join(format!("avibe-update-status-{label}-{}-{unique}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).expect("scratch directory");
        dir
    }

    const AVAILABLE_FIXTURE: &str = include_str!("../../../tests/fixtures/desktop_update_status_available.json");

    fn at(seconds: u64) -> SystemTime {
        UNIX_EPOCH + Duration::from_secs(seconds)
    }

    fn available(latest: &str) -> UpdateStatusRecord {
        UpdateStatusRecord::new(
            UpdateStatusPhase::Available,
            "3.2.2-rc.13",
            Some(latest.to_owned()),
            Channel::Test,
            at(1_792_550_160),
        )
    }

    fn written(store_dir: &std::path::Path) -> serde_json::Value {
        serde_json::from_slice(&fs::read(store_dir.join(UPDATE_STATUS_FILE)).expect("read")).expect("json")
    }

    #[test]
    fn an_available_record_is_written_in_the_runtime_shape() {
        let dir = scratch_dir("shape");
        let store = UpdateStatusStore::new(dir.join(UPDATE_STATUS_FILE));
        store.write(&available("3.2.3")).expect("write");

        // The document the Runtime relays in `tests/test_version_identity.py`.
        let shared: serde_json::Value = serde_json::from_str(AVAILABLE_FIXTURE).expect("fixture");
        let actual = written(&dir);
        for key in [
            "schema_version",
            "phase",
            "current",
            "latest",
            "channel",
            "has_update",
            "checked_at",
        ] {
            assert_eq!(actual[key], shared[key], "{key}");
        }
        assert_eq!(actual["remote_install_allowed"], false);
        assert_eq!(actual["remote_request"], serde_json::Value::Null);
        fs::remove_dir_all(dir).expect("cleanup");
    }

    #[test]
    fn only_available_claims_an_update_and_unconcluded_checks_carry_no_version() {
        for (phase, has_update, keeps_latest) in [
            (UpdateStatusPhase::Available, true, true),
            (UpdateStatusPhase::Current, false, true),
            (UpdateStatusPhase::Disabled, false, false),
            (UpdateStatusPhase::Error, false, false),
        ] {
            let record = UpdateStatusRecord::new(phase, "3.2.2", Some("3.2.3".to_owned()), Channel::Stable, at(0));
            assert_eq!(record.has_update, has_update, "{phase:?}");
            assert_eq!(record.latest.is_some(), keeps_latest, "{phase:?}");
        }
    }

    #[test]
    fn a_record_the_runtime_would_reject_is_never_written() {
        let dir = scratch_dir("reject");
        let store = UpdateStatusStore::new(dir.join(UPDATE_STATUS_FILE));
        store.write(&available("3.2.3")).expect("write");
        let before = written(&dir);
        for record in [
            UpdateStatusRecord {
                schema_version: 2,
                ..available("3.2.3")
            },
            UpdateStatusRecord {
                latest: None,
                ..available("3.2.3")
            },
            UpdateStatusRecord {
                latest: Some(" ".to_owned()),
                ..available("3.2.3")
            },
        ] {
            assert!(store.write(&record).is_err(), "{record:?}");
            assert_eq!(written(&dir), before, "a refused record leaves the last one");
        }
        fs::remove_dir_all(dir).expect("cleanup");
    }

    fn request_json(request_id: &str, target: &str, state: &str, created_at: &str, expires_at: &str) -> Vec<u8> {
        serde_json::to_vec(&serde_json::json!({
            "schema_version": 1,
            "request_id": request_id,
            "target": target,
            "state": state,
            "created_at": created_at,
            "expires_at": expires_at,
        }))
        .expect("request JSON")
    }

    fn request_now(value: &str) -> SystemTime {
        chrono::DateTime::parse_from_rfc3339(value).expect("timestamp").into()
    }

    #[test]
    fn request_parser_rejects_malformed_forged_state_stale_and_expired_input() {
        let valid = request_json(
            "123e4567-e89b-12d3-a456-426614174000",
            "3.2.4-rc.1",
            "pending",
            "2026-10-29T02:00:00Z",
            "2026-10-29T02:30:00Z",
        );
        let request = parse_desktop_update_request(&valid, request_now("2026-10-29T02:05:00Z")).unwrap();
        assert_eq!(request.request_id, "123e4567-e89b-12d3-a456-426614174000");

        for malformed in [
            request_json(
                "not-a-uuid",
                "3.2.4",
                "pending",
                "2026-10-29T02:00:00Z",
                "2026-10-29T02:30:00Z",
            ),
            request_json(
                "123E4567-e89b-12d3-a456-426614174000",
                "3.2.4",
                "pending",
                "2026-10-29T02:00:00Z",
                "2026-10-29T02:30:00Z",
            ),
            request_json(
                "123e4567-e89b-12d3-a456-426614174000",
                "v3.2.4",
                "pending",
                "2026-10-29T02:00:00Z",
                "2026-10-29T02:30:00Z",
            ),
            request_json(
                "123e4567-e89b-12d3-a456-426614174000",
                "3.2.4",
                "acknowledged",
                "2026-10-29T02:00:00Z",
                "2026-10-29T02:30:00Z",
            ),
            request_json(
                "123e4567-e89b-12d3-a456-426614174000",
                "3.2.4",
                "pending",
                "2026-10-29T02:30:00Z",
                "2026-10-29T02:00:00Z",
            ),
        ] {
            assert!(parse_desktop_update_request(&malformed, request_now("2026-10-29T02:05:00Z")).is_err());
        }

        let expired = request_json(
            "123e4567-e89b-12d3-a456-426614174000",
            "3.2.4",
            "pending",
            "2026-10-29T01:00:00Z",
            "2026-10-29T01:30:00Z",
        );
        assert!(matches!(
            parse_desktop_update_request(&expired, request_now("2026-10-29T02:05:00Z")),
            Err(DesktopUpdateRequestError::Expired(_))
        ));

        let explicitly_expired = request_json(
            "123e4567-e89b-12d3-a456-426614174000",
            "3.2.4",
            "expired",
            "2026-10-29T02:00:00Z",
            "2026-10-29T02:30:00Z",
        );
        assert!(matches!(
            parse_desktop_update_request(&explicitly_expired, request_now("2026-10-29T02:05:00Z")),
            Err(DesktopUpdateRequestError::Expired(_))
        ));
    }

    #[test]
    fn every_remote_status_field_degrades_without_losing_the_ordinary_result() {
        let cases = [
            ("request_id", "list", serde_json::json!([]), false),
            ("request_id", "object", serde_json::json!({}), false),
            ("request_id", "number", serde_json::json!(7), false),
            ("request_id", "null", serde_json::Value::Null, false),
            ("request_id", "opaque-string", serde_json::json!("opaque-request"), true),
            ("target", "list", serde_json::json!([]), false),
            ("target", "object", serde_json::json!({}), false),
            ("target", "number", serde_json::json!(7), false),
            ("target", "null", serde_json::Value::Null, false),
            ("target", "opaque-string", serde_json::json!("opaque-target"), true),
            ("phase", "list", serde_json::json!([]), false),
            ("phase", "object", serde_json::json!({}), false),
            ("phase", "number", serde_json::json!(7), false),
            ("phase", "null", serde_json::Value::Null, false),
            ("phase", "unknown-string", serde_json::json!("unknown"), false),
            ("progress", "list", serde_json::json!([]), false),
            ("progress", "object", serde_json::json!({}), false),
            ("progress", "out-of-range-number", serde_json::json!(101), false),
            ("progress", "null", serde_json::Value::Null, true),
            ("progress", "unknown-string", serde_json::json!("unknown"), false),
            ("error_code", "list", serde_json::json!([]), false),
            ("error_code", "object", serde_json::json!({}), false),
            ("error_code", "number", serde_json::json!(7), false),
            ("error_code", "null", serde_json::Value::Null, true),
            ("error_code", "opaque-string", serde_json::json!("unknown"), true),
            ("updated_at", "list", serde_json::json!([]), false),
            ("updated_at", "object", serde_json::json!({}), false),
            ("updated_at", "number", serde_json::json!(7), false),
            ("updated_at", "null", serde_json::Value::Null, false),
            ("updated_at", "unknown-string", serde_json::json!("unknown"), false),
        ];

        for (field, shape, field_value, kept) in cases {
            let mut value = serde_json::json!({
                "schema_version": 1,
                "phase": "available",
                "current": "3.2.3",
                "latest": "3.2.4",
                "channel": "test",
                "has_update": true,
                "checked_at": "2026-10-29T02:00:00Z",
                "remote_install_allowed": true,
                "remote_request": {
                    "request_id": "request-1",
                    "target": "3.2.4",
                    "phase": "downloading",
                    "progress": 42,
                    "error_code": null,
                    "updated_at": "2026-10-29T02:00:00Z"
                }
            });
            value["remote_request"][field] = field_value;
            let record: UpdateStatusRecord = serde_json::from_value(value).expect("ordinary status remains readable");

            assert_eq!(record.phase, UpdateStatusPhase::Available, "{field}-{shape}");
            assert_eq!(record.remote_request.is_some(), kept, "{field}-{shape}");
            assert!(record.remote_install_allowed, "{field}-{shape}");
        }
    }

    #[test]
    fn readers_never_observe_a_partial_replacement() {
        let dir = scratch_dir("atomic");
        let store = UpdateStatusStore::new(dir.join(UPDATE_STATUS_FILE));
        // Long enough that a non-atomic write would expose a torn prefix.
        let versions = ["3.2.3".to_owned(), format!("3.2.4-rc.{}", "9".repeat(4096))];
        store.write(&available(&versions[0])).expect("initial write");

        let done = Arc::new(AtomicBool::new(false));
        let reader = {
            let path = dir.join(UPDATE_STATUS_FILE);
            let versions = versions.clone();
            let done = Arc::clone(&done);
            std::thread::spawn(move || {
                let mut reads = 0;
                while !done.load(Ordering::SeqCst) || reads == 0 {
                    let bytes = fs::read(&path).expect("the file is always present");
                    let value: serde_json::Value = serde_json::from_slice(&bytes).expect("a complete record");
                    assert!(versions.iter().any(|version| value["latest"] == version.as_str()));
                    reads += 1;
                }
            })
        };
        for round in 0..200 {
            store.write(&available(&versions[round % 2])).expect("replace");
        }
        done.store(true, Ordering::SeqCst);
        reader.join().expect("reader thread");

        let leftovers: Vec<_> = fs::read_dir(&dir)
            .expect("list")
            .map(|entry| entry.expect("entry").file_name())
            .filter(|name| name != UPDATE_STATUS_FILE)
            .collect();
        assert!(leftovers.is_empty(), "temporary files left behind: {leftovers:?}");
        fs::remove_dir_all(dir).expect("cleanup");
    }
}
