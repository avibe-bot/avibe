//! Shares the Show a desktop window displays as its link, a PDF, or an image of
//! what is on screen, through the system share menu (C1 v1 outbound,
//! docs/plans/2026-11-19-desktop-show-outbound-sharing.md). The requests and
//! answers are the iPhone App's `share.capabilities` / `share.present`
//! (docs/plans/2026-11-18-ios-show-outbound-sharing.md), carried by the one
//! command `desktop_show_share`.
//!
//! Which Show, and which pixels, are decided by state the shell owns, never by
//! the page's DOM or URL. The Workbench may share a link to a Show the user can
//! use. A Show app window may share only the Show the shell opened it for: its
//! link, or a PDF or image of the window's whole web content. The window's
//! registered identity and current document fence the operation: a navigation,
//! a closed window, or a Runtime switch ends it as cancelled. No URL, path,
//! geometry or content is accepted from the page, and no permission is written.

#![cfg_attr(not(target_os = "macos"), allow(dead_code))]

use std::{
    collections::HashMap,
    io::Write,
    path::{Path, PathBuf},
    sync::{Arc, Mutex},
    time::{Duration, Instant, SystemTime},
};

use serde_json::{json, Value};
use tauri::{AppHandle, Manager, Webview};
use tokio::sync::oneshot;

/// Tells the page that this window can share a Show's link. Top-level
/// loopback documents only; the command still checks the window itself.
const MARKER: &str = "if(window.self===window.top&&(location.hostname==='127.0.0.1'||location.hostname==='[::1]'))Object.defineProperty(window,'__AVIBE_DESKTOP_SHOW_SHARE__',{value:1});";
/// A Show window also exports its content as a PDF or image.
const EXPORT_MARKER: &str = "if(window.self===window.top&&(location.hostname==='127.0.0.1'||location.hostname==='[::1]'))Object.defineProperty(window,'__AVIBE_DESKTOP_SHOW_EXPORT__',{value:1});";

/// The oldest macOS every call here exists on: `-[NSSharingServicePicker close]`
/// is macOS 13 (the isolated script world is 11). Older systems keep copying
/// the link; the app's own floor is lower.
const MIN_MACOS: (u64, u64) = (13, 0);

fn supported_on(version: (u64, u64)) -> bool {
    version >= MIN_MACOS
}

/// Whether this Mac runs every API the share calls.
pub(crate) fn supported() -> bool {
    #[cfg(target_os = "macos")]
    {
        supported_on(crate::macos_show_share::os_version())
    }
    #[cfg(not(target_os = "macos"))]
    {
        false
    }
}

fn marker_for(supported: bool) -> &'static str {
    if supported {
        MARKER
    } else {
        ""
    }
}

/// The Workbench's marker: it shares links. Nothing where sharing cannot run.
pub(crate) fn marker() -> &'static str {
    marker_for(supported())
}

/// A Show window's markers: it shares its link and exports its content.
pub(crate) fn show_window_markers() -> String {
    if supported() {
        format!("{MARKER}{EXPORT_MARKER}")
    } else {
        String::new()
    }
}

/// Who asks: the Workbench, or a Show window bound to one Show.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum Window<'a> {
    Workbench,
    Show(&'a str),
}

/// What a window may share. The Workbench has no trusted tie to the pixels of
/// a Show it embeds, so it shares links only; a Show window shares only the
/// Show it was opened for, as a link or as its own content.
pub(crate) fn decide(window: Window<'_>, request: &Request) -> Result<(), &'static str> {
    match window {
        Window::Workbench if request.kind != Kind::Link => Err("capability_mismatch"),
        Window::Show(bound) if bound != request.session_id => Err("forbidden"),
        _ => Ok(()),
    }
}

pub(crate) const MAX_BYTES: usize = 50 * 1024 * 1024;
const MAX_LINK_BYTES: usize = 32 * 1024;
const MAX_SESSION_BYTES: usize = 200;
const COMPLETION_GRACE: Duration = Duration::from_secs(5 * 60);
#[cfg(not(test))]
const OPERATION_LIFETIME: Duration = Duration::from_secs(15 * 60);
/// Short in tests, which wait for the watcher's expiry.
#[cfg(test)]
const OPERATION_LIFETIME: Duration = Duration::from_millis(1200);
const ORPHAN_LIFETIME: Duration = Duration::from_secs(24 * 60 * 60);
#[cfg(not(test))]
const RETRY_CLEANUP: Duration = Duration::from_secs(60);
/// Short in tests, which wait for the retry.
#[cfg(test)]
const RETRY_CLEANUP: Duration = Duration::from_millis(300);

/// The C1 v1 capability answer for a window: links everywhere, PDF and image
/// in a Show window, one attachment.
pub(crate) fn capabilities(window: Window<'_>) -> Value {
    let exports = matches!(window, Window::Show(_));
    json!({
        "version": 1,
        "inbound_kinds": [],
        "outbound_kinds": if exports { json!(["link", "pdf", "image"]) } else { json!(["link"]) },
        "max_bytes": if exports {
            json!({ "link": MAX_LINK_BYTES, "pdf": MAX_BYTES, "image": MAX_BYTES })
        } else {
            json!({ "link": MAX_LINK_BYTES })
        },
        "max_total_bytes": if exports { MAX_BYTES } else { MAX_LINK_BYTES },
        "max_attachments": 1,
        "supports_pending": false,
    })
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum Kind {
    Link,
    Pdf,
    Image,
}

impl Kind {
    fn parse(value: &str) -> Option<Self> {
        match value {
            "link" => Some(Self::Link),
            "pdf" => Some(Self::Pdf),
            "image" => Some(Self::Image),
            _ => None,
        }
    }
    pub(crate) fn as_str(self) -> &'static str {
        match self {
            Self::Link => "link",
            Self::Pdf => "pdf",
            Self::Image => "image",
        }
    }
    fn file_name(self) -> Option<&'static str> {
        match self {
            Self::Link => None,
            Self::Pdf => Some("Show.pdf"),
            Self::Image => Some("Show.png"),
        }
    }
}

/// One `share.present`: a UUIDv4 operation, a kind, and the Show's Session.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct Request {
    pub operation_id: String,
    pub kind: Kind,
    pub session_id: String,
}

#[derive(Debug, PartialEq, Eq)]
pub(crate) enum Call {
    Capabilities,
    Present(Request),
}

fn is_uuid_v4(value: &str) -> bool {
    let bytes = value.as_bytes();
    bytes.len() == 36
        && bytes.iter().enumerate().all(|(index, byte)| match index {
            8 | 13 | 18 | 23 => *byte == b'-',
            _ => byte.is_ascii_hexdigit(),
        })
        && bytes[14] == b'4'
        && matches!(bytes[19].to_ascii_lowercase(), b'8' | b'9' | b'a' | b'b')
}

fn valid_session(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= MAX_SESSION_BYTES
        && !value.chars().any(|c| c.is_control() || c == '/' || c == '\\')
}

