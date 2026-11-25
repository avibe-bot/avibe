//! These two hosted credentials are not browser cookies for import/export. Only
//! a native account ceremony can fill the slots. Hosted still verifies the proof
//! against the app subject, SID and live issuer; presence is not authorization.
use super::{ClientError, Response};
use reqwest::Method;
use serde_json::Value;
use std::time::{Duration, Instant};
use url::Url;

pub(super) const START: &str = "__Host-avibe-sign-in-start";
pub(super) const PROOF: &str = "__Host-avibe-sign-in-proof";
const FIRST_PARTY: &str = "https://avibe.bot/api/auth/first-party/start";
const COMPLETE: &str = "https://avibe.bot/api/auth/first-party/email-code";
const PASSKEY: &str = "https://avibe.bot/api/passkey-primary/verify";
const ESTABLISH: &str = "https://avibe.bot/api/desktop/session";
const GRANT: &str = "https://avibe.bot/api/v1/claims/grant";
const CHALLENGE_FACTOR: &str = "https://avibe.bot/api/two-factor/passkey-challenge";
const ENROLL_TOTP: &str = "https://avibe.bot/api/two-factor/totp";
const SETUP_TOTP: &str = "https://avibe.bot/api/two-factor/totp/setup";
const VERIFY_FACTOR: &str = "https://avibe.bot/api/two-factor/verify";

pub(super) fn reserved(name: &str) -> bool {
    matches!(name, START | PROOF)
}
pub(super) fn one_shot(method: &Method, url: &Url) -> bool {
    method == Method::POST
        && (matches!(
            url.as_str(),
            FIRST_PARTY | COMPLETE | PASSKEY | GRANT | CHALLENGE_FACTOR | VERIFY_FACTOR | SETUP_TOTP | ENROLL_TOTP
        ) || url.path() == "/auth/v1/otp")
}
#[derive(Clone)]
struct Secret {
    value: String,
    until: Instant,
}
#[derive(Default)]
pub(super) struct Ceremony {
    start: Option<(String, Secret)>,
    proof: Option<Secret>,
}
impl Clone for Ceremony {
    fn clone(&self) -> Self {
        // A transaction and its start cookie belong to the *one* in-flight
        // submit. A fenced account snapshot may carry proof, never another
        // chance to submit the code.
        Self {
            start: None,
            proof: self.proof.clone(),
        }
    }
}
#[derive(Clone, Copy, PartialEq, Eq)]
pub(super) enum Route {
    Other,
    Start,
    Complete,
    Passkey,
    Establish,
    Grant,
}
impl Route {
    pub(super) fn request(method: &Method, url: &Url, body: Option<&Value>) -> Self {
        if method != Method::POST {
            return Self::Other;
        }
        match url.as_str() {
            FIRST_PARTY if body.is_some_and(|b| b["kind"] == "email_code") => Self::Start,
            COMPLETE => Self::Complete,
            PASSKEY => Self::Passkey,
            ESTABLISH => Self::Establish,
            GRANT => Self::Grant,
            _ => Self::Other,
        }
    }
}
fn handle(body: &Value) -> Option<&str> {
    body["transaction"]
        .as_str()
        .filter(|s| s.len() == 43 && s.bytes().all(|c| c.is_ascii_alphanumeric() || matches!(c, b'_' | b'-')))
}
impl Ceremony {
    pub(super) fn outbound(&mut self, route: Route, body: Option<&Value>) -> Result<Option<String>, ClientError> {
        match route {
            Route::Start => self.start = None,
            Route::Complete => {
                // Take before I/O: even losing the response cannot replay this ceremony.
                let (transaction, secret) = self
                    .start
                    .take()
                    .ok_or_else(|| ClientError::new("invalid_transaction"))?;
                if body.and_then(handle) != Some(transaction.as_str()) || Instant::now() >= secret.until {
                    return Err(ClientError::new("invalid_transaction"));
                }
                return Ok(Some(format!("{START}={}", secret.value)));
            }
            Route::Grant => {
                if let Some(proof) = self.proof.as_ref().filter(|s| Instant::now() < s.until) {
                    return Ok(Some(format!("{PROOF}={}", proof.value)));
                }
                self.proof = None;
            }
            _ => {}
        }
        Ok(None)
    }

