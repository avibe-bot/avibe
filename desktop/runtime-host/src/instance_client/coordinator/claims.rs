//! The claim belongs to one admitted account document, outside the cloneable account
//! transaction. Neither reserving nor revoking a receipt constructs a machine client.
//! `open` returns only a fresh local receipt. `read {receipt}` spends its one start;
//! `cancel {receipt}` can revoke from reservation through review, including in-flight
//! I/O. A same-document dismissal must use cancellation, not a visibility assertion.
//! If dismissal wins before Open returns, no Read is necessary: the unused receipt
//! can be cancelled or left to native expiry. A receipt never confirms a decision.
use super::{AccountFlow, AccountState, Coordinator, Epoch};
use crate::{
    instance_client::{
        machine::{hosted::HostedClaim, MachineAttempt, MachineOutcome},
        protocol::{Account, AccountStatus, ClientError},
    },
    RuntimeHost,
};
mod binding_observation;
mod decision;
mod proof;
#[cfg(test)]
mod tests;
use serde::Deserialize;
use serde_json::{json, Value};
use std::{
    sync::{
        atomic::{AtomicU64, Ordering},
        Arc, Mutex,
    },
    time::Duration,
};
use tokio::{sync::oneshot, time::Instant};
use uuid::Uuid;

type Result<T> = std::result::Result<T, ClientError>;
fn stale() -> ClientError {
    ClientError::new("stale_generation")
}
fn busy() -> ClientError {
    ClientError::new("claim_in_progress")
}
fn changed() -> ClientError {
    ClientError::new("decision_changed")
}
fn auth() -> ClientError {
    ClientError::new("authentication_required")
}
// Just enough for the direct local round trip. A lost Open reply holds no pipe or
// pairing admission and cannot leave a document busy through the machine ticket TTL.
const RECEIPT_TTL: Duration = Duration::from_secs(30);

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ClaimRequest {
    pub expected_generation: u64,
    pub action: ClaimAction,
}
#[derive(Deserialize)]
#[serde(tag = "type", rename_all = "snake_case", deny_unknown_fields)]
pub enum ClaimAction {
    Open {},
    ObserveCurrentBinding {},
    Read {
        receipt: String,
    },
    Check {
        revision: String,
        prefix: String,
    },
    Confirm {
        receipt: String,
        revision: String,
    },
    SendProofCode {
        receipt: String,
        offer: String,
    },
    SubmitProofCode {
        receipt: String,
        delivery: String,
        code: String,
    },
    ReviewAfterProof {
        receipt: String,
        submission: String,
    },
    SubmitFactorCode {
        receipt: String,
        submission: String,
        method: crate::instance_client::protocol::CodeMethod,
        code: String,
    },
    SubmitFactorPasskey {
        receipt: String,
        submission: String,
    },
    PrepareTotpSetup {
        receipt: String,
        submission: String,
    },
    FinishTotpSetup {
        receipt: String,
        setup: String,
        code: String,
    },
    Cancel {
        receipt: String,
    },
}
impl ClaimAction {
    pub fn opens_document(&self) -> bool {
        matches!(self, Self::Open {} | Self::ObserveCurrentBinding {})
    }
    pub fn cancels(&self) -> bool {
        matches!(self, Self::Cancel { .. })
    }
}
fn opaque(s: &str) -> Result<Uuid> {
    Uuid::parse_str(s)
        .ok()
        .filter(|r| r.to_string() == s)
        .ok_or_else(changed)
}

