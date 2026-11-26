//! The selected instance's update state, as the shell's trusted pages read it.
//!
//! Everything here is display data rebuilt from the instance's released
//! `/api/version` answer: fields are allow-listed, versions must parse as
//! versions, phases are enumerated and errors are codes. Nothing the instance
//! writes as free text (an error description, a package URL, installer output)
//! reaches the bridge. A field this module does not know is dropped.
use super::protocol::ClientError;
use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use serde_json::{json, Map, Value};

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
pub struct InstanceUpdate {
    pub instance_id: String,
    pub status: Status,
    pub snapshot: Option<Value>,
    pub can_manage: bool,
    pub request: Option<Receipt>,
    pub fetched_at: Option<String>,
    /// A read that started after `request` settled has been published: the
    /// snapshot can show that request's effect. Native only.
    #[serde(skip)]
    pub(crate) request_seen: bool,
    /// For an install: a desktop-managed host's request id. Native only.
    #[serde(skip)]
    pub(crate) install_ref: Option<String>,
    /// The snapshot a standalone Runtime answered the user's own check with.
    /// Native only.
    #[serde(skip)]
    pub(crate) checked: Option<Value>,
}
#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum Status {
    Loading,
    Ready,
    Unreachable,
    Unauthorized,
}
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
pub struct Receipt {
    pub action: Action,
    pub phase: Phase,
    pub target: Option<String>,
    pub error_code: Option<String>,
    pub updated_at: String,
    /// The shell refuses another action for this instance now because of this
    /// request (`update_in_progress`): it is on its way, or being followed.
    /// Recomputed at every publication, and published when time alone ends it.
    pub following: bool,
    /// An install only: the version the instance ran when it was sent (a
    /// validated version, from the snapshot); null otherwise or when unknown.
    pub installed_from: Option<String>,
}
#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum Action {
    Install,
    Check,
    Prereleases,
    AutoUpdate,
}
#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum Phase {
    Sending,
    Accepted,
    Failed,
}

/// How long the shell keeps following an install it sent before it stops
/// rereading on its own. A standalone install takes about a minute; a
/// desktop-managed host's request expires after 30 minutes.
const FOLLOW_INSTALL: chrono::Duration = chrono::Duration::minutes(30);
/// A standalone Runtime that still answers on the version it ran when it
/// accepted an install, without an install of its own in progress, this long
/// after accepting, did not restart into the new one (its restart could not be
/// scheduled, or activation was deferred). The shell stops following it.
const STANDALONE_RESTART: chrono::Duration = chrono::Duration::minutes(5);

impl InstanceUpdate {
    pub fn loading(instance_id: String) -> Self {
        Self {
            instance_id,
            status: Status::Loading,
            snapshot: None,
            can_manage: false,
            request: None,
            fetched_at: None,
            request_seen: false,
            install_ref: None,
            checked: None,
        }
    }

    /// Whether something this shell sent has not settled, so the shell keeps
    /// rereading without the popup open. Bounded: it stops on its own.
    pub fn unsettled(&self, now: DateTime<Utc>) -> bool {
        let Some(request) = &self.request else {
            return false;
        };
        let Ok(at) = DateTime::parse_from_rfc3339(&request.updated_at) else {
            return false;
        };
        if now.signed_duration_since(at) >= FOLLOW_INSTALL || self.status == Status::Unauthorized {
            return false;
        }
        let seen = self.request_seen;
        match (request.phase, request.action) {
            (Phase::Sending, _) => true,
            (Phase::Failed, _) => false,
            // An accepted install is followed until the instance reports another
            // version (a standalone Runtime answers before its scheduled
            // restart, still on the old one) or its own request ends.
            (Phase::Accepted, Action::Install) => !self.install_ended(at.with_timezone(&Utc), now),
            (Phase::Accepted, _) => {
                !seen
                    || self
                        .snapshot
                        .as_ref()
                        .is_some_and(|snapshot| running(snapshot, request.action))
            }
        }
    }

