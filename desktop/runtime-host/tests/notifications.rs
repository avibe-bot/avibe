use std::collections::{HashMap, VecDeque};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use async_trait::async_trait;
use avibe_runtime_host::notifications::{
    run_notifications, EventStream, NotificationFilter, NotificationGate, NotificationIntent, NotificationSink,
    NotificationTransport, ReconnectBackoff, RunDetail, RunTimestamps, SseDecoder, StreamError, RETAINED_KEYS,
    RETENTION, UNREAD_REFRESH_INTERVAL,
};
use serde_json::{json, Value};
use tokio::time::Instant;

struct FakeStream {
    chunks: VecDeque<Vec<u8>>,
    drop_after: bool,
    gap: Duration,
}

#[async_trait]
impl EventStream for FakeStream {
    async fn next_chunk(&mut self) -> Result<Option<Vec<u8>>, StreamError> {
        if let Some(chunk) = self.chunks.pop_front() {
            tokio::time::sleep(self.gap).await;
            return Ok(Some(chunk));
        }
        if self.drop_after {
            Ok(None)
        } else {
            std::future::pending().await
        }
    }
}

#[derive(Default)]
struct FakeTransport {
    streams: Mutex<VecDeque<FakeStream>>,
    connected_at: Mutex<Vec<Instant>>,
    details: Mutex<HashMap<String, RunDetail>>,
    refetched: Mutex<Vec<String>>,
    detail_delay: Duration,
    unread: Mutex<Option<u64>>,
    unread_reads: Mutex<Vec<Instant>>,
}

#[async_trait]
impl NotificationTransport for FakeTransport {
    async fn connect(&self) -> Result<Box<dyn EventStream>, StreamError> {
        self.connected_at.lock().unwrap().push(Instant::now());
        self.streams
            .lock()
            .unwrap()
            .pop_front()
            .map(|stream| Box::new(stream) as Box<dyn EventStream>)
            .ok_or(StreamError)
    }

    async fn run_detail(&self, run_id: &str) -> Option<RunDetail> {
        self.refetched.lock().unwrap().push(run_id.to_owned());
        tokio::time::sleep(self.detail_delay).await;
        self.details.lock().unwrap().get(run_id).cloned()
    }

    async fn unread_total(&self) -> Option<u64> {
        self.unread_reads.lock().unwrap().push(Instant::now());
        *self.unread.lock().unwrap()
    }
}

impl FakeTransport {
    fn stream(&self, frames: impl IntoIterator<Item = Vec<u8>>, drop_after: bool) {
        self.paced_stream(frames, drop_after, Duration::ZERO);
    }

    fn paced_stream(&self, frames: impl IntoIterator<Item = Vec<u8>>, drop_after: bool, gap: Duration) {
        self.streams.lock().unwrap().push_back(FakeStream {
            chunks: frames.into_iter().collect(),
            drop_after,
            gap,
        });
    }
}

struct FakeSink {
    enabled: AtomicBool,
    focused: AtomicBool,
    visible: AtomicBool,
    delivered: Mutex<Vec<NotificationIntent>>,
    sources: Mutex<Vec<Option<String>>>,
    unread: Mutex<Vec<u64>>,
}

impl Default for FakeSink {
    fn default() -> Self {
        Self {
            enabled: AtomicBool::new(true),
            focused: AtomicBool::new(false),
            visible: AtomicBool::new(true),
            delivered: Mutex::new(Vec::new()),
            sources: Mutex::new(Vec::new()),
            unread: Mutex::new(Vec::new()),
        }
    }
}

impl NotificationSink for FakeSink {
    fn gate(&self) -> NotificationGate {
        NotificationGate {
            enabled: self.enabled.load(Ordering::SeqCst),
            focused: self.focused.load(Ordering::SeqCst),
            visible: self.visible.load(Ordering::SeqCst),
        }
    }

    fn deliver(&self, intent: NotificationIntent, source: Option<String>) {
        self.delivered.lock().unwrap().push(intent);
        self.sources.lock().unwrap().push(source);
    }

    fn show_unread(&self, total: u64) {
        self.unread.lock().unwrap().push(total);
    }
}

