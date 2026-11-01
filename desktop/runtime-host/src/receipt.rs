//! The bounded stdout protocol for the bundled desktop startup command.

use std::io::{self, Read};
use std::process::ChildStdout;
use std::sync::{mpsc, Arc, Mutex, OnceLock};
use std::thread;
use std::time::{Duration, Instant};

use serde::Deserialize;

use crate::bootstrap_log::BootstrapLog;

pub const START_RECEIPT_PREFIX: &str = "@avibe-desktop-start:";
pub const START_RECEIPT_MAX_BYTES: usize = 4096;

#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum StartupReceiptOutcome {
    Ready,
    Failed,
}

#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum StartupReceiptReason {
    StartupFailed,
    HandoverRefused,
    HandoverFailed,
    RuntimeClaimRefused,
    RuntimeNotReady,
}

#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct StartupReceipt {
    pub schema_version: u32,
    pub outcome: StartupReceiptOutcome,
    pub origin: String,
    #[serde(default)]
    pub reason: Option<StartupReceiptReason>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ReceiptEvent {
    Receipt(StartupReceipt),
    Invalid,
    Eof,
    TransportError,
}

const EXIT_DRAIN: Duration = Duration::from_millis(100);

// At most three messages: one verdict, EOF after a verdict, and helper exit.
enum TransportEvent {
    Verdict(ReceiptEvent),
    Exited(Instant),
}

#[derive(Clone)]
pub struct StartupReceiptStream {
    sender: mpsc::SyncSender<TransportEvent>,
    receiver: Arc<Mutex<mpsc::Receiver<TransportEvent>>>,
    terminal: Arc<OnceLock<ReceiptEvent>>,
    exited: Arc<OnceLock<Instant>>,
}

impl Default for StartupReceiptStream {
    fn default() -> Self {
        let (sender, receiver) = mpsc::sync_channel(3);
        Self {
            sender,
            receiver: Arc::new(Mutex::new(receiver)),
            terminal: Arc::new(OnceLock::new()),
            exited: Arc::new(OnceLock::new()),
        }
    }
}

impl StartupReceiptStream {
    #[cfg(test)]
    pub(crate) fn from_events(events: impl IntoIterator<Item = ReceiptEvent>) -> Self {
        let stream = Self::default();
        for event in events {
            stream.sender.try_send(TransportEvent::Verdict(event)).unwrap();
        }
        stream
    }

    pub fn spawn(mut stdout: ChildStdout, log: BootstrapLog) -> Self {
        let stream = Self::default();
        let sender = stream.sender.clone();
        thread::spawn(move || {
            let event = read_receipts(&mut stdout);
            let drain = matches!(event, ReceiptEvent::Receipt(_));
            if let ReceiptEvent::Receipt(receipt) = &event {
                log.record(
                    "runtime.start.receipt",
                    &[("outcome", format!("{:?}", receipt.outcome))],
                );
            }
            let _ = sender.try_send(TransportEvent::Verdict(event));
            // Keep ordinary post-commit output from breaking the helper's pipe.
            // The consumer has one terminal verdict and ignores later messages.
            // A future descendant inheriting stdout can keep only this detached
            // reader blocked; it cannot extend the admission deadline.
            if drain {
                let event = match io::copy(&mut stdout, &mut io::sink()) {
                    Ok(_) => ReceiptEvent::Eof,
                    Err(_) => ReceiptEvent::TransportError,
                };
                let _ = sender.try_send(TransportEvent::Verdict(event));
            }
        });
        stream
    }

    fn record(&self, event: TransportEvent) {
        match event {
            TransportEvent::Verdict(verdict) => {
                let _ = self.terminal.set(verdict);
            }
            TransportEvent::Exited(at) => {
                let _ = self.exited.set(at);
            }
        }
    }

    pub fn try_next(&self) -> Option<ReceiptEvent> {
        if self.terminal.get().is_none() {
            // Another observer may be waiting on this retained attempt. Never
            // block the async admission thread on its channel lock.
            if let Ok(receiver) = self.receiver.try_lock() {
                while let Ok(event) = receiver.try_recv() {
                    self.record(event);
                }
            }
            if self.exited.get().is_some_and(|at| at.elapsed() >= EXIT_DRAIN) {
                let _ = self.terminal.set(ReceiptEvent::Eof);
            }
        }
        self.terminal.get().cloned()
    }

