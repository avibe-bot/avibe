use std::collections::VecDeque;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use async_trait::async_trait;
use chrono::DateTime;
use serde::Deserialize;
use tokio::time::Instant;
use url::Url;

use crate::LoopbackOrigin;

pub const RETAINED_KEYS: usize = 512;
pub const RETENTION: Duration = Duration::from_secs(24 * 60 * 60);
const MAX_EVENT_BYTES: usize = 64 * 1024;
const MAX_DETAIL_BYTES: usize = 256 * 1024;
const REQUEST_TIMEOUT: Duration = Duration::from_secs(5);
const STREAM_IDLE_TIMEOUT: Duration = Duration::from_secs(45);

#[derive(Debug, PartialEq, Eq)]
pub struct SseFrame {
    pub event: String,
    pub data: String,
}

#[derive(Default)]
pub struct SseDecoder {
    line: Vec<u8>,
    event: String,
    data: String,
    skip_lf: bool,
    started: bool,
    bytes: usize,
    discarded: bool,
    line_has_bytes: bool,
}

impl SseDecoder {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn push(&mut self, chunk: &[u8]) -> Vec<SseFrame> {
        let mut frames = Vec::new();
        for byte in chunk {
            if self.skip_lf && *byte == b'\n' {
                self.skip_lf = false;
                continue;
            }
            self.skip_lf = *byte == b'\r';
            if matches!(byte, b'\r' | b'\n') {
                self.finish_line(&mut frames);
            } else {
                self.line_has_bytes = true;
                if self.bytes < MAX_EVENT_BYTES {
                    self.line.push(*byte);
                    self.bytes += 1;
                } else {
                    self.discarded = true;
                }
            }
        }
        frames
    }