/// The request, exactly as the Web UI sends it, or `None`. Unknown fields fail.
pub(crate) fn parse(value: &Value) -> Option<Call> {
    let object = value.as_object()?;
    match object.get("method")?.as_str()? {
        "share.capabilities" if object.len() == 1 => Some(Call::Capabilities),
        "share.present" if object.len() == 4 => {
            let operation_id = object.get("operationId")?.as_str()?;
            let kind = Kind::parse(object.get("kind")?.as_str()?)?;
            let session_id = object.get("sessionId")?.as_str()?;
            (is_uuid_v4(operation_id) && valid_session(session_id)).then(|| {
                Call::Present(Request {
                    operation_id: operation_id.to_ascii_lowercase(),
                    kind,
                    session_id: session_id.to_owned(),
                })
            })
        }
        _ => None,
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum Status {
    /// The system reported that a service took the item. Not delivered or read.
    Completed,
    Cancelled,
    Failed,
    Expired,
}

impl Status {
    fn as_str(self) -> &'static str {
        match self {
            Self::Completed => "completed",
            Self::Cancelled => "cancelled",
            Self::Failed => "failed",
            Self::Expired => "expired",
        }
    }
}

/// The answer the Web UI reads: the same operation and kind, a status, and an
/// error code for failures.
pub(crate) fn answer(request: &Request, status: Status, error: Option<&str>) -> Value {
    let mut value = json!({
        "operationId": request.operation_id,
        "kind": request.kind.as_str(),
        "status": status.as_str(),
    });
    if let Some(code) = error {
        value["error"] = json!({ "code": code });
    }
    value
}

struct Active {
    request: Request,
    caller: String,
    started: Instant,
    token: u64,
    replies: Vec<oneshot::Sender<Value>>,
    /// A boundary has seen its window move on.
    moved: bool,
}

/// One operation's export on disk, from its claim until its removal is
/// acknowledged. While a lease exists its operation id is reserved: no other
/// operation may use it, so no other writer or remover can touch its directory.
#[derive(Clone, Copy, Debug)]
struct Lease {
    /// The token of the operation that claimed it.
    owner: u64,
    stage: Stage,
    /// When its removal falls due; set when its operation ends.
    due: Option<Instant>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Stage {
    /// Its writer is still writing.
    Writing,
    /// Written, or the write failed; waiting for its removal to fall due.
    Written,
    /// A cleaner is removing it now.
    Removing,
}

struct Terminal {
    request: Request,
    caller: String,
    answer: Value,
    at: Instant,
}

/// What `admit` decided for a request.
pub(crate) enum Admission {
    /// A new operation, which the caller now owns.
    Start(u64, oneshot::Receiver<Value>),
    /// The same operation is in flight; wait for its answer.
    Wait(oneshot::Receiver<Value>),
    /// Answered now: a replayed result, or a refusal.
    Answer(Value),
}

/// One share at a time. Its answer is replayed to a repeated request for the
/// same operation from the same window for a day, and refused to anyone else.
///
/// Every decision is taken and committed under the one lock that holds this
/// state (toctou-inventory.md): an end observes, concludes and commits at once;
/// a claim checks the operation's recorded currency where it creates the lease.
/// Facts are read and effects run outside it.
#[derive(Default)]
pub(crate) struct Coordinator {
    active: Option<Active>,
    terminal: HashMap<String, Terminal>,
    leases: HashMap<String, Lease>,
    next: u64,
}

impl Coordinator {
    pub(crate) fn admit(&mut self, request: &Request, caller: &str, now: Instant) -> Admission {
        self.terminal
            .retain(|_, terminal| now.saturating_duration_since(terminal.at) < ORPHAN_LIFETIME);
        if let Some(terminal) = self.terminal.get(&request.operation_id) {
            return Admission::Answer(if terminal.request == *request && terminal.caller == caller {
                terminal.answer.clone()
            } else {
                answer(request, Status::Failed, Some("operation_conflict"))
            });
        }
        if let Some(active) = &mut self.active {
            if active.request == *request && active.caller == caller {
                let (sender, receiver) = oneshot::channel();
                active.replies.push(sender);
                return Admission::Wait(receiver);
            }
            let code = if active.request.operation_id == request.operation_id {
                "operation_conflict"
            } else {
                "busy"
            };
            return Admission::Answer(answer(request, Status::Failed, Some(code)));
        }
        // An earlier operation's file still owns this id, after its answer is
        // forgotten: the id is not reused until that file is gone.
        if self.leases.contains_key(&request.operation_id) {
            return Admission::Answer(answer(request, Status::Failed, Some("operation_conflict")));
        }
        self.next += 1;
        let (sender, receiver) = oneshot::channel();
        self.active = Some(Active {
            request: request.clone(),
            caller: caller.to_owned(),
            started: now,
            token: self.next,
            replies: vec![sender],
            moved: false,
        });
        Admission::Start(self.next, receiver)
    }

    pub(crate) fn is_active(&self, token: u64) -> bool {
        self.active.as_ref().is_some_and(|active| active.token == token)
    }

    /// Records what a boundary saw of live operation `token`, and answers with
    /// what it now is: once seen `Moved`, it stays `Moved`, so coming back to
    /// the route later does not make it live again.
    pub(crate) fn observe(&mut self, token: u64, seen: Currency) -> Currency {
        match self.active.as_mut().filter(|active| active.token == token) {
            None => Currency::Ended,
            Some(active) => {
                if seen == Currency::Moved {
                    active.moved = true;
                }
                if active.moved {
                    Currency::Moved
                } else {
                    seen
                }
            }
        }
    }

    /// Claims the file of live operation `token` before a byte is written, so
    /// it has an owner from the start. Refused for an ended operation, for one
    /// already seen to have moved on, and for an id that already has a file.
    pub(crate) fn claim(&mut self, token: u64) -> bool {
        let Some(id) = self
            .active
            .as_ref()
            .filter(|active| active.token == token && !active.moved)
            .map(|active| active.request.operation_id.clone())
        else {
            return false;
        };
        if self.leases.contains_key(&id) {
            return false;
        }
        self.leases.insert(
            id,
            Lease {
                owner: token,
                stage: Stage::Writing,
                due: None,
            },
        );
        true
    }

    /// The writer of operation `owner` is done with `id`, whether it wrote or
    /// not.
    pub(crate) fn release(&mut self, id: &str, owner: u64) {
        if let Some(lease) = self
            .leases
            .get_mut(id)
            .filter(|lease| lease.owner == owner && lease.stage == Stage::Writing)
        {
            lease.stage = Stage::Written;
        }
    }

    /// The one terminal transaction. Records what the ending boundary saw of
    /// operation `token`, reads the currency kept for it, lets `conclude`
    /// decide what `end` means, and commits that answer, all under this lock:
    /// a departure recorded by any boundary before this commit cancels it, and
    /// a departure recorded after it finds the operation ended. The watcher's
    /// look (`End::Watch`) expires this very token once it is overdue. The
    /// caller closes the menu, answers the waiters and cleans up afterwards.
    pub(crate) fn end(
        &mut self,
        token: u64,
        seen: Currency,
        end: End,
        now: Instant,
    ) -> Option<(Vec<oneshot::Sender<Value>>, Value)> {
        let currency = self.observe(token, seen);
        let end = match end {
            End::Watch if self.overdue(now) == Some(token) => End::Expired,
            end => end,
        };
        let (status, error) = conclude(currency, end)?;
        self.commit(token, status, error, now)
    }

    /// Stores the answer `end` decided. The file the operation claimed is kept
    /// for the completion grace after a completed share, and released now
    /// otherwise.
    fn commit(
        &mut self,
        token: u64,
        status: Status,
        error: Option<&str>,
        now: Instant,
    ) -> Option<(Vec<oneshot::Sender<Value>>, Value)> {
        if !self.is_active(token) {
            return None;
        }
        let active = self.active.take()?;
        let value = answer(&active.request, status, error);
        let id = active.request.operation_id.clone();
        if let Some(lease) = self.leases.get_mut(&id).filter(|lease| lease.owner == token) {
            lease.due = Some(if status == Status::Completed {
                now + COMPLETION_GRACE
            } else {
                now
            });
        }
        self.terminal.insert(
            id,
            Terminal {
                request: active.request,
                caller: active.caller,
                answer: value.clone(),
                at: now,
            },
        );
        Some((active.replies, value))
    }

    /// The active operation, when it has outlived its fifteen minutes.
    pub(crate) fn overdue(&self, now: Instant) -> Option<u64> {
        self.active
            .as_ref()
            .filter(|active| now.saturating_duration_since(active.started) >= OPERATION_LIFETIME)
            .map(|active| active.token)
    }

    /// Files that may now be removed, with the operation that owns each: due,
    /// and no longer being written. Each is handed to exactly one cleaner and
    /// stays leased, so its id stays reserved, until `removed` acknowledges it.
    pub(crate) fn due(&mut self, now: Instant) -> Vec<(String, u64)> {
        let mut due = Vec::new();
        for (id, lease) in &mut self.leases {
            if lease.stage == Stage::Written && lease.due.is_some_and(|at| at <= now) {
                lease.stage = Stage::Removing;
                due.push((id.clone(), lease.owner));
            }
        }
        due
    }

    /// The cleaner's acknowledgement for the file of operation `owner`: gone,
    /// so its id is free; or still there, so it is tried again shortly. Only
    /// the removal `due` handed out is acknowledged.
    pub(crate) fn removed(&mut self, id: &str, owner: u64, gone: bool, now: Instant) {
        let Some(lease) = self
            .leases
            .get_mut(id)
            .filter(|lease| lease.owner == owner && lease.stage == Stage::Removing)
        else {
            return;
        };
        if gone {
            self.leases.remove(id);
        } else {
            lease.stage = Stage::Written;
            lease.due = Some(now + RETRY_CLEANUP);
        }
    }

    /// When the next lease falls due. One still being written is not counted
    /// (its writer's release starts the cleanup), nor one being removed (its
    /// cleaner acknowledges it).
    pub(crate) fn next_due(&self) -> Option<Instant> {
        self.leases
            .values()
            .filter(|lease| lease.stage == Stage::Written)
            .filter_map(|lease| lease.due)
            .min()
    }
}

/// The share directory. Only operation directories named by their UUID are
/// made, read or removed here, and no name, path or content is logged.
pub(crate) struct Files {
    root: PathBuf,
}

impl Files {
    pub(crate) fn new(root: PathBuf) -> Self {
        Self { root }
    }

    pub(crate) fn default_root() -> PathBuf {
        std::env::temp_dir().join("AvibeShowShares")
    }

    fn private_dir(path: &Path) -> std::io::Result<()> {
        match std::fs::create_dir(path) {
            Ok(()) => {}
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {}
            Err(error) => return Err(error),
        }
        let metadata = std::fs::symlink_metadata(path)?;
        if !metadata.is_dir() || metadata.file_type().is_symlink() {
            return Err(std::io::Error::other("not a directory"));
        }
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o700))?;
        }
        Ok(())
    }

    /// Writes one export under an opaque name and returns its path.
    pub(crate) fn write(&self, id: &str, kind: Kind, data: &[u8]) -> std::io::Result<PathBuf> {
        let name = kind.file_name().ok_or_else(|| std::io::Error::other("link"))?;
        if !is_uuid_v4(id) || data.is_empty() || data.len() > MAX_BYTES {
            return Err(std::io::Error::other("invalid export"));
        }
        if let Some(parent) = self.root.parent() {
            std::fs::create_dir_all(parent)?;
        }
        Self::private_dir(&self.root)?;
        let directory = self.root.join(id);
        let existed = std::fs::symlink_metadata(&directory).is_ok();
        Self::private_dir(&directory)?;
        let path = directory.join(name);
        let written = (|| {
            let mut options = std::fs::OpenOptions::new();
            options.write(true).create_new(true);
            #[cfg(unix)]
            {
                use std::os::unix::fs::OpenOptionsExt;
                options.mode(0o600);
            }
            let mut file = options.open(&path)?;
            file.write_all(data)?;
            file.sync_all()
        })();
        if let Err(error) = written {
            // Only the operation directory this call made goes; never the root,
            // never one that was there before.
            if !existed {
                let _ = std::fs::remove_dir_all(&directory);
            }
            return Err(error);
        }
        Ok(path)
    }

    pub(crate) fn remove(&self, id: &str) -> std::io::Result<()> {
        if !is_uuid_v4(id) {
            return Ok(());
        }
        match std::fs::remove_dir_all(self.root.join(id)) {
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
            other => other,
        }
    }

    /// Removes operation directories older than a day, left by a shell that
    /// quit or crashed before its cleanup ran.
    pub(crate) fn remove_orphans(&self, now: SystemTime) -> std::io::Result<()> {
        let entries = match std::fs::read_dir(&self.root) {
            Ok(entries) => entries,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(()),
            Err(error) => return Err(error),
        };
        for entry in entries.flatten() {
            let Some(name) = entry.file_name().to_str().map(str::to_owned) else {
                continue;
            };
            let Ok(metadata) = std::fs::symlink_metadata(entry.path()) else {
                continue;
            };
            if !is_uuid_v4(&name) || !metadata.is_dir() || metadata.file_type().is_symlink() {
                continue;
            }
            let created = metadata.created().or_else(|_| metadata.modified());
            if created.is_ok_and(|at| now.duration_since(at).is_ok_and(|age| age >= ORPHAN_LIFETIME)) {
                let _ = std::fs::remove_dir_all(entry.path());
            }
        }
        Ok(())
    }
}

/// A link the page's own Show APIs produced: https, or this Runtime's origin.
pub(crate) fn valid_link(link: &str, origin: &url::Url) -> Option<url::Url> {
    if link.len() > MAX_LINK_BYTES {
        return None;
    }
    let url = url::Url::parse(link).ok()?;
    let same_origin = url.origin() == origin.origin();
    ((url.scheme() == "https" || same_origin) && url.username().is_empty() && url.password().is_none()).then_some(url)
}

/// What an operation can see of its window at a boundary.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct Facts {
    /// The window's registration holds: not retired, it owns its native
    /// resource, and its document generation is the one admitted.
    pub registered: bool,
    /// The Runtime receipt holds: epoch, presentation owner and role.
    pub receipt: bool,
    /// The top document's URL, from WebKit.
    pub url: Option<url::Url>,
}

/// What the operation was admitted for: its Runtime's origin, and for a Show
/// window, the registered route.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct Binding {
    pub origin: url::Url,
    pub route: Option<String>,
}

/// Whether an operation can still act.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum Currency {
    Live,
    /// The window, its document or its Runtime moved on.
    Moved,
    /// The operation already ended.
    Ended,
}

