//! Account half of one Desktop attempt. Only native orchestration owns this object; never
//! store or serialize it. A hostname-only recorded read is *not* a target-kind/ID receipt.
use super::{retired, text, MachineAttempt, MachineCompletion, MachineTicket, Result};
use crate::instance_client::{
    account::AccountFlow,
    protocol::{Account, AccountState, AccountStatus, ClientError},
    transport::{Epoch, FactorCookies, NativeSession, Response},
};
use reqwest::Method;
use serde::Deserialize;
use serde_json::json;
use uuid::Uuid;

mod projection;
pub(crate) use projection::Screen;
use projection::{Evidence, Read, Target};
#[cfg(test)]
mod tests;

const GRANT: &str = "/api/v1/claims/grant";
const SESSION: &str = "/api/mobile/session";
fn session_matches(response: &Response, a: &Account) -> bool {
    #[derive(Deserialize)]
    struct Identity {
        account: Account,
    }
    response.status == 200 && serde_json::from_slice::<Identity>(&response.body).is_ok_and(|i| &i.account == a)
}
fn bad() -> ClientError {
    ClientError::new("invalid_claim_response")
}
fn changed() -> ClientError {
    ClientError::new("decision_changed")
}
fn uncertain() -> ClientError {
    ClientError::new("fresh_claim_required")
}

// This type has no production constructor outside the exact successful submit below.
pub(super) struct ReviewedGrant {
    claim_id: String,
    expected_subject: String,
    target: Target,
    epochs: [Epoch; 2],
}
impl ReviewedGrant {
    pub(super) fn into_parts(self) -> (String, String, String, Option<String>, [Epoch; 2]) {
        (
            self.claim_id,
            self.expected_subject,
            self.target.hostname,
            self.target.existing_id,
            self.epochs,
        )
    }
    #[cfg(test)]
    pub(super) fn fixture(id: &str, subject: &str, hostname: &str, existing: Option<&str>) -> Self {
        let epoch = Epoch::new(std::sync::Arc::new(std::sync::atomic::AtomicU64::new(1)), 1);
        Self {
            claim_id: id.into(),
            expected_subject: subject.into(),
            epochs: [epoch.clone(), epoch],
            target: Target {
                prefix: String::new(),
                hostname: hostname.into(),
                existing_id: existing.map(str::to_owned),
            },
        }
    }
}

pub(crate) enum GrantResponse {
    Matched(MatchedSubmission),
    ProofRequired(ProofNeeded),
}

/// Only the exact hosted proof refusal can move an unarmed machine here. The
/// consumed decision is retained for a future fresh read, never as an arm.
/// No Clone, serialization or conversion to ReviewedGrant.
pub(crate) struct ProofNeeded {
    machine: MachineAttempt,
    epochs: [Epoch; 2],
    account: Account,
    refused_revision: Uuid,
    target: Target,
}
impl ProofNeeded {
    pub(crate) fn check(&self, account: &Account) -> Result<()> {
        for epoch in &self.epochs {
            epoch.check()?;
        }
        if &self.account != account {
            return Err(changed());
        }
        self.machine.check_live()
    }
    /// Rebuild a review on the original live pipe, never from a page's target
    /// or ticket. The old confirmation is spent. Even the same hostname must
    /// not silently change from new to existing, or to a different existing ID.
    pub(crate) async fn reread(self, flow: &mut AccountFlow, state: &AccountState) -> Result<HostedClaim> {
        let a = state.identity.as_ref().ok_or_else(changed)?;
        self.check(a)?;
        let [account_epoch, document_epoch] = self.epochs;
        let mut claim = HostedClaim::new(self.machine, account_epoch, document_epoch, state)?;
        claim.after_proof = true;
        claim.current(flow, state)?;
        let response = flow
            .session
            .request(Method::GET, SESSION, None)
            .await
            .map_err(|_| ClientError::new("control_plane_unreachable"))?;
        claim.current(flow, state)?;
        if !session_matches(&response, a) {
            return Err(if error_code(&response) == Some((403, "two_factor_required")) {
                ClientError::new("two_factor_required")
            } else {
                changed()
            });
        }
        let requested = self.target.existing_id.is_none().then_some(self.target.prefix.as_str());
        let mut jar = flow.session.clone();
        let credential = flow.session.claim_read_cookies();
        let evidence = claim
            .read_ticket(&mut jar, flow, state, requested, credential.as_ref())
            .await?;
        claim.current(flow, state)?;
        let Evidence::Review(read) = evidence else {
            return Err(changed());
        };
        if read.target.as_ref() != Some(&self.target)
            && !(requested.is_some()
                && matches!(&read.screen, Screen::New { taken: true, .. } | Screen::Capacity { .. }))
        {
            return Err(changed());
        }
        flow.session = jar;
        claim.review = Some(read);
        Ok(claim)
    }
    pub(crate) fn refused_revision(&self) -> Uuid {
        self.refused_revision
    }
    pub(crate) fn deadline(&self) -> tokio::time::Instant {
        self.machine.ticket.deadline
    }
}

