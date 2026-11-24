use std::collections::{HashMap, VecDeque};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use async_trait::async_trait;
use chrono::DateTime;
use serde::Deserialize;
use tokio::sync::{Notify, RwLock, Semaphore};
use tokio::task::JoinSet;
use tokio::time::Instant;
use url::Url;

use crate::LoopbackOrigin;

mod answers;
mod registry;
use answers::{AnswerPosts, Turn};
pub use registry::{NotificationReceipt, NotificationRegistry, NotificationSnapshot};

#[derive(Clone, Debug)]
pub struct UnreadState {
    pub total: u64,
    /// None on an old server or a malformed/incomplete map. Badge painting is
    /// independent; withdrawal must fail closed without a complete raw map.
    pub by_session: Option<HashMap<String, u64>>,
}

impl UnreadState {
    fn parse(inbox: &serde_json::Value) -> Option<Self> {
        let total = inbox
            .get("badge_total")
            .or_else(|| inbox.get("unread_total"))?
            .as_u64()?;
        let by_session = (|| {
            let values = inbox.get("unread_by_session")?.as_object()?;
            let mut map = HashMap::new();
            let mut sum = 0u64;
            for (id, value) in values {
                if id.is_empty() {
                    return None;
                }
                let count = value.as_u64()?;
                sum = sum.checked_add(count)?;
                map.insert(id.clone(), count);
            }
            // Unlike the paged rows, this map covers all unread Sessions. Its
            // raw total must agree: a partial map must not look like a read.
            (inbox.get("unread_total")?.as_u64()? == sum).then_some(map)
        })();
        Some(Self { total, by_session })
    }
}

pub const RETAINED_KEYS: usize = 512;
pub const RETENTION: Duration = Duration::from_secs(24 * 60 * 60);
const MAX_EVENT_BYTES: usize = 64 * 1024;
const MAX_DETAIL_BYTES: usize = 256 * 1024;
const REQUEST_TIMEOUT: Duration = Duration::from_secs(5);
const STREAM_IDLE_TIMEOUT: Duration = Duration::from_secs(45);
const MAX_DETAIL_LOOKUPS: usize = 16;
const MAX_PENDING_LOOKUPS: usize = 64;
/// Shortest gap between two unread reads. Events arriving inside it collapse
/// into one read after it, so a busy stream costs at most one read per gap.
pub const UNREAD_REFRESH_INTERVAL: Duration = Duration::from_secs(1);

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
    AnswerRequested,
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

/// Account preferences are independent of the shell's device/focus gate.
#[derive(Clone, Copy, Debug, Deserialize, PartialEq, Eq)]
pub struct NotificationPreferences {
    pub enabled: bool,
    pub bots: bool,
    pub sessions: bool,
}

impl Default for NotificationPreferences {
    fn default() -> Self {
        Self {
            enabled: true,
            bots: true,
            sessions: true,
        }
    }
}

impl NotificationPreferences {
    fn allows_run(self, category: Option<&str>) -> bool {
        self.enabled
            && match category {
                Some("bot") => self.bots,
                Some("session") => self.sessions,
                // Old servers, failed details, and overload have no trustworthy
                // category. Generic copy must not bypass a quiet category.
                _ => self.bots && self.sessions,
            }
    }
}

struct DeliveryContext {
    slots: Arc<Semaphore>,
    answers: Arc<Mutex<AnswerPosts>>,
    current: Arc<RwLock<NotificationPreferences>>,
    at_receipt: NotificationPreferences,
    presentation_generation: u64,
    receipt: Arc<NotificationReceipt>,
}

pub trait NotificationSink: Send + Sync {
    /// Only a sink that actually submits native reply actions can replace a
    /// turn's ordinary success notice with its question.
    fn supports_answer_actions(&self) -> bool {
        false
    }