    pub fn finish(&self) {
        let _ = self.sender.try_send(TransportEvent::Exited(Instant::now()));
    }

    pub fn cancel(&self) {
        let _ = self.terminal.set(ReceiptEvent::TransportError);
    }

    pub async fn changed(&self, remaining: Duration) {
        let stream = self.clone();
        // Include time queued for the blocking pool/channel mutex in the same
        // budget. The GUI thread never performs a blocking pipe or channel read.
        let deadline = Instant::now() + remaining;
        let _ = tokio::task::spawn_blocking(move || {
            if stream.terminal.get().is_some() {
                return;
            }
            if let Ok(receiver) = stream.receiver.lock() {
                let deadline = stream
                    .exited
                    .get()
                    .map_or(deadline, |at| deadline.min(*at + EXIT_DRAIN));
                if let Ok(event) = receiver.recv_timeout(deadline.saturating_duration_since(Instant::now())) {
                    stream.record(event);
                }
            }
        })
        .await;
    }
}

/// One terminal result, at most 4096 retained line bytes and one 8 KiB read.
/// Acceptance is the end of the read batch containing the first valid frame.
/// A second protocol prefix in that batch rejects it; later bytes are outside
/// the acceptance boundary. Only the dedicated reader thread may block on I/O.
fn read_receipts(mut reader: impl Read) -> ReceiptEvent {
    let mut line = Vec::new();
    let mut oversized = false;
    let mut chunk = [0_u8; 8192];
    loop {
        let count = match reader.read(&mut chunk) {
            Ok(0) => {
                return if line.starts_with(START_RECEIPT_PREFIX.as_bytes()) {
                    ReceiptEvent::Invalid
                } else {
                    ReceiptEvent::Eof
                }
            }
            Ok(count) => count,
            Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
            Err(_) => return ReceiptEvent::TransportError,
        };
        let mut candidate = None;
        for byte in &chunk[..count] {
            if line.len() < START_RECEIPT_MAX_BYTES {
                line.push(*byte);
            } else {
                oversized = true;
            }
            let protocol = line.starts_with(START_RECEIPT_PREFIX.as_bytes());
            if protocol && (oversized || (line.len() == START_RECEIPT_MAX_BYTES && *byte != b'\n')) {
                return ReceiptEvent::Invalid;
            }
            if protocol && candidate.is_some() {
                return ReceiptEvent::Invalid;
            }
            if *byte == b'\n' {
                if protocol {
                    match parse_frame(&line) {
                        Ok(receipt) => candidate = Some(receipt),
                        Err(()) => return ReceiptEvent::Invalid,
                    }
                }
                line.clear();
                oversized = false;
            }
        }
        if let Some(receipt) = candidate {
            return ReceiptEvent::Receipt(receipt);
        }
    }
}