fn stamps_only(stamps: RunTimestamps) -> RunDetail {
    RunDetail {
        stamps,
        ..RunDetail::default()
    }
}

fn frame(event: &str, data: Value) -> Vec<u8> {
    format!(
        "event: {event}\r\ndata: {}\r\n\r\n",
        json!({"type": event, "data": data})
    )
    .into_bytes()
}

fn approval(request_id: impl ToString, status: &str) -> Vec<u8> {
    frame(
        "vaults.updated",
        json!({"request_id": request_id.to_string(), "request_status": status}),
    )
}

fn run(run_id: impl ToString, run_type: &str, seconds: u64) -> Vec<u8> {
    frame(
        "runs.updated",
        json!({
            "run_id": run_id.to_string(), "run_type": run_type, "status": "succeeded",
            "started_at": "2026-09-10T12:00:00+00:00",
            "completed_at": format!("2026-09-10T12:{:02}:{:02}+00:00", seconds / 60, seconds % 60),
        }),
    )
}

fn spawn(transport: Arc<FakeTransport>, sink: Arc<FakeSink>) -> tokio::task::JoinHandle<()> {
    tokio::spawn(async move {
        run_notifications(transport, Arc::new(Mutex::new(NotificationFilter::default())), sink).await;
    })
}

async fn settle() {
    for _ in 0..20 {
        tokio::task::yield_now().await;
    }
}

#[tokio::test(start_paused = true)]
async fn global_sse_is_an_allow_list_not_a_second_event_bus() {
    let transport = Arc::new(FakeTransport::default());
    let sink = Arc::new(FakeSink::default());
    let mut chunks = vec![b": stream connected\n\nevent: connected\ndata: {}\n\n: ping\n\n".to_vec()];
    for event in [
        "message.new",
        "session.activity",
        "queue.updated",
        "inbox.unread.changed",
        "show.event",
        "invented.event",
    ] {
        chunks.push(frame(event, json!({
            "request_id": "approval", "request_status": "pending", "run_id": "run", "status": "succeeded", "run_type": "scheduled",
        })));
    }
    chunks.extend([approval("approval", "pending"), run("run", "watch", 2)]);
    transport.stream(chunks, false);
    let task = spawn(transport, sink.clone());
    settle().await;
    assert_eq!(
        *sink.delivered.lock().unwrap(),
        [NotificationIntent::ApprovalRequested, NotificationIntent::RunSucceeded]
    );
    task.abort();
}

#[tokio::test(start_paused = true)]
async fn approvals_reset_only_on_observed_non_pending_transitions() {
    let transport = Arc::new(FakeTransport::default());
    let sink = Arc::new(FakeSink::default());
    transport.stream(
        [
            approval("request", "pending"),
            approval("request", "pending"),
            approval("request", "approved"),
            approval("request", "pending"),
            approval("request", "pending"),
        ],
        false,
    );
    let task = spawn(transport, sink.clone());
    settle().await;
    assert_eq!(
        *sink.delivered.lock().unwrap(),
        vec![NotificationIntent::ApprovalRequested; 2]
    );
    task.abort();
}

#[tokio::test(start_paused = true)]
async fn malformed_or_mismatched_envelopes_cannot_become_attention_and_runtime_copy_is_never_forwarded() {
    let transport = Arc::new(FakeTransport::default());
    let sink = Arc::new(FakeSink::default());
    let pending = json!({"request_id": "request", "request_status": "pending"});
    transport.stream(
        [
            b"event: vaults.updated\ndata: not-json\n\n".to_vec(),
            b"event: vaults.updated\ndata: {\"type\":\"runs.updated\",\"data\":{}}\n\n".to_vec(),
            frame("vaults.updated", json!({"request_status": "pending"})),
            frame("vaults.updated", json!({"request_id": "", "request_status": "pending"})),
            frame(
                "runs.updated",
                json!({"run_id": "run", "status": "running", "run_type": "scheduled"}),
            ),
            frame(
                "runs.updated",
                json!({"run_id": "", "status": "failed", "run_type": "scheduled"}),
            ),
            frame("vaults.updated", pending),
            frame(
                "runs.updated",
                json!({
                    "run_id": "run", "status": "failed", "run_type": "scheduled", "session_id": "untrusted\ntext",
                    "title": "untrusted title", "body": "untrusted body", "error": "untrusted error",
                }),
            ),
        ],
        false,
    );
    let task = spawn(transport, sink.clone());
    settle().await;
    assert_eq!(
        *sink.delivered.lock().unwrap(),
        [NotificationIntent::ApprovalRequested, NotificationIntent::RunFailed]
    );
    task.abort();
}