    fn registry(&self) -> &Mutex<NotificationRegistry>;
    fn gate(&self) -> NotificationGate;
    /// Capture before asynchronous detail/badge reads. The sink rechecks this
    /// receipt at the final output boundary, including queued main-thread work.
    fn presentation_generation(&self) -> u64 {
        0
    }
    /// `source` names where a run happened ("Project · Session"), already
    /// sanitized by [`RunDetail::source`]; `None` keeps the generic copy.
    fn deliver(
        &self,
        intent: NotificationIntent,
        source: Option<String>,
        presentation_generation: u64,
        receipt: Arc<NotificationReceipt>,
    );
    /// Recheck the presentation/connection generation at the native boundary,
    /// then retire only this pre-request snapshot. Focus/preferences do not
    /// gate reads: reading the visible Workbench must withdraw its old posts.
    fn withdraw(&self, snapshot: NotificationSnapshot, unread: HashMap<String, u64>, presentation_generation: u64);
    /// The Runtime's badge total (loud-category unread), for the app icon.
    /// It is state rather than an interruption: no device/focus gate applies.
    fn show_unread(&self, total: u64, presentation_generation: u64);
    /// An Agent question the user can answer from the notification. The
    /// default forgets the reservation: a sink without answers shows nothing.
    fn deliver_answer_request(
        &self,
        request: AnswerRequest,
        source: Option<String>,
        presentation_generation: u64,
        receipt: Arc<NotificationReceipt>,
    ) {
        let _ = (request, source, presentation_generation);
        self.registry()
            .lock()
            .expect("notification registry lock")
            .forget(receipt.id());
    }
    /// A remote desktop update event is only a nudge. The shell must re-read
    /// and validate its request file; the SSE payload never reaches this API.
    fn desktop_update_requested(&self) {}
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
    questions: RetainedKeys,
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

/// The run detail the shell reads back after a terminal event: the stamps
/// that decide whether it is background work, plus the labels that say where
/// it ran. Every label is Runtime text on its way to an OS surface, so it only
/// leaves through [`RunDetail::source`].
#[derive(Clone, Default, Deserialize)]
pub struct RunDetail {
    #[serde(flatten)]
    pub stamps: RunTimestamps,
    pub session_platform: Option<String>,
    pub session_is_workbench: Option<bool>,
    pub session_project_name: Option<String>,
    pub session_label: Option<String>,
    pub definition_name: Option<String>,
    pub notification_category: Option<String>,
    pub session_id: Option<String>,
    pub metadata: Option<serde_json::Value>,
}

const SOURCE_PART_CHARS: usize = 48;

/// Invisible formatting a single-line title has no use for: bidi marks,
/// embeddings, overrides and isolates (which let a label render its own text
/// backwards), zero-width space, word joiner, BOM, and the private-use area.
/// ZWNJ, ZWJ and variation selectors stay: they spell real words and emoji and
/// cannot reorder text. Same set as `core/citations.py::_CONTROL_RE`.
fn is_invisible_format(character: char) -> bool {
    matches!(
        character,
        '\u{061c}'
            | '\u{200b}'
            | '\u{200e}'
            | '\u{200f}'
            | '\u{202a}'..='\u{202e}'
            | '\u{2060}'
            | '\u{2066}'..='\u{2069}'
            | '\u{feff}'
            | '\u{e000}'..='\u{f8ff}'
    )
}

fn clean_label(value: Option<&str>) -> Option<String> {
    let words = value?
        .chars()
        .filter(|character| !is_invisible_format(*character))
        .map(|character| {
            if character.is_control() || matches!(character, '\u{2028}' | '\u{2029}') {
                ' '
            } else {
                character
            }
        })
        .collect::<String>();
    let collapsed = words.split_whitespace().collect::<Vec<_>>().join(" ");
    if collapsed.is_empty() {
        return None;
    }
    if collapsed.chars().count() <= SOURCE_PART_CHARS {
        return Some(collapsed);
    }
    let mut truncated = collapsed.chars().take(SOURCE_PART_CHARS - 1).collect::<String>();
    truncated.truncate(truncated.trim_end().len());
    truncated.push('…');
    Some(truncated)
}

impl RunDetail {
    /// "Project · Session" for a Workbench session, the channel for an IM
    /// session, and the task or watch name when no session resolved. Control
    /// characters and invisible bidi/zero-width formatting are removed,
    /// whitespace collapsed, and each part bounded.
    pub fn source(&self) -> Option<String> {
        let project = clean_label(self.session_project_name.as_deref());
        let place = clean_label(self.session_label.as_deref()).or_else(|| clean_label(self.definition_name.as_deref()));
        match (project, place) {
            (Some(project), Some(place)) if project != place => Some(format!("{project} · {place}")),
            (project, place) => place.or(project),
        }
    }
}

#[derive(Deserialize)]
struct RunEvent {
    run_id: String,
    session_id: Option<String>,
    status: String,
    run_type: Option<String>,
    #[serde(flatten)]
    stamps: RunTimestamps,
}

/// The most quick replies a notification offers as buttons; the rest stay in the conversation.
pub const MAX_ANSWER_OPTIONS: usize = 3;
const PREVIEW_CHARS: usize = 180;

/// One Agent question the user can answer from the notification: the message
/// whose quick replies are still open, the options it offers, and the
/// Runtime's revision of that option set (`None` from an older Runtime,
/// which then gets no buttons).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AnswerRequest {
    pub session_id: String,
    pub message_id: String,
    pub revision: Option<String>,
    pub options: Vec<String>,
    pub preview: Option<String>,
    pub turn_id: Option<String>,
}

