//! The page's `direct` capability in a window of another computer's instance
//! (`docs/plans/ios-app.md` section 4, as the phone App answers it). Words
//! and numbers only: `pages.rs` puts them into real windows.
//!
//! The page learns its window's state by delivery: the shell evaluates a
//! script in the window's document on every load and every change, and the
//! injected bridge (`page_bridge.js`) answers `direct.status()` from what it
//! was last given. Only a request the shell has to act on comes the other
//! way, as a link to `avibe-app://direct` that the shell refuses to follow:
//! the way an app launch is asked for. No command or message handler is
//! given to the instance's origin.
//!
//! What crosses is the path's kind, two counters and the choice of path. No
//! address, peer, relay, host, or anything of the listener's.
use super::{choices::Mode, AddressFamily, HandoverResult, PathStatus, PreferredPath, SelectedPath};
use serde::Deserialize;
use serde_json::{json, Value};
use url::Url;

const PAGE_BRIDGE: &str = include_str!("page_bridge.js");
/// The longest request the shell reads.
const MAX_REQUEST: usize = 1024;

/// The script a window of another computer's instance starts every document
/// with. `token` is that window's alone. `asks_before_leaving`: this
/// system's WebView puts a page's leave question before the shell can
/// refuse a link (WebView2), so the script keeps its own request's from the
/// page; where it does not (WebKit), the script keeps nothing from it.
pub fn script(token: &str, version: &str, asks_before_leaving: bool) -> String {
    format!(
        "({PAGE_BRIDGE})({},{},{});",
        json!(token),
        json!(version),
        asks_before_leaving
    )
}

/// A window's state as its page counts it. The core counts per client, and
/// a window outlives clients: `sequence` and `closure` here only ever grow.
#[derive(Default)]
pub struct Ledger {
    preferred: PreferredPath,
    sequence: u64,
    closure: u64,
    /// The session whose route the window had at the last look, and that
    /// client's own closure count then.
    routed: Option<(u64, u64)>,
}

impl Ledger {
    /// Takes the window's state now: the session whose route is on it, with
    /// that client's status, or `None` for a window on the tunnel alone.
    /// `true` when the page has something new to hear.
    ///
    /// A window that has no route prefers the tunnel whatever a client says.
    /// Its connections are closed for it when its client says so, and when
    /// its route goes: the listener they ran through is gone.
    pub fn observe(&mut self, routed: Option<(u64, PathStatus)>) -> bool {
        let (preferred, lost) = match routed {
            Some((session, status)) => {
                let lost = match self.routed {
                    Some((before, closure)) if before == session => status.closure.saturating_sub(closure),
                    Some(_) => 1,
                    None => 0,
                };
                self.routed = Some((session, status.closure));
                (status.preferred, lost)
            }
            None => (PreferredPath::Tunnel, u64::from(self.routed.take().is_some())),
        };
        if preferred == self.preferred && lost == 0 {
            return false;
        }
        self.preferred = preferred;
        self.sequence += 1;
        self.closure += lost;
        true
    }

    /// `direct.status()`: the event's fields, and the class of an address
    /// path. The core measures no round trip and keeps no race record, so
    /// neither is given.
    pub fn status(&self) -> Value {
        let (preferred, path, scope) = match self.preferred {
            PreferredPath::Tunnel => ("tunnel", None, None),
            PreferredPath::Direct(SelectedPath::Relay) => ("direct", Some("relay"), None),
            PreferredPath::Direct(SelectedPath::Lan(family)) => ("direct", Some(word(family)), Some("local")),
            PreferredPath::Direct(SelectedPath::Punched(family)) => ("direct", Some(word(family)), Some("global")),
        };
        let mut status = json!({
            "preferred": preferred,
            "sequence": self.sequence,
            "closure": self.closure,
            "path": path,
        });
        if let Some(scope) = scope {
            status["path_scope"] = json!(scope);
        }
        status
    }
}

fn word(family: AddressFamily) -> &'static str {
    match family {
        AddressFamily::Ipv4 => "ipv4",
        AddressFamily::Ipv6 => "ipv6",
    }
}