#[tokio::test(start_paused = true)]
async fn refetch_timeout_is_bounded_and_cannot_block_later_attention_forever() {
    let transport = Arc::new(FakeTransport {
        detail_delay: Duration::from_secs(60),
        ..FakeTransport::default()
    });
    let sink = Arc::new(FakeSink::default());
    transport.stream(
        [
            frame("runs.updated", json!({"run_id": "run", "status": "failed"})),
            approval("request", "pending"),
        ],
        false,
    );
    let task = spawn(transport.clone(), sink.clone());
    settle().await;
    tokio::time::advance(Duration::from_secs(5)).await;
    settle().await;
    assert_eq!(*transport.refetched.lock().unwrap(), ["run"]);
    assert_eq!(*sink.delivered.lock().unwrap(), [NotificationIntent::ApprovalRequested]);
    task.abort();
}

#[tokio::test(start_paused = true)]
async fn idle_streams_reconnect_and_rejected_connections_back_off_without_bootstrap_recovery() {
    let transport = Arc::new(FakeTransport::default());
    let sink = Arc::new(FakeSink::default());
    transport.stream([b": stream connected\n\n".to_vec()], false);
    let task = spawn(transport.clone(), sink.clone());
    settle().await;
    tokio::time::advance(Duration::from_secs(45)).await;
    settle().await;
    for seconds in [1, 2, 4, 8, 16, 30, 30] {
        let before = transport.connected_at.lock().unwrap().len();
        tokio::time::advance(Duration::from_secs(seconds)).await;
        settle().await;
        assert_eq!(transport.connected_at.lock().unwrap().len(), before + 1);
    }
    transport.stream([approval("recovered", "pending")], false);
    tokio::time::advance(Duration::from_secs(30)).await;
    settle().await;
    assert_eq!(*sink.delivered.lock().unwrap(), [NotificationIntent::ApprovalRequested]);
    task.abort();
}

#[tokio::test(start_paused = true)]
async fn durable_duration_not_arrival_or_attach_time_controls_all_other_run_kinds() {
    let transport = Arc::new(FakeTransport::default());
    let sink = Arc::new(FakeSink::default());
    tokio::time::advance(Duration::from_secs(60)).await;
    let mut chunks = Vec::new();
    let mut expected = 0;
    for kind in ["agent_run", "scheduled", "watch", "task", "foreground", "new_kind"] {
        for seconds in [0, 2, 29, 30, 31, 90] {
            chunks.push(run(format!("{kind}-{seconds}"), kind, seconds));
            expected += usize::from(seconds >= 30 || matches!(kind, "scheduled" | "watch"));
        }
    }
    transport.stream(chunks, false);
    let task = spawn(transport.clone(), sink.clone());
    settle().await;
    assert_eq!(
        *sink.delivered.lock().unwrap(),
        vec![NotificationIntent::RunSucceeded; expected]
    );
    // Only runs that notify are read back, once each, for their source label;
    // an unavailable detail never withholds one the event already qualified.
    assert_eq!(transport.refetched.lock().unwrap().len(), expected);
    task.abort();
}