#[derive(Deserialize)]
struct MessageEvent {
    id: String,
    #[serde(rename = "type")]
    kind: Option<String>,
    session_id: Option<String>,
    platform: Option<String>,
    author: Option<String>,
    text: Option<String>,
    #[serde(default)]
    content: Option<serde_json::Value>,
    metadata: Option<serde_json::Value>,
}

fn turn_id(metadata: Option<&serde_json::Value>) -> Option<&str> {
    metadata?.get("turn_id")?.as_str().filter(|id| !id.trim().is_empty())
}

impl MessageEvent {
    fn question(self) -> Option<AnswerRequest> {
        // Only a `result` counts as unread, which is what withdraws the
        // notification once the question is read anywhere.
        if self.id.is_empty()
            || self.author.as_deref() != Some("agent")
            || self.platform.as_deref() != Some("avibe")
            || self.kind.as_deref() != Some("result")
        {
            return None;
        }
        let session_id = self.session_id.filter(|id| !id.is_empty())?;
        let content = self.content?;
        if content
            .get("quick_reply_chosen")
            .is_some_and(|chosen| !chosen.is_null())
        {
            return None;
        }
        let options = content
            .get("quick_replies")?
            .as_array()?
            .iter()
            .map(|option| option.as_str().map(str::to_owned))
            .collect::<Option<Vec<_>>>()?;
        if options.is_empty() || options.iter().any(|option| option.trim().is_empty()) {
            return None;
        }
        let revision = content
            .get("quick_reply_revision")
            .and_then(serde_json::Value::as_str)
            .filter(|revision| revision.len() == 64 && revision.bytes().all(|byte| byte.is_ascii_hexdigit()))
            .map(str::to_owned);
        Some(AnswerRequest {
            session_id,
            message_id: self.id,
            revision,
            options,
            preview: preview(self.text.as_deref()),
            turn_id: turn_id(self.metadata.as_ref()).map(str::to_owned),
        })
    }
}

/// The message text as a notification body: one line, bounded, without the
/// invisible formatting a label may not carry.
fn preview(text: Option<&str>) -> Option<String> {
    let words = text?
        .chars()
        .filter(|character| !is_invisible_format(*character))
        .map(|character| if character.is_control() { ' ' } else { character })
        .collect::<String>();
    let collapsed = words.split_whitespace().collect::<Vec<_>>().join(" ");
    if collapsed.is_empty() {
        return None;
    }
    if collapsed.chars().count() <= PREVIEW_CHARS {
        return Some(collapsed);
    }
    let mut truncated = collapsed.chars().take(PREVIEW_CHARS - 1).collect::<String>();
    truncated.truncate(truncated.trim_end().len());
    truncated.push('…');
    Some(truncated)
}

enum Candidate {
    Approval {
        request_id: String,
    },
    Question(AnswerRequest),
    Run {
        run_id: String,
        session_id: Option<String>,
        stamps: RunTimestamps,
        intent: NotificationIntent,
        /// Already background work by type or by the event's own stamps; the
        /// detail read only supplies the source label.
        background: bool,
    },
}

