//! The desktop updater's last conclusion, published for the Runtime.
//!
//! Only the shell decides whether this computer can install a release: it owns
//! the update channel, skipped versions, signature checks, and updater
//! artifacts. After each completed check it records the result in
//! `update-status.json`, beside `computer-use.json`, and a desktop-managed
//! Runtime relays `latest` and `has_update` from that file to browsers
//! (`vibe/desktop_update_status.py`) without deciding anything itself.
//!
//! Both readers ignore unknown fields, so a newer shell never breaks an older
//! Runtime. This module knows nothing about Tauri.

use std::fs::{self, OpenOptions};
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

use serde::{Deserialize, Serialize};

use crate::update::Channel;

pub const UPDATE_STATUS_SCHEMA_VERSION: u32 = 1;
pub const UPDATE_STATUS_FILE: &str = "update-status.json";

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
        }
    }

    pub fn validate(&self) -> Result<(), String> {
        if self.schema_version != UPDATE_STATUS_SCHEMA_VERSION {
            return Err(format!("unsupported schema_version: {}", self.schema_version));
        }
        if self.has_update && self.latest.as_deref().is_none_or(|version| version.trim().is_empty()) {
            return Err("latest must be non-empty when has_update is true".to_owned());
        }
        Ok(())
    }
}

fn rfc3339(moment: SystemTime) -> String {
    let since_epoch = moment.duration_since(UNIX_EPOCH).unwrap_or_default();
    chrono::DateTime::from_timestamp(since_epoch.as_secs() as i64, 0)
        .map(|moment| moment.to_rfc3339_opts(chrono::SecondsFormat::Secs, true))
        .unwrap_or_default()
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum StoredUpdateStatus {
    Missing,
    Current(UpdateStatusRecord),
    Invalid(String),
}

#[derive(Clone, Debug)]
pub struct UpdateStatusStore {
    path: PathBuf,
}

impl UpdateStatusStore {
    pub fn new(path: PathBuf) -> Self {
        Self { path }
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    pub fn read(&self) -> StoredUpdateStatus {
        let bytes = match fs::read(&self.path) {
            Ok(bytes) => bytes,
            Err(error) if error.kind() == io::ErrorKind::NotFound => return StoredUpdateStatus::Missing,
            Err(error) => return StoredUpdateStatus::Invalid(format!("status file is unreadable: {error}")),
        };
        let record = match serde_json::from_slice::<UpdateStatusRecord>(&bytes) {
            Ok(record) => record,
            Err(error) => return StoredUpdateStatus::Invalid(format!("status file is malformed: {error}")),
        };
        match record.validate() {
            Ok(()) => StoredUpdateStatus::Current(record),
            Err(error) => StoredUpdateStatus::Invalid(error),
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

    #[test]
    fn an_available_record_round_trips_in_the_runtime_shape() {
        let dir = scratch_dir("round-trip");
        let store = UpdateStatusStore::new(dir.join(UPDATE_STATUS_FILE));
        assert_eq!(store.read(), StoredUpdateStatus::Missing);

        let record = available("3.2.3");
        store.write(&record).expect("write");
        assert_eq!(store.read(), StoredUpdateStatus::Current(record));

        // The document the Runtime relays in `tests/test_version_identity.py`.
        let written: serde_json::Value = serde_json::from_slice(&fs::read(store.path()).expect("read")).expect("json");
        let shared: serde_json::Value = serde_json::from_str(AVAILABLE_FIXTURE).expect("fixture");
        assert_eq!(written, shared);
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
            record.validate().expect("every constructed record is valid");
        }
    }

    #[test]
    fn unusable_files_read_as_invalid_and_extra_fields_are_ignored() {
        let dir = scratch_dir("invalid");
        let store = UpdateStatusStore::new(dir.join(UPDATE_STATUS_FILE));
        let mut base = serde_json::to_value(available("3.2.3")).expect("serialize");

        let mut future = base.clone();
        future["schema_version"] = 2.into();
        let mut no_latest = base.clone();
        no_latest["latest"] = serde_json::Value::Null;
        let mut blank_latest = base.clone();
        blank_latest["latest"] = " ".into();
        let mut unknown_phase = base.clone();
        unknown_phase["phase"] = "installed".into();
        for (label, bytes) in [
            ("future schema", serde_json::to_vec(&future).expect("json")),
            ("update without latest", serde_json::to_vec(&no_latest).expect("json")),
            (
                "update with blank latest",
                serde_json::to_vec(&blank_latest).expect("json"),
            ),
            ("unpersisted phase", serde_json::to_vec(&unknown_phase).expect("json")),
            ("truncated", br#"{"schema_version":1,"phase":"avail"#.to_vec()),
        ] {
            fs::write(store.path(), bytes).expect("write");
            assert!(matches!(store.read(), StoredUpdateStatus::Invalid(_)), "{label}");
        }
        assert!(store
            .write(&UpdateStatusRecord {
                latest: None,
                ..available("3.2.3")
            })
            .is_err());

        base["shell_hint"] = "newer shells may add fields".into();
        fs::write(store.path(), serde_json::to_vec(&base).expect("json")).expect("write");
        assert_eq!(store.read(), StoredUpdateStatus::Current(available("3.2.3")));
        fs::remove_dir_all(dir).expect("cleanup");
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
            let store = store.clone();
            let done = Arc::clone(&done);
            std::thread::spawn(move || {
                let mut reads = 0;
                while !done.load(Ordering::SeqCst) || reads == 0 {
                    match store.read() {
                        StoredUpdateStatus::Current(record) => assert!(record.has_update),
                        other => panic!("reader saw {other:?}"),
                    }
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