#[tokio::test(start_paused = true)]
async fn terminal_stamps_cover_offsets_updated_at_precision_and_fail_closed_values() {
    let transport = Arc::new(FakeTransport::default());
    let sink = Arc::new(FakeSink::default());
    let cases = [
        ("2026-09-10T20:00:00+08:00", "2026-09-10T12:00:30Z", true),
        ("2026-09-10T12:00:00.000001Z", "2026-09-10T12:00:30Z", false),
        ("2026-09-10T12:00:00Z", "2026-09-10T12:00:29.999999Z", false),
        ("2026-09-10T12:00:00Z", "2026-09-10T11:59:00Z", false),
        ("invalid", "2026-09-10T12:00:30Z", false),
        ("2026-09-10T12:00:00Z", "invalid", false),
    ];
    let mut chunks = Vec::new();
    let mut expected = 0;
    for (index, (started, completed, long)) in cases.iter().enumerate() {
        for stamp in ["completed_at", "updated_at"] {
            let mut data = json!({"run_id": format!("{index}-{stamp}"), "status": "failed", "started_at": started});
            data[stamp] = json!(completed);
            chunks.push(frame("runs.updated", data));
            expected += usize::from(*long);
        }
    }
    chunks.push(frame(
        "runs.updated",
        json!({
            "run_id": "completed-wins", "status": "succeeded", "started_at": "2026-09-10T12:00:00Z",
            "completed_at": "2026-09-10T12:00:02Z", "updated_at": "2026-09-10T12:01:00Z",
        }),
    ));
    transport.stream(chunks, false);
    let task = spawn(transport.clone(), sink.clone());
    settle().await;
    assert_eq!(
        *sink.delivered.lock().unwrap(),
        vec![NotificationIntent::RunFailed; expected]
    );
    assert_eq!(transport.refetched.lock().unwrap().len(), expected);
    task.abort();
}

#[tokio::test(start_paused = true)]
async fn missing_stamps_refetch_once_per_retained_terminal_and_then_fail_closed() {
    let transport = Arc::new(FakeTransport::default());
    let sink = Arc::new(FakeSink::default());
    let complete = RunTimestamps {
        started_at: Some("2026-09-10T12:00:00Z".into()),
        completed_at: Some("2026-09-10T12:00:30Z".into()),
        ..RunTimestamps::default()
    };
    let mut chunks = Vec::new();
    for (index, stamps) in [
        complete.clone(),
        RunTimestamps {
            started_at: None,
            ..complete.clone()
        },
        RunTimestamps {
            completed_at: None,
            ..complete.clone()
        },
        RunTimestamps::default(),
    ]
    .into_iter()
    .enumerate()
    {
        let key = index.to_string();
        transport
            .details
            .lock()
            .unwrap()
            .insert(key.clone(), stamps_only(stamps));
        let event = frame(
            "runs.updated",
            json!({"run_id": key, "status": "canceled", "run_type": "agent_run"}),
        );
        chunks.extend([event.clone(), event]);
    }
    let event = frame("runs.updated", json!({"run_id": "404", "status": "canceled"}));
    chunks.extend([event.clone(), event]);
    for (key, data) in [
        ("fill-start", json!({"completed_at": "2026-09-10T12:00:30Z"})),
        ("fill-end", json!({"started_at": "2026-09-10T12:00:00Z"})),
    ] {
        transport
            .details
            .lock()
            .unwrap()
            .insert(key.into(), stamps_only(complete.clone()));
        let mut data = data;
        data["run_id"] = json!(key);
        data["status"] = json!("canceled");
        chunks.push(frame("runs.updated", data));
    }
    transport.stream(chunks, false);
    let task = spawn(transport.clone(), sink.clone());
    settle().await;
    assert_eq!(
        *sink.delivered.lock().unwrap(),
        vec![NotificationIntent::RunCanceled; 3]
    );
    assert_eq!(transport.refetched.lock().unwrap().len(), 7);
    task.abort();
}

#[tokio::test(start_paused = true)]
async fn both_key_classes_evict_the_oldest_and_allow_it_to_notify_again() {
    for approval_class in [true, false] {
        let transport = Arc::new(FakeTransport::default());
        let sink = Arc::new(FakeSink::default());
        let event = |index| {
            if approval_class {
                approval(index, "pending")
            } else {
                run(index, "watch", 0)
            }
        };
        let mut chunks = (0..=RETAINED_KEYS).map(event).collect::<Vec<_>>();
        chunks.push(event(RETAINED_KEYS));
        chunks.push(event(0));
        transport.stream(chunks, false);
        let task = spawn(transport, sink.clone());
        settle().await;
        assert_eq!(sink.delivered.lock().unwrap().len(), RETAINED_KEYS + 2);
        task.abort();
    }
}