impl Currency {
    /// What a boundary sees of its window: `Live` while its window's
    /// registration and its Runtime's receipt hold, its top document is on the
    /// Runtime's origin, and in a Show window its path is still the registered
    /// `/apps/show/<id>`; `Moved` otherwise. A single-page `pushState` or
    /// `replaceState` away changes WebKit's URL, so it is seen here. The
    /// Workbench is bound to its document and Runtime, not to whichever chat it
    /// shows. Whether the operation is still active, and what was seen of it
    /// before, the coordinator adds under its lock (`Coordinator::observe`).
    pub(crate) fn of(binding: &Binding, facts: &Facts) -> Self {
        let on_route = facts.url.as_ref().is_some_and(|url| {
            url.origin() == binding.origin.origin() && binding.route.as_deref().is_none_or(|route| url.path() == route)
        });
        if facts.registered && facts.receipt && on_route {
            Self::Live
        } else {
            Self::Moved
        }
    }
}

/// What brought an operation to its end.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum End {
    /// A boundary stopped the run: an effect failed or the operation is not live.
    Error(&'static str),
    /// The system share service reported.
    Service(Status, Option<&'static str>),
    /// The operation outlived its fifteen minutes.
    Expired,
    /// The watcher's periodic look: it ends the operation only if it moved on,
    /// or, as `Expired`, if it is overdue.
    Watch,
}

/// The one place that picks a terminal status, from the operation's observed
/// currency and what ended it: an ended operation needs nothing; a window that
/// moved on cancelled it, whatever the service or the clock said; otherwise
/// the service's report, the expiry, or the failure stands.
fn conclude(currency: Currency, end: End) -> Option<(Status, Option<&'static str>)> {
    match (currency, end) {
        (Currency::Ended, _) => None,
        (Currency::Moved, _) => Some((Status::Cancelled, None)),
        (Currency::Live, End::Error(code)) => Some((Status::Failed, Some(code))),
        (Currency::Live, End::Service(status, error)) => Some((status, error)),
        (Currency::Live, End::Expired) => Some((Status::Expired, Some("expired"))),
        (Currency::Live, End::Watch) => None,
    }
}

/// The item handed to the share menu.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) enum Shared {
    Link(String),
    File(PathBuf),
}

/// How the attempt to show the share menu came out.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum Presented {
    Shown,
    /// The window, its document or its Runtime moved on before the menu showed.
    Moved,
    /// The operation ended before the menu showed.
    Ended,
    Failed,
}

/// Where the menu is created (the main thread), the operation's say over it.
pub(crate) struct Gate {
    operation: Arc<Operation>,
    shell: Arc<Shell>,
}

impl Gate {
    /// Whether the menu may be created, from the window's facts read there.
    pub(crate) fn admit(&self, facts: &Facts) -> Currency {
        self.operation.observe(&self.shell, facts)
    }

    /// Whether the operation still owns its menu, from the coordinator alone:
    /// no WebKit or AppKit call, so nothing else runs on the main thread
    /// meanwhile.
    pub(crate) fn holds(&self) -> Currency {
        self.shell
            .coordinator
            .lock()
            .expect("show share")
            .observe(self.operation.token, Currency::Live)
    }
}

/// The open share menus, by operation token, kept where they are created and
/// closed: the main thread. A menu is registered after the system has shown it
/// and then checked against its operation in the same step; closing takes it
/// by token. Whichever of the two comes second closes it, so a menu never
/// outlives its operation without an owner, whether its close was queued
/// before or after it was shown, and whatever AppKit runs while showing it.
pub(crate) struct Menus<M> {
    open: HashMap<u64, M>,
}

impl<M> Default for Menus<M> {
    fn default() -> Self {
        Self { open: HashMap::new() }
    }
}

impl<M> Menus<M> {
    /// Registers the menu just shown for `token`, then asks `holds` whether
    /// its operation still owns it. If the operation ended or moved on
    /// meanwhile, its close may already have run and found nothing: the menu
    /// is handed back for the caller to close.
    pub(crate) fn register(&mut self, token: u64, menu: M, holds: impl FnOnce() -> Currency) -> (Presented, Option<M>) {
        self.open.insert(token, menu);
        match holds() {
            Currency::Live => (Presented::Shown, None),
            Currency::Moved => (Presented::Moved, self.open.remove(&token)),
            Currency::Ended => (Presented::Ended, self.open.remove(&token)),
        }
    }

    /// The menu of `token`, if one is registered, for the caller to close.
    pub(crate) fn close(&mut self, token: u64) -> Option<M> {
        self.open.remove(&token)
    }
}

/// Everything an operation does to the world, so its lifecycle can be driven
/// against real WebKit and AppKit in the shell and against a script in tests.
#[async_trait::async_trait]
pub(crate) trait Effects: Send + Sync {
    /// The window's facts, read where they can be read safely.
    async fn facts(&self) -> Facts;
    /// The Show's access and link, from the instance's read APIs.
    async fn read(&self, session: &str) -> Option<Value>;
    /// The window's whole web content, encoded as `kind`.
    async fn capture(&self, kind: Kind) -> Option<Vec<u8>>;
    /// Writes one export into the share directory.
    async fn write(&self, files: &Files, id: &str, kind: Kind, bytes: Vec<u8>) -> std::io::Result<PathBuf>;
    /// Shows the share menu if `gate` still admits it where it is created,
    /// then registers it in the operation's `Menus` under `gate.holds()`;
    /// `report` runs once with the person's choice.
    async fn present(
        &self,
        item: Shared,
        token: u64,
        gate: Gate,
        report: Box<dyn FnOnce(Status, Option<&'static str>) + Send>,
    ) -> Presented;
    /// Closes the menu of `token`, if one is registered.
    fn close(&self, token: u64);
}

pub(crate) struct Shell {
    coordinator: Mutex<Coordinator>,
    files: Files,
}

impl Shell {
    pub(crate) fn new(files: Files) -> Self {
        Self {
            coordinator: Mutex::new(Coordinator::default()),
            files,
        }
    }

    fn active(&self, token: u64) -> bool {
        self.coordinator.lock().expect("show share").is_active(token)
    }

    /// The writer's claim on an operation's file; dropping it, on any exit,
    /// releases the file to its lease and runs the cleanup.
    fn claim(self: &Arc<Self>, token: u64, id: &str) -> Option<Claim> {
        self.coordinator
            .lock()
            .expect("show share")
            .claim(token)
            .then(|| Claim {
                shell: self.clone(),
                id: id.to_owned(),
                owner: token,
            })
    }

    /// Ends operation `token` if `end`, with what was `seen`, ends it
    /// (`Coordinator::end`); then, outside the lock, closes its menu, answers
    /// every waiter and cleans up. True if this call ended it.
    fn end(self: &Arc<Self>, effects: &Arc<dyn Effects>, token: u64, seen: Currency, end: End) -> bool {
        let ended = self
            .coordinator
            .lock()
            .expect("show share")
            .end(token, seen, end, Instant::now());
        let Some((replies, value)) = ended else { return false };
        effects.close(token);
        for reply in replies {
            let _ = reply.send(value.clone());
        }
        self.clean();
        true
    }

    /// Removes every export whose lease has ended, then waits for the next one.
    fn clean(self: &Arc<Self>) {
        let now = Instant::now();
        let due = self.coordinator.lock().expect("show share").due(now);
        for (id, owner) in due {
            let gone = self.files.remove(&id).is_ok();
            self.coordinator
                .lock()
                .expect("show share")
                .removed(&id, owner, gone, now);
        }
        let next = self.coordinator.lock().expect("show share").next_due();
        if let Some(next) = next {
            let shell = self.clone();
            tauri::async_runtime::spawn(async move {
                tokio::time::sleep(next.saturating_duration_since(Instant::now())).await;
                shell.clean();
            });
        }
    }
}

struct Claim {
    shell: Arc<Shell>,
    id: String,
    owner: u64,
}

impl Drop for Claim {
    fn drop(&mut self) {
        self.shell
            .coordinator
            .lock()
            .expect("show share")
            .release(&self.id, self.owner);
        self.shell.clean();
    }
}

/// How often an operation checks that its window still shows its Show.
const WATCH: Duration = Duration::from_millis(250);

/// One admitted share: its request, its token, and what it was admitted for.
pub(crate) struct Operation {
    pub request: Request,
    pub token: u64,
    pub binding: Binding,
}

impl Operation {
    /// Runs the operation to its end: a worker that prepares and presents,
    /// and a watcher that ends it once it expires or its window moves on.
    pub(crate) fn start(self, shell: Arc<Shell>, effects: Arc<dyn Effects>) {
        let operation = Arc::new(self);
        let (watch_shell, watch_effects, watched) = (shell.clone(), effects.clone(), operation.clone());
        tauri::async_runtime::spawn(async move { watched.watch(watch_shell, watch_effects).await });
        tauri::async_runtime::spawn(async move {
            if let Err(code) = operation.run(&shell, &effects).await {
                operation.end(&shell, &effects, End::Error(code)).await;
            }
        });
    }

    /// The only way an operation ends: the window's facts, read now, and
    /// `end` go to the one terminal transaction (`Coordinator::end`).
    async fn end(&self, shell: &Arc<Shell>, effects: &Arc<dyn Effects>, end: End) -> bool {
        let facts = effects.facts().await;
        shell.end(effects, self.token, Currency::of(&self.binding, &facts), end)
    }

    async fn watch(&self, shell: Arc<Shell>, effects: Arc<dyn Effects>) {
        loop {
            tokio::time::sleep(WATCH).await;
            if !shell.active(self.token) {
                return;
            }
            // Moved or overdue ends it, in the same transaction that looks:
            // the menu goes, and nothing is shared from it.
            if self.end(&shell, &effects, End::Watch).await {
                return;
            }
        }
    }

    /// The operation's currency at this boundary, as the coordinator keeps it:
    /// once a boundary has seen it `Moved`, it stays `Moved`.
    fn observe(&self, shell: &Shell, facts: &Facts) -> Currency {
        shell
            .coordinator
            .lock()
            .expect("show share")
            .observe(self.token, Currency::of(&self.binding, facts))
    }