    pub(super) fn receive(&mut self, route: Route, response: &Response, fields: Fields<'_>) {
        // Exact terminal body is evidence of which response this is, not proof
        // of the user, and not a reason to discard a valid ordinary login.
        let json = || response.json().ok();
        let accepted_proof = response.status == 200
            && match route {
                Route::Complete => json().is_some_and(|b| b["bound"] == true),
                Route::Passkey => json().is_some_and(|b| b["ok"] == true && b["next"].is_string()),
                _ => false,
            };
        if matches!(route, Route::Passkey | Route::Complete | Route::Establish) {
            // Never inherit an older proof on replacement, even if the new login has none.
            self.proof = None;
        }
        match fields.proof.valid(PROOF) {
            Some(Write::Set(s)) if accepted_proof => self.proof = Some(s),
            Some(Write::Clear) => self.proof = None,
            _ => {}
        }
        match fields.start.valid(START) {
            Some(Write::Set(s)) if route == Route::Start && response.status == 200 => {
                self.start = json().and_then(|b| Some((handle(&b)?.to_owned(), s)));
            }
            Some(Write::Clear) => self.start = None,
            _ => {}
        }
    }
}

#[derive(Default)]
pub(super) struct Fields<'a> {
    start: Field<'a>,
    proof: Field<'a>,
}
impl<'a> Fields<'a> {
    pub(super) fn capture(&mut self, name: &str, raw: &'a str) -> bool {
        let field = match name {
            START => &mut self.start,
            PROOF => &mut self.proof,
            _ => return false,
        };
        *field = match field {
            Field::Absent => Field::One(raw),
            _ => Field::Ambiguous,
        };
        true
    }
}
#[derive(Default)]
enum Field<'a> {
    #[default]
    Absent,
    One(&'a str),
    Ambiguous,
}
enum Write {
    Clear,
    Set(Secret),
}
impl Field<'_> {
    fn valid(&self, name: &str) -> Option<Write> {
        let Self::One(raw) = self else { return None };
        // Inspect the original attributes. Cookie::parse accepts Domain= as
        // absent, Secure=false as Secure, and overwrites duplicate paths.
        let mut parts = raw.split(';');
        let (key, value) = parts.next()?.trim().split_once('=')?;
        if key != name {
            return None;
        }
        let mut seen = 0u8;
        let mut seconds = 0u64;
        for attr in parts {
            let (k, v) = attr
                .trim()
                .split_once('=')
                .map_or((attr.trim(), None), |(k, v)| (k, Some(v)));
            let bit = match (k.to_ascii_lowercase().as_str(), v) {
                ("secure", None) => 1,
                ("httponly", None) => 2,
                ("path", Some("/")) => 4,
                ("samesite", Some(v)) if v.eq_ignore_ascii_case("lax") => 8,
                ("max-age", Some(v)) if !v.is_empty() && v.bytes().all(|b| b.is_ascii_digit()) => {
                    seconds = v.parse().ok()?;
                    16
                }
                // Max-Age is required and takes precedence; a producer may also emit Expires.
                ("expires", Some(_)) if cookie::Cookie::parse(*raw).ok()?.expires_datetime().is_some() => 32,
                _ => return None,
            };
            if seen & bit != 0 {
                return None;
            }
            seen |= bit;
        }
        if seen & 31 != 31 {
            return None;
        }
        if value.is_empty() && seconds == 0 {
            return Some(Write::Clear);
        }
        if seconds == 0
            || seconds > (if name == START { 600 } else { 90 * 86400 })
            || value.is_empty()
            || value.len() > 4096
            || !value
                .bytes()
                .all(|c| c.is_ascii_alphanumeric() || matches!(c, b'.' | b'_' | b'-'))
        {
            return None;
        }
        Some(Write::Set(Secret {
            value: value.to_owned(),
            until: Instant::now() + Duration::from_secs(seconds),
        }))
    }
}