#[tokio::test(start_paused = true)]
async fn both_key_classes_expire_after_24_hours_despite_missing_transitions() {
    let filter = Arc::new(Mutex::new(NotificationFilter::default()));
    let sink = Arc::new(FakeSink::default());
    for (elapsed, expected) in [
        (Duration::ZERO, 2),
        (RETENTION - Duration::from_secs(1), 2),
        (Duration::from_secs(1), 4),
    ] {
        tokio::time::advance(elapsed).await;
        let transport = Arc::new(FakeTransport::default());
        transport.stream([approval("same", "pending"), run("same", "watch", 0)], false);
        let retained = filter.clone();
        let output = sink.clone();
        let task = tokio::spawn(async move { run_notifications(transport, retained, output).await });
        settle().await;
        assert_eq!(sink.delivered.lock().unwrap().len(), expected);
        task.abort();
        assert!(task.await.unwrap_err().is_cancelled());
    }
}

#[tokio::test(start_paused = true)]
async fn gate_suppression_is_not_a_queue_or_a_disconnect() {
    for enabled in [false, true] {
        for focused in [false, true] {
            for visible in [false, true] {
                let transport = Arc::new(FakeTransport::default());
                let sink = Arc::new(FakeSink::default());
                sink.enabled.store(enabled, Ordering::SeqCst);
                sink.focused.store(focused, Ordering::SeqCst);
                sink.visible.store(visible, Ordering::SeqCst);
                transport.stream([approval("same", "pending")], true);
                transport.stream([approval("same", "pending"), approval("new", "pending")], false);
                let task = spawn(transport.clone(), sink.clone());
                settle().await;
                let expected = usize::from(enabled && !(focused && visible));
                assert_eq!(sink.delivered.lock().unwrap().len(), expected);
                sink.enabled.store(true, Ordering::SeqCst);
                sink.focused.store(false, Ordering::SeqCst);
                tokio::time::advance(Duration::from_secs(1)).await;
                settle().await;
                assert_eq!(sink.delivered.lock().unwrap().len(), expected + 1);
                assert_eq!(transport.connected_at.lock().unwrap().len(), 2);
                task.abort();
            }
        }
    }
}

#[tokio::test(start_paused = true)]
async fn focus_is_rechecked_after_refetch_without_deferring_the_notification() {
    let transport = Arc::new(FakeTransport {
        detail_delay: Duration::from_secs(2),
        ..FakeTransport::default()
    });
    let sink = Arc::new(FakeSink::default());
    transport.details.lock().unwrap().insert(
        "run".into(),
        stamps_only(RunTimestamps {
            started_at: Some("2026-09-10T12:00:00Z".into()),
            completed_at: Some("2026-09-10T12:01:00Z".into()),
            ..RunTimestamps::default()
        }),
    );
    transport.stream(
        [frame("runs.updated", json!({"run_id": "run", "status": "succeeded"}))],
        false,
    );
    let task = spawn(transport, sink.clone());
    settle().await;
    sink.focused.store(true, Ordering::SeqCst);
    tokio::time::advance(Duration::from_secs(2)).await;
    settle().await;
    assert!(sink.delivered.lock().unwrap().is_empty());
    sink.focused.store(false, Ordering::SeqCst);
    settle().await;
    assert!(sink.delivered.lock().unwrap().is_empty());
    task.abort();
}

#[tokio::test(start_paused = true)]
async fn reconnect_discards_partial_frames_keeps_dedup_and_has_no_bootstrap_dependency() {
    let transport = Arc::new(FakeTransport::default());
    let sink = Arc::new(FakeSink::default());
    transport.stream(
        [approval("first", "pending"), b"event: vaults.updated\ndata: {".to_vec()],
        true,
    );
    transport.stream([approval("first", "pending"), approval("second", "pending")], false);
    let task = spawn(transport.clone(), sink.clone());
    settle().await;
    assert_eq!(sink.delivered.lock().unwrap().len(), 1);
    tokio::time::advance(Duration::from_millis(999)).await;
    settle().await;
    assert_eq!(transport.connected_at.lock().unwrap().len(), 1);
    tokio::time::advance(Duration::from_millis(1)).await;
    settle().await;
    assert_eq!(transport.connected_at.lock().unwrap().len(), 2);
    assert_eq!(sink.delivered.lock().unwrap().len(), 2);
    task.abort();
    assert!(task.await.unwrap_err().is_cancelled());
    tokio::time::advance(Duration::from_secs(120)).await;
    assert_eq!(transport.connected_at.lock().unwrap().len(), 2);
}

