//! The rendezvous request the instance's own top document makes for native
//! code: what is asked, the fixed script that asks it, and how its reply is
//! read. The platform adapters (`webview_macos`, `webview_windows`) run the
//! script in a world page script cannot reach and hand the body to
//! `Client::refresh`. Nothing here opens a socket, and no page can call it.
//!
//! The script is the iPhone App's (`ios/Avibe/DirectRendezvousFetcher.swift`):
//! a same-origin `POST`, so the request carries that WebView's signed-in
//! session and meets the instance's usual Origin and CSRF checks.

/// The instance's rendezvous route (docs/plans/remote-direct-phase1.md).
pub const PATH: &str = "/api/direct/rendezvous";
/// The most bytes of an answer that are read; the contract's own bound.
pub const MAX_ANSWER_BYTES: usize = 16 * 1024;
/// The script gives up after this long. `Client::refresh` allows five
/// seconds for the whole fetch, so the script's own refusal comes first.
pub const DEADLINE_MILLISECONDS: u32 = 4_000;
/// The name of the world the script runs in. Only these adapters use it.
pub const WORLD: &str = "bot.avibe.direct.rendezvous";

/// What `Client::refresh_reply` asks one rendezvous request to carry: this
/// client's endpoint key, and the diagnostic report the instance logs
/// (`leg_faults`, `path_use`: fixed words, counts and durations only).
pub(super) struct Asked {
    pub(super) endpoint: String,
    pub(super) report: Report,
}

/// The diagnostic part of a request. Empty lists are not sent.
#[derive(Default)]
pub(super) struct Report {
    pub(super) leg_faults: Vec<serde_json::Value>,
    pub(super) path_use: Vec<serde_json::Value>,
}

/// One rendezvous request of one top document: its exact origin, the
/// absolute URL (a page can add a `<base>` element, which would move a
/// relative one), and the body.
pub struct Request {
    origin: String,
    url: String,
    body: String,
}

impl Request {
    /// `origin` is the serialized origin the selected instance's top document
    /// must have, `https://host` or `scheme://host:port`. `client_endpoint_id`
    /// is what `Client::refresh` passes. `None` for anything else, so nothing
    /// a caller did not mean is ever written into a script or a request.
    pub fn new(origin: &str, client_endpoint_id: &str) -> Option<Self> {
        Self::reporting(origin, client_endpoint_id, &Report::default())
    }

    /// The request the core asked for, with its report. The report is the
    /// core's own (`Client::refresh_reply`): each entry has exactly the keys
    /// the instance requires, and one it does not accept makes it ignore
    /// that list whole. An instance that does not know a list ignores it.
    pub(super) fn asked(origin: &str, asked: &Asked) -> Option<Self> {
        Self::reporting(origin, &asked.endpoint, &asked.report)
    }

    fn reporting(origin: &str, client_endpoint_id: &str, report: &Report) -> Option<Self> {
        if origin_of(origin).as_deref() != Some(origin) {
            return None;
        }
        let id_ok = !client_endpoint_id.is_empty()
            && client_endpoint_id.len() <= 128
            && client_endpoint_id
                .bytes()
                .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit());
        if !id_ok {
            return None;
        }
        let mut body = serde_json::json!({ "version": 1, "client_endpoint_id": client_endpoint_id });
        for (name, entries) in [("leg_faults", &report.leg_faults), ("path_use", &report.path_use)] {
            if !entries.is_empty() {
                body[name] = serde_json::Value::Array(entries.clone());
            }
        }
        Some(Self {
            origin: origin.to_owned(),
            url: format!("{origin}{PATH}"),
            body: body.to_string(),
        })
    }

    pub fn origin(&self) -> &str {
        &self.origin
    }

    /// The script's arguments, by name: all strings.
    pub fn arguments(&self) -> [(&'static str, &str); 3] {
        [("url", &self.url), ("origin", &self.origin), ("body", &self.body)]
    }

    /// The same call as one expression, for an engine that evaluates
    /// expressions rather than function bodies. The arguments are JSON
    /// string literals, so nothing in them can end the expression.
    pub fn expression(&self) -> String {
        let literal = |value: &str| serde_json::Value::from(value).to_string();
        format!(
            "(async (url, origin, body) => {{\n{}\n}})({}, {}, {})",
            script(),
            literal(&self.url),
            literal(&self.origin),
            literal(&self.body),
        )
    }
}

/// The serialized origin of an absolute `http` or `https` URL, lowercase
/// host, default port left out; `None` for anything else, a URL with user
/// information included. A document is the instance's only if this equals
/// the request's origin exactly.
pub fn origin_of(url: &str) -> Option<String> {
    let (scheme, rest) = url.split_once("://")?;
    let default_port = match scheme {
        "https" => 443,
        "http" => 80,
        _ => return None,
    };
    let authority = rest.split(['/', '?', '#']).next()?;
    let (host, port) = match authority.rsplit_once(':') {
        Some((host, port)) => (host, Some(port)),
        None => (authority, None),
    };
    let host_ok = !host.is_empty()
        && host.len() <= 253
        && host
            .bytes()
            .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'.' || byte == b'-');
    if !host_ok {
        return None;
    }
    let port = match port {
        None => None,
        Some(text) => {
            if text.is_empty() || text.len() > 5 || !text.bytes().all(|byte| byte.is_ascii_digit()) {
                return None;
            }
            let port: u16 = text.parse().ok()?;
            if port == 0 {
                return None;
            }
            (port != default_port).then_some(port)
        }
    };
    Some(match port {
        Some(port) => format!("{scheme}://{host}:{port}"),
        None => format!("{scheme}://{host}"),
    })
}