    /// The shell refuses another action for this instance because of its
    /// request: exactly what `send_instance` checks, and what it publishes.
    pub fn following(&self, now: DateTime<Utc>) -> bool {
        self.request.as_ref().is_some_and(|request| {
            request.phase == Phase::Sending || (request.action == Action::Install && self.unsettled(now))
        })
    }

    /// Brings the published `following` up to date. True when it changed.
    pub(crate) fn settle_following(&mut self, now: DateTime<Utc>) -> bool {
        let following = self.following(now);
        match self.request.as_mut() {
            Some(request) if request.following != following => {
                request.following = following;
                true
            }
            _ => false,
        }
    }

    fn install_ended(&self, accepted: DateTime<Utc>, now: DateTime<Utc>) -> bool {
        let Some(snapshot) = self.snapshot.as_ref().filter(|_| self.status == Status::Ready) else {
            return false;
        };
        if running(snapshot, Action::Install) {
            return false;
        }
        let from = self
            .request
            .as_ref()
            .and_then(|request| request.installed_from.as_deref());
        let remote = &snapshot["remote_request"];
        let refused = self.install_ref.is_some()
            && remote["request_id"].as_str() == self.install_ref.as_deref()
            && matches!(remote["phase"].as_str(), Some("failed" | "declined" | "expired"));
        // A standalone Runtime still on the old version after its answer was
        // read, with no install of its own reported, did not take this one.
        let not_taken = snapshot["managed_by"].is_null()
            && self.request_seen
            && now.signed_duration_since(accepted) >= STANDALONE_RESTART;
        refused || not_taken || snapshot["current"].as_str() != from
    }

    /// A receipt left `sending` by a reply that could not be published (its
    /// generation ended while the instance stayed selected) is marked
    /// interrupted, never resent. What was installed is the snapshot's
    /// `current`; no result is inferred here.
    pub(crate) fn reconcile(&mut self, in_flight: bool) {
        if let Some(request) = self.request.as_mut().filter(|request| request.phase == Phase::Sending) {
            if !in_flight {
                request.phase = Phase::Failed;
                request.error_code = Some("interrupted".into());
                request.updated_at = now();
            }
        }
    }
}

/// A standalone Runtime's unprompted version reads leave silent releases out
/// (docs/release.md); the user's own check finds them. That check's offer
/// stands over later reads while the instance runs the same version, until the
/// next check answers. The Workbench's `keepCheckedOffer` does the same.
pub(crate) fn keep_checked_offer(mut read: Value, checked: Option<&Value>) -> Value {
    let Some(checked) = checked.filter(|checked| checked["has_update"] == true && checked["latest"].is_string()) else {
        return read;
    };
    if !read["managed_by"].is_null()
        || read["current"] != checked["current"]
        || (read["has_update"] == true && read["latest"] == checked["latest"])
    {
        return read;
    }
    read["latest"] = checked["latest"].clone();
    read["has_update"] = json!(true);
    read
}

/// The instance reports the accepted action still running.
fn running(snapshot: &Value, action: Action) -> bool {
    match action {
        Action::Install => {
            let remote = &snapshot["remote_request"];
            !snapshot["install"].is_null()
                || (remote.is_object()
                    && !matches!(
                        remote["phase"].as_str(),
                        Some("completed" | "failed" | "declined" | "expired")
                    ))
        }
        _ => snapshot["control_request"]["phase"] == "checking" || snapshot["phase"] == "checking",
    }
}

/// chrono is built without its clock here; read the system's.
pub(crate) fn utc_now() -> DateTime<Utc> {
    DateTime::<Utc>::from(std::time::SystemTime::now())
}
pub(crate) fn now() -> String {
    utc_now().to_rfc3339_opts(chrono::SecondsFormat::Millis, true)
}

/// Error codes the bridge may carry. Anything else the instance answers becomes
/// `request_failed`; its own text never crosses.
const CODES: &[&str] = &[
    "check_failed",
    "confirmation_required",
    "desktop_managed_runtime",
    "desktop_shell_unavailable",
    "download_failed",
    "expired",
    "install_failed",
    "instance_unauthorized",
    "instance_unreachable",
    "interrupted",
    "invalid_request",
    "no_update_available",
    "not_desktop_managed",
    "request_failed",
    "requires_host",
    "target_mismatch",
    "update_check_failed",
    "update_in_progress",
];
pub(crate) fn error_code(value: Option<&str>) -> String {
    value
        .filter(|code| CODES.contains(code))
        .unwrap_or("request_failed")
        .to_owned()
}