#[test]
fn framing_is_invariant_under_byte_chunking_and_line_endings() {
    for ending in ["\n", "\r\n", "\r"] {
        let source = [
            "\u{feff}: stream connected",
            "",
            "event: vaults.updated",
            "id: ignored",
            "retry: 0",
            "data: {",
            "data: \"type\":\"vaults.updated\",\"data\":{\"request_id\":\"请求\",\"request_status\":\"pending\"}}",
            "",
            "",
        ]
        .join(ending);
        for chunk_size in 1..=source.len() {
            let mut decoder = SseDecoder::new();
            let frames = source
                .as_bytes()
                .chunks(chunk_size)
                .flat_map(|chunk| decoder.push(chunk))
                .collect::<Vec<_>>();
            assert_eq!(frames.len(), 1);
            assert_eq!(frames[0].event, "vaults.updated");
            assert_eq!(
                serde_json::from_str::<Value>(&frames[0].data).unwrap()["data"]["request_id"],
                "请求"
            );
        }
    }
}

#[test]
fn oversized_or_invalid_frames_are_discarded_until_the_next_real_blank_line() {
    let mut source = b"event: vaults.updated\ndata: ".to_vec();
    source.extend(vec![b'x'; 128 * 1024]);
    source.extend(b"\nevent: vaults.updated\ndata: hidden\n\n");
    source.extend(b"event: vaults.updated\ndata: \xff\n\n");
    source.extend(approval("valid", "pending"));
    let mut decoder = SseDecoder::new();
    let frames = source
        .chunks(71)
        .flat_map(|chunk| decoder.push(chunk))
        .collect::<Vec<_>>();
    assert_eq!(frames.len(), 1);
    assert!(frames[0].data.contains("valid"));
}

#[test]
fn reconnect_backoff_is_positive_capped_and_resettable() {
    let mut backoff = ReconnectBackoff::default();
    for expected in [1, 2, 4, 8, 16, 30, 30, 30, 30] {
        assert_eq!(backoff.next_delay(), Duration::from_secs(expected));
    }
    backoff.reset();
    assert_eq!(backoff.next_delay(), Duration::from_secs(1));
}

#[tokio::test(start_paused = true)]
async fn run_notifications_name_their_source_from_sanitized_detail_labels() {
    let transport = Arc::new(FakeTransport::default());
    let sink = Arc::new(FakeSink::default());
    let label = |project: Option<&str>, session: Option<&str>, definition: Option<&str>| RunDetail {
        session_project_name: project.map(Into::into),
        session_label: session.map(Into::into),
        definition_name: definition.map(Into::into),
        ..RunDetail::default()
    };
    let long = "长".repeat(80);
    let cases = [
        (
            label(Some("avibe-app"), Some("桌面版检查更新"), Some("rc21 release run")),
            Some("avibe-app · 桌面版检查更新".to_owned()),
        ),
        (label(None, Some("#dev-ops"), None), Some("#dev-ops".to_owned())),
        (
            label(None, None, Some("nightly audit")),
            Some("nightly audit".to_owned()),
        ),
        (label(Some("solo"), Some("solo"), None), Some("solo".to_owned())),
        (
            label(Some(" \u{1b}[31mproj\n"), Some("line\r\nbreak\u{7}\t here"), None),
            Some("[31mproj · line break here".to_owned()),
        ),
        (label(Some(&long), None, None), Some(format!("{}…", "长".repeat(47)))),
        (label(Some("  "), Some("\n"), None), None),
        (
            label(
                Some("inv\u{202e}oice\u{2066}\u{200b}"),
                Some("a\u{2028}b\u{feff}\u{e001}"),
                None,
            ),
            Some("invoice · a b".to_owned()),
        ),
        (
            label(None, Some("👨\u{200d}👩 नम\u{200c}स्ते"), None),
            Some("👨\u{200d}👩 नम\u{200c}स्ते".to_owned()),
        ),
        (
            label(Some("\u{202e}\u{2069}"), None, Some("audit")),
            Some("audit".to_owned()),
        ),
        (RunDetail::default(), None),
    ];
    let mut chunks = Vec::new();
    let mut expected = Vec::new();
    for (index, (detail, source)) in cases.into_iter().enumerate() {
        transport.details.lock().unwrap().insert(index.to_string(), detail);
        chunks.push(run(index, "watch", 0));
        expected.push(source);
    }
    // A detail that cannot be read keeps the generic copy rather than the run.
    chunks.push(run("unreadable", "scheduled", 0));
    expected.push(None);
    transport.stream(chunks, false);
    let task = spawn(transport, sink.clone());
    settle().await;
    assert_eq!(*sink.sources.lock().unwrap(), expected);
    assert_eq!(sink.delivered.lock().unwrap().len(), expected.len());
    task.abort();
}