    /// Prepares the item and presents it. Every boundary asks `observe`; any
    /// answer but `Live` ends the run, and `end` concludes it.
    async fn run(self: &Arc<Self>, shell: &Arc<Shell>, effects: &Arc<dyn Effects>) -> Result<(), &'static str> {
        let request = &self.request;
        let state = effects.read(&request.session_id).await.ok_or("forbidden")?;
        self.check(shell, effects, "forbidden").await?;
        if state.get("allowed") != Some(&Value::Bool(true)) {
            return Err("forbidden");
        }
        let item = match request.kind {
            Kind::Link => {
                let link = state.get("url").and_then(Value::as_str).ok_or("invalid_link")?;
                let url = valid_link(link, &self.binding.origin).ok_or("invalid_link")?;
                Shared::Link(url.to_string())
            }
            Kind::Pdf | Kind::Image => {
                if state.get("visible") != Some(&Value::Bool(true)) {
                    return Err("not_visible");
                }
                let bytes = effects.capture(request.kind).await.ok_or("export_failed")?;
                self.check(shell, effects, "view_changed").await?;
                // The file has an owner before it has a byte: an operation
                // that ended, or was seen to move on, gets no claim and
                // writes nothing.
                let Some(claim) = shell.claim(self.token, &request.operation_id) else {
                    return Err("view_changed");
                };
                let written = effects
                    .write(&shell.files, &request.operation_id, request.kind, bytes)
                    .await;
                drop(claim);
                Shared::File(written.map_err(|_| "export_failed")?)
            }
        };
        self.check(shell, effects, "view_changed").await?;
        // Checked again where the menu is created, with the same observation,
        // and once more when the shown menu is registered.
        let gate = Gate {
            operation: self.clone(),
            shell: shell.clone(),
        };
        // The service's report ends the operation through the same transaction.
        let (report_operation, report_shell, report_effects) = (self.clone(), shell.clone(), effects.clone());
        let report = Box::new(move |status: Status, error: Option<&'static str>| {
            tauri::async_runtime::spawn(async move {
                report_operation
                    .end(&report_shell, &report_effects, End::Service(status, error))
                    .await;
            });
        });
        match effects.present(item, self.token, gate, report).await {
            Presented::Shown | Presented::Ended => Ok(()),
            Presented::Moved => Err("view_changed"),
            Presented::Failed => Err("presentation_failed"),
        }
    }

    /// `Err(code)` unless the operation is still live; `end` concludes it,
    /// with what this check saw kept.
    async fn check(
        &self,
        shell: &Arc<Shell>,
        effects: &Arc<dyn Effects>,
        code: &'static str,
    ) -> Result<(), &'static str> {
        let facts = effects.facts().await;
        match self.observe(shell, &facts) {
            Currency::Live => Ok(()),
            Currency::Moved | Currency::Ended => Err(code),
        }
    }
}

/// Manages the share state and clears exports a previous run left behind.
pub(crate) fn setup(app: &AppHandle) {
    let files = Files::new(Files::default_root());
    let _ = files.remove_orphans(SystemTime::now());
    app.manage(Arc::new(Shell::new(files)));
}

/// `share.capabilities` and `share.present` for the Workbench and Show app
/// windows on this computer's Runtime (capability `show-share`).
#[tauri::command]
pub async fn desktop_show_share(webview: Webview, request: Value) -> Result<Value, String> {
    let Some(call) = parse(&request) else {
        return Err("invalid_request".into());
    };
    if !supported() {
        return Err("unsupported".into());
    }
    let Some(source) = crate::app_windows::share_source(&webview) else {
        return Err("forbidden".into());
    };
    let window = source.bound_show().map_or(Window::Workbench, Window::Show);
    match call {
        Call::Capabilities => Ok(capabilities(window)),
        Call::Present(request) => Ok(present(webview, request).await),
    }
}

#[cfg(not(target_os = "macos"))]
async fn present(_webview: Webview, request: Request) -> Value {
    answer(&request, Status::Failed, Some("capability_mismatch"))
}

#[cfg(target_os = "macos")]
async fn present(webview: Webview, request: Request) -> Value {
    let Some(source) = crate::app_windows::share_source(&webview) else {
        return answer(&request, Status::Failed, Some("forbidden"));
    };
    if let Err(code) = decide(source.bound_show().map_or(Window::Workbench, Window::Show), &request) {
        return answer(&request, Status::Failed, Some(code));
    }
    let Some(shell) = webview
        .app_handle()
        .try_state::<Arc<Shell>>()
        .map(|shell| shell.inner().clone())
    else {
        return answer(&request, Status::Failed, Some("unavailable"));
    };
    let admission = shell
        .coordinator
        .lock()
        .expect("show share")
        .admit(&request, webview.label(), Instant::now());
    let receiver = match admission {
        Admission::Answer(value) => return value,
        Admission::Wait(receiver) => receiver,
        Admission::Start(token, receiver) => {
            let binding = Binding {
                origin: source.origin().clone(),
                route: source.bound_route().map(str::to_owned),
            };
            let effects: Arc<dyn Effects> = Arc::new(macos::WebKit::new(webview.app_handle().clone(), source));
            Operation {
                request: request.clone(),
                token,
                binding,
            }
            .start(shell, effects);
            receiver
        }
    };
    receiver
        .await
        .unwrap_or_else(|_| answer(&request, Status::Failed, Some("unavailable")))
}

#[cfg(target_os = "macos")]
mod macos {
    use std::{cell::RefCell, path::PathBuf};

    use objc2::{rc::Retained, MainThreadMarker};
    use objc2_foundation::{NSPoint, NSRect, NSSize};
    use objc2_web_kit::WKWebView;
    use serde_json::Value;
    use tauri::{AppHandle, Webview};
    use tokio::sync::oneshot;

    use super::{Currency, Facts, Files, Gate, Kind, Menus, Presented, Shared, Status};
    use crate::{
        app_windows::ShareSource,
        macos_show_share::{self as native, Item, Outcome, Picker},
    };

    thread_local! {
        /// Main thread only: the open share menu of each operation.
        static PICKERS: RefCell<Menus<Picker>> = RefCell::new(Menus::default());
    }

    /// The operation's WebKit and AppKit: its window's web view, the share
    /// scripts, the capture, and the system share menu.
    pub(super) struct WebKit {
        app: AppHandle,
        source: ShareSource,
    }

    impl WebKit {
        pub(super) fn new(app: AppHandle, source: ShareSource) -> Self {
            Self { app, source }
        }
    }

    /// The window's facts, on the main thread inside a WebView call: the
    /// registration and Runtime receipt read only atomics and std mutexes, and
    /// the URL is WebKit's own, so nothing waits on the event loop here.
    fn facts_of(source: &ShareSource, webview: &WKWebView) -> Facts {
        Facts {
            registered: source.document_current(),
            receipt: source.receipt_current(),
            url: native::document_url(webview).and_then(|url| url::Url::parse(&url).ok()),
        }
    }

    fn with_wk(view: &Webview, run: impl FnOnce(&WKWebView, MainThreadMarker) + Send + 'static) -> bool {
        view.with_webview(move |platform| {
            let Some(mtm) = MainThreadMarker::new() else { return };
            // SAFETY: Tauri hands out the live WKWebView, on the main thread,
            // for the duration of this closure.
            let Some(webview) = (unsafe { Retained::retain(platform.inner().cast::<WKWebView>()) }) else {
                return;
            };
            run(&webview, mtm);
        })
        .is_ok()
    }

    #[async_trait::async_trait]
    impl super::Effects for WebKit {
        /// Read on the main thread, from WebKit's own URL, without any getter
        /// that waits on the event loop. While the system share menu tracks the
        /// mouse, this read and the menu's closing wait until it closes.
        async fn facts(&self) -> Facts {
            let (sender, receiver) = oneshot::channel::<Facts>();
            let source = self.source.clone();
            let started = with_wk(self.source.view(), move |webview, _| {
                let _ = sender.send(facts_of(&source, webview));
            });
            let unknown = Facts {
                registered: false,
                receipt: false,
                url: None,
            };
            if !started {
                return unknown;
            }
            receiver.await.unwrap_or(unknown)
        }

        async fn read(&self, session: &str) -> Option<Value> {
            let (sender, receiver) = oneshot::channel::<Option<String>>();
            let origin = self.source.origin().origin().ascii_serialization();
            let arguments = [("sessionID", session.to_owned()), ("origin", origin)];
            let wrapped = format!(
                "const run = async () => {{\n{}\n}};\nreturn JSON.stringify(await run());",
                native::READ
            );
            let started = with_wk(self.source.view(), move |webview, mtm| {
                let pairs: Vec<(&str, &str)> = arguments.iter().map(|(key, value)| (*key, value.as_str())).collect();
                native::call_script(webview, mtm, &wrapped, &pairs, move |answer| {
                    let _ = sender.send(answer);
                });
            });
            if !started {
                return None;
            }
            let text = receiver.await.ok()??;
            serde_json::from_str(&text).ok()
        }

        async fn capture(&self, kind: Kind) -> Option<Vec<u8>> {
            let (sender, receiver) = oneshot::channel::<Option<Vec<u8>>>();
            let started = with_wk(self.source.view(), move |webview, mtm| {
                if !native::within_pixel_cap(webview) {
                    let _ = sender.send(None);
                    return;
                }
                native::snapshot(webview, mtm, None, move |image| {
                    let bytes = image.and_then(|image| match kind {
                        Kind::Image => native::png(&image),
                        Kind::Pdf => native::pdf(&image, mtm),
                        Kind::Link => None,
                    });
                    let _ = sender.send(bytes.filter(|bytes| bytes.len() <= super::MAX_BYTES));
                });
            });
            if !started {
                return None;
            }
            receiver.await.ok()?
        }

        async fn write(&self, files: &Files, id: &str, kind: Kind, bytes: Vec<u8>) -> std::io::Result<PathBuf> {
            files.write(id, kind, &bytes)
        }