fn invalid() -> ClientError {
    ClientError::new("invalid_response")
}

/// SemVer (the desktop app's `3.2.8-rc.4`) or the PEP 440 forms an Avibe
/// Runtime reports (`3.2.8rc4`, `3.2.8.post1`, `3.2.9.dev5+g1a2b3c4`).
pub fn is_version(value: &str) -> bool {
    if value.is_empty() || value.len() > 128 {
        return false;
    }
    if semver::Version::parse(value).is_ok() {
        return true;
    }
    let (public, local) = match value.split_once('+') {
        Some((public, local)) => (public, Some(local)),
        None => (value, None),
    };
    if local.is_some_and(|local| {
        local.is_empty()
            || local
                .split('.')
                .any(|part| part.is_empty() || !part.bytes().all(|b| b.is_ascii_alphanumeric()))
    }) {
        return false;
    }
    pep440_public(public)
}
fn pep440_public(value: &str) -> bool {
    let digits = |s: &str| !s.is_empty() && s.len() <= 9 && s.bytes().all(|b| b.is_ascii_digit());
    let release_end = value
        .find(|c: char| !(c.is_ascii_digit() || c == '.'))
        .unwrap_or(value.len());
    let (release, mut rest) = value.split_at(release_end);
    // `3.2.8.post1` puts its separator inside the digit run.
    let mut release = release.to_owned();
    for marker in [".post", ".dev"] {
        if rest.starts_with(&marker[1..]) && release.ends_with('.') {
            release.pop();
            rest = &value[release.len()..];
        }
    }
    let parts: Vec<_> = release.split('.').collect();
    if !(2..=4).contains(&parts.len()) || !parts.iter().all(|part| digits(part)) {
        return false;
    }
    for prefix in ["a", "b", "rc"] {
        if let Some(n) = rest.strip_prefix(prefix) {
            let end = n.find('.').unwrap_or(n.len());
            if !digits(&n[..end]) {
                return false;
            }
            rest = &n[end..];
            break;
        }
    }
    for marker in [".post", ".dev"] {
        if let Some(n) = rest.strip_prefix(marker) {
            let end = n.find('.').unwrap_or(n.len());
            if !digits(&n[..end]) {
                return false;
            }
            rest = &n[end..];
        }
    }
    rest.is_empty()
}

fn version(v: &Value) -> bool {
    v.as_str().is_some_and(is_version)
}
fn date(v: &Value) -> bool {
    v.as_str()
        .is_some_and(|s| s.len() <= 64 && DateTime::parse_from_rfc3339(s).is_ok())
}
fn request_id(v: &Value) -> bool {
    v.as_str()
        .is_some_and(|s| !s.trim().is_empty() && s.len() <= 128 && s.bytes().all(|b| b.is_ascii_graphic()))
}
fn one(v: &Value, allowed: &[&str]) -> bool {
    v.as_str().is_some_and(|s| allowed.contains(&s))
}
fn nullable(v: &Value, check: impl Fn(&Value) -> bool) -> bool {
    v.is_null() || check(v)
}
fn field(source: &Value, key: &str, check: impl Fn(&Value) -> bool) -> Result<Value, ClientError> {
    let value = source.get(key).unwrap_or(&Value::Null);
    if check(value) {
        Ok(value.clone())
    } else {
        Err(invalid())
    }
}
fn code(v: &Value) -> Result<Value, ClientError> {
    match v {
        Value::Null => Ok(Value::Null),
        Value::String(code) => Ok(json!(error_code(Some(code)))),
        _ => Err(invalid()),
    }
}

const HOST_ACTIONS: [&str; 4] = ["check", "prereleases", "auto_update", "install"];