fn parse_frame(frame: &[u8]) -> Result<StartupReceipt, ()> {
    if frame.len() > START_RECEIPT_MAX_BYTES
        || !frame.starts_with(START_RECEIPT_PREFIX.as_bytes())
        || !frame.ends_with(b"\n")
    {
        return Err(());
    }
    let payload = &frame[START_RECEIPT_PREFIX.len()..frame.len() - 1];
    let receipt: StartupReceipt = serde_json::from_slice(payload).map_err(|_| ())?;
    if receipt.schema_version != 1 {
        return Err(());
    }
    match (&receipt.outcome, &receipt.reason) {
        (StartupReceiptOutcome::Ready, None) => Ok(receipt),
        (StartupReceiptOutcome::Failed, Some(_)) => Ok(receipt),
        _ => Err(()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;
    use std::process::{Command, Stdio};
    use std::time::{Duration, Instant};

    #[test]
    fn accepts_only_the_v1_ready_shape() {
        let frame = format!(
            "{START_RECEIPT_PREFIX}{{\"schema_version\":1,\"outcome\":\"ready\",\"origin\":\"http://127.0.0.1:5123\"}}\n"
        );
        let receipt = parse_frame(frame.as_bytes()).expect("valid ready frame");
        assert_eq!(receipt.outcome, StartupReceiptOutcome::Ready);
        assert_eq!(receipt.reason, None);
    }

    #[test]
    fn requires_a_reason_for_failed_and_rejects_unknown_fields() {
        for payload in [
            r#"{"schema_version":1,"outcome":"failed","origin":"http://127.0.0.1:5123"}"#,
            r#"{"schema_version":1,"outcome":"ready","origin":"http://127.0.0.1:5123","reason":"startup_failed"}"#,
            r#"{"schema_version":1,"outcome":"failed","origin":"http://127.0.0.1:5123","reason":"unknown"}"#,
            r#"{"schema_version":1,"outcome":"ready","origin":"http://127.0.0.1:5123","extra":true}"#,
        ] {
            let frame = format!("{START_RECEIPT_PREFIX}{payload}\n");
            assert_eq!(parse_frame(frame.as_bytes()), Err(()));
        }
    }

    #[test]
    fn frame_limit_counts_prefix_json_and_newline() {
        let payload = format!(
            "{START_RECEIPT_PREFIX}{{\"schema_version\":1,\"outcome\":\"ready\",\"origin\":\"{}\"}}\n",
            "a".repeat(START_RECEIPT_MAX_BYTES)
        );
        assert_eq!(parse_frame(payload.as_bytes()), Err(()));
    }

    #[test]
    fn ordinary_output_is_not_a_protocol_event() {
        assert_eq!(
            read_receipts(Cursor::new(b"Web UI:\nordinary output\n"),),
            ReceiptEvent::Eof,
        );
    }

    #[test]
    fn malformed_truncated_and_conflicting_batches_are_terminal() {
        let ready = format!(
            "{START_RECEIPT_PREFIX}{{\"schema_version\":1,\"outcome\":\"ready\",\"origin\":\"http://127.0.0.1:80\"}}\n"
        );
        for bytes in [
            format!("{START_RECEIPT_PREFIX}{{}}invalid\n{ready}"),
            format!("{ready}{ready}"),
            format!("{ready}{START_RECEIPT_PREFIX}"),
            ready.trim_end().to_owned(),
            format!("{START_RECEIPT_PREFIX}{}", "x".repeat(4096)),
        ] {
            assert_eq!(read_receipts(Cursor::new(bytes)), ReceiptEvent::Invalid,);
        }
    }

    #[test]
    fn exited_helper_does_not_wait_for_an_inherited_stdout_writer() {
        let python = std::env::var_os("PYTHON").unwrap_or_else(|| "python3".into());
        let mut child = Command::new(python).arg("-c").arg(
            "import subprocess,sys; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(0.8)'], stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL)"
        ).stdout(Stdio::piped()).stderr(Stdio::null()).spawn().unwrap();
        let stream = StartupReceiptStream::spawn(child.stdout.take().unwrap(), BootstrapLog::disabled());
        assert!(child.wait().unwrap().success());
        let started = Instant::now();
        stream.finish();
        while stream.try_next().is_none() && started.elapsed() < Duration::from_millis(300) {
            thread::sleep(Duration::from_millis(5));
        }
        assert_eq!(stream.try_next(), Some(ReceiptEvent::Eof));
        assert!(started.elapsed() < Duration::from_millis(300));
        // The controlled writer has a bounded lifetime and owns no Runtime.
        thread::sleep(Duration::from_millis(800));
    }

    #[test]
    fn python_producer_reaches_the_rust_stream_with_real_bytes() {
        let manifest_dir = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"));
        let root = manifest_dir
            .parent()
            .and_then(std::path::Path::parent)
            .expect("repository root");
        let scratch = std::env::temp_dir().join(format!("avibe receipt with spaces 非 ASCII {}", std::process::id()));
        std::fs::create_dir_all(&scratch).expect("scratch directory");
        let python = std::env::var_os("PYTHON").unwrap_or_else(|| "python3".into());
        let mut child = Command::new(&python)
            .current_dir(&scratch)
            .arg("-c")
            .arg(
                "import sys; print('x' * 5120); \
                 sys.path.insert(0, sys.argv[1]); \
                 from vibe.desktop_startup import emit_desktop_start_receipt; \
                 emit_desktop_start_receipt(origin='http://127.0.0.1:5123', outcome='ready')",
            )
            .arg(root)
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .or_else(|_| {
                Command::new("python")
                    .current_dir(&scratch)
                    .arg("-c")
                    .arg(
                        "import sys; print('x' * 5120); \
                         sys.path.insert(0, sys.argv[1]); \
                         from vibe.desktop_startup import emit_desktop_start_receipt; \
                         emit_desktop_start_receipt(origin='http://127.0.0.1:5123', outcome='ready')",
                    )
                    .arg(root)
                    .stdout(Stdio::piped())
                    .stderr(Stdio::null())
                    .spawn()
            })
            .expect("a Python interpreter is available");
        let stdout = child.stdout.take().expect("receipt stdout pipe");
        let stream = StartupReceiptStream::spawn(stdout, BootstrapLog::disabled());
        let status = child.wait().expect("Python producer exits");
        assert!(status.success());

        let deadline = Instant::now() + Duration::from_secs(2);
        let mut receipt = None;
        while Instant::now() < deadline {
            if let Some(ReceiptEvent::Receipt(value)) = stream.try_next() {
                receipt = Some(value);
                break;
            }
            std::thread::sleep(Duration::from_millis(5));
        }
        let receipt = receipt.expect("Python receipt reaches the Rust parser");
        assert_eq!(receipt.schema_version, 1);
        assert_eq!(receipt.outcome, StartupReceiptOutcome::Ready);
        assert_eq!(receipt.origin, "http://127.0.0.1:5123");
        std::fs::remove_dir_all(scratch).ok();
    }

    #[test]
    fn real_stdout_batch_rejects_a_second_receipt_before_acceptance() {
        let frame = format!(
            "{START_RECEIPT_PREFIX}{{\"schema_version\":1,\"outcome\":\"ready\",\"origin\":\"http://127.0.0.1:5123\"}}\n"
        );
        let python = std::env::var_os("PYTHON").unwrap_or_else(|| "python3".into());
        let mut child = Command::new(&python)
            .arg("-c")
            .arg(format!(
                "import sys; sys.stdout.write({frame:?} + {frame:?}); sys.stdout.flush()"
            ))
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .or_else(|_| {
                Command::new("python")
                    .arg("-c")
                    .arg(format!(
                        "import sys; sys.stdout.write({frame:?} + {frame:?}); sys.stdout.flush()"
                    ))
                    .stdout(Stdio::piped())
                    .stderr(Stdio::null())
                    .spawn()
            })
            .expect("a Python interpreter is available");
        let stdout = child.stdout.take().expect("receipt stdout pipe");
        let stream = StartupReceiptStream::spawn(stdout, BootstrapLog::disabled());

        let deadline = Instant::now() + Duration::from_secs(2);
        let mut event = None;
        while Instant::now() < deadline {
            if let Some(next) = stream.try_next() {
                event = Some(next);
                break;
            }
            std::thread::sleep(Duration::from_millis(5));
        }
        assert_eq!(event, Some(ReceiptEvent::Invalid));
        assert!(child.wait().expect("Python producer exits").success());
    }

    #[test]
    fn real_stdout_reports_an_oversized_prefixed_frame_without_waiting_for_newline() {
        let python = std::env::var_os("PYTHON").unwrap_or_else(|| "python3".into());
        let script = format!(
            "import sys,time; sys.stdout.write({prefix:?} + 'x' * 4096); sys.stdout.flush(); time.sleep(1)",
            prefix = START_RECEIPT_PREFIX,
        );
        let mut child = Command::new(&python)
            .arg("-c")
            .arg(&script)
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .or_else(|_| {
                Command::new("python")
                    .arg("-c")
                    .arg(&script)
                    .stdout(Stdio::piped())
                    .stderr(Stdio::null())
                    .spawn()
            })
            .expect("a Python interpreter is available");
        let stdout = child.stdout.take().expect("receipt stdout pipe");
        let stream = StartupReceiptStream::spawn(stdout, BootstrapLog::disabled());

        let deadline = Instant::now() + Duration::from_secs(2);
        let mut event = None;
        while Instant::now() < deadline {
            if let Some(next) = stream.try_next() {
                event = Some(next);
                break;
            }
            std::thread::sleep(Duration::from_millis(5));
        }
        assert_eq!(event, Some(ReceiptEvent::Invalid));
        assert!(
            child.try_wait().unwrap().is_none(),
            "oversize is terminal before newline or child exit"
        );
        let _ = child.kill();
        let _ = child.wait();
    }
}
