//! One typed-code delivery. Neither the issuer's jar nor a JS-supplied identity
//! can complete it. Cloned account transactions share the dispatch latch: a lost
//! future cannot leave the previous snapshot free to submit the same delivery.
#[cfg(test)]
mod tests;
use super::{Account, ClientError, NativeSession, Response, EMAIL_REDIRECT};
use reqwest::{header::HeaderMap, Method};
use serde_json::{json, Value};
use std::{
    sync::{Arc, Mutex},
    time::Duration,
};
use tokio::time::Instant;
use url::Url;

#[derive(Clone)]
pub(super) struct Mailbox {
    email: String,
    subject: Option<String>,
}
impl Mailbox {
    pub(super) fn login(email: &str) -> Result<Self, ClientError> {
        let email = email.trim();
        let (local, host) = email.split_once('@').ok_or_else(|| ClientError::new("invalid_email"))?;
        if local.is_empty()
            || host.is_empty()
            || host.contains('@')
            || email.encode_utf16().count() > 254
            || email.chars().any(|c| c.is_whitespace() || c.is_control())
        {
            return Err(ClientError::new("invalid_email"));
        }
        Ok(Self {
            email: email.to_lowercase(),
            subject: None,
        })
    }
    // Only the privately witnessed account can be used for a step-up. There is
    // no bridge parameter for either field. The producer checks this subject
    // against the newly verified issuer identity before setting bound:true.
    pub(super) fn same_account(account: &Account) -> Result<Self, ClientError> {
        if account.id.is_empty() || account.id.encode_utf16().count() > 128 {
            return Err(ClientError::new("account_changed"));
        }
        let mut owner = Self::login(&account.email)?;
        owner.subject = Some(account.id.clone());
        Ok(owner)
    }
    pub(super) fn email(&self) -> &str {
        &self.email
    }
    pub(super) fn subject(&self) -> Option<&str> {
        self.subject.as_deref()
    }
    pub(super) fn matches(&self, email: &str, subject: Option<&str>) -> bool {
        email.trim().to_lowercase() == self.email
            && subject
                .zip(self.subject.as_deref())
                .is_none_or(|(seen, expected)| seen == expected)
    }
    pub(super) async fn deliver(self, mut issuer: NativeSession, headers: HeaderMap) -> Result<TypedCode, ClientError> {
        let mut url = Url::parse("https://placeholder.test/auth/v1/otp").expect("fixed path");
        url.query_pairs_mut().append_pair("redirect_to", EMAIL_REDIRECT);
        let r = issuer
            .request_with_headers(
                Method::POST,
                &url[url::Position::BeforePath..],
                Some(json!({"email":self.email,"create_user":self.subject.is_none()})),
                headers,
            )
            .await
            .map_err(transport_error)?;
        if !r.success() {
            return Err(if r.status == 429 {
                limited(retry_after(&r))
            } else {
                ClientError::new("request_failed")
            });
        }
        Ok(TypedCode {
            mailbox: self,
            phase: Arc::new(Mutex::new(Phase::Ready)),
        })
    }
}
#[derive(Clone)]
pub(super) struct TypedCode {
    pub(super) mailbox: Mailbox,
    phase: Arc<Mutex<Phase>>,
}
/// Owns one accepted start. The code and the jar bearing the matching start
/// cookie move together and never enter a clone, a record, or a bridge reply.
/// Dropping this value cannot forward anything.
pub(super) struct PreparedCode {
    delivery: TypedCode,
    candidate: NativeSession,
    transaction: String,
    code: String,
}