#[tokio::test(start_paused = true)]
async fn slow_detail_reads_never_hold_back_the_stream_or_its_approvals() {
    // Same chunk, and one event per chunk (the Runtime's usual shape).
    let chunked = |separate: bool| {
        let mut frames = (0..5).map(|index| run(index, "watch", 0)).collect::<Vec<_>>();
        frames.push(approval("request", "pending"));
        if separate {
            frames
        } else {
            vec![frames.concat()]
        }
    };
    for separate in [false, true] {
        let transport = Arc::new(FakeTransport {
            detail_delay: Duration::from_secs(60),
            ..FakeTransport::default()
        });
        let sink = Arc::new(FakeSink::default());
        transport.stream(chunked(separate), false);
        let task = spawn(transport.clone(), sink.clone());
        settle().await;
        assert_eq!(*sink.delivered.lock().unwrap(), [NotificationIntent::ApprovalRequested]);
        assert_eq!(transport.refetched.lock().unwrap().len(), 5);
        tokio::time::advance(Duration::from_secs(5)).await;
        settle().await;
        let mut expected = vec![NotificationIntent::ApprovalRequested];
        expected.extend([NotificationIntent::RunSucceeded; 5]);
        assert_eq!(*sink.delivered.lock().unwrap(), expected);
        assert_eq!(*sink.sources.lock().unwrap(), vec![None; 6]);
        task.abort();
    }
}

#[tokio::test(start_paused = true)]
async fn runs_beyond_the_lookup_limit_queue_instead_of_being_dropped() {
    let transport = Arc::new(FakeTransport {
        detail_delay: Duration::from_secs(1),
        ..FakeTransport::default()
    });
    // Foreground runs without stamps: only their detail can qualify them.
    for index in 0..40 {
        transport.details.lock().unwrap().insert(
            format!("run-{index}"),
            stamps_only(RunTimestamps {
                started_at: Some("2026-09-10T12:00:00Z".into()),
                completed_at: Some("2026-09-10T12:00:45Z".into()),
                ..RunTimestamps::default()
            }),
        );
    }
    let sink = Arc::new(FakeSink::default());
    transport.stream(
        (0..40).map(|index| {
            frame(
                "runs.updated",
                json!({"run_id": format!("run-{index}"), "status": "succeeded"}),
            )
        }),
        false,
    );
    let task = spawn(transport.clone(), sink.clone());
    settle().await;
    assert_eq!(transport.refetched.lock().unwrap().len(), 16);
    for _ in 0..3 {
        tokio::time::advance(Duration::from_secs(1)).await;
        settle().await;
    }
    assert_eq!(transport.refetched.lock().unwrap().len(), 40);
    assert_eq!(
        *sink.delivered.lock().unwrap(),
        vec![NotificationIntent::RunSucceeded; 40]
    );
    task.abort();
}