impl NotificationFilter {
    fn consider(&mut self, frame: SseFrame, now: Instant) -> Option<Candidate> {
        if !matches!(frame.event.as_str(), "vaults.updated" | "runs.updated" | "message.new") {
            return None;
        }
        let envelope: Envelope = serde_json::from_str(&frame.data).ok()?;
        if envelope.event != frame.event {
            return None;
        }
        if frame.event == "message.new" {
            let message: MessageEvent = serde_json::from_value(envelope.data).ok()?;
            let question = message.question()?;
            return self
                .questions
                .insert(&question.message_id, now)
                .then_some(Candidate::Question(question));
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
                .then_some(Candidate::Approval {
                    request_id: approval.request_id,
                });
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
        let background = matches!(run.run_type.as_deref(), Some("scheduled" | "watch")) || run.stamps.is_long_running();
        (background || run.stamps.missing()).then_some(Candidate::Run {
            run_id: run.run_id,
            session_id: run.session_id,
            stamps: run.stamps,
            intent,
            background,
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
    async fn run_detail(&self, run_id: &str) -> Option<RunDetail>;
    async fn unread(&self) -> Option<UnreadState>;
    async fn preferences(&self) -> Option<NotificationPreferences>;
    /// Where a Session is and which account category it belongs to.
    async fn session_detail(&self, session_id: &str) -> Option<SessionDetail> {
        let _ = session_id;
        None
    }
}

/// One Session's Inbox row as the shell needs it: its labels and category.
#[derive(Clone, Default, Deserialize)]
pub struct SessionDetail {
    pub session_id: String,
    pub project_name: Option<String>,
    pub title: Option<String>,
    pub category: Option<String>,
}

impl SessionDetail {
    /// "Project · Session", with the same cleaning and bounds as a run's source.
    pub fn source(&self) -> Option<String> {
        let project = clean_label(self.project_name.as_deref());
        let place = clean_label(self.title.as_deref());
        match (project, place) {
            (Some(project), Some(place)) if project != place => Some(format!("{project} · {place}")),
            (project, place) => place.or(project),
        }
    }
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

    async fn run_detail(&self, run_id: &str) -> Option<RunDetail> {
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

    /// The same whole-account total the Workbench Inbox badges, counted by the
    /// Runtime: the shell never derives a count of its own.
    async fn unread(&self) -> Option<UnreadState> {
        let mut url = self.url("/api/inbox");
        url.query_pairs_mut()
            .append_pair("platform", "avibe")
            .append_pair("limit", "1");
        let response = self.client.get(url).timeout(REQUEST_TIMEOUT).send().await.ok()?;
        if !response.status().is_success() {
            return None;
        }
        let bytes = bounded_detail_body(response).await?;
        let inbox: serde_json::Value = serde_json::from_slice(&bytes).ok()?;
        // Fall back only when absent, not when malformed: an explicit zero
        // must never turn into the raw (possibly quiet-category) unread count.
        UnreadState::parse(&inbox)
    }

    async fn session_detail(&self, session_id: &str) -> Option<SessionDetail> {
        let mut url = self.url("/api/inbox");
        url.query_pairs_mut()
            .append_pair("platform", "avibe")
            .append_pair("session", session_id)
            .append_pair("limit", "1");
        let response = self.client.get(url).timeout(REQUEST_TIMEOUT).send().await.ok()?;
        if !response.status().is_success() {
            return None;
        }
        let bytes = bounded_detail_body(response).await?;
        let inbox: serde_json::Value = serde_json::from_slice(&bytes).ok()?;
        inbox
            .get("sessions")?
            .as_array()?
            .iter()
            .filter_map(|row| serde_json::from_value::<SessionDetail>(row.clone()).ok())
            .find(|row| row.session_id == session_id)
    }

    async fn preferences(&self) -> Option<NotificationPreferences> {
        let response = self
            .client
            .get(self.url("/api/notifications/preferences"))
            .timeout(REQUEST_TIMEOUT)
            .send()
            .await
            .ok()?;
        if !response.status().is_success() {
            return None;
        }
        let bytes = bounded_detail_body(response).await?;
        let document: serde_json::Value = serde_json::from_slice(&bytes).ok()?;
        if !document.get("ok")?.as_bool()? {
            return None;
        }
        serde_json::from_value(document.get("preferences")?.clone()).ok()
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
    transport: Arc<dyn NotificationTransport>,
    filter: Arc<Mutex<NotificationFilter>>,
    sink: Arc<dyn NotificationSink>,
) {
    let mut backoff = ReconnectBackoff::default();
    let preferences = Arc::new(RwLock::new(NotificationPreferences::default()));
    // Scoped to this adopted Runtime connection; a presentation change is
    // additionally fenced by the epoch in each exact turn key.
    let answers = Arc::new(Mutex::new(AnswerPosts::default()));
    // Any frame may follow an unread change, and listing the event types that
    // can would miss the next one added, so every connection and every frame
    // (the 15-second heartbeat included) asks for a fresh total. The reader
    // owns the rate. Dropping the set with this future stops it.
    let unread_changed = Arc::new(Notify::new());
    let mut unread = JoinSet::new();
    unread.spawn(follow_unread(transport.clone(), sink.clone(), unread_changed.clone()));
    // Detail reads run beside the stream, never inside it: a slow read must
    // not hold back the next chunk (and the approval it may carry). Dropping
    // the set with this future aborts whatever is still in flight.
    let mut lookups = JoinSet::new();
    let slots = Arc::new(Semaphore::new(MAX_DETAIL_LOOKUPS));
    loop {
        let connected_at = Instant::now();
        if let Ok(mut stream) = transport.connect().await {
            unread_changed.notify_one();
            refresh_preferences(&transport, &preferences).await;
            let mut decoder = SseDecoder::new();
            while let Ok(Ok(Some(chunk))) = tokio::time::timeout(STREAM_IDLE_TIMEOUT, stream.next_chunk()).await {
                if Instant::now().duration_since(connected_at) >= STREAM_IDLE_TIMEOUT {
                    backoff.reset();
                }
                while lookups.try_join_next().is_some() {}
                // Suppression is not a queue: frames still reach the filter so
                // its de-duplication stays current, then are dropped.
                for frame in decoder.push(&chunk) {
                    unread_changed.notify_one();
                    if frame.event == "notifications.preferences.updated" {
                        // Only a nudge: never trust preferences from the SSE payload.
                        refresh_preferences(&transport, &preferences).await;
                        continue;
                    }
                    let at_receipt = *preferences.read().await;
                    let presentation_generation = sink.presentation_generation();
                    let allowed_at_receipt = sink.gate().allows() && at_receipt.enabled;
                    if frame.event == "desktop.update.requested" {
                        sink.desktop_update_requested();
                        continue;
                    }
                    let candidate = filter
                        .lock()
                        .ok()
                        .and_then(|mut filter| filter.consider(frame, Instant::now()))
                        .filter(|_| allowed_at_receipt);
                    match candidate {
                        None => {}
                        Some(Candidate::Approval { request_id }) => {
                            if sink.gate().allows() {
                                let receipt = sink
                                    .registry()
                                    .lock()
                                    .expect("notification registry lock")
                                    .reserve(None);
                                if let Some(receipt) = receipt {
                                    receipt.set_opens(format!("avibe://vaults/request/{request_id}"));
                                    sink.deliver(
                                        NotificationIntent::ApprovalRequested,
                                        None,
                                        presentation_generation,
                                        receipt,
                                    );
                                }
                            }
                        }
                        Some(Candidate::Question(request)) => {
                            if !sink.gate().allows() {
                                continue;
                            }
                            let Some(receipt) = sink
                                .registry()
                                .lock()
                                .expect("notification registry lock")
                                .reserve(Some(request.session_id.clone()))
                            else {
                                continue;
                            };
                            while lookups.try_join_next().is_some() {}
                            if lookups.len() >= MAX_PENDING_LOOKUPS {
                                // A question's category is only known from its
                                // read; overloaded, it fails closed.
                                sink.registry()
                                    .lock()
                                    .expect("notification registry lock")
                                    .forget(receipt.id());
                                continue;
                            }
                            if sink.supports_answer_actions() && request.revision.is_some() {
                                if let Some(turn) = Turn::new(
                                    presentation_generation,
                                    Some(&request.session_id),
                                    request.turn_id.as_deref(),
                                ) {
                                    answers.lock().expect("answer posts").observe(turn, &receipt);
                                }
                            }
                            let (transport, sink) = (transport.clone(), sink.clone());
                            let preferences = DeliveryContext {
                                slots: slots.clone(),
                                answers: answers.clone(),
                                current: preferences.clone(),
                                at_receipt,
                                presentation_generation,
                                receipt,
                            };
                            lookups.spawn(async move {
                                notify_question(transport, sink, request, preferences).await;
                            });
                        }
                        Some(Candidate::Run {
                            run_id,
                            session_id,
                            stamps,
                            intent,
                            background,
                        }) => {
                            let Some(receipt) = sink
                                .registry()
                                .lock()
                                .expect("notification registry lock")
                                .reserve(session_id)
                            else {
                                continue;
                            };
                            while lookups.try_join_next().is_some() {}
                            if lookups.len() >= MAX_PENDING_LOOKUPS {
                                // Overloaded: the backlog is bounded. Work the
                                // event already qualifies notifies now with
                                // generic copy; a run only its read could
                                // qualify fails closed.
                                sink.registry()
                                    .lock()
                                    .expect("notification registry lock")
                                    .forget(receipt.id());
                                if background && at_receipt.allows_run(None) && sink.gate().allows() {
                                    sink.deliver(intent, None, presentation_generation, receipt);
                                }
                                continue;
                            }
                            let (transport, sink) = (transport.clone(), sink.clone());
                            let preferences = DeliveryContext {
                                slots: slots.clone(),
                                answers: answers.clone(),
                                current: preferences.clone(),
                                at_receipt,
                                presentation_generation,
                                receipt,
                            };
                            lookups.spawn(async move {
                                notify_run(transport, sink, run_id, stamps, intent, background, preferences).await;
                            });
                        }
                    }
                }
            }
        }
        tokio::time::sleep(backoff.next_delay()).await;
    }
}

/// Refresh before consuming further frames. The bounded write lock also holds
/// in-flight run deliveries until the new preference snapshot is known. On a
/// failed/old-server read, keep the last valid value (initially all on).
async fn refresh_preferences(
    transport: &Arc<dyn NotificationTransport>,
    preferences: &RwLock<NotificationPreferences>,
) {
    let mut current = preferences.write().await;
    if let Ok(Some(fresh)) = tokio::time::timeout(REQUEST_TIMEOUT, transport.preferences()).await {
        *current = fresh;
    }
}

/// Reads the total after each signal, then waits out the refresh interval.
/// `Notify` keeps at most one stored signal, so any number of events during a
/// read or a wait become exactly one more read. A failed read keeps the badge
/// already shown until the next frame — at the latest the next heartbeat.
async fn follow_unread(
    transport: Arc<dyn NotificationTransport>,
    sink: Arc<dyn NotificationSink>,
    changed: Arc<Notify>,
) {
    loop {
        changed.notified().await;
        let presentation_generation = sink.presentation_generation();
        let snapshot = sink.registry().lock().expect("notification registry lock").snapshot();
        if let Some(unread) = transport.unread().await {
            sink.show_unread(unread.total, presentation_generation);
            if let Some(by_session) = unread.by_session {
                sink.withdraw(snapshot, by_session, presentation_generation);
            }
        }
        tokio::time::sleep(UNREAD_REFRESH_INTERVAL).await;
    }
}

/// One bounded read per question: it names the source and the account
/// category whose switch decides whether the question interrupts at all.
async fn notify_question(
    transport: Arc<dyn NotificationTransport>,
    sink: Arc<dyn NotificationSink>,
    request: AnswerRequest,
    preferences: DeliveryContext,
) {
    let Ok(slot) = preferences.slots.clone().acquire_owned().await else {
        sink.registry()
            .lock()
            .expect("notification registry lock")
            .forget(preferences.receipt.id());
        return;
    };
    let detail = tokio::time::timeout(REQUEST_TIMEOUT, transport.session_detail(&request.session_id))
        .await
        .ok()
        .flatten();
    drop(slot);
    let category = detail.as_ref().and_then(|detail| detail.category.as_deref());
    let current = preferences.current.read().await;
    let receipt = preferences.receipt;
    if detail.is_none()
        || !preferences.at_receipt.allows_run(category)
        || !current.allows_run(category)
        || receipt.retired()
        || !sink.gate().allows()
    {
        sink.registry()
            .lock()
            .expect("notification registry lock")
            .forget(receipt.id());
        return;
    }
    // A question is a new Workbench message: reading its Session withdraws
    // it, also when that happened before the shell sampled it as unread.
    receipt.confirm_unread_message();
    let source = detail.as_ref().and_then(SessionDetail::source);
    sink.deliver_answer_request(request, source, preferences.presentation_generation, receipt);
}

/// One bounded read per terminal run: it names the source and, when the event
/// lacked stamps, decides whether the run was background work at all.
async fn notify_run(
    transport: Arc<dyn NotificationTransport>,
    sink: Arc<dyn NotificationSink>,
    run_id: String,
    mut stamps: RunTimestamps,
    intent: NotificationIntent,
    background: bool,
    preferences: DeliveryContext,
) {
    let Ok(slot) = preferences.slots.clone().acquire_owned().await else {
        sink.registry()
            .lock()
            .expect("notification registry lock")
            .forget(preferences.receipt.id());
        return;
    };
    let detail = tokio::time::timeout(REQUEST_TIMEOUT, transport.run_detail(&run_id))
        .await
        .ok()
        .flatten();
    drop(slot);
    let source = detail.as_ref().and_then(RunDetail::source);
    // The snapshot covers Avibe Sessions, not IM channels. Unknown/old-server
    // details keep their notification instead of treating absence as a read.
    if detail.as_ref().is_some_and(|detail| {
        detail.session_is_workbench == Some(true) || detail.session_platform.as_deref() == Some("avibe")
    }) {
        // Failures are `error` messages, and cancellation may settle without
        // any message. Success is eligible only after the registry also sees
        // an unread -> read transition; success alone is not result evidence.
        preferences
            .receipt
            .confirm_workbench_session(matches!(intent, NotificationIntent::RunSucceeded));
    }
    let category = detail
        .as_ref()
        .and_then(|detail| detail.notification_category.as_deref());
    let current = preferences.current.read().await;
    if !preferences.at_receipt.allows_run(category) || !current.allows_run(category) {
        sink.registry()
            .lock()
            .expect("notification registry lock")
            .forget(preferences.receipt.id());
        return;
    }
    drop(current);
    let turn = detail.as_ref().and_then(|detail| {
        // A stale/inconsistent detail cannot establish a different Session's
        // ownership, even if the turn string happens to match.
        (detail.session_id.as_deref() == preferences.receipt.session_id())
            .then(|| {
                Turn::new(
                    preferences.presentation_generation,
                    detail.session_id.as_deref(),
                    turn_id(detail.metadata.as_ref()),
                )
            })
            .flatten()
    });
    let category = category.map(str::to_owned);
    if let Some(detail) = detail {
        stamps.fill_missing(detail.stamps);
    }
    if (background || stamps.is_long_running()) && sink.gate().allows() && !preferences.receipt.retired() {
        if matches!(intent, NotificationIntent::RunSucceeded) {
            if let Some(turn) = turn {
                preferences
                    .receipt
                    .follow_answer(answers::Completion::new(&preferences.answers, turn.clone()));
                let submissions = preferences.answers.lock().expect("answer posts").submissions(&turn);
                if answers::any_submitted(submissions).await {
                    sink.registry()
                        .lock()
                        .expect("notification registry lock")
                        .forget(preferences.receipt.id());
                    return;
                }
            }
        }
        // A pending OS response may outlive the preference/focus snapshot.
        let current = preferences.current.read().await;
        if !current.allows_run(category.as_deref()) || !sink.gate().allows() || preferences.receipt.retired() {
            sink.registry()
                .lock()
                .expect("notification registry lock")
                .forget(preferences.receipt.id());
            return;
        }
        if !preferences.receipt.readable() {
            sink.registry()
                .lock()
                .expect("notification registry lock")
                .forget(preferences.receipt.id());
        }
        sink.deliver(intent, source, preferences.presentation_generation, preferences.receipt);
    } else {
        sink.registry()
            .lock()
            .expect("notification registry lock")
            .forget(preferences.receipt.id());
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const REVISION: &str = "591e4424a227942cfaa898ff551771f8d762d17663e9e4b77d74c8d983582885";

    fn message(data: serde_json::Value) -> SseFrame {
        SseFrame {
            event: "message.new".into(),
            data: serde_json::json!({ "type": "message.new", "data": data }).to_string(),
        }
    }

    fn agent_question(id: &str) -> serde_json::Value {
        serde_json::json!({
            "id": id,
            "session_id": "ses_a",
            "platform": "avibe",
            "author": "agent",
            "type": "result",
            "text": "PR #4031 passed review.\n\nMerge it now?",
            "content": { "quick_replies": ["Yes", "No"], "quick_reply_revision": REVISION },
        })
    }

    #[test]
    fn an_open_agent_question_on_the_workbench_becomes_one_answer_request() {
        let mut filter = NotificationFilter::default();
        let now = Instant::now();
        let Some(Candidate::Question(request)) = filter.consider(message(agent_question("m1")), now) else {
            panic!("an open question is a candidate");
        };
        assert_eq!(
            request,
            AnswerRequest {
                session_id: "ses_a".into(),
                message_id: "m1".into(),
                revision: Some(REVISION.into()),
                options: vec!["Yes".into(), "No".into()],
                preview: Some("PR #4031 passed review. Merge it now?".into()),
                turn_id: None,
            }
        );
        // The same message again (a mirror republish) asks nothing new.
        assert!(filter.consider(message(agent_question("m1")), now).is_none());
    }

    #[test]
    fn only_an_unanswered_agent_question_with_real_options_qualifies() {
        let base = agent_question("m");
        let variants: Vec<(&str, serde_json::Value)> = vec![
            ("/author", serde_json::json!("user")),
            ("/platform", serde_json::json!("slack")),
            ("/type", serde_json::json!("assistant")),
            ("/session_id", serde_json::json!("")),
            ("/content/quick_replies", serde_json::json!([])),
            ("/content/quick_replies", serde_json::json!(["Yes", " "])),
            ("/content/quick_replies", serde_json::json!(["Yes", 3])),
            ("/content/quick_reply_chosen", serde_json::json!("Yes")),
            ("/content/quick_reply_chosen", serde_json::json!("")),
        ];
        for (index, (pointer, value)) in variants.into_iter().enumerate() {
            let mut data = base.clone();
            let (parent, key) = pointer.rsplit_once('/').unwrap();
            let parent = if parent.is_empty() {
                &mut data
            } else {
                data.pointer_mut(parent).unwrap()
            };
            parent[key] = value;
            data["id"] = serde_json::json!(format!("m{index}"));
            let mut filter = NotificationFilter::default();
            assert!(
                filter.consider(message(data), Instant::now()).is_none(),
                "{pointer} must not qualify"
            );
        }
    }

    #[test]
    fn a_question_without_a_valid_revision_is_offered_without_buttons() {
        for revision in [
            serde_json::Value::Null,
            serde_json::json!("short"),
            serde_json::json!(REVISION.replace('5', "g")),
        ] {
            let mut data = agent_question("m1");
            data["content"]["quick_reply_revision"] = revision;
            let mut filter = NotificationFilter::default();
            let Some(Candidate::Question(request)) = filter.consider(message(data), Instant::now()) else {
                panic!("the question itself still qualifies");
            };
            assert_eq!(request.revision, None);
        }
    }

    #[test]
    fn a_preview_is_one_bounded_line_without_invisible_formatting() {
        assert_eq!(preview(Some("a\u{202e}b\n\n c")), Some("ab c".into()));
        assert_eq!(preview(Some(" \n ")), None);
        let long = preview(Some(&"字".repeat(400))).unwrap();
        assert_eq!(long.chars().count(), PREVIEW_CHARS);
        assert!(long.ends_with('…'));
    }

    #[test]
    fn a_session_source_names_its_project_and_title_once() {
        let detail = |project: Option<&str>, title: Option<&str>| SessionDetail {
            session_id: "ses_a".into(),
            project_name: project.map(str::to_owned),
            title: title.map(str::to_owned),
            category: Some("session".into()),
        };
        assert_eq!(
            detail(Some("avibe-app"), Some("Dock fix")).source().as_deref(),
            Some("avibe-app · Dock fix")
        );
        assert_eq!(detail(Some("same"), Some("same")).source().as_deref(), Some("same"));
        assert_eq!(detail(None, Some("Dock fix")).source().as_deref(), Some("Dock fix"));
        assert_eq!(detail(None, None).source(), None);
    }

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