/// How a request ended. `Ready` carries the answer's bytes exactly as the
/// instance sent them with `200`; the core validates them. Every other
/// outcome leaves the tunnel in use. Families only: nothing here names an
/// address, a cookie, or an identity.
#[derive(Debug, PartialEq, Eq)]
pub enum Reply {
    Ready(String),
    /// The document is not the instance's, or has no CSRF cookie yet.
    NotReady,
    /// Native ownership was lost; never retain a lease across selections.
    /// The selection this request was started for is no longer the one on
    /// show: another session, account or instance view. Never a page that is
    /// only loading or reloading in the same selection; that is `NotReady`.
    SelectionChanged,
    /// The instance answered, and not with `200`.
    Refused(u16),
    /// A service worker controls the document, or took control meanwhile.
    Controlled,
    /// The network, the deadline, a redirect, an answer that is too large or
    /// not text, or a reply that is not the script's.
    Failed,
}

impl Reply {
    /// What an adapter delivers for a request whose selection is, or is no
    /// longer, the one on show. Every completion of an adapter passes
    /// through this, so no outcome can outlive the selection it was for.
    pub fn for_selection(self, current: bool) -> Self {
        if current {
            self
        } else {
            Reply::SelectionChanged
        }
    }

    /// What `Client::refresh` takes.
    pub fn body(self) -> Option<String> {
        match self {
            Reply::Ready(body) => Some(body),
            _ => None,
        }
    }

    /// Reads the script's reply, a JSON string, or its absence.
    pub fn read(reply: Option<&str>) -> Self {
        let Some(value) = reply.and_then(|text| serde_json::from_str::<serde_json::Value>(text).ok()) else {
            return Reply::Failed;
        };
        if value.get("ok").and_then(serde_json::Value::as_bool) != Some(true) {
            return match value.get("failure").and_then(serde_json::Value::as_str) {
                Some("origin" | "csrf") => Reply::NotReady,
                Some("controlled") => Reply::Controlled,
                _ => Reply::Failed,
            };
        }
        let status = value.get("status").and_then(serde_json::Value::as_u64);
        let body = value.get("body").and_then(serde_json::Value::as_str);
        match (status, body) {
            (Some(200), Some(body)) if body.len() <= MAX_ANSWER_BYTES => Reply::Ready(body.to_owned()),
            (Some(200), _) => Reply::Failed,
            (Some(status), Some(_)) => u16::try_from(status).map_or(Reply::Failed, Reply::Refused),
            _ => Reply::Failed,
        }
    }
}

/// The body of an async function of `url`, `origin` and `body`. It answers a
/// JSON string: `{ok: true, status, body}` or `{ok: false, failure}`.
///
/// A separate world does not keep a service worker out: a worker that
/// controls the document answers its requests whichever world made them. So
/// the script refuses for the whole request: it listens for a change of
/// controller before it looks, and looks again before the request, after
/// each await, and before it answers.
pub fn script() -> String {
    SCRIPT
        .replace("DEADLINE_MILLISECONDS", &DEADLINE_MILLISECONDS.to_string())
        .replace("MAX_ANSWER_BYTES", &MAX_ANSWER_BYTES.to_string())
}

const SCRIPT: &str = r#"const fail = (failure) => JSON.stringify({ok: false, failure});
if (window.location.origin !== origin) return fail("origin");
const workers = navigator.serviceWorker;
let taken = false;
const onControllerChange = () => {
  taken = true;
};
if (workers) workers.addEventListener("controllerchange", onControllerChange);
const controlled = () => taken || Boolean(workers && workers.controller);
const controller = new AbortController();
let timedOut = false;
let timer = null;
try {
  if (controlled()) return fail("controlled");
  const cookiePrefix = "vibe_csrf_token=";
  let token = null;
  for (const part of document.cookie.split(";")) {
    const trimmed = part.trim();
    if (trimmed.startsWith(cookiePrefix)) {
      try {
        token = decodeURIComponent(trimmed.slice(cookiePrefix.length));
      } catch {
        token = null;
      }
      break;
    }
  }
  if (!token) return fail("csrf");
  timer = window.setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, DEADLINE_MILLISECONDS);
  const response = await fetch(url, {
    method: "POST",
    credentials: "same-origin",
    cache: "no-store",
    redirect: "error",
    headers: {
      "Content-Type": "application/json",
      "X-Vibe-CSRF-Token": token
    },
    body,
    signal: controller.signal
  });
  if (controlled()) return fail("controlled");
  if (response.redirected || response.url !== url || response.type !== "basic") {
    return fail("redirect");
  }
  let text = "";
  if (response.status === 200 && response.body) {
    const reader = response.body.getReader();
    const decoder = new TextDecoder("utf-8", {fatal: true});
    let total = 0;
    for (;;) {
      const {done, value} = await reader.read();
      if (controlled()) return fail("controlled");
      if (done) break;
      total += value.byteLength;
      if (total > MAX_ANSWER_BYTES) return fail("oversize");
      text += decoder.decode(value, {stream: true});
    }
    text += decoder.decode();
  }
  if (controlled()) return fail("controlled");
  return JSON.stringify({ok: true, status: response.status, body: text});
} catch (error) {
  return fail(controlled() ? "controlled" : timedOut ? "timeout" : "network");
} finally {
  if (timer !== null) window.clearTimeout(timer);
  if (workers) workers.removeEventListener("controllerchange", onControllerChange);
  controller.abort();
}"#;