#[tokio::test(start_paused = true)]
async fn an_overloaded_backlog_is_bounded_and_falls_back_to_generic_copy() {
    let transport = Arc::new(FakeTransport {
        detail_delay: Duration::from_secs(60),
        ..FakeTransport::default()
    });
    let sink = Arc::new(FakeSink::default());
    let mut frames = (0..100).map(|index| run(index, "watch", 0)).collect::<Vec<_>>();
    frames.push(frame(
        "runs.updated",
        json!({"run_id": "unstamped", "status": "succeeded"}),
    ));
    transport.stream(frames, false);
    let task = spawn(transport.clone(), sink.clone());
    settle().await;
    // 64 are pending (16 reading); the other 36 known-background runs notify
    // at once without a source, and the unstamped overflow fails closed.
    assert_eq!(transport.refetched.lock().unwrap().len(), 16);
    assert_eq!(sink.delivered.lock().unwrap().len(), 36);
    for _ in 0..4 {
        tokio::time::advance(Duration::from_secs(5)).await;
        settle().await;
    }
    assert_eq!(transport.refetched.lock().unwrap().len(), 64);
    assert_eq!(
        *sink.delivered.lock().unwrap(),
        vec![NotificationIntent::RunSucceeded; 100]
    );
    assert!(!transport.refetched.lock().unwrap().contains(&"unstamped".to_owned()));
    task.abort();
}

#[tokio::test(start_paused = true)]
async fn stopping_the_loop_cancels_detail_reads_in_flight() {
    let transport = Arc::new(FakeTransport {
        detail_delay: Duration::from_secs(1),
        ..FakeTransport::default()
    });
    let sink = Arc::new(FakeSink::default());
    transport.stream([run("slow", "watch", 0)], false);
    let task = spawn(transport.clone(), sink.clone());
    settle().await;
    assert_eq!(*transport.refetched.lock().unwrap(), ["slow"]);
    task.abort();
    assert!(task.await.unwrap_err().is_cancelled());
    tokio::time::advance(Duration::from_secs(5)).await;
    settle().await;
    assert!(sink.delivered.lock().unwrap().is_empty());
}

#[tokio::test(start_paused = true)]
async fn every_connection_and_frame_refreshes_the_runtime_unread_total_at_a_bounded_rate() {
    let transport = Arc::new(FakeTransport::default());
    let sink = Arc::new(FakeSink::default());
    // Gated off: the badge is state, not an interruption.
    sink.enabled.store(false, Ordering::SeqCst);
    sink.focused.store(true, Ordering::SeqCst);
    *transport.unread.lock().unwrap() = Some(3);
    let gap = UNREAD_REFRESH_INTERVAL / 10;
    let events = ["message.new", "session.activity", "invented.event", "heartbeat"];
    let frames: Vec<Vec<u8>> = (0..8).map(|i| frame(events[i % events.len()], json!({}))).collect();
    transport.paced_stream(frames, false, gap);
    let start = Instant::now();
    let task = spawn(transport.clone(), sink.clone());
    settle().await;
    // The connection itself reads the current total.
    assert_eq!(*sink.unread.lock().unwrap(), [3]);
    *transport.unread.lock().unwrap() = Some(0);
    for _ in 0..30 {
        tokio::time::advance(gap).await;
        settle().await;
    }
    // Eight frames of any type inside the cooldown collapse into one trailing
    // read, and that read happens after the last of them.
    assert_eq!(*sink.unread.lock().unwrap(), [3, 0]);
    let reads = transport.unread_reads.lock().unwrap().clone();
    assert_eq!(reads.len(), 2);
    assert!(reads[1] >= start + gap * 8);
    assert!(sink.delivered.lock().unwrap().is_empty());
    task.abort();
}

#[tokio::test(start_paused = true)]
async fn a_failed_unread_read_keeps_the_last_total_until_the_next_frame() {
    let transport = Arc::new(FakeTransport::default());
    let sink = Arc::new(FakeSink::default());
    *transport.unread.lock().unwrap() = Some(5);
    transport.stream([frame("heartbeat", json!({}))], true);
    transport.stream([frame("message.new", json!({}))], false);
    let task = spawn(transport.clone(), sink.clone());
    settle().await;
    *transport.unread.lock().unwrap() = None;
    // The first stream drops; the reconnect and its frame read and fail.
    for _ in 0..4 {
        tokio::time::advance(UNREAD_REFRESH_INTERVAL).await;
        settle().await;
    }
    assert!(transport.unread_reads.lock().unwrap().len() >= 2);
    assert_eq!(*sink.unread.lock().unwrap(), [5]);
    task.abort();
}