#[derive(Default)]
pub(super) struct Claims {
    clock: Arc<AtomicU64>,
    // Short physical admission, before account checks outside the shell lock.
    opening_until: Option<Instant>,
    active: Option<Active>,
}
struct Active {
    receipt: Uuid, // native random correlation only; not a review revision or machine ticket
    account: Account,
    stage: Stage,
    // Sender lives with the receipt, including the transition from review to proof.
    _stop: Option<oneshot::Sender<()>>,
}
enum Stage {
    Observing {
        until: Instant,
    },
    Reserved {
        until: Instant,
    },
    Proof(proof::StepUp),
    Reading {
        interrupt: Option<oneshot::Sender<()>>,
        idle: Option<HostedClaim>,
        delivery: Delivery,
    },
}
#[derive(PartialEq, Eq)]
enum Delivery {
    EditOnly, // a delivered definite refusal permits a read, never a confirmation
    Unreleased,
    Released(Uuid), // passed the final native delivery gate, not proof of human interaction
    Confirming(Uuid, Instant),
}
enum UseReview {
    Edit,
    Confirm { receipt: Uuid },
    ContinueConsent { receipt: Uuid },
}
impl Claims {
    pub(super) fn retire(&mut self) {
        self.clock.fetch_add(1, Ordering::SeqCst);
        self.opening_until = None;
        self.active.take();
    }
    fn epoch(&self) -> Epoch {
        Epoch::new(self.clock.clone(), self.clock.load(Ordering::SeqCst))
    }
    fn expire(&mut self) {
        if self.opening_until.is_some_and(|until| Instant::now() >= until)
            || self.active.as_ref().is_some_and(|a| match &a.stage {
                Stage::Reserved { until } | Stage::Observing { until } => Instant::now() >= *until,
                Stage::Reading {
                    delivery: Delivery::Confirming(_, until),
                    ..
                } => Instant::now() >= *until,
                Stage::Reading { idle: Some(c), .. } => !c.machine_live(),
                Stage::Proof(step) => !step.live(&a.account),
                _ => false,
            })
        {
            self.retire();
        }
    }
    fn reserve(&mut self, account: Account) -> Result<Uuid> {
        if self.active.is_some() {
            return Err(busy());
        }
        let receipt = Uuid::new_v4();
        self.opening_until = None;
        self.active = Some(Active {
            receipt,
            account,
            _stop: None,
            stage: Stage::Reserved {
                until: Instant::now() + RECEIPT_TTL,
            },
        });
        Ok(receipt)
    }
    fn cancel(&mut self, receipt: Uuid) -> Result<()> {
        self.expire();
        if !self.active.as_ref().is_some_and(|a| a.receipt == receipt) {
            return Err(changed());
        }
        self.retire(); // also fences a review already handed back to the shell but not yet delivered
        Ok(())
    }
    fn begin(&mut self, receipt: Uuid, account: &Account, wake: oneshot::Sender<()>) -> Result<oneshot::Receiver<()>> {
        self.expire(); // synchronous also when the native observer was suspended
        let active = self
            .active
            .as_mut()
            .filter(|a| a.receipt == receipt && &a.account == account)
            .ok_or_else(changed)?;
        if !matches!(active.stage, Stage::Reserved { .. }) {
            return Err(changed());
        }
        let (stop, stopped) = oneshot::channel();
        active._stop = Some(stop);
        active.stage = Stage::Reading {
            interrupt: Some(wake),
            idle: None,
            delivery: Delivery::Unreleased,
        };
        Ok(stopped)
    }
    fn borrow_review(
        &mut self,
        expected: Uuid,
        account: &Account,
        usage: UseReview,
        wake: oneshot::Sender<()>,
    ) -> Result<(Uuid, HostedClaim)> {
        let active = self
            .active
            .as_mut()
            .filter(|a| &a.account == account)
            .ok_or_else(changed)?;
        let Stage::Reading {
            interrupt,
            idle,
            delivery,
            ..
        } = &mut active.stage
        else {
            return Err(changed());
        };
        let claim = idle.as_ref().ok_or_else(busy)?;
        if claim.revision() != expected {
            return Err(changed());
        }
        // A released revision identifies a review, not permission for every action.
        // Reject a mismatched action before taking the helper or spending the revision.
        *delivery = match usage {
            UseReview::Edit
                if claim.can_edit()
                    && (*delivery == Delivery::Released(expected) || *delivery == Delivery::EditOnly) =>
            {
                Delivery::Unreleased
            }
            UseReview::ContinueConsent { receipt }
                if receipt == active.receipt && *delivery == Delivery::Unreleased && claim.can_confirm() =>
            {
                Delivery::Confirming(expected, claim.deadline()?)
            }
            UseReview::Confirm { receipt }
                if receipt == active.receipt && *delivery == Delivery::Released(expected) && claim.can_confirm() =>
            {
                Delivery::Confirming(expected, claim.deadline()?)
            }
            _ => return Err(changed()),
        };
        *interrupt = Some(wake);
        Ok((active.receipt, idle.take().expect("checked review")))
    }
    fn finish_read(
        &mut self,
        receipt: Uuid,
        claim: HostedClaim,
        flow: &AccountFlow,
        account: &AccountState,
    ) -> Result<(Uuid, Value)> {
        let active = self
            .active
            .as_mut()
            .filter(|a| a.receipt == receipt && Some(&a.account) == account.identity.as_ref())
            .ok_or_else(stale)?;
        let Stage::Reading {
            interrupt,
            idle,
            delivery: Delivery::Unreleased,
            ..
        } = &mut active.stage
        else {
            return Err(stale());
        };
        if idle.is_some() || interrupt.is_none() {
            return Err(stale());
        }
        let view = claim.screen(flow, account)?;
        let body = (view.revision, view.private_reply());
        *idle = Some(claim);
        interrupt.take();
        Ok(body)
    }
    fn confirming(&mut self, receipt: Uuid, revision: Uuid, account: &Account) -> Result<&mut Instant> {
        let active = self
            .active
            .as_mut()
            .filter(|a| a.receipt == receipt && &a.account == account)
            .ok_or_else(stale)?;
        match &mut active.stage {
            Stage::Reading {
                idle: None,
                delivery: Delivery::Confirming(r, until),
                ..
            } if *r == revision && Instant::now() < *until => Ok(until),
            _ => Err(stale()),
        }
    }
}
/// Non-cloneable shell owner. Reload, hide, close or replacement drops this exact lifetime.
pub struct ClaimDocument {
    owner: Arc<Mutex<Claims>>,
    epoch: Epoch,
}
impl ClaimDocument {
    pub fn caller(&self) -> Result<ClaimCaller> {
        self.epoch.check()?;
        Ok(ClaimCaller {
            owner: self.owner.clone(),
            epoch: self.epoch.clone(),
        })
    }
}
impl Drop for ClaimDocument {
    fn drop(&mut self) {
        let mut o = self.owner.lock().expect("claim owner");
        if self.epoch.check().is_ok() {
            o.retire();
        }
    }
}
/// Native admission only, not deserializable, and cannot manufacture another document.
#[derive(Clone)]
pub struct ClaimCaller {
    owner: Arc<Mutex<Claims>>,
    epoch: Epoch,
}
impl ClaimCaller {
    pub fn check(&self) -> Result<()> {
        self.epoch.check()
    }
    pub fn monitoring(&self) -> bool {
        let mut o = self.owner.lock().expect("claim owner");
        if self.check().is_err() {
            return false;
        }
        o.expire();
        o.opening_until.is_some() || o.active.is_some()
    }
    pub fn retire(&self) {
        let mut o = self.owner.lock().expect("claim owner");
        if self.check().is_ok() {
            o.retire();
        }
    }
}
/// Held natively until the shell has rechecked its physical window and document.
/// For a cancel ack the stamp is the *post*-cancel epoch. It carries no action.
pub struct ClaimReply<'a> {
    body: ReplyBody<'a>,
    account: Epoch,
    caller: ClaimCaller,
}
enum ReplyBody<'a> {
    Observation(&'a Coordinator, Box<binding_observation::BindingObservation<'a>>),
    Decision(&'a Coordinator, Box<decision::DecisionOutcome<'a>>),
    Mail(Box<proof::PendingMail<'a>>),
    StartLimited(&'a Coordinator, Box<proof::StartLimit<'a>>),
    Submission(Box<proof::SubmittedReply>),
    PasskeyRefusal(Box<proof::PasskeyRefusal<'a>>),
    Setup(Box<proof::TotpSetupReply<'a>>),
    Enrolled(Box<proof::EnrolledReply>),
    RereadFailed(Box<proof::RereadFailed<'a>>),
    ProofStatus(&'a Coordinator, Uuid, Uuid, super::claim_binding::BindingId),
    // Even a sanitized rejection can disclose account or ticket state. Keep the
    // original read receipt (not the machine) revocable through shell delivery.
    ReadFailed(ClientError, Borrow<'a>),
    Reserved(Uuid),
    Review(Uuid, Value),
    // Owner-conflict is an observation about one request credential, not a reset permit.
    Conflict(Uuid, Value, std::sync::Weak<Mutex<super::Inner>>),
    Cancelled,
}
impl ClaimReply<'_> {
    pub fn deliver(self) -> Result<Option<Value>> {
        // Never wait on an account transaction while the shell holds its document lock.
        // Read the current account before taking the claim lock (the writer's lock order).
        // These owners revalidate the receipt, account, credential and document
        // themselves. Never hold the claim mutex across their final check or Drop.
        let body = match self.body {
            ReplyBody::Observation(c, observation) => return observation.take(c).map(Some),
            ReplyBody::Decision(c, decision) => {
                return Ok(Some(match *decision {
                    decision::DecisionOutcome::NameTaken(reply) => reply.deliver(c)?,
                    decision::DecisionOutcome::Terminal(reply) => {
                        json!({"state": if reply.deliver(c)? { "bound" } else { "declined" }})
                    }
                    decision::DecisionOutcome::ProofOffered(reply) => {
                        json!({"state":"confirmation_required", "offer":reply.deliver()?.to_string()})
                    }
                }))
            }
            ReplyBody::Mail(mail) => {
                return Ok(Some(
                    json!({"state":"mail_accepted", "delivery":mail.deliver()?.to_string()}),
                ))
            }
            ReplyBody::StartLimited(c, limit) => return limit.deliver(c, &self.caller, &self.account),
            ReplyBody::PasskeyRefusal(refusal) => return refusal.deliver(),
            ReplyBody::Submission(s) => {
                return Ok(Some(
                    json!({"state":"verification_accepted", "submission":s.deliver()?.to_string()}),
                ))
            }
            ReplyBody::Setup(s) => {
                let (setup, material) = s.deliver()?;
                return Ok(Some(
                    json!({"state":"totp_setup", "setup":setup.to_string(), "manual_key":material.manual_key, "qr_payload":material.qr_payload}),
                ));
            }
            ReplyBody::Enrolled(s) => {
                use crate::instance_client::account::EnrolledCodes;
                let (id, codes) = s.deliver()?;
                let recovery = match codes {
                    EnrolledCodes::Codes(codes) => json!({"state":"issued", "codes":codes}),
                    EnrolledCodes::NotIssued => json!({"state":"not_issued"}),
                    EnrolledCodes::Unavailable => json!({"state":"unavailable"}),
                };
                return Ok(Some(
                    json!({"state":"enrollment_accepted", "submission":id.to_string(), "recovery":recovery}),
                ));
            }
            ReplyBody::ProofStatus(c, receipt, submitted, id) => {
                return c.deliver_proof_status(&self.caller, &self.account, receipt, submitted, id);
            }
            ReplyBody::RereadFailed(f) => return f.deliver(&self.account, &self.caller),
            body => body,
        };
        let current = match &body {
            ReplyBody::Conflict(_, _, inner) => Some(inner.upgrade().ok_or_else(stale)?),
            _ => None,
        };
        let live = current
            .as_ref()
            .map(|inner| inner.try_lock().map_err(|_| busy()))
            .transpose()?;
        let mut owner = self.caller.owner.lock().expect("claim owner");
        self.account.check()?;
        self.caller.check()?;
        owner.expire();
        self.caller.check()?;
        match body {
            ReplyBody::Reserved(receipt)
                if owner
                    .active
                    .as_ref()
                    .is_some_and(|a| a.receipt == receipt && matches!(a.stage, Stage::Reserved { .. })) =>
            {
                Ok(Some(json!({"receipt":receipt.to_string()})))
            }
            ReplyBody::Review(revision, value) | ReplyBody::Conflict(revision, value, _) => {
                let Some(Stage::Reading {
                    idle: Some(c),
                    delivery,
                    ..
                }) = owner.active.as_mut().map(|a| &mut a.stage)
                else {
                    return Err(stale());
                };
                if c.revision() != revision || *delivery != Delivery::Unreleased {
                    return Err(stale());
                }
                if c.is_owner_conflict() {
                    check_conflict_owner(c, live.as_deref().ok_or_else(stale)?)?;
                }
                *delivery = Delivery::Released(revision);
                Ok(Some(value))
            }
            ReplyBody::ReadFailed(error, mut read) => {
                // The owner is already locked. Consume only the original receipt
                // here rather than relocking this mutex in Borrow::drop.
                read.returned = true;
                owner.cancel(read.receipt).map_err(|_| stale())?;
                Err(error)
            }
            ReplyBody::Cancelled if owner.active.is_none() => Ok(None),
            _ => Err(stale()),
        }
    }
}
fn check_conflict_owner(claim: &HostedClaim, inner: &super::Inner) -> Result<()> {
    let account = inner.state.account.identity.as_ref().ok_or_else(stale)?;
    if inner.record.account_id.as_ref() != Some(&account.id) || inner.account.resumes.as_ref() != Some(&account.id) {
        return Err(stale());
    }
    claim.check_conflict(&inner.account, &inner.state.account)
}

struct Borrow<'a> {
    owner: &'a Mutex<Claims>,
    receipt: Uuid,
    returned: bool,
}
impl Drop for Borrow<'_> {
    fn drop(&mut self) {
        if !self.returned {
            let mut owner = self.owner.lock().expect("claim owner");
            if owner.active.as_ref().is_some_and(|a| a.receipt == self.receipt) {
                owner.retire();
            }
        }
    }
}
enum ReadWork {
    First(oneshot::Receiver<()>),
    Edit(Box<HostedClaim>, String),
}
impl Coordinator {
    /// Called under the actual view-role lock after shell admission. Never take `inner`
    /// there. The real signed-in account is checked outside the shell lock before Open replies.
    pub fn claim_document(&self, expected: u64) -> Result<ClaimDocument> {
        let mut owner = self.claims.lock().expect("claim owner");
        Epoch::new(self.generation.clone(), expected).check()?;
        owner.expire();
        if owner.opening_until.is_some() || owner.active.is_some() {
            return Err(busy());
        }
        owner.retire();
        owner.opening_until = Some(Instant::now() + RECEIPT_TTL);
        Ok(ClaimDocument {
            owner: self.claims.clone(),
            epoch: owner.epoch(),
        })
    }
    fn check_claim_admission(&self) -> Result<()> {
        if self.signing_out.load(Ordering::SeqCst)
            || self.binding.lock().expect("ordinary binding").occupied()
            || self
                .credential_io
                .try_lock()
                .map_err(|_| busy())?
                .cleanup_error
                .is_some()
        {
            return Err(busy());
        }
        Ok(())
    }
    /// No network, host or helper is available to this phase. Only an explicit Read
    /// with the returned native receipt can consume the reservation.
    fn reserve_claim(&self, caller: &ClaimCaller, account_epoch: Epoch) -> Result<ClaimReply<'static>> {
        let _operation = self.operation.try_lock().map_err(|_| busy())?;
        self.check_claim_admission()?;
        let inner = self.inner.try_lock().map_err(|_| busy())?;
        let mut owner = self.claims.lock().expect("claim owner");
        account_epoch.check()?;
        owner.expire();
        caller.check()?;
        if inner.state.account.status != AccountStatus::SignedIn || !inner.account.session.is_account() {
            return Err(auth());
        }
        let receipt = owner.reserve(inner.state.account.identity.clone().ok_or_else(auth)?)?;
        Ok(ClaimReply {
            body: ReplyBody::Reserved(receipt),
            account: account_epoch,
            caller: caller.clone(),
        })
    }
    /// Private direct replies, never `ClientState` or broadcast events. Neither
    /// a proof offer nor mail acceptance is a confirmation, grant or retry.
    pub async fn claim_review(
        self: &Arc<Self>,
        host: &RuntimeHost,
        caller: &ClaimCaller,
        locale: &str,
        r: ClaimRequest,
    ) -> Result<ClaimReply<'_>> {
        if !Arc::ptr_eq(&self.claims, &caller.owner) {
            return Err(stale());
        }
        let epoch = Epoch::new(self.generation.clone(), r.expected_generation);
        epoch.check()?;
        caller.check()?;
        match &r.action {
            ClaimAction::Open {} => return self.reserve_claim(caller, epoch),
            ClaimAction::ObserveCurrentBinding {} => {
                let obs = self.observe_current_binding(host, caller, epoch.clone()).await?;
                return Ok(ClaimReply {
                    body: ReplyBody::Observation(self, Box::new(obs)),
                    account: epoch,
                    caller: caller.clone(),
                });
            }
            ClaimAction::Confirm { receipt, revision } => {
                let decision = self
                    .confirm_claim(caller, r.expected_generation, opaque(receipt)?, opaque(revision)?)
                    .await?;
                return Ok(ClaimReply {
                    body: ReplyBody::Decision(self, Box::new(decision)),
                    account: epoch,
                    caller: caller.clone(),
                });
            }
            ClaimAction::SendProofCode { receipt, offer } => {
                let mail = self
                    .send_proof_code(caller, r.expected_generation, opaque(receipt)?, opaque(offer)?)
                    .await?;
                return Ok(ClaimReply {
                    body: ReplyBody::Mail(Box::new(mail)),
                    account: epoch,
                    caller: caller.clone(),
                });
            }
            ClaimAction::SubmitProofCode {
                receipt,
                delivery,
                code,
            } => {
                let start = self
                    .prepare_proof_code(caller, r.expected_generation, opaque(receipt)?, opaque(delivery)?, code)
                    .await?;
                let body = match start {
                    proof::StartOutcome::Ready(start) => {
                        ReplyBody::Submission(Box::new(self.forward_proof_code(*start).await?))
                    }
                    proof::StartOutcome::Limited(limit) => ReplyBody::StartLimited(self, Box::new(limit)),
                };
                return Ok(ClaimReply {
                    body,
                    account: epoch,
                    caller: caller.clone(),
                });
            }
            ClaimAction::ReviewAfterProof { receipt, submission } => {
                return self
                    .reread_after_proof(caller, r.expected_generation, opaque(receipt)?, opaque(submission)?)
                    .await;
            }
            ClaimAction::SubmitFactorCode {
                receipt,
                submission,
                method,
                code,
            } => {
                let receipt = opaque(receipt)?;
                let id = self.factor_predecessor(caller, receipt, opaque(submission)?, false)?;
                let reply = self
                    .submit_claim_factor_code(caller, r.expected_generation, receipt, id, method.clone(), code.clone())
                    .await?;
                return Ok(ClaimReply {
                    body: ReplyBody::Submission(Box::new(reply)),
                    account: epoch,
                    caller: caller.clone(),
                });
            }
            ClaimAction::SubmitFactorPasskey { receipt, submission } => {
                let receipt = opaque(receipt)?;
                let id = self.factor_predecessor(caller, receipt, opaque(submission)?, false)?;
                let body = self
                    .submit_claim_factor_passkey(caller, r.expected_generation, receipt, id)
                    .await?;
                return Ok(ClaimReply {
                    body,
                    account: epoch,
                    caller: caller.clone(),
                });
            }
            ClaimAction::PrepareTotpSetup { receipt, submission } => {
                let receipt = opaque(receipt)?;
                let id = self.factor_predecessor(caller, receipt, opaque(submission)?, false)?;
                let reply = self
                    .prepare_claim_totp_setup(caller, r.expected_generation, receipt, id)
                    .await?;
                return Ok(ClaimReply {
                    body: ReplyBody::Setup(Box::new(reply)),
                    account: epoch,
                    caller: caller.clone(),
                });
            }
            ClaimAction::FinishTotpSetup { receipt, setup, code } => {
                let receipt = opaque(receipt)?;
                let id = self.factor_predecessor(caller, receipt, opaque(setup)?, true)?;
                let reply = self
                    .finish_claim_totp_enrollment(caller, r.expected_generation, receipt, id, code.clone())
                    .await?;
                return Ok(ClaimReply {
                    body: ReplyBody::Enrolled(Box::new(reply)),
                    account: epoch,
                    caller: caller.clone(),
                });
            }
            ClaimAction::Cancel { receipt } => {
                // Before operation/inner/credential locks. The receipt stays valid for
                // cancellation while a first read, edit, or its direct reply is in flight.
                let receipt = opaque(receipt)?;
                let mut owner = self.claims.lock().expect("claim owner");
                epoch.check()?;
                caller.check()?;
                owner.cancel(receipt)?;
                return Ok(ClaimReply {
                    body: ReplyBody::Cancelled,
                    account: epoch,
                    caller: ClaimCaller {
                        owner: self.claims.clone(),
                        epoch: owner.epoch(),
                    },
                });
            }
            _ => {}
        }
        let (interrupt, mut interrupted) = oneshot::channel();
        let (mut flow, state, receipt, work) = {
            let _operation = self.operation.try_lock().map_err(|_| busy())?;
            self.check_claim_admission()?;
            let inner = self.inner.lock().expect("client state");
            let mut owner = self.claims.lock().expect("claim owner");
            epoch.check()?;
            caller.check()?;
            if inner.state.account.status != AccountStatus::SignedIn || !inner.account.session.is_account() {
                return Err(auth());
            }
            let a = inner.state.account.identity.as_ref().ok_or_else(auth)?;
            let (receipt, work) = match r.action {
                ClaimAction::Read { receipt } => {
                    let receipt = opaque(&receipt)?;
                    (receipt, ReadWork::First(owner.begin(receipt, a, interrupt)?))
                }
                ClaimAction::Check { revision, prefix } => {
                    let (receipt, claim) = owner.borrow_review(opaque(&revision)?, a, UseReview::Edit, interrupt)?;
                    (receipt, ReadWork::Edit(Box::new(claim), prefix))
                }
                _ => return Err(changed()),
            };
            (
                inner.account.for_claim(epoch.clone()),
                inner.state.account.clone(),
                receipt,
                work,
            )
        };
        let mut borrow = Borrow {
            owner: &self.claims,
            receipt,
            returned: false,
        };
        let read = async {
            let (mut claim, requested) = match work {
                ReadWork::Edit(claim, prefix) => (*claim, Some(prefix)),
                ReadWork::First(stop) => {
                    let subject = state.identity.as_ref().ok_or_else(stale)?.id.clone();
                    let m = MachineAttempt::open(host, self.transport.clone(), subject, locale, stop).await?;
                    (HostedClaim::new(m, epoch.clone(), caller.epoch.clone(), &state)?, None)
                }
            };
            let expected = claim.revision();
            claim.read(&mut flow, &state, expected, requested.as_deref()).await?;
            Ok(claim)
        };
        let read = tokio::select! {biased; _=&mut interrupted=>return Err(stale()), result=read=>result};
        match read.and_then(|claim| self.finish_claim_review(caller, epoch.clone(), claim, flow, &mut borrow, None)) {
            Ok(review) => Ok(review),
            // Pre-admission and cancellation failures are generic. A helper or
            // hosted refusal acquired inside this read goes through the same
            // physical-document and account gates as a successful review.
            Err(error) => Ok(ClaimReply {
                body: ReplyBody::ReadFailed(error, borrow),
                account: epoch,
                caller: caller.clone(),
            }),
        }
    }

    fn finish_claim_review(
        &self,
        caller: &ClaimCaller,
        epoch: Epoch,
        claim: HostedClaim,
        flow: AccountFlow,
        borrow: &mut Borrow<'_>,
        reread: Option<&proof::RereadOrigin>,
    ) -> Result<ClaimReply<'static>> {
        // No operation lock over HTTP. A concurrent ordinary transaction either owns
        // this lock or has retired the document at commit; never overwrite its jar.
        let _operation = self.operation.try_lock().map_err(|_| busy())?;
        self.check_claim_admission()?;
        let mut inner = self.inner.lock().expect("client state");
        let mut owner = self.claims.lock().expect("claim owner");
        epoch.check()?;
        caller.check()?;
        if let Some(origin) = reread {
            origin.check(
                self,
                &inner,
                &owner
                    .active
                    .as_ref()
                    .filter(|a| a.receipt == borrow.receipt)
                    .ok_or_else(stale)?
                    .account,
            )?;
        }
        let conflict = claim.is_owner_conflict();
        if conflict {
            check_conflict_owner(&claim, &inner)?;
        }
        let (revision, value) = owner.finish_read(borrow.receipt, claim, &flow, &inner.state.account)?;
        let body = if conflict {
            ReplyBody::Conflict(revision, value, Arc::downgrade(&self.inner))
        } else {
            inner.account.session = flow.session;
            ReplyBody::Review(revision, value)
        };
        borrow.returned = true;
        Ok(ClaimReply {
            body,
            account: epoch,
            caller: caller.clone(),
        })
    }
}