/// The hosted affirmative reply is not an arm. Keep the exact worker and decision
/// together; it is impossible to provide replacement ticket or target inputs here.
#[must_use]
pub(crate) struct MatchedSubmission {
    revision: Uuid,
    machine: MachineAttempt,
    grant: ReviewedGrant,
}
impl MatchedSubmission {
    pub(crate) fn dispatch(self, revision: Uuid, account: &Account) -> Result<MachineCompletion> {
        if self.revision != revision || self.grant.expected_subject != account.id {
            return Err(changed());
        }
        self.machine.dispatch(self.grant)
    }
}

pub(crate) struct ReviewView<'a> {
    pub(crate) revision: Uuid,
    pub(crate) account_email: &'a str,
    pub(crate) device_name: &'a str,
    pub(crate) screen: &'a Screen,
    pub(crate) can_confirm: bool,
}

impl ReviewView<'_> {
    /// Only the private direct command reply. No subject, ticket, immutable instance ID
    /// or masked former owner here (the latter belongs to a separate D1u preview).
    pub(crate) fn private_reply(&self) -> serde_json::Value {
        let screen = match self.screen {
            Screen::New {
                prefilled,
                selected,
                taken,
                names,
            } => json!({"state":"new","prefilled_prefix":prefilled,"prefix":selected,"taken":taken,"names":names}),
            Screen::Existing { prefix, hostname } if self.can_confirm => {
                json!({"state":"existing","prefix":prefix,"hostname":hostname})
            }
            Screen::Existing { .. } => json!({"state":"local_runtime_upgrade_required"}),
            Screen::OtherAccount { .. } => json!({"state":"owner_conflict"}),
            Screen::Capacity { prefilled, names } => {
                json!({"state":"name_holding_limit_reached","prefilled_prefix":prefilled,"names":names})
            }
        };
        json!({"revision":self.revision.to_string(),"account_email":self.account_email,"device_name":self.device_name,"screen":screen})
    }
}

