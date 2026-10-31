//! The bounded stdout protocol for the bundled desktop startup command.

use std::collections::VecDeque;
use std::io::{BufRead, BufReader};
use std::process::ChildStdout;
use std::sync::{Arc, Mutex};
use std::thread;

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

#[derive(Clone, Default)]
pub struct StartupReceiptStream {
    events: Arc<Mutex<VecDeque<ReceiptEvent>>>,
}

impl StartupReceiptStream {
    #[cfg(test)]
    pub(crate) fn from_events(events: impl IntoIterator<Item = ReceiptEvent>) -> Self {
        Self {
            events: Arc::new(Mutex::new(events.into_iter().collect())),
        }
    }

    pub fn spawn(stdout: ChildStdout, log: BootstrapLog) -> Self {
        let stream = Self::default();
        let events = stream.events.clone();
        thread::spawn(move || read_receipts(stdout, events, log));
        stream
    }

    pub fn try_next(&self) -> Option<ReceiptEvent> {
        self.events.lock().ok()?.pop_front()
    }
}

fn read_receipts(stdout: ChildStdout, events: Arc<Mutex<VecDeque<ReceiptEvent>>>, log: BootstrapLog) {
    let mut reader = BufReader::new(stdout);
    let prefix = START_RECEIPT_PREFIX.as_bytes();
    let mut line = Vec::new();
    loop {
        line.clear();
        let mut oversized = false;
        loop {
            let buffer = match reader.fill_buf() {
                Ok(buffer) => buffer,
                Err(_) => {
                    push_event(&events, ReceiptEvent::TransportError);
                    return;
                }
            };
            if buffer.is_empty() {
                push_event(
                    &events,
                    if line.starts_with(prefix) {
                        ReceiptEvent::Invalid
                    } else {
                        ReceiptEvent::Eof
                    },
                );
                return;
            }

            let newline = buffer.iter().position(|byte| *byte == b'\n');
            let consumed = newline.map_or(buffer.len(), |position| position + 1);
            let room = START_RECEIPT_MAX_BYTES.saturating_sub(line.len());
            let retained = consumed.min(room);
            line.extend_from_slice(&buffer[..retained]);
            oversized |= consumed > room;
            reader.consume(consumed);

            if newline.is_none() {
                continue;
            }

            if line.starts_with(prefix) {
                if oversized {
                    push_event(&events, ReceiptEvent::Invalid);
                } else {
                    match parse_frame(&line) {
                        Ok(receipt) => {
                            log.record(
                                "runtime.start.receipt",
                                &[
                                    ("outcome", format!("{:?}", receipt.outcome)),
                                    ("bytes", line.len().to_string()),
                                ],
                            );
                            // The complete valid frame is the transport
                            // acceptance point. Stop reading here so a
                            // descendant that inherited stdout cannot keep a
                            // successful bootstrap thread blocked on EOF.
                            push_event(&events, ReceiptEvent::Receipt(receipt));
                            return;
                        }
                        Err(()) => push_event(&events, ReceiptEvent::Invalid),
                    }
                }
            }
            break;
        }
    }
}

fn parse_frame(frame: &[u8]) -> Result<StartupReceipt, ()> {
    if frame.len() > START_RECEIPT_MAX_BYTES || !frame.ends_with(b"\n") {
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

fn push_event(events: &Arc<Mutex<VecDeque<ReceiptEvent>>>, event: ReceiptEvent) {
    if let Ok(mut events) = events.lock() {
        events.push_back(event);
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
        let mut reader = Cursor::new(b"Web UI:\nordinary output\n".to_vec());
        let mut line = Vec::new();
        assert_eq!(reader.read_until(b'\n', &mut line).unwrap(), 8);
        assert!(!line.starts_with(START_RECEIPT_PREFIX.as_bytes()));
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
            .arg(&root)
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
                    .arg(&root)
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
            if let Some(event) = stream.try_next() {
                if let ReceiptEvent::Receipt(value) = event {
                    receipt = Some(value);
                    break;
                }
            }
            std::thread::sleep(Duration::from_millis(5));
        }
        let receipt = receipt.expect("Python receipt reaches the Rust parser");
        assert_eq!(receipt.schema_version, 1);
        assert_eq!(receipt.outcome, StartupReceiptOutcome::Ready);
        assert_eq!(receipt.origin, "http://127.0.0.1:5123");
        std::fs::remove_dir_all(scratch).ok();
    }
}
