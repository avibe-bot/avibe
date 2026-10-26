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

use serde::Serialize;

use crate::update::Channel;

const UPDATE_STATUS_SCHEMA_VERSION: u32 = 1;
pub const UPDATE_STATUS_FILE: &str = "update-status.json";

static TEMP_FILE_SEQUENCE: AtomicU64 = AtomicU64::new(0);

/// A completed check. In-flight phases (checking, downloading, installing) are
/// never persisted, so a recheck cannot make the web forget a known update. The
/// install handoff is not a result either: the next launch's check publishes it.
#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum UpdateStatusPhase {
    Current,
    Available,
    Disabled,
    Error,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
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
        assert_eq!(written(&dir), shared);
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
