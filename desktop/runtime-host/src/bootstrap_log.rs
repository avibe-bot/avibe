//! Why a bootstrap failed, in a file the user can send us.
//!
//! The shell's failure surface is a notice code. A code says that something
//! went wrong; it cannot say what. Everything that would answer "what" is
//! discarded today: the endpoint helper's stderr goes to the null device, and
//! nothing records whether a launch reused the installed Runtime or paid to
//! extract a fresh one. This module keeps those facts on disk, beside the
//! Runtime the application installed for itself.
//!
//! Three rules hold it to that job:
//!
//! 1. **Diagnostics never change the outcome they describe.** Every I/O error
//!    here is dropped. A log that cannot be written must not turn a working
//!    launch into a failed one, nor one failure into a different one.
//! 2. **The interesting attempt is usually the previous one**, so the file is
//!    appended to and trimmed from the front. Starting the application never
//!    truncates it.
//! 3. **Only what a failure needs.** No environment, no configuration, no
//!    credentials.

use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::{SystemTime, UNIX_EPOCH};

/// The file name the shell writes its bootstrap diagnostics to.
///
/// The shell joins it onto the application's local data directory, so on macOS
/// the file is `~/Library/Application Support/bot.avibe.desktop/bootstrap.log`.
/// Anything that later tells the user where to find it must derive the path
/// from this name rather than restate it.
pub const BOOTSTRAP_LOG_NAME: &str = "bootstrap.log";

/// How much history the file keeps: enough for many attempts carrying a few KB
/// of captured stderr each, small enough that a user can attach it to a message.
const MAX_LOG_BYTES: usize = 64 * 1024;

/// An append-only diagnostics file, or nothing at all.
///
/// `disabled()` is not an error case. A development shell drives an
/// already-installed `vibe` and owns no application data directory, and the
/// code that records must not have to know which of the two it is holding.
#[derive(Clone, Debug, Default)]
pub struct BootstrapLog {
    sink: Option<Arc<Sink>>,
}

#[derive(Debug)]
struct Sink {
    path: PathBuf,
    /// Appending and trimming is read-modify-write, and the bootstrap thread is
    /// not the only thread that can reach a launcher.
    gate: Mutex<()>,
}

impl BootstrapLog {
    pub fn at(path: PathBuf) -> Self {
        Self {
            sink: Some(Arc::new(Sink {
                path,
                gate: Mutex::new(()),
            })),
        }
    }

    pub fn disabled() -> Self {
        Self::default()
    }

    pub fn path(&self) -> Option<&Path> {
        self.sink.as_ref().map(|sink| sink.path.as_path())
    }

    /// Appends one record: a timestamp, the application version, the event, and
    /// the fields that event carries.
    ///
    /// Every value is written through `{:?}`, which quotes and escapes it. That
    /// is what keeps the file's one structural promise — a record is a line —
    /// true even though captured stderr is full of newlines and quotes.
    pub fn record(&self, event: &str, fields: &[(&str, String)]) {
        let Some(sink) = self.sink.as_ref() else {
            return;
        };
        let mut line = format!("{} {event} app={:?}", timestamp(), env!("CARGO_PKG_VERSION"));
        for (key, value) in fields {
            line.push_str(&format!(" {key}={value:?}"));
        }
        line.push('\n');
        let _ = sink.append(line.as_bytes());
    }
}

impl Sink {
    fn append(&self, record: &[u8]) -> std::io::Result<()> {
        let _guard = self.gate.lock().unwrap_or_else(|poisoned| poisoned.into_inner());
        if let Some(parent) = self.path.parent() {
            fs::create_dir_all(parent)?;
        }
        let mut file = OpenOptions::new().create(true).append(true).open(&self.path)?;
        file.write_all(record)?;
        let length = file.metadata()?.len();
        drop(file);
        if length as usize > MAX_LOG_BYTES {
            self.trim()?;
        }
        Ok(())
    }

    /// Keeps the newest `MAX_LOG_BYTES` and drops what precedes them.
    ///
    /// The cut lands on a line boundary so the file stays readable from its
    /// first byte, and the rewrite goes through a sibling file so a crash mid-
    /// trim cannot leave a half-written log where a whole one used to be.
    fn trim(&self) -> std::io::Result<()> {
        let data = fs::read(&self.path)?;
        let cut = data.len().saturating_sub(MAX_LOG_BYTES);
        let start = data[cut..]
            .iter()
            .position(|byte| *byte == b'\n')
            .map(|offset| cut + offset + 1)
            // A boundary at the very end would keep nothing at all; the newest
            // record matters more than a clean first line.
            .filter(|start| *start < data.len())
            .unwrap_or(cut);
        let temporary = self.path.with_extension("trim");
        fs::write(&temporary, &data[start..])?;
        fs::rename(&temporary, &self.path)
    }
}