/// `direct.preference()`. The desktop app never sets a choice back itself.
pub fn preference(mode: Mode) -> Value {
    json!({
        "mode": match mode {
            Mode::Automatic => "automatic",
            Mode::Tunnel => "tunnel",
        },
        "reverted": null,
    })
}

/// `direct.handover()`, in the phone App's words.
pub fn handover(result: &HandoverResult) -> Value {
    match result {
        HandoverResult::Done { closed, kept } => json!({ "closed": closed, "kept": kept }),
        HandoverResult::NotUsable => json!({ "closed": 0, "kept": 0, "skipped": "not_usable" }),
        HandoverResult::OtherDocuments => json!({ "closed": 0, "kept": 0, "skipped": "other_documents" }),
    }
}

#[derive(Debug, PartialEq, Eq)]
pub enum Method {
    Handover,
    Preference,
    SetPreference(Mode),
}

/// What a page asked, once it has shown its window's token.
#[derive(Debug, PartialEq, Eq)]
pub struct Request {
    pub id: String,
    pub method: Method,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Wire {
    token: String,
    id: String,
    method: String,
    #[serde(default)]
    params: Params,
}

#[derive(Default, Deserialize)]
#[serde(deny_unknown_fields)]
struct Params {
    mode: Option<Mode>,
}

/// `avibe-app://direct?request=…`, whoever sent it.
pub fn is_request(url: &Url) -> bool {
    url.scheme() == "avibe-app" && url.host_str() == Some("direct")
}

impl Request {
    /// Reads a request that carries `token`. Anything else is nothing: a
    /// frame inside the page can follow a link too, and does not know it.
    pub fn parse(url: &Url, token: &str) -> Option<Self> {
        if !is_request(url) || !url.path().is_empty() || url.fragment().is_some() {
            return None;
        }
        let pairs: Vec<_> = url.query_pairs().collect();
        if pairs.len() != 1 || pairs[0].0 != "request" || pairs[0].1.len() > MAX_REQUEST {
            return None;
        }
        let wire: Wire = serde_json::from_str(&pairs[0].1).ok()?;
        if !same(wire.token.as_bytes(), token.as_bytes()) || !id(&wire.id) {
            return None;
        }
        let method = match (wire.method.as_str(), wire.params.mode) {
            ("direct.handover", None) => Method::Handover,
            ("direct.preference", None) => Method::Preference,
            ("direct.setPreference", Some(mode)) => Method::SetPreference(mode),
            _ => return None,
        };
        Some(Self { id: wire.id, method })
    }
}

fn id(value: &str) -> bool {
    (1..=64).contains(&value.len()) && value.bytes().all(|byte| byte.is_ascii_alphanumeric() || byte == b'-')
}

/// Compares every byte, however early two tokens differ.
fn same(left: &[u8], right: &[u8]) -> bool {
    left.len() == right.len() && left.iter().zip(right).fold(0, |differs, (a, b)| differs | (a ^ b)) == 0
}

/// What is delivered to a window's document.
pub enum Message {
    /// The window's state, on every load and every change.
    State { status: Value, preference: Value },
    /// The answer to one request, and the choice of path when it asked
    /// about that.
    Reply {
        id: String,
        result: Value,
        preference: Option<Value>,
    },
}

/// The script that delivers `message` to the document of `origin` in the
/// window's top frame, and to no other document: a navigation can commit
/// between the shell's look at the window and this script's run. A state
/// that arrives before the bridge is kept for it; an answer has no one to
/// wait for.
pub fn delivery(origin: &str, message: &Message) -> String {
    let (body, early) = match message {
        Message::State { status, preference } => (json!({ "status": status, "preference": preference }), true),
        Message::Reply { id, result, preference } => {
            let mut body = json!({ "reply": { "id": id, "result": result } });
            if let Some(preference) = preference {
                body["preference"] = preference.clone();
            }
            (body, false)
        }
    };
    let keep = if early {
        "else window.__AVIBE_DIRECT_EARLY__=m;"
    } else {
        ""
    };
    format!(
        "(()=>{{if(window.self!==window.top||window.location.origin!=={})return;const m={};const d=window.__AVIBE_DIRECT_DELIVER__;if(typeof d==='function')d(m);{}}})();",
        json!(origin),
        body,
        keep
    )
}