enum Phase {
    Ready,
    Wait(Instant),
    // Set before I/O; if the submitting future disappears, require a fresh delivery.
    Spent(Option<Instant>),
    Bound(Box<NativeSession>),
}
pub(super) fn retry_after(r: &Response) -> u64 {
    // Both pinned producers emit integral seconds, issuer forwarding accepts
    // up to seven digits. Keep the native deadline even past the u8 wire hint.
    r.headers
        .get("retry-after")
        .and_then(|h| h.to_str().ok())
        .map(str::trim)
        .filter(|s| !s.is_empty() && s.len() <= 7 && s.bytes().all(|c| c.is_ascii_digit()))
        .and_then(|s| s.parse::<u64>().ok())
        .unwrap_or(60)
        .max(1)
}
pub(super) fn limited(seconds: u64) -> ClientError {
    let mut e = ClientError::new("rate_limited");
    e.retry_until = Some(Instant::now() + Duration::from_secs(seconds));
    e.retry_after_seconds = Some(seconds.clamp(1, 255) as u8);
    e
}
pub(super) fn transport_error(e: ClientError) -> ClientError {
    match e.code.as_str() {
        "network_unavailable" | "stale_generation" => e,
        _ => ClientError::new("request_failed"),
    }
}
pub(in crate::instance_client) fn validate_code(code: &str) -> Result<(), ClientError> {
    if code.len() == 6 && code.bytes().all(|b| b.is_ascii_digit()) {
        Ok(())
    } else {
        Err(ClientError::new("invalid_or_expired"))
    }
}
impl TypedCode {
    fn set(&self, phase: Phase) {
        *self.phase.lock().expect("typed code") = phase;
    }
    fn cooldown(phase: &Phase) -> Result<(), ClientError> {
        if let Phase::Wait(t) | Phase::Spent(Some(t)) = phase {
            let remaining = t.saturating_duration_since(Instant::now());
            if !remaining.is_zero() {
                return Err(limited(remaining.as_secs() + u64::from(remaining.subsec_nanos() != 0)));
            }
        }
        Ok(())
    }
    pub(super) fn discard_bound(&self) {
        self.set(Phase::Spent(None));
    }
    pub(super) fn can_resend(&self) -> Result<(), ClientError> {
        Self::cooldown(&self.phase.lock().expect("typed code"))
    }
    /// None is discovery only: never start a ceremony on refresh. Only the
    /// credential owner subsequently publishes the accepted ordinary session.
    pub(super) async fn session(&self, base: &NativeSession, code: Option<&str>) -> Result<NativeSession, ClientError> {
        {
            let phase = self.phase.lock().expect("typed code");
            Self::cooldown(&phase)?;
            if let Phase::Bound(accepted) = &*phase {
                return base.account_attempt(Some(accepted));
            }
        }
        let code = code.ok_or_else(|| ClientError::new("invalid_action"))?;
        self.prepare(base, code).await?.forward().await
    }
    pub(super) async fn prepare(&self, base: &NativeSession, code: &str) -> Result<PreparedCode, ClientError> {
        validate_code(code)?;
        let mut candidate = base.account_attempt(None)?;
        {
            let mut phase = self.phase.lock().expect("typed code");
            Self::cooldown(&phase)?;
            if !matches!(*phase, Phase::Ready | Phase::Wait(_)) {
                return Err(ClientError::new("fresh_code_required"));
            }
            *phase = Phase::Spent(None);
        }
        let mut start = json!({"kind":"email_code","email":self.mailbox.email});
        if let Some(sub) = &self.mailbox.subject {
            start["expected_subject"] = json!(sub);
        }
        let started = candidate
            .request(Method::POST, "/api/auth/first-party/start", Some(start))
            .await;
        // The start carries no code. An explicit later submission may try it again.
        let tx = match started {
            Ok(r) if r.status == 200 => r.json().ok().and_then(|v| {
                v["transaction"]
                    .as_str()
                    .filter(|s| {
                        s.len() == 43 && s.bytes().all(|b| b.is_ascii_alphanumeric() || matches!(b, b'_' | b'-'))
                    })
                    .map(str::to_owned)
            }),
            Ok(r) if r.status == 429 => {
                let secs = retry_after(&r);
                self.set(Phase::Wait(Instant::now() + Duration::from_secs(secs)));
                return Err(limited(secs));
            }
            Ok(_) => None,
            Err(e) => {
                self.set(Phase::Ready);
                return Err(transport_error(e));
            }
        };
        let Some(tx) = tx else {
            self.set(Phase::Ready);
            return Err(ClientError::new("request_failed"));
        };
        Ok(PreparedCode {
            delivery: self.clone(),
            candidate,
            transaction: tx,
            code: code.to_owned(),
        })
    }
}
impl PreparedCode {
    pub(super) fn fence(&mut self, epoch: super::Epoch) {
        self.candidate.fence(epoch);
    }
    pub(super) async fn forward(self) -> Result<NativeSession, ClientError> {
        let Self {
            delivery,
            mut candidate,
            transaction,
            code,
        } = self;
        let completed = candidate
            .request(
                Method::POST,
                "/api/auth/first-party/email-code",
                Some(json!({"transaction":transaction,"email":delivery.mailbox.email,"code":code})),
            )
            .await;
        let r = match completed {
            Ok(r) => r,
            Err(e) if e.code == "invalid_transaction" => {
                delivery.set(Phase::Ready);
                return Err(ClientError::new("request_failed"));
            } // no matching native start cookie: no dispatch
            Err(e) if e.code == "stale_generation" => return Err(e),
            Err(_) => return Err(ClientError::new("code_delivery_unknown")),
        };
        let body = r.json().unwrap_or(Value::Null);
        match (r.status, body["error"].as_str()) {
            (200, None) if body["bound"] == true && candidate.has_discovery_credentials() => {
                delivery.set(Phase::Bound(Box::new(candidate.clone())));
                Ok(candidate)
            }
            (403, Some("verification_invalid")) => {
                delivery.set(Phase::Ready);
                Err(ClientError::new("invalid_or_expired"))
            }
            (429, reason) => {
                let secs = retry_after(&r);
                let until = Instant::now() + Duration::from_secs(secs);
                delivery.set(
                    if reason == Some("rate_limited") && body["code_delivery"] == "not_forwarded" {
                        Phase::Wait(until)
                    } else {
                        Phase::Spent(Some(until))
                    },
                );
                Err(limited(secs))
            }
            (503, Some("unavailable")) if body["code_delivery"] == "not_forwarded" => {
                delivery.set(Phase::Ready);
                Err(ClientError::new("provider_unavailable"))
            }
            _ => Err(ClientError::new("code_delivery_unknown")),
        }
    }
}