pub(crate) struct HostedClaim {
    machine: Option<MachineAttempt>,
    account_epoch: Epoch,
    document_epoch: Epoch,
    identity: Account,
    revision: Uuid,
    review: Option<Read>,
    // Only an authenticated conflict on this unarmed ticket can populate this witness.
    conflict_credential: Option<FactorCookies>,
    dispatched: bool,
    after_proof: bool,
}
impl HostedClaim {
    pub(crate) fn new(
        machine: MachineAttempt,
        account_epoch: Epoch,
        document_epoch: Epoch,
        state: &AccountState,
    ) -> Result<Self> {
        let identity = state
            .identity
            .as_ref()
            .filter(|a| state.status == AccountStatus::SignedIn && a.id == machine.subject && text(&a.email, 320))
            .ok_or_else(changed)?
            .clone();
        account_epoch.check()?;
        document_epoch.check()?;
        Ok(Self {
            machine: Some(machine),
            account_epoch,
            document_epoch,
            identity,
            revision: Uuid::new_v4(),
            review: None,
            conflict_credential: None,
            dispatched: false,
            after_proof: false,
        })
    }
    /// A read-only stamp for late delivery, including when the worker has already
    /// consumed the arm or the grant response has closed this private pipe.
    pub(in crate::instance_client) fn adoption(&self) -> Result<tokio::sync::watch::Receiver<bool>> {
        Ok(self.machine.as_ref().ok_or_else(retired)?.adoption.clone())
    }
    pub(crate) fn machine_live(&self) -> bool {
        self.machine.as_ref().is_some_and(|m| m.check_live().is_ok())
    }
    pub(crate) fn deadline(&self) -> Result<tokio::time::Instant> {
        Ok(self.machine.as_ref().ok_or_else(retired)?.ticket.deadline)
    }
    pub(crate) fn revision(&self) -> Uuid {
        self.revision
    }
    fn current<'a>(&self, flow: &AccountFlow, state: &'a AccountState) -> Result<(&MachineAttempt, &'a Account)> {
        self.account_epoch.check()?;
        self.document_epoch.check()?;
        let m = self.machine.as_ref().ok_or_else(retired)?;
        m.check_live()?;
        if !flow.session.is_account() {
            return Err(ClientError::new("invalid_backend"));
        }
        let a = state
            .identity
            .as_ref()
            .filter(|a| state.status == AccountStatus::SignedIn && *a == &self.identity)
            .ok_or_else(changed)?;
        Ok((m, a))
    }
    pub(crate) fn is_owner_conflict(&self) -> bool {
        matches!(
            self.review.as_ref().map(|r| &r.screen),
            Some(Screen::OtherAccount { .. })
        )
    }
    pub(crate) fn check_conflict(&self, flow: &AccountFlow, state: &AccountState) -> Result<()> {
        self.current(flow, state)?;
        let expected = self.conflict_credential.as_ref().ok_or_else(changed)?;
        if self.dispatched || !self.is_owner_conflict() || flow.session.claim_read_cookies().as_ref() != Some(expected)
        {
            return Err(changed());
        }
        Ok(())
    }
    fn consume_revision(&mut self, expected: Uuid) -> Result<()> {
        if expected != self.revision || self.dispatched {
            return Err(changed());
        }
        self.revision = Uuid::new_v4();
        Ok(())
    }
    pub(crate) fn screen(&self, flow: &AccountFlow, state: &AccountState) -> Result<ReviewView<'_>> {
        let (machine, _) = self.current(flow, state)?;
        let review = self.review.as_ref().ok_or_else(changed)?;
        Ok(ReviewView {
            revision: self.revision,
            account_email: &self.identity.email,
            device_name: &machine.ticket.device_name,
            screen: &review.screen,
            can_confirm: review
                .target
                .as_ref()
                .is_some_and(|t| t.existing_id.is_none() || machine.ticket.existing_readback),
        })
    }
    // An edit invalidates the prior decision *before* awaiting anything, including failed
    // or cancelled reads. A delayed edit/confirm never consumes a newer revision.
    pub(crate) async fn read(
        &mut self,
        flow: &mut AccountFlow,
        state: &AccountState,
        expected: Uuid,
        requested: Option<&str>,
    ) -> Result<()> {
        self.consume_revision(expected)?;
        let previous = self.review.take();
        self.conflict_credential = None;
        if requested.is_some_and(|p| !projection::new_prefix(p)) {
            return Err(ClientError::new("invalid_prefix"));
        }
        if requested.is_some()
            && previous
                .as_ref()
                .is_none_or(|r| !matches!(r.screen, Screen::New { .. } | Screen::Capacity { .. }))
        {
            return Err(changed());
        }
        self.current(flow, state)?;
        let mut jar = flow.session.clone();
        let credential = flow.session.claim_read_cookies();
        let evidence = self
            .read_ticket(&mut jar, flow, state, requested, credential.as_ref())
            .await?;
        self.current(flow, state)?;
        match evidence {
            Evidence::Review(read) => {
                if matches!(&read.screen, Screen::OtherAccount { .. }) {
                    self.conflict_credential = Some(credential.ok_or_else(changed)?);
                    // Observation only. A cookie from the answer did not authenticate the request.
                } else {
                    flow.session = jar;
                }
                self.review = Some(read);
                Ok(())
            }
            Evidence::RecordedHostname(_) => Err(changed()), // never adopt an unsolicited or resumed grant
        }
    }
    async fn read_ticket(
        &self,
        jar: &mut NativeSession,
        flow: &AccountFlow,
        state: &AccountState,
        requested: Option<&str>,
        credential: Option<&FactorCookies>,
    ) -> Result<Evidence> {
        let (m, _) = self.current(flow, state)?;
        let mut body = json!({"user_code":m.ticket.user_code});
        if let Some(prefix) = requested {
            body["prefix"] = json!(prefix);
        }
        let response = jar
            .read_claim_ticket(body, credential)
            .await
            .map_err(|_| ClientError::new("control_plane_unreachable"))?;
        let (m, a) = self.current(flow, state)?;
        if response.status == 200 {
            return projection::parse(&response.body, &m.ticket, &m.locale, a, requested);
        }
        if error_code(&response) == Some((409, "name_holding_limit_reached")) {
            // A capacity error, unlike read success, has no subject. Before exposing its
            // holder-only inventory verify the *request* jar, not a cookie from this error.
            let generic = Read {
                screen: Screen::Capacity {
                    prefilled: None,
                    names: None,
                },
                target: None,
            };
            let (Ok(details), Some(sent)) = (projection::capacity(&response.body, requested), credential) else {
                // Generic capacity needs no holder inventory. An unattributed error
                // is not an account refresh; do not carry its Set-Cookie into the review.
                *jar = flow.session.clone();
                return Ok(Evidence::Review(generic));
            };
            let mut original = flow.session.clone();
            // Compare at both sends, not just by looking at the jar after the POST.
            // For example, a scoped cookie can expire during the first round trip.
            let auth = original.read_frozen_account(sent).await.map_err(|_| changed())?;
            self.current(flow, state)?;
            if !session_matches(&auth, a) {
                return Err(changed());
            }
            return Ok(Evidence::Review(details));
        }
        Err(refusal(&response, false).unwrap_or_else(bad))
    }
    /// Only the opaque revision comes back from the confirmed review. Neither the prefix,
    /// target ID/kind, expected subject, nor the proof acknowledgement comes from that call.
    pub(crate) async fn confirm(
        &mut self,
        flow: &mut AccountFlow,
        state: &AccountState,
        expected: Uuid,
    ) -> Result<GrantResponse> {
        self.consume_revision(expected)?;
        let target = self.review.take().and_then(|r| r.target).ok_or_else(changed)?;
        let (m, a) = self.current(flow, state)?;
        if target.existing_id.is_some() && !m.ticket.existing_readback {
            return Err(ClientError::new("local_runtime_upgrade_required"));
        }
        let body = json!({"user_code":m.ticket.user_code,"prefix":target.prefix,"mode":"desktop","confirm_device":true,"expected_subject":a.id,"existing_instance_id":target.existing_id});
        let mut jar = flow.session.clone();
        // Cancellation after this point spends the submission too. Only an explicit read +
        // another affirmative revision after a *definite* refusal may submit again.
        self.dispatched = true;
        let result = jar.request(Method::POST, GRANT, Some(body)).await;
        self.current(flow, state)?;
        if let Ok(r) = &result {
            if r.status == 200 && projection::grant_matches(&r.body, &target) {
                let (m, a) = self.current(flow, state)?;
                let grant = ReviewedGrant {
                    claim_id: m.ticket.claim_id.clone(),
                    expected_subject: a.id.clone(),
                    target,
                    epochs: [self.account_epoch.clone(), self.document_epoch.clone()],
                };
                flow.session = jar;
                return Ok(GrantResponse::Matched(MatchedSubmission {
                    revision: expected,
                    machine: self.machine.take().ok_or_else(retired)?,
                    grant,
                }));
            }
            if r.status == 403 && exact_proof_refusal(&r.body) {
                if self.after_proof {
                    self.machine.take();
                    return Err(ClientError::new("reauthentication_failed"));
                }
                flow.session = jar;
                return Ok(GrantResponse::ProofRequired(ProofNeeded {
                    machine: self.machine.take().ok_or_else(retired)?,
                    epochs: [self.account_epoch.clone(), self.document_epoch.clone()],
                    account: self.identity.clone(),
                    refused_revision: expected,
                    target,
                }));
            }
            if let Some(error) = refusal(r, true) {
                flow.session = jar; // this exact-origin refusal can still refresh the ordinary session
                self.dispatched = !matches!(
                    error.code.as_str(),
                    "name_taken" | "name_holding_limit_reached" | "two_factor_required" | "rate_limited"
                );
                if self.dispatched {
                    self.machine.take();
                }
                return Err(error);
            }
        }
        // No second grant POST. Use the original jar and code. At the pinned producer a
        // granted read contains only hostname, no recorded kind/immutable existing ID or
        // evidence tying it to *this* dispatch. Even a matching name cannot arm the helper.
        let mut original = flow.session.clone();
        let evidence = self.read_ticket(&mut original, flow, state, None, None).await;
        self.current(flow, state)?;
        self.machine.take(); // unresolved result retires this private pipe; no later retry
        if matches!(evidence,Ok(Evidence::RecordedHostname(ref h)) if h != &target.hostname) {
            return Err(changed());
        }
        Err(uncertain())
    }
}