fn remote_request(v: &Value) -> Result<Value, ClientError> {
    if v.is_null() {
        return Ok(Value::Null);
    }
    Ok(json!({
        "request_id": field(v, "request_id", request_id)?,
        "target": field(v, "target", version)?,
        "phase": field(v, "phase", |v| one(v, &[
            "requested", "acknowledged", "checking", "awaiting_host", "downloading", "installing",
            "restarting", "completed", "failed", "declined", "expired",
        ]))?,
        "progress": field(v, "progress", |v| nullable(v, |v| v.as_u64().is_some_and(|n| n <= 100)))?,
        "error_code": code(v.get("error_code").unwrap_or(&Value::Null))?,
        "updated_at": field(v, "updated_at", date)?,
    }))
}
fn control_request(v: &Value) -> Result<Value, ClientError> {
    if v.is_null() {
        return Ok(Value::Null);
    }
    Ok(json!({
        "request_id": field(v, "request_id", request_id)?,
        "action": field(v, "action", |v| one(v, &HOST_ACTIONS[..3]))?,
        "enabled": field(v, "enabled", |v| nullable(v, Value::is_boolean))?,
        "phase": field(v, "phase", |v| one(v, &["checking", "completed", "failed", "expired"]))?,
        "error_code": code(v.get("error_code").unwrap_or(&Value::Null))?,
        "updated_at": field(v, "updated_at", date)?,
    }))
}
fn install(v: &Value) -> Result<Value, ClientError> {
    if v.is_null() {
        return Ok(Value::Null);
    }
    Ok(json!({
        "phase": field(v, "phase", |v| one(v, &["installing", "restarting"]))?,
        "target": field(v, "target", |v| nullable(v, version))?,
        "started_at": field(v, "started_at", date)?,
    }))
}
/// Only the kind: a revision or a dirty tree is the instance's business.
fn build(v: &Value) -> Result<Value, ClientError> {
    Ok(json!({"kind": field(v, "kind", |v| one(v, &["package", "source"]))?}))
}