    fn finish_line(&mut self, frames: &mut Vec<SseFrame>) {
        if !std::mem::replace(&mut self.line_has_bytes, false) {
            if !self.discarded && !self.data.is_empty() {
                self.data.pop();
                frames.push(SseFrame {
                    event: std::mem::take(&mut self.event),
                    data: std::mem::take(&mut self.data),
                });
            }
            self.event.clear();
            self.data.clear();
            self.bytes = 0;
            self.discarded = false;
            return;
        }
        let line = std::mem::take(&mut self.line);
        let Ok(line) = std::str::from_utf8(&line) else {
            self.discarded = true;
            return;
        };
        let line = if !std::mem::replace(&mut self.started, true) {
            line.strip_prefix('\u{feff}').unwrap_or(line)
        } else {
            line
        };
        if self.discarded || line.starts_with(':') {
            return;
        }
        let (field, value) = line.split_once(':').unwrap_or((line, ""));
        let value = value.strip_prefix(' ').unwrap_or(value);
        match field {
            "event" => self.event = value.to_owned(),
            "data" => {
                self.data.push_str(value);
                self.data.push('\n');
            }
            _ => {}
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum NotificationIntent {
    ApprovalRequested,
    RunSucceeded,
    RunFailed,
    RunCanceled,
}

#[derive(Clone, Copy, Debug)]
pub struct NotificationGate {
    pub enabled: bool,
    pub focused: bool,
    pub visible: bool,
}

impl NotificationGate {
    pub fn allows(self) -> bool {
        self.enabled && !(self.focused && self.visible)
    }
}

pub trait NotificationSink: Send + Sync {
    fn gate(&self) -> NotificationGate;
    fn deliver(&self, intent: NotificationIntent);
}

#[derive(Default)]
struct RetainedKeys(VecDeque<(String, Instant)>);

impl RetainedKeys {
    fn insert(&mut self, key: &str, now: Instant) -> bool {
        self.0.retain(|(_, inserted)| now.duration_since(*inserted) < RETENTION);
        if let Some(index) = self.0.iter().position(|(retained, _)| retained == key) {
            let retained = self.0.remove(index).expect("the retained key exists");
            self.0.push_back(retained);
            return false;
        }
        if self.0.len() == RETAINED_KEYS {
            self.0.pop_front();
        }
        self.0.push_back((key.to_owned(), now));
        true
    }

    fn remove(&mut self, key: &str) {
        self.0.retain(|(retained, _)| retained != key);
    }
}

#[derive(Default)]
pub struct NotificationFilter {
    approvals: RetainedKeys,
    runs: RetainedKeys,
}

#[derive(Deserialize)]
struct Envelope {
    #[serde(rename = "type")]
    event: String,
    data: serde_json::Value,
}

#[derive(Deserialize)]
struct Approval {
    request_id: String,
    request_status: String,
}

#[derive(Clone, Default, Deserialize)]
pub struct RunTimestamps {
    pub started_at: Option<String>,
    pub completed_at: Option<String>,
    pub updated_at: Option<String>,
}

impl RunTimestamps {
    fn completion(&self) -> Option<&str> {
        self.completed_at.as_deref().or(self.updated_at.as_deref())
    }

    fn missing(&self) -> bool {
        self.started_at.is_none() || self.completion().is_none()
    }

    fn is_long_running(&self) -> bool {
        let duration = self
            .started_at
            .as_deref()
            .zip(self.completion())
            .and_then(|(start, end)| {
                let start = DateTime::parse_from_rfc3339(start).ok()?;
                let end = DateTime::parse_from_rfc3339(end).ok()?;
                Some(end.signed_duration_since(start))
            });
        duration.is_some_and(|duration| duration >= chrono::Duration::seconds(30))
    }

    fn fill_missing(&mut self, other: Self) {
        if self.started_at.is_none() {
            self.started_at = other.started_at;
        }
        if self.completion().is_none() {
            self.completed_at = other.completed_at;
            self.updated_at = other.updated_at;
        }
    }
}

#[derive(Deserialize)]
struct RunEvent {
    run_id: String,
    status: String,
    run_type: Option<String>,
    #[serde(flatten)]
    stamps: RunTimestamps,
}

enum Candidate {
    Ready(NotificationIntent),
    Refetch {
        run_id: String,
        stamps: RunTimestamps,
        intent: NotificationIntent,
    },
}

impl NotificationFilter {
    fn consider(&mut self, frame: SseFrame, now: Instant) -> Option<Candidate> {
        if !matches!(frame.event.as_str(), "vaults.updated" | "runs.updated") {
            return None;
        }
        let envelope: Envelope = serde_json::from_str(&frame.data).ok()?;
        if envelope.event != frame.event {
            return None;
        }
        if frame.event == "vaults.updated" {
            let approval: Approval = serde_json::from_value(envelope.data).ok()?;
            if approval.request_id.is_empty() {
                return None;
            }
            if approval.request_status != "pending" {
                self.approvals.remove(&approval.request_id);
                return None;
            }
            return self
                .approvals
                .insert(&approval.request_id, now)
                .then_some(Candidate::Ready(NotificationIntent::ApprovalRequested));
        }
        let run: RunEvent = serde_json::from_value(envelope.data).ok()?;
        if run.run_id.is_empty() {
            return None;
        }
        let intent = match run.status.as_str() {
            "succeeded" => NotificationIntent::RunSucceeded,
            "failed" => NotificationIntent::RunFailed,
            "canceled" => NotificationIntent::RunCanceled,
            _ => return None,
        };
        if !self.runs.insert(&run.run_id, now) {
            return None;
        }
        if matches!(run.run_type.as_deref(), Some("scheduled" | "watch")) || run.stamps.is_long_running() {
            return Some(Candidate::Ready(intent));
        }
        run.stamps.missing().then_some(Candidate::Refetch {
            run_id: run.run_id,
            stamps: run.stamps,
            intent,
        })
    }
}

#[async_trait]
pub trait EventStream: Send {
    async fn next_chunk(&mut self) -> Result<Option<Vec<u8>>, StreamError>;
}

#[derive(Debug)]
pub struct StreamError;

#[async_trait]
pub trait NotificationTransport: Send + Sync {
    async fn connect(&self) -> Result<Box<dyn EventStream>, StreamError>;
    async fn run_timestamps(&self, run_id: &str) -> Option<RunTimestamps>;
}

pub struct HttpNotificationTransport {
    client: reqwest::Client,
    origin: LoopbackOrigin,
}

impl HttpNotificationTransport {
    pub fn new(origin: LoopbackOrigin) -> Result<Self, reqwest::Error> {
        Ok(Self {
            client: reqwest::Client::builder()
                .connect_timeout(REQUEST_TIMEOUT)
                .redirect(reqwest::redirect::Policy::none())
                .no_proxy()
                .build()?,
            origin,
        })
    }

    fn url(&self, path: &str) -> Url {
        Url::parse(&format!("{}{path}", self.origin.as_str())).expect("the Runtime origin is validated")
    }
}

struct HttpEventStream(reqwest::Response);

#[async_trait]
impl EventStream for HttpEventStream {
    async fn next_chunk(&mut self) -> Result<Option<Vec<u8>>, StreamError> {
        self.0
            .chunk()
            .await
            .map(|chunk| chunk.map(|chunk| chunk.to_vec()))
            .map_err(|_| StreamError)
    }
}

#[async_trait]
impl NotificationTransport for HttpNotificationTransport {
    async fn connect(&self) -> Result<Box<dyn EventStream>, StreamError> {
        let response = tokio::time::timeout(
            REQUEST_TIMEOUT,
            self.client
                .get(self.url("/api/events"))
                .header("Accept", "text/event-stream")
                .send(),
        )
        .await
        .map_err(|_| StreamError)?
        .map_err(|_| StreamError)?;
        let content_type = response
            .headers()
            .get(reqwest::header::CONTENT_TYPE)
            .and_then(|value| value.to_str().ok());
        if response.status() != reqwest::StatusCode::OK
            || !content_type.is_some_and(|value| {
                value
                    .split(';')
                    .next()
                    .is_some_and(|mime| mime.trim() == "text/event-stream")
            })
        {
            return Err(StreamError);
        }
        Ok(Box::new(HttpEventStream(response)))
    }

    async fn run_timestamps(&self, run_id: &str) -> Option<RunTimestamps> {
        if matches!(run_id, "" | "." | "..") {
            return None;
        }
        let mut url = self.url("/api/harness/runs/");
        url.path_segments_mut().ok()?.pop_if_empty().push(run_id);
        let response = self.client.get(url).timeout(REQUEST_TIMEOUT).send().await.ok()?;
        if !response.status().is_success() {
            return None;
        }
        let bytes = bounded_detail_body(response).await?;
        let detail: serde_json::Value = serde_json::from_slice(&bytes).ok()?;
        if !detail.get("ok")?.as_bool()? || detail.get("run")?.get("id")?.as_str()? != run_id {
            return None;
        }
        serde_json::from_value(detail.get("run")?.clone()).ok()
    }
}

async fn bounded_detail_body(mut response: reqwest::Response) -> Option<Vec<u8>> {
    if response
        .content_length()
        .is_some_and(|size| size > MAX_DETAIL_BYTES as u64)
    {
        return None;
    }
    let mut body = Vec::new();
    while let Some(chunk) = response.chunk().await.ok()? {
        if chunk.len() > MAX_DETAIL_BYTES.saturating_sub(body.len()) {
            return None;
        }
        body.extend_from_slice(&chunk);
    }
    Some(body)
}

#[derive(Default)]
pub struct ReconnectBackoff {
    failures: u32,
}

impl ReconnectBackoff {
    pub fn next_delay(&mut self) -> Duration {
        let delay = Duration::from_secs(1u64 << self.failures.min(5)).min(Duration::from_secs(30));
        self.failures = self.failures.saturating_add(1);
        delay
    }

    pub fn reset(&mut self) {
        self.failures = 0;
    }
}

pub async fn run_notifications(
    transport: &dyn NotificationTransport,
    filter: Arc<Mutex<NotificationFilter>>,
    sink: &dyn NotificationSink,
) {
    let mut backoff = ReconnectBackoff::default();
    loop {
        let connected_at = Instant::now();
        if let Ok(mut stream) = transport.connect().await {
            let mut decoder = SseDecoder::new();
            while let Ok(Ok(Some(chunk))) = tokio::time::timeout(STREAM_IDLE_TIMEOUT, stream.next_chunk()).await {
                if Instant::now().duration_since(connected_at) >= STREAM_IDLE_TIMEOUT {
                    backoff.reset();
                }
                let allowed_at_receipt = sink.gate().allows();
                let candidates = decoder
                    .push(&chunk)
                    .into_iter()
                    .filter_map(|frame| {
                        filter
                            .lock()
                            .ok()
                            .and_then(|mut filter| filter.consider(frame, Instant::now()))
                    })
                    .collect::<Vec<_>>();
                for candidate in candidates.into_iter().filter(|_| allowed_at_receipt) {
                    let intent = match candidate {
                        Candidate::Ready(intent) => Some(intent),
                        Candidate::Refetch {
                            run_id,
                            mut stamps,
                            intent,
                        } => {
                            if !sink.gate().allows() {
                                continue;
                            }
                            if let Ok(Some(detail)) =
                                tokio::time::timeout(REQUEST_TIMEOUT, transport.run_timestamps(&run_id)).await
                            {
                                stamps.fill_missing(detail);
                            }
                            stamps.is_long_running().then_some(intent)
                        }
                    };
                    if let Some(intent) = intent.filter(|_| sink.gate().allows()) {
                        sink.deliver(intent);
                    }
                }
            }
        }
        tokio::time::sleep(backoff.next_delay()).await;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test(start_paused = true)]
    async fn both_retained_sets_are_bounded_by_size_and_age_even_without_transition_events() {
        for keys in [
            &mut NotificationFilter::default().approvals,
            &mut NotificationFilter::default().runs,
        ] {
            let created = Instant::now();
            for index in 0..RETAINED_KEYS * 3 {
                assert!(keys.insert(&index.to_string(), created));
                assert!(keys.0.len() <= RETAINED_KEYS);
            }
            assert_eq!(keys.0.len(), RETAINED_KEYS);
            assert!(keys.insert("0", created));
            assert!(!keys.insert("0", created));
            tokio::time::advance(RETENTION).await;
            assert!(keys.insert("0", Instant::now()));
            assert_eq!(keys.0.len(), 1);
        }
    }
}