// Compare a typed response; unknown upstream strings (even harmless-looking identifiers)
// may be credentials. They never become ClientError or diagnostics.
#[derive(Deserialize)]
struct ErrorCode<'a> {
    error: &'a str,
}
fn error_code(r: &Response) -> Option<(u16, &str)> {
    Some((r.status, serde_json::from_slice::<ErrorCode>(&r.body).ok()?.error))
}
fn refusal(r: &Response, grant: bool) -> Option<ClientError> {
    let code = match error_code(r)? {
        (401, "unauthorized") => "signed_out",
        (403, "two_factor_required") => "two_factor_required",
        (404, "claim_unavailable") => "claim_unavailable",
        (409, "taken") => "name_taken",
        (409, "name_holding_limit_reached") => "name_holding_limit_reached",
        (409, "binding_unavailable") if grant && exact_paired_refusal(&r.body) => {
            "paired_installation_loopback_required"
        }
        (409, "binding_unavailable" | "other_account") => "binding_unavailable",
        (409, "decision_changed" | "already_granted" | "already_started") => "decision_changed",
        (410, "already_redeemed") => "fresh_claim_required",
        (429, "rate_limited") => "rate_limited",
        _ => return None,
    };
    Some(ClientError::new(code))
}
fn exact_proof_refusal(body: &[u8]) -> bool {
    #[derive(Deserialize)]
    #[serde(deny_unknown_fields)]
    struct ProofRefusal<'a> {
        error: &'a str,
    }
    serde_json::from_slice::<ProofRefusal>(body).is_ok_and(|r| r.error == "first_factor_reauthentication_required")
}
fn exact_paired_refusal(body: &[u8]) -> bool {
    #[derive(Deserialize)]
    #[serde(deny_unknown_fields)]
    struct Denial<'a> {
        error: &'a str,
        reason: &'a str,
    }
    serde_json::from_slice::<Denial>(body)
        .is_ok_and(|d| d.error == "binding_unavailable" && d.reason == "paired_installation_loopback_required")
}