/// Rebuilds an `UpdateSnapshot` (ui/src/components/update/types.ts) plus the
/// Runtime's optional `install`. Fails on any malformed known field.
pub fn snapshot(v: &Value) -> Result<Value, ClientError> {
    let object = v.as_object().ok_or_else(invalid)?;
    let mut out = Map::new();
    out.insert("current".into(), field(v, "current", version)?);
    out.insert("latest".into(), field(v, "latest", |v| nullable(v, version))?);
    out.insert("has_update".into(), field(v, "has_update", Value::is_boolean)?);
    // A check failure is reported, never the instance's description of it.
    out.insert(
        "error".into(),
        match object.get("error").unwrap_or(&Value::Null) {
            Value::Null => Value::Null,
            Value::String(_) => json!("error"),
            _ => return Err(invalid()),
        },
    );
    for (key, value) in object {
        let value = match key.as_str() {
            "managed_by" => field(v, key, |v| v == "desktop")?,
            "phase" => field(v, key, |v| {
                nullable(v, |v| {
                    one(v, &["current", "available", "disabled", "error", "checking"])
                })
            })?,
            "desktop_version" => field(v, key, |v| nullable(v, version))?,
            "checked_at" => field(v, key, |v| nullable(v, date))?,
            "prereleases" | "auto_update" => field(v, key, |v| nullable(v, Value::is_boolean))?,
            "remote_install_allowed" => field(v, key, Value::is_boolean)?,
            "host_actions" => {
                let listed = value.as_array().filter(|a| a.len() <= 16).ok_or_else(invalid)?;
                json!(HOST_ACTIONS
                    .iter()
                    .filter(|action| listed.iter().any(|item| item == **action))
                    .collect::<Vec<_>>())
            }
            "remote_request" => remote_request(value)?,
            "control_request" => control_request(value)?,
            "install" => install(value)?,
            "build" => build(value)?,
            _ => continue,
        };
        out.insert(key.clone(), value);
    }
    Ok(Value::Object(out))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn at(minutes: i64) -> DateTime<Utc> {
        DateTime::parse_from_rfc3339("2026-11-20T07:00:00Z")
            .unwrap()
            .with_timezone(&Utc)
            + chrono::Duration::minutes(minutes)
    }
    /// An install accepted at minute 0 from 3.2.7, its answer read since.
    fn accepted(snapshot: Value, status: Status) -> InstanceUpdate {
        InstanceUpdate {
            status,
            snapshot: Some(snapshot),
            can_manage: true,
            request: Some(Receipt {
                action: Action::Install,
                phase: Phase::Accepted,
                target: Some("3.2.8".into()),
                error_code: None,
                updated_at: "2026-11-20T07:00:00Z".into(),
                following: true,
                installed_from: Some("3.2.7".into()),
            }),
            request_seen: true,
            ..InstanceUpdate::loading("one".into())
        }
    }

    // B1 follow-up: a standalone Runtime that answers on the version it
    // accepted from, without an install of its own, five minutes on, did not
    // take it; the shell stops following and says so in `following`.
    #[test]
    fn a_standalone_install_that_did_not_restart_stops_being_followed_after_five_minutes() {
        let old = json!({"current": "3.2.7", "latest": "3.2.8", "has_update": true, "install": null});
        let mut update = accepted(old.clone(), Status::Ready);
        assert!(update.following(at(4)));
        assert!(update.following(at(4) + chrono::Duration::seconds(59)));
        assert!(!update.following(at(5)));
        assert!(!update.unsettled(at(5)));
        // Published as soon as it changes, whatever changed it.
        assert!(update.settle_following(at(5)));
        assert!(!update.request.as_ref().unwrap().following);
        assert!(!update.settle_following(at(6)));

        // Followed as today: the Runtime reports its own install, does not
        // answer, has not been read since accepting, or is desktop-managed.
        let mut running = old.clone();
        running["install"] = json!({"phase": "restarting", "target": "3.2.8", "started_at": "2026-11-20T07:00:30Z"});
        let mut managed = old.clone();
        managed["managed_by"] = json!("desktop");
        let mut unread = accepted(old.clone(), Status::Ready);
        unread.request_seen = false;
        for (case, update) in [
            ("own install", accepted(running, Status::Ready)),
            ("not answering", accepted(old.clone(), Status::Unreachable)),
            ("unread", unread),
            ("desktop-managed", accepted(managed, Status::Ready)),
        ] {
            for minute in [5, 29] {
                assert!(update.following(at(minute)), "{case} at {minute}");
            }
            assert!(!update.following(at(30)), "{case}: the follow limit still applies");
        }
        // Another version ends it at once, as before.
        let mut new = old;
        new["current"] = json!("3.2.8");
        assert!(!accepted(new, Status::Ready).following(at(1)));
    }

    #[test]
    fn following_is_on_its_way_or_a_followed_install_only() {
        let mut update = accepted(json!({"current": "3.2.7"}), Status::Ready);
        let request = update.request.as_mut().unwrap();
        request.action = Action::Check;
        request.installed_from = None;
        assert!(!update.following(at(0)));
        update.request.as_mut().unwrap().phase = Phase::Sending;
        assert!(update.following(at(0)));
        update.request.as_mut().unwrap().phase = Phase::Failed;
        assert!(!update.following(at(0)));
        update.request = None;
        assert!(!update.following(at(0)));
    }

    #[test]
    fn versions_are_semver_or_runtime_pep440() {
        for ok in [
            "3.2.8",
            "3.2.8-rc.4",
            "3.2.8rc4",
            "3.2.8a1",
            "3.2.8b2",
            "3.2.8.post1",
            "3.2.9.dev5",
            "3.2.9.dev5+g1a2b3c4.d20261120",
            "3.2.8rc4.post1",
            "1.0",
        ] {
            assert!(is_version(ok), "{ok}");
        }
        for bad in [
            "",
            "v3.2.8",
            "3.2.8 ",
            "3.2.8rc",
            "3.2.8-rc 4",
            "latest",
            "3.2.8+",
            "3.2.8+a..b",
            "<b>3.2.8</b>",
            "3",
            "3.2.8\n",
            "https://x/3.2.8",
            "3.2.8rc4x",
        ] {
            assert!(!is_version(bad), "{bad}");
        }
    }
}