/// UTC, to the millisecond.
///
/// `chrono` is compiled here without its clock feature, so the moment comes
/// from `SystemTime` and chrono only formats it. A log a user sends us is read
/// against our own records, not against their wall clock, so UTC is the useful
/// zone.
fn timestamp() -> String {
    let now = SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default();
    chrono::DateTime::from_timestamp(now.as_secs() as i64, now.subsec_nanos())
        .map(|moment| moment.to_rfc3339_opts(chrono::SecondsFormat::Millis, true))
        .unwrap_or_else(|| "unknown".to_owned())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicUsize, Ordering};

    fn scratch_dir(label: &str) -> PathBuf {
        static COUNTER: AtomicUsize = AtomicUsize::new(0);

        let unique = COUNTER.fetch_add(1, Ordering::SeqCst);
        let dir = std::env::temp_dir().join(format!("avibe-bootstrap-log-{label}-{}-{unique}", std::process::id()));
        fs::create_dir_all(&dir).expect("scratch directory is created");
        dir
    }

    #[test]
    fn a_disabled_log_has_no_path_and_writes_nothing() {
        let log = BootstrapLog::disabled();
        assert!(log.path().is_none());
        // The record call still has to be safe to make; every caller makes it
        // unconditionally so that the recording path is the same in both shells.
        log.record("endpoint.query", &[("outcome", "ok".to_owned())]);
    }

    #[test]
    fn captured_output_cannot_forge_a_record_of_its_own() {
        let dir = scratch_dir("escaping");
        let log = BootstrapLog::at(dir.join(BOOTSTRAP_LOG_NAME));

        log.record(
            "endpoint.query",
            &[(
                "stderr",
                "Traceback:\n2000-01-01T00:00:00.000Z endpoint.query outcome=\"ok\"\n".to_owned(),
            )],
        );

        let written = fs::read_to_string(dir.join(BOOTSTRAP_LOG_NAME)).expect("the log is written");
        assert_eq!(
            written.lines().count(),
            1,
            "a captured newline must not become a second record: {written}"
        );
        assert!(written.contains("outcome=\\\"ok\\\""), "{written}");
        assert!(written.contains("Traceback:\\n"), "{written}");

        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn every_record_carries_a_timestamp_and_the_application_version() {
        let dir = scratch_dir("stamp");
        let log = BootstrapLog::at(dir.join(BOOTSTRAP_LOG_NAME));

        log.record("runtime.prepare", &[("outcome", "reused".to_owned())]);

        let written = fs::read_to_string(dir.join(BOOTSTRAP_LOG_NAME)).expect("the log is written");
        let (stamp, rest) = written.trim_end().split_once(' ').expect("a stamped record");
        assert!(stamp.ends_with('Z') && stamp.len() >= 24, "{stamp}");
        assert_eq!(
            rest,
            format!("runtime.prepare app={:?} outcome=\"reused\"", env!("CARGO_PKG_VERSION"))
        );

        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn the_log_stays_bounded_across_many_attempts_and_keeps_the_newest() {
        let dir = scratch_dir("bounded");
        let path = dir.join(BOOTSTRAP_LOG_NAME);
        let log = BootstrapLog::at(path.clone());

        // Roughly 2MB of records through a 64KB file.
        for attempt in 0..2_000 {
            log.record(
                "endpoint.query",
                &[("attempt", attempt.to_string()), ("stderr", "x".repeat(1_000))],
            );
        }

        let written = fs::read_to_string(&path).expect("the log is written");
        assert!(
            written.len() <= MAX_LOG_BYTES,
            "the log grew to {} bytes",
            written.len()
        );
        assert!(written.contains("attempt=\"1999\""), "the newest attempt is kept");
        assert!(!written.contains("attempt=\"0\""), "the oldest attempt is dropped");
        for line in written.lines() {
            assert!(
                line.contains(" endpoint.query app="),
                "trimming cut inside a record: {line:.80}"
            );
        }

        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn nothing_is_written_outside_the_directory_the_log_was_given() {
        let root = scratch_dir("contained");
        let data_dir = root.join("application-data");
        let log = BootstrapLog::at(data_dir.join(BOOTSTRAP_LOG_NAME));

        for attempt in 0..200 {
            log.record(
                "endpoint.query",
                &[("attempt", attempt.to_string()), ("stderr", "x".repeat(1_000))],
            );
        }

        let mut entries: Vec<String> = fs::read_dir(&data_dir)
            .expect("the log created its own directory")
            .map(|entry| {
                entry
                    .expect("a readable entry")
                    .file_name()
                    .to_string_lossy()
                    .into_owned()
            })
            .collect();
        entries.sort();
        // Including the trim's sibling file: it is renamed over the log, never
        // left behind, and it never leaves this directory.
        assert_eq!(entries, [BOOTSTRAP_LOG_NAME]);
        let outside: Vec<String> = fs::read_dir(&root)
            .expect("the scratch root is readable")
            .map(|entry| {
                entry
                    .expect("a readable entry")
                    .file_name()
                    .to_string_lossy()
                    .into_owned()
            })
            .collect();
        assert_eq!(outside, ["application-data"]);

        fs::remove_dir_all(&root).ok();
    }
}
