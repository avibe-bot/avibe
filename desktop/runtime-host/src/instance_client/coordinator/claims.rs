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
    Read { receipt: String },
    Check { revision: String, prefix: String },
    Cancel { receipt: String },
}
impl ClaimAction {
    pub fn opens(&self) -> bool {
        matches!(self, Self::Open {})
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
    Unreleased,
    Released(Uuid), // passed the final native delivery gate, not proof of human interaction
    Confirming(Uuid, Instant),
}
enum UseReview {
    Edit,
    Confirm { receipt: Uuid },
}
impl Claims {
    pub(super) fn retire(&mut self) {
        self.clock.fetch_add(1, Ordering::SeqCst);
        self.active.take();
    }
    fn epoch(&self) -> Epoch {
        Epoch::new(self.clock.clone(), self.clock.load(Ordering::SeqCst))
    }
    fn expire(&mut self) {
        if self.active.as_ref().is_some_and(|a| match &a.stage {
            Stage::Reserved { until } => Instant::now() >= *until,
            Stage::Reading {
                delivery: Delivery::Confirming(_, until),
                ..
            } => Instant::now() >= *until,
            Stage::Reading { idle: Some(c), .. } => !c.machine_live(),
            Stage::Proof(step) => !step.live(&a.account),
            _ => false,
        }) {
            self.retire();
        }
    }
    fn reserve(&mut self, account: Account) -> Result<Uuid> {
        if self.active.is_some() {
            return Err(busy());
        }
        let receipt = Uuid::new_v4();
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
        *delivery = match usage {
            UseReview::Edit => Delivery::Unreleased,
            UseReview::Confirm { receipt }
                if receipt == active.receipt && *delivery == Delivery::Released(expected) =>
            {
                Delivery::Confirming(expected, claim.deadline()?)
            }
            UseReview::Confirm { .. } => return Err(changed()),
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
        o.active.is_some()
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
        let current = match &self.body {
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
        match self.body {
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
        if owner.active.is_some() {
            return Err(busy());
        }
        owner.retire();
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
    /// A private direct reply, never `ClientState` or a broadcast. No grant, OTP,
    /// reset or publication action is exposed by this non-granting boundary.
    pub async fn claim_review(
        &self,
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
        match read.and_then(|claim| self.finish_claim_review(caller, epoch.clone(), claim, flow, &mut borrow)) {
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
    ) -> Result<ClaimReply<'static>> {
        // No operation lock over HTTP. A concurrent ordinary transaction either owns
        // this lock or has retired the document at commit; never overwrite its jar.
        let _operation = self.operation.try_lock().map_err(|_| busy())?;
        self.check_claim_admission()?;
        let mut inner = self.inner.lock().expect("client state");
        let mut owner = self.claims.lock().expect("claim owner");
        epoch.check()?;
        caller.check()?;
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