        /// Shows the share menu at the window's top right, under a Show
        /// window's title-bar share button, if the gate still admits it on the
        /// main thread where the menu is created; then registers it, and closes
        /// it at once if its operation ended or moved on while it was being
        /// shown (AppKit may run other main-thread work, this operation's close
        /// included, inside the call that shows it; that is assumed, not
        /// reproduced, and either order ends with the menu closed).
        async fn present(
            &self,
            item: Shared,
            token: u64,
            gate: Gate,
            report: Box<dyn FnOnce(Status, Option<&'static str>) + Send>,
        ) -> Presented {
            let (sender, receiver) = oneshot::channel::<Presented>();
            let checked = self.source.clone();
            let started = with_wk(self.source.view(), move |webview, mtm| {
                match gate.admit(&facts_of(&checked, webview)) {
                    Currency::Ended => {
                        let _ = sender.send(Presented::Ended);
                        return;
                    }
                    Currency::Moved => {
                        let _ = sender.send(Presented::Moved);
                        return;
                    }
                    Currency::Live => {}
                }
                let (bounds, _, _) = native::geometry(webview);
                let anchor = NSRect::new(
                    NSPoint::new(bounds.origin.x + bounds.size.width - 24.0, bounds.origin.y),
                    NSSize::new(1.0, 1.0),
                );
                let shared = match &item {
                    Shared::Link(link) => Item::Link(link),
                    Shared::File(path) => Item::File(path),
                };
                let report = Box::new(move |outcome: Outcome| match outcome {
                    Outcome::Completed => report(Status::Completed, None),
                    Outcome::Cancelled => report(Status::Cancelled, None),
                    Outcome::Failed => report(Status::Failed, Some("system_share_failed")),
                });
                let Some(picker) = native::present(webview, mtm, shared, anchor, report) else {
                    let _ = sender.send(Presented::Failed);
                    return;
                };
                let (presented, stray) =
                    PICKERS.with_borrow_mut(|pickers| pickers.register(token, picker, || gate.holds()));
                // Closed outside the registry: its delegate may report at once.
                if let Some(picker) = stray {
                    picker.close();
                }
                let _ = sender.send(presented);
            });
            if !started {
                return Presented::Failed;
            }
            receiver.await.unwrap_or(Presented::Failed)
        }

        /// Queued on the main thread. A menu not registered yet when this runs
        /// is closed by its registration, which finds its operation ended.
        fn close(&self, token: u64) {
            let _ = self.app.run_on_main_thread(move || {
                if let Some(picker) = PICKERS.with_borrow_mut(|pickers| pickers.close(token)) {
                    picker.close();
                }
            });
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const ID: &str = "3f2b8c4e-9a1d-4b6e-8f00-123456789abc";

    fn present(kind: &str, session: &str) -> Value {
        json!({ "method": "share.present", "operationId": ID, "kind": kind, "sessionId": session })
    }

    fn ids(due: Vec<(String, u64)>) -> Vec<String> {
        due.into_iter().map(|(id, _)| id).collect()
    }

    fn request(kind: Kind) -> Request {
        Request {
            operation_id: ID.into(),
            kind,
            session_id: "ses-1".into(),
        }
    }

    #[test]
    fn shares_only_where_every_api_it_calls_exists() {
        // `-[NSSharingServicePicker close]` is macOS 13; the isolated world is macOS 11.
        assert!(!supported_on((11, 7)));
        assert!(!supported_on((12, 7)));
        assert!(supported_on((13, 0)));
        assert!(supported_on((26, 1)));
        assert_eq!(marker_for(false), "");
        assert!(marker_for(true).contains("__AVIBE_DESKTOP_SHOW_SHARE__"));
    }

    // Review 9904474a P1: a Show's script could forge a frame (`history.replaceState`)
    // or hide another Show's frame in the Workbench, and the old DOM checks
    // accepted both. Nothing in the decision reads the page now.
    #[test]
    fn the_workbench_exports_nothing_whatever_frame_its_page_claims() {
        for kind in [Kind::Pdf, Kind::Image] {
            assert_eq!(decide(Window::Workbench, &request(kind)), Err("capability_mismatch"));
        }
        assert_eq!(decide(Window::Workbench, &request(Kind::Link)), Ok(()));
    }

    #[test]
    fn a_show_window_shares_only_its_own_show_even_with_another_show_s_frame_in_it() {
        let other = Request {
            session_id: "ses-b".into(),
            ..request(Kind::Link)
        };
        for kind in [Kind::Link, Kind::Pdf, Kind::Image] {
            assert_eq!(
                decide(Window::Show("ses-1"), &Request { kind, ..request(kind) }),
                Ok(())
            );
            assert_eq!(
                decide(Window::Show("ses-1"), &Request { kind, ..other.clone() }),
                Err("forbidden")
            );
        }
    }

    // Review 9904474a P2: a window that moved on cancels; it does not fail.
    #[test]
    fn a_share_stopped_by_its_window_moving_on_is_cancelled_not_failed() {
        let ends = [
            End::Error("view_changed"),
            End::Service(Status::Completed, None),
            End::Service(Status::Failed, Some("system_share_failed")),
            End::Expired,
            End::Watch,
        ];
        for end in ends {
            assert_eq!(
                conclude(Currency::Moved, end),
                Some((Status::Cancelled, None)),
                "{end:?}"
            );
            assert_eq!(conclude(Currency::Ended, end), None, "{end:?}");
        }
        assert_eq!(
            conclude(Currency::Live, End::Error("export_failed")),
            Some((Status::Failed, Some("export_failed")))
        );
        assert_eq!(
            conclude(Currency::Live, End::Service(Status::Completed, None)),
            Some((Status::Completed, None))
        );
        assert_eq!(
            conclude(Currency::Live, End::Expired),
            Some((Status::Expired, Some("expired")))
        );
        assert_eq!(conclude(Currency::Live, End::Watch), None, "a live look ends nothing");
    }

    #[test]
    fn once_seen_moved_an_operation_stays_moved() {
        let mut coordinator = Coordinator::default();
        let Admission::Start(token, _) = coordinator.admit(&request(Kind::Link), "main", Instant::now()) else {
            panic!()
        };
        assert_eq!(coordinator.observe(token, Currency::Live), Currency::Live);
        assert_eq!(coordinator.observe(token, Currency::Moved), Currency::Moved);
        assert_eq!(coordinator.observe(token, Currency::Live), Currency::Moved);
        assert_eq!(coordinator.observe(token + 1, Currency::Live), Currency::Ended);
    }

    // Review 9904474a P2: an export written after its operation ended is its
    // writer's to remove; cleanup has already run.
    #[test]
    fn a_file_is_claimed_before_it_is_written_and_cleaned_after_its_writer_is_done() {
        let mut coordinator = Coordinator::default();
        let now = Instant::now();
        let Admission::Start(token, _) = coordinator.admit(&request(Kind::Image), "main", now) else {
            panic!()
        };
        assert!(coordinator.claim(token));
        assert!(!coordinator.claim(token), "one claim per operation");
        // The operation ends while the file is written: nothing is due yet.
        coordinator.end(token, Currency::Live, End::Expired, now);
        assert!(coordinator.due(now).is_empty());
        assert!(
            coordinator.next_due().is_none(),
            "a lease being written does not schedule"
        );
        coordinator.release(ID, token);
        assert_eq!(coordinator.due(now), vec![(ID.to_owned(), token)]);
        // An ended operation gets no claim, so its worker writes nothing.
        assert!(!coordinator.claim(token));
    }

    // toctou-inventory T1: an end observes its operation live and unlocks; a watcher
    // records the departure; the end then commits what it had chosen. The recorded
    // departure must win: cancelled, and the file due at once.
    #[test]
    fn a_departure_recorded_before_the_end_commits_cancels_it() {
        for (status, error) in [
            (Status::Completed, None),
            (Status::Failed, Some("export_failed")),
            (Status::Expired, Some("expired")),
        ] {
            let mut coordinator = Coordinator::default();
            let now = Instant::now();
            let Admission::Start(token, _) = coordinator.admit(&request(Kind::Image), "main", now) else {
                panic!()
            };
            assert!(coordinator.claim(token));
            coordinator.release(ID, token);
            // What the end saw (live) and the watcher's departure, recorded first:
            // the end's observation and commit are now one transaction, so the
            // only interleaving left is the departure before it.
            assert_eq!(coordinator.observe(token, Currency::Moved), Currency::Moved);
            let end = match status {
                Status::Expired => End::Expired,
                Status::Failed => End::Error(error.unwrap()),
                status => End::Service(status, error),
            };
            let (_, value) = coordinator.end(token, Currency::Live, end, now).unwrap();
            assert_eq!(value["status"], "cancelled", "{status:?}");
            assert!(value.get("error").is_none(), "{status:?}");
            assert_eq!(coordinator.due(now).len(), 1, "{status:?}: due at once");
        }
    }

    // toctou-inventory W1: the worker's check saw the operation live; a watcher then
    // records the departure before the claim. A known-cancelled operation starts no
    // new export.
    #[test]
    fn a_departure_recorded_before_the_claim_refuses_it() {
        let mut coordinator = Coordinator::default();
        let now = Instant::now();
        let Admission::Start(token, _) = coordinator.admit(&request(Kind::Pdf), "main", now) else {
            panic!()
        };
        assert_eq!(coordinator.observe(token, Currency::Live), Currency::Live);
        assert_eq!(coordinator.observe(token, Currency::Moved), Currency::Moved);
        assert!(!coordinator.claim(token), "no export for a moved operation");
        assert!(coordinator.next_due().is_none());
    }

    // toctou-inventory A2/A6/G2-G6: the answer is forgotten after a day, but the
    // operation id stays its files' until they are gone. A request reusing it is
    // refused while the old file is being written, waits for removal, or is being
    // removed.
    #[test]
    fn an_operation_id_stays_owned_until_its_files_are_gone() {
        let later = |now: Instant| now + ORPHAN_LIFETIME + Duration::from_secs(1);
        let refused = |coordinator: &mut Coordinator, at: Instant, stage: &str| {
            let Admission::Answer(value) = coordinator.admit(&request(Kind::Image), "main", at) else {
                panic!("{stage}: the id was reused while its old file is owned")
            };
            assert_eq!(value["error"]["code"], "operation_conflict", "{stage}");
        };
        let now = Instant::now();

        // Still being written: the operation ended mid-write and the writer is slow.
        let mut coordinator = Coordinator::default();
        let Admission::Start(token, _) = coordinator.admit(&request(Kind::Image), "main", now) else {
            panic!()
        };
        assert!(coordinator.claim(token));
        coordinator.end(token, Currency::Live, End::Service(Status::Cancelled, None), now);
        refused(&mut coordinator, later(now), "writing");

        // Written and due, its removal not yet run (or failing ever since).
        let mut coordinator = Coordinator::default();
        let Admission::Start(token, _) = coordinator.admit(&request(Kind::Image), "main", now) else {
            panic!()
        };
        assert!(coordinator.claim(token));
        coordinator.release(ID, token);
        coordinator.end(token, Currency::Live, End::Service(Status::Completed, None), now);
        refused(&mut coordinator, later(now), "awaiting removal");

        // Taken by a cleaner, whose removal is still running.
        let mut coordinator = Coordinator::default();
        let Admission::Start(token, _) = coordinator.admit(&request(Kind::Image), "main", now) else {
            panic!()
        };
        assert!(coordinator.claim(token));
        coordinator.release(ID, token);
        coordinator.end(token, Currency::Live, End::Service(Status::Cancelled, None), now);
        assert_eq!(coordinator.due(now).len(), 1);
        refused(&mut coordinator, later(now), "being removed");

        // Its removal fails: the file is kept and tried again, the id still owned.
        // An acknowledgement from anyone but its owner changes nothing.
        coordinator.removed(ID, token + 1, true, now);
        refused(&mut coordinator, later(now), "acknowledged by another");
        coordinator.removed(ID, token, false, now);
        refused(&mut coordinator, later(now), "removal failed");
        assert_eq!(coordinator.next_due(), Some(now + RETRY_CLEANUP));
        assert!(coordinator.due(now).is_empty(), "not before the retry");
        assert_eq!(ids(coordinator.due(now + RETRY_CLEANUP)), vec![ID.to_owned()]);
        // Gone: the id is free again.
        coordinator.removed(ID, token, true, now);
        assert!(matches!(
            coordinator.admit(&request(Kind::Image), "main", later(now)),
            Admission::Start(..)
        ));
    }

    // toctou-inventory G2/G4/G5: a file is handed to one cleaner at a time; a
    // cleaner's late acknowledgement cannot touch a later lease of the same id.
    #[test]
    fn a_file_has_one_remover_and_only_its_removal_is_acknowledged() {
        let mut coordinator = Coordinator::default();
        let now = Instant::now();
        let Admission::Start(token, _) = coordinator.admit(&request(Kind::Image), "main", now) else {
            panic!()
        };
        assert!(coordinator.claim(token));
        coordinator.release(ID, token);
        coordinator.end(token, Currency::Live, End::Expired, now);
        assert_eq!(coordinator.due(now), vec![(ID.to_owned(), token)]);
        assert!(coordinator.due(now).is_empty(), "a second cleaner gets nothing");
        assert!(coordinator.next_due().is_none(), "no timer for a removal in progress");
        // A writer's release or a second acknowledgement after the first does nothing.
        coordinator.release(ID, token);
        coordinator.removed(ID, token, true, now);
        coordinator.removed(ID, token, false, now);
        assert!(coordinator.next_due().is_none(), "a stale failure revives nothing");
        assert!(matches!(
            coordinator.admit(&request(Kind::Image), "main", now + ORPHAN_LIFETIME),
            Admission::Start(..)
        ));
    }

    // toctou-inventory O2/Q1/N4 (token isolation): once operation A has ended and B
    // runs, nothing late from A reaches B: no observation, end, expiry, claim or close.
    #[test]
    fn nothing_late_from_an_ended_operation_reaches_the_next() {
        let mut coordinator = Coordinator::default();
        let now = Instant::now();
        let Admission::Start(a, _) = coordinator.admit(&request(Kind::Link), "main", now) else {
            panic!()
        };
        coordinator.end(a, Currency::Live, End::Service(Status::Cancelled, None), now);
        let other = Request {
            operation_id: "3f2b8c4e-9a1d-4b6e-8f00-000000000000".into(),
            ..request(Kind::Image)
        };
        let Admission::Start(b, _) = coordinator.admit(&other, "main", now) else {
            panic!()
        };
        assert_eq!(coordinator.observe(a, Currency::Moved), Currency::Ended);
        assert!(coordinator
            .end(a, Currency::Moved, End::Service(Status::Completed, None), now)
            .is_none());
        assert!(coordinator
            .end(a, Currency::Live, End::Watch, now + OPERATION_LIFETIME)
            .is_none());
        assert!(!coordinator.claim(a));
        assert_eq!(coordinator.observe(b, Currency::Live), Currency::Live, "B is not moved");
        assert!(coordinator.claim(b), "B still claims its file");

        let mut menus = Menus::default();
        assert_eq!(menus.register(b, "menu b", || Currency::Live), (Presented::Shown, None));
        assert_eq!(menus.close(a), None, "A's close leaves B's menu");
        assert_eq!(menus.close(b), Some("menu b"));
    }

    // toctou-inventory N2/N3/T3: registration and close meet in `Menus`; whichever
    // comes second closes the menu.
    #[test]
    fn a_menu_is_closed_whichever_of_its_registration_and_its_close_comes_second() {
        let mut menus = Menus::default();
        // Close first (it ran while the system was still showing the menu, or before),
        // then registration: the operation has ended, so the menu comes back to close.
        assert_eq!(menus.close(7), None);
        assert_eq!(
            menus.register(7, "menu", || Currency::Ended),
            (Presented::Ended, Some("menu"))
        );
        assert_eq!(menus.close(7), None, "nothing left registered");
        // Seen moved on but not yet ended: closed too, and reported as moved.
        assert_eq!(
            menus.register(8, "menu", || Currency::Moved),
            (Presented::Moved, Some("menu"))
        );
        // Registration first, then close: the close takes it.
        assert_eq!(menus.register(9, "menu", || Currency::Live), (Presented::Shown, None));
        assert_eq!(menus.close(9), Some("menu"));
    }

    #[test]
    fn accepts_only_the_exact_requests() {
        assert_eq!(
            parse(&json!({ "method": "share.capabilities" })),
            Some(Call::Capabilities)
        );
        assert_eq!(parse(&present("pdf", "ses-1")), Some(Call::Present(request(Kind::Pdf))));
        let upper = json!({ "method": "share.present", "operationId": ID.to_uppercase(), "kind": "image", "sessionId": "ses-1" });
        assert_eq!(parse(&upper), Some(Call::Present(request(Kind::Image))));
        let mut extra = present("link", "ses-1");
        extra["url"] = json!("https://example.com");
        let mut path = present("link", "ses-1");
        path["sessionId"] = json!("../etc");
        for bad in [
            json!({ "method": "share.capabilities", "x": 1 }),
            json!({ "method": "show.export" }),
            extra,
            path,
            present("video", "ses-1"),
            present("link", ""),
            present("link", &"x".repeat(201)),
            present("link", "a\u{0}b"),
            present("link", "a\\b"),
            json!({ "method": "share.present", "operationId": "3f2b8c4e-9a1d-1b6e-8f00-123456789abc", "kind": "link", "sessionId": "s" }),
            json!({ "method": "share.present", "operationId": "3f2b8c4e-9a1d-4b6e-7f00-123456789abc", "kind": "link", "sessionId": "s" }),
            json!({ "method": "share.present", "operationId": "not-a-uuid", "kind": "link", "sessionId": "s" }),
            json!([]),
        ] {
            assert_eq!(parse(&bad), None, "{bad}");
        }
    }

    #[test]
    fn answers_name_the_operation_and_only_failures_carry_a_code() {
        assert_eq!(
            answer(&request(Kind::Pdf), Status::Completed, None),
            json!({ "operationId": ID, "kind": "pdf", "status": "completed" })
        );
        assert_eq!(
            answer(&request(Kind::Link), Status::Failed, Some("forbidden"))["error"]["code"],
            "forbidden"
        );
        let caps = capabilities(Window::Show("ses-1"));
        assert_eq!(caps["version"], 1);
        assert_eq!(caps["outbound_kinds"], json!(["link", "pdf", "image"]));
        assert_eq!(caps["inbound_kinds"], json!([]));
        assert_eq!(capabilities(Window::Workbench)["outbound_kinds"], json!(["link"]));
    }

    #[test]
    fn one_operation_at_a_time_with_replay_only_to_its_own_window() {
        let mut coordinator = Coordinator::default();
        let now = Instant::now();
        let Admission::Start(token, _first) = coordinator.admit(&request(Kind::Pdf), "main", now) else {
            panic!("start")
        };
        assert!(matches!(
            coordinator.admit(&request(Kind::Pdf), "main", now),
            Admission::Wait(_)
        ));
        let Admission::Answer(conflict) = coordinator.admit(&request(Kind::Image), "main", now) else {
            panic!()
        };
        assert_eq!(conflict["error"]["code"], "operation_conflict");
        let other = Request {
            operation_id: "3f2b8c4e-9a1d-4b6e-8f00-000000000000".into(),
            ..request(Kind::Link)
        };
        let Admission::Answer(busy) = coordinator.admit(&other, "main", now) else {
            panic!()
        };
        assert_eq!(busy["error"]["code"], "busy");

        let (replies, value) = coordinator
            .end(token, Currency::Live, End::Service(Status::Cancelled, None), now)
            .unwrap();
        assert_eq!(replies.len(), 2);
        assert_eq!(value["status"], "cancelled");
        assert!(
            coordinator
                .end(token, Currency::Live, End::Service(Status::Completed, None), now)
                .is_none(),
            "finishes once"
        );

        let Admission::Answer(replayed) = coordinator.admit(&request(Kind::Pdf), "main", now) else {
            panic!()
        };
        assert_eq!(replayed, value);
        let Admission::Answer(foreign) = coordinator.admit(&request(Kind::Pdf), "avibe-app-show-00", now) else {
            panic!()
        };
        assert_eq!(foreign["error"]["code"], "operation_conflict");
        // A day later the operation is forgotten and starts again.
        assert!(matches!(
            coordinator.admit(&request(Kind::Pdf), "main", now + ORPHAN_LIFETIME),
            Admission::Start(..)
        ));
    }

    #[test]
    fn files_stay_five_minutes_after_completion_and_go_at_once_otherwise() {
        let now = Instant::now();
        for (end, wait) in [
            (End::Service(Status::Completed, None), COMPLETION_GRACE),
            (End::Service(Status::Cancelled, None), Duration::ZERO),
            (End::Error("export_failed"), Duration::ZERO),
            (End::Expired, Duration::ZERO),
        ] {
            let mut coordinator = Coordinator::default();
            let Admission::Start(token, _) = coordinator.admit(&request(Kind::Image), "main", now) else {
                panic!()
            };
            assert!(coordinator.claim(token));
            coordinator.release(ID, token);
            coordinator.end(token, Currency::Live, end, now);
            if wait > Duration::ZERO {
                assert!(coordinator.due(now + wait - Duration::from_secs(1)).is_empty());
            }
            assert_eq!(ids(coordinator.due(now + wait)), vec![ID.to_owned()], "{end:?}");
            assert!(coordinator.next_due().is_none());
        }
        let mut coordinator = Coordinator::default();
        let Admission::Start(token, _) = coordinator.admit(&request(Kind::Link), "main", now) else {
            panic!()
        };
        coordinator.end(token, Currency::Live, End::Service(Status::Completed, None), now);
        assert!(coordinator.next_due().is_none(), "a link writes no file");
    }

    #[test]
    fn an_operation_is_overdue_after_fifteen_minutes() {
        let mut coordinator = Coordinator::default();
        let now = Instant::now();
        let Admission::Start(token, _) = coordinator.admit(&request(Kind::Pdf), "main", now) else {
            panic!()
        };
        assert_eq!(
            coordinator.overdue(now + OPERATION_LIFETIME - Duration::from_secs(1)),
            None
        );
        assert_eq!(coordinator.overdue(now + OPERATION_LIFETIME), Some(token));
        // The watcher's look expires exactly this token, once it is overdue.
        let early = now + OPERATION_LIFETIME - Duration::from_secs(1);
        assert!(coordinator.end(token, Currency::Live, End::Watch, early).is_none());
        assert!(coordinator
            .end(token + 1, Currency::Live, End::Watch, now + OPERATION_LIFETIME)
            .is_none());
        let (_, value) = coordinator
            .end(token, Currency::Live, End::Watch, now + OPERATION_LIFETIME)
            .unwrap();
        assert_eq!(value["status"], "expired");
    }

    #[test]
    fn writes_private_opaque_files_and_removes_only_its_own_directories() {
        let root = tempfile::tempdir().unwrap();
        let files = Files::new(root.path().join("shares"));
        let path = files.write(ID, Kind::Pdf, b"%PDF").unwrap();
        assert_eq!(path.file_name().unwrap(), "Show.pdf");
        assert_eq!(path.parent().unwrap().file_name().unwrap(), ID);
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            assert_eq!(std::fs::metadata(&path).unwrap().permissions().mode() & 0o777, 0o600);
            assert_eq!(
                std::fs::metadata(path.parent().unwrap()).unwrap().permissions().mode() & 0o777,
                0o700
            );
        }
        assert!(files.write(ID, Kind::Pdf, b"again").is_err(), "never overwrites");
        assert!(files.write("../x", Kind::Image, b"x").is_err());
        assert!(files.write(ID, Kind::Link, b"x").is_err());
        assert!(files
            .write("3f2b8c4e-9a1d-4b6e-8f00-000000000001", Kind::Image, b"")
            .is_err());

        let foreign = root.path().join("shares").join("keep");
        std::fs::create_dir(&foreign).unwrap();
        files.remove_orphans(SystemTime::now()).unwrap();
        assert!(path.exists(), "a fresh export stays");
        files
            .remove_orphans(SystemTime::now() + ORPHAN_LIFETIME + Duration::from_secs(1))
            .unwrap();
        assert!(!path.exists(), "a day-old export goes");
        assert!(foreign.exists(), "a directory it did not make stays");
        files.remove(ID).unwrap();
        files.remove("../keep").unwrap();
        assert!(foreign.exists());
    }

    #[test]
    fn shares_only_https_links_or_this_runtime_s_own() {
        let origin = url::Url::parse("http://127.0.0.1:5123/").unwrap();
        assert!(valid_link("https://avibe.bot/p/abc/", &origin).is_some());
        assert!(valid_link("http://127.0.0.1:5123/show/ses-1/", &origin).is_some());
        for bad in [
            "http://example.com/show/",
            "http://127.0.0.1:9999/show/",
            "https://user:pw@avibe.bot/p/",
            "file:///etc/passwd",
            "javascript:alert(1)",
        ] {
            assert!(valid_link(bad, &origin).is_none(), "{bad}");
        }
        assert!(valid_link(&format!("https://avibe.bot/{}", "a".repeat(MAX_LINK_BYTES)), &origin).is_none());
    }

    /// The operation lifecycle against a scripted window (lifecycle-class-design.md §5).
    /// Each test moves the window, its Runtime or the clock exactly between two
    /// boundaries of a real `Operation`, and reads the answer, the menus and the disk.
    mod lifecycle {
        use std::sync::atomic::{AtomicUsize, Ordering};

        use super::*;

        const ORIGIN: &str = "http://127.0.0.1:5123/";
        const ROUTE: &str = "/apps/show/ses-1";

        type Hook = Box<dyn Fn(&Arc<Shell>, &Arc<Fake>, u64) + Send + Sync>;
        type Reporter = Box<dyn FnOnce(Status, Option<&'static str>) + Send>;
        type Owner = (Arc<Shell>, std::sync::Weak<Fake>, u64);

        #[derive(Default)]
        struct Hooks {
            read: Option<Hook>,
            write: Option<Hook>,
            /// After a successful write, before it returns.
            written: Option<Hook>,
            present: Option<Hook>,
            /// While the system shows the menu: after the gate admitted it,
            /// before it is registered.
            shown: Option<Hook>,
            /// After the main thread's gate has decided, before the result returns.
            presented: Option<Hook>,
            /// After the nth facts read, counting from one, before it returns.
            facts: Option<(usize, Hook)>,
        }

        struct World {
            path: String,
            receipt: bool,
            registered: bool,
        }

        struct Fake {
            world: Mutex<World>,
            hooks: Hooks,
            fail_write: bool,
            fail_present: bool,
            facts_read: AtomicUsize,
            writes: AtomicUsize,
            /// Every menu shown, by token.
            menus: Mutex<Vec<u64>>,
            /// The menus registered and not yet closed, as in the shell.
            open: Mutex<Menus<u64>>,
            /// Every menu closed, by token.
            closed: Mutex<Vec<u64>>,
            report: Mutex<Option<Reporter>>,
            this: Mutex<Option<Owner>>,
        }

        impl Fake {
            fn new(hooks: Hooks) -> Arc<Self> {
                Arc::new(Self {
                    world: Mutex::new(World {
                        path: ROUTE.into(),
                        receipt: true,
                        registered: true,
                    }),
                    hooks,
                    fail_write: false,
                    fail_present: false,
                    facts_read: AtomicUsize::new(0),
                    writes: AtomicUsize::new(0),
                    menus: Mutex::default(),
                    open: Mutex::default(),
                    closed: Mutex::default(),
                    report: Mutex::default(),
                    this: Mutex::default(),
                })
            }
            fn fire(&self, hook: &Option<Hook>) {
                if let Some(hook) = hook {
                    let (shell, fake, token) = self.this.lock().unwrap().clone().unwrap();
                    hook(&shell, &fake.upgrade().unwrap(), token);
                }
            }
            fn move_to(&self, path: &str) {
                self.world.lock().unwrap().path = path.into();
            }
        }

        #[async_trait::async_trait]
        impl Effects for Fake {
            async fn facts(&self) -> Facts {
                let world = self.world.lock().unwrap();
                let facts = Facts {
                    registered: world.registered,
                    receipt: world.receipt,
                    url: url::Url::parse(ORIGIN).unwrap().join(&world.path).ok(),
                };
                drop(world);
                let count = self.facts_read.fetch_add(1, Ordering::SeqCst) + 1;
                if let Some((at, hook)) = &self.hooks.facts {
                    if *at == count {
                        let (shell, fake, token) = self.this.lock().unwrap().clone().unwrap();
                        hook(&shell, &fake.upgrade().unwrap(), token);
                    }
                }
                facts
            }
            async fn read(&self, _session: &str) -> Option<Value> {
                self.fire(&self.hooks.read);
                Some(json!({ "allowed": true, "url": format!("{ORIGIN}show/ses-1/"), "visible": true }))
            }
            async fn capture(&self, _kind: Kind) -> Option<Vec<u8>> {
                Some(b"pixels".to_vec())
            }
            async fn write(&self, files: &Files, id: &str, kind: Kind, bytes: Vec<u8>) -> std::io::Result<PathBuf> {
                self.writes.fetch_add(1, Ordering::SeqCst);
                self.fire(&self.hooks.write);
                if self.fail_write {
                    // The disk fills after the file exists.
                    let directory = files.root.join(id);
                    std::fs::create_dir_all(&directory)?;
                    std::fs::write(directory.join("Show.png"), b"part")?;
                    return Err(std::io::Error::other("disk full"));
                }
                let path = files.write(id, kind, &bytes)?;
                self.fire(&self.hooks.written);
                Ok(path)
            }
            async fn present(
                &self,
                _item: Shared,
                token: u64,
                gate: Gate,
                report: Box<dyn FnOnce(Status, Option<&'static str>) + Send>,
            ) -> Presented {
                self.fire(&self.hooks.present);
                // The main thread reads the facts and creates the menu at once.
                let world = self.world.lock().unwrap();
                let facts = Facts {
                    registered: world.registered,
                    receipt: world.receipt,
                    url: url::Url::parse(ORIGIN).unwrap().join(&world.path).ok(),
                };
                drop(world);
                let presented = match gate.admit(&facts) {
                    Currency::Ended => Presented::Ended,
                    Currency::Moved => Presented::Moved,
                    Currency::Live if self.fail_present => Presented::Failed,
                    Currency::Live => {
                        self.menus.lock().unwrap().push(token);
                        *self.report.lock().unwrap() = Some(report);
                        self.fire(&self.hooks.shown);
                        let (presented, stray) = self.open.lock().unwrap().register(token, token, || gate.holds());
                        if let Some(menu) = stray {
                            self.closed.lock().unwrap().push(menu);
                        }
                        presented
                    }
                };
                self.fire(&self.hooks.presented);
                presented
            }
            fn close(&self, token: u64) {
                let menu = self.open.lock().unwrap().close(token);
                if let Some(menu) = menu {
                    self.closed.lock().unwrap().push(menu);
                }
            }
        }

        struct Run {
            fake: Arc<Fake>,
            token: u64,
            answer: oneshot::Receiver<Value>,
            root: tempfile::TempDir,
        }

        fn start(kind: Kind, route: Option<&str>, fake: Arc<Fake>) -> Run {
            let root = tempfile::tempdir().unwrap();
            let shell = Arc::new(Shell::new(Files::new(root.path().join("shares"))));
            let request = request(kind);
            let Admission::Start(token, answer) =
                shell.coordinator.lock().unwrap().admit(&request, "w", Instant::now())
            else {
                panic!("start")
            };
            *fake.this.lock().unwrap() = Some((shell.clone(), Arc::downgrade(&fake), token));
            let binding = Binding {
                origin: url::Url::parse(ORIGIN).unwrap(),
                route: route.map(str::to_owned),
            };
            let effects: Arc<dyn Effects> = fake.clone();
            Operation {
                request,
                token,
                binding,
            }
            .start(shell.clone(), effects);
            Run {
                fake,
                token,
                answer,
                root,
            }
        }

        /// The operation's answer, or `None` if it has not ended within two seconds.
        async fn status(run: &mut Run) -> Option<String> {
            tokio::time::timeout(Duration::from_secs(2), &mut run.answer)
                .await
                .ok()
                .and_then(Result::ok)
                .map(|answer| answer["status"].as_str().unwrap_or_default().to_owned())
        }

        fn expire(shell: &Arc<Shell>, fake: &Arc<Fake>, token: u64) {
            let effects: Arc<dyn Effects> = fake.clone();
            shell.end(&effects, token, Currency::Live, End::Expired);
        }

        fn residue(run: &Run) -> bool {
            run.root.path().join("shares").join(ID).exists()
        }

        // Review 45533723, third P2: leaving the registered route by pushState during
        // preparation is a cancellation, not a failure.
        #[tokio::test]
        async fn leaving_the_show_route_during_preparation_cancels() {
            let fake = Fake::new(Hooks {
                read: Some(Box::new(|_, fake, _| fake.move_to("/chat/ses-2"))),
                ..Hooks::default()
            });
            let mut run = start(Kind::Link, Some(ROUTE), fake);
            assert_eq!(status(&mut run).await.as_deref(), Some("cancelled"));
            assert!(run.fake.menus.lock().unwrap().is_empty());
        }

        // Review 45533723, third P2: leaving the route while the menu is open ends the
        // share as cancelled and closes the menu.
        #[tokio::test]
        async fn leaving_the_show_route_while_the_menu_is_open_cancels_and_closes_it() {
            let fake = Fake::new(Hooks::default());
            let mut run = start(Kind::Link, Some(ROUTE), fake);
            for _ in 0..40 {
                if !run.fake.menus.lock().unwrap().is_empty() {
                    break;
                }
                tokio::time::sleep(Duration::from_millis(25)).await;
            }
            assert_eq!(*run.fake.menus.lock().unwrap(), vec![run.token]);
            run.fake.move_to("/chat/ses-2");
            assert_eq!(status(&mut run).await.as_deref(), Some("cancelled"));
            assert_eq!(*run.fake.closed.lock().unwrap(), vec![run.token]);
        }

        // Review 45533723, second P2: a Runtime that lost authority just before the menu
        // is created gets no menu, and the share is cancelled.
        #[tokio::test]
        async fn a_runtime_lost_before_the_menu_is_created_gets_no_menu() {
            let fake = Fake::new(Hooks {
                present: Some(Box::new(|_, fake, _| fake.world.lock().unwrap().receipt = false)),
                ..Hooks::default()
            });
            let mut run = start(Kind::Link, None, fake);
            assert_eq!(status(&mut run).await.as_deref(), Some("cancelled"));
            assert!(run.fake.menus.lock().unwrap().is_empty(), "no menu for a lost Runtime");
        }

        // Review 45533723, first P2: an operation that expires while its export is being
        // written, whose write then fails after creating the file, leaves nothing.
        #[tokio::test]
        async fn an_expired_operation_s_failed_write_leaves_nothing() {
            let fake = Arc::new(Fake {
                fail_write: true,
                ..Arc::into_inner(Fake::new(Hooks {
                    write: Some(Box::new(expire)),
                    ..Hooks::default()
                }))
                .unwrap()
            });
            let mut run = start(Kind::Image, Some(ROUTE), fake);
            assert_eq!(status(&mut run).await.as_deref(), Some("expired"));
            tokio::time::sleep(Duration::from_millis(200)).await;
            assert!(!residue(&run), "the partial export must not stay behind");
        }

        // An operation that expires while its export is written, after its cleanup has
        // already run, leaves nothing either, also when the first removal fails.
        // Unix only: the failed removal is a read-only directory.
        #[cfg(unix)]
        #[tokio::test]
        async fn a_late_export_is_removed_and_a_failed_removal_retried() {
            use std::os::unix::fs::PermissionsExt;
            let fake = Fake::new(Hooks {
                // Expiry and its cleanup, before the file exists.
                write: Some(Box::new(expire)),
                // The first removal of the file fails: the share directory is
                // read-only for a moment.
                written: Some(Box::new(|shell, _, _| {
                    let root = shell.files.root.clone();
                    std::fs::set_permissions(&root, std::fs::Permissions::from_mode(0o500)).unwrap();
                    std::thread::spawn(move || {
                        std::thread::sleep(Duration::from_millis(150));
                        std::fs::set_permissions(&root, std::fs::Permissions::from_mode(0o700)).unwrap();
                    });
                })),
                ..Hooks::default()
            });
            let mut run = start(Kind::Image, Some(ROUTE), fake);
            assert_eq!(status(&mut run).await.as_deref(), Some("expired"));
            tokio::time::sleep(Duration::from_millis(1000)).await;
            assert!(!residue(&run), "the export must be removed once removal can succeed");
        }

        // The other way round: the Workbench is bound to its document and Runtime, not to
        // a route, so a link share survives the Workbench changing chats.
        #[tokio::test]
        async fn a_workbench_route_change_does_not_cancel_a_link_share() {
            let fake = Fake::new(Hooks {
                read: Some(Box::new(|_, fake, _| fake.move_to("/chat/ses-2"))),
                ..Hooks::default()
            });
            let mut run = start(Kind::Link, None, fake);
            for _ in 0..40 {
                if !run.fake.menus.lock().unwrap().is_empty() {
                    break;
                }
                tokio::time::sleep(Duration::from_millis(25)).await;
            }
            assert_eq!(*run.fake.menus.lock().unwrap(), vec![run.token], "the menu shows");
            run.fake.move_to("/chat/ses-3");
            tokio::time::sleep(Duration::from_millis(400)).await;
            let report = run.fake.report.lock().unwrap().take().expect("menu report");
            report(Status::Completed, None);
            assert_eq!(status(&mut run).await.as_deref(), Some("completed"));
        }

        async fn menu_shown(run: &Run) {
            for _ in 0..40 {
                if !run.fake.menus.lock().unwrap().is_empty() {
                    return;
                }
                tokio::time::sleep(Duration::from_millis(25)).await;
            }
            panic!("no menu");
        }

        fn service(run: &Run, status: Status, error: Option<&'static str>) {
            let report = run.fake.report.lock().unwrap().take().expect("menu report");
            report(status, error);
        }

        // Review 15bb7a7b, first P2: a service that reports after the window left its
        // Show (or its Runtime) does not make the share completed; it is cancelled.
        #[tokio::test]
        async fn a_service_reporting_after_the_window_moved_on_is_cancelled() {
            for leave in [
                (|fake: &Fake| fake.move_to("/chat/ses-2")) as fn(&Fake),
                |fake: &Fake| fake.world.lock().unwrap().receipt = false,
            ] {
                let mut run = start(Kind::Link, Some(ROUTE), Fake::new(Hooks::default()));
                menu_shown(&run).await;
                leave(&run.fake);
                service(&run, Status::Completed, None);
                assert_eq!(status(&mut run).await.as_deref(), Some("cancelled"));
            }
        }

        // Review 15bb7a7b, second P2: a departure seen during preparation stays a
        // cancellation, even if the window is back on its route by the time it ends.
        #[tokio::test]
        async fn a_departure_seen_during_preparation_stays_cancelled_after_coming_back() {
            let fake = Fake::new(Hooks {
                read: Some(Box::new(|_, fake, _| fake.move_to("/chat/ses-2"))),
                // Back on the route right after the check that saw it gone.
                facts: Some((1, Box::new(|_, fake, _| fake.move_to(ROUTE)))),
                ..Hooks::default()
            });
            let mut run = start(Kind::Link, Some(ROUTE), fake);
            assert_eq!(status(&mut run).await.as_deref(), Some("cancelled"));
        }

        // Review 15bb7a7b, second P2, at the menu: the gate saw the window gone; coming
        // back before the share ends does not turn that into a failure.
        #[tokio::test]
        async fn a_departure_seen_by_the_menu_gate_stays_cancelled_after_coming_back() {
            let fake = Fake::new(Hooks {
                present: Some(Box::new(|_, fake, _| fake.move_to("/chat/ses-2"))),
                presented: Some(Box::new(|_, fake, _| fake.move_to(ROUTE))),
                ..Hooks::default()
            });
            let mut run = start(Kind::Link, Some(ROUTE), fake);
            assert_eq!(status(&mut run).await.as_deref(), Some("cancelled"));
            assert!(run.fake.menus.lock().unwrap().is_empty());
        }

        async fn closed(run: &Run) -> Vec<u64> {
            for _ in 0..40 {
                if !run.fake.closed.lock().unwrap().is_empty() {
                    break;
                }
                tokio::time::sleep(Duration::from_millis(25)).await;
            }
            run.fake.closed.lock().unwrap().clone()
        }

        // toctou-inventory N2/N3: the operation ends while the system is showing its
        // menu; its close runs before the menu is registered and finds nothing. The
        // registration finds the operation ended and closes the menu itself.
        #[tokio::test]
        async fn a_menu_shown_for_an_operation_that_ended_meanwhile_is_closed() {
            let fake = Fake::new(Hooks {
                shown: Some(Box::new(expire)),
                ..Hooks::default()
            });
            let mut run = start(Kind::Link, Some(ROUTE), fake);
            assert_eq!(status(&mut run).await.as_deref(), Some("expired"));
            assert_eq!(*run.fake.menus.lock().unwrap(), vec![run.token]);
            assert_eq!(closed(&run).await, vec![run.token], "closed by its registration");
            assert_eq!(run.fake.open.lock().unwrap().close(run.token), None);
        }

        // The same with a departure the watcher recorded but has not ended yet: the
        // registration closes the menu, and the share is cancelled.
        #[tokio::test]
        async fn a_menu_shown_after_its_window_moved_on_is_closed_and_cancelled() {
            let fake = Fake::new(Hooks {
                shown: Some(Box::new(|shell, fake, token| {
                    fake.move_to("/chat/ses-2");
                    shell.coordinator.lock().unwrap().observe(token, Currency::Moved);
                })),
                ..Hooks::default()
            });
            let mut run = start(Kind::Link, Some(ROUTE), fake);
            assert_eq!(status(&mut run).await.as_deref(), Some("cancelled"));
            assert_eq!(closed(&run).await, vec![run.token]);
        }

        // The service reports while its menu is being shown: the operation ends once,
        // as reported, and its menu is closed whichever of the end and the
        // registration comes first.
        #[tokio::test]
        async fn a_report_during_the_show_ends_once_and_closes_the_menu() {
            let fake = Fake::new(Hooks {
                shown: Some(Box::new(|_, fake, _| {
                    let report = fake.report.lock().unwrap().take().expect("menu report");
                    report(Status::Completed, None);
                })),
                ..Hooks::default()
            });
            let mut run = start(Kind::Link, Some(ROUTE), fake);
            assert_eq!(status(&mut run).await.as_deref(), Some("completed"));
            assert_eq!(closed(&run).await, vec![run.token]);
        }

        // Every way an operation can end, with its window still on its Show.
        #[tokio::test]
        async fn a_live_operation_ends_as_its_service_or_its_effect_says() {
            for (reported, expected) in [
                ((Status::Completed, None), "completed"),
                ((Status::Cancelled, None), "cancelled"),
                ((Status::Failed, Some("system_share_failed")), "failed"),
            ] {
                let mut run = start(Kind::Link, Some(ROUTE), Fake::new(Hooks::default()));
                menu_shown(&run).await;
                service(&run, reported.0, reported.1);
                assert_eq!(status(&mut run).await.as_deref(), Some(expected));
            }
            // The menu could not be shown.
            let fake = Arc::new(Fake {
                fail_present: true,
                ..Arc::into_inner(Fake::new(Hooks::default())).unwrap()
            });
            let mut run = start(Kind::Link, Some(ROUTE), fake);
            assert_eq!(status(&mut run).await.as_deref(), Some("failed"));
            // The menu stays open past the operation's lifetime: the watcher expires it.
            let mut run = start(Kind::Link, Some(ROUTE), Fake::new(Hooks::default()));
            menu_shown(&run).await;
            let expired = tokio::time::timeout(Duration::from_secs(3), &mut run.answer)
                .await
                .unwrap()
                .unwrap();
            assert_eq!(expired["status"], "expired");
            assert_eq!(*run.fake.closed.lock().unwrap(), vec![run.token]);
        }
    }
}
