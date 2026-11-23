//! Private native decision owner. Neither the grant nor a local publication notice
//! is deliverable as a successful account binding.
use super::{proof::PendingProofOffer, *};
use crate::instance_client::{
    machine::{hosted::GrantResponse, publication::ObservedOwner},
    transport::FactorCookies,
};

pub(super) enum DecisionOutcome<'a> {
    Terminal(PendingDecision),
    ProofOffered(PendingProofOffer<'a>),
    NameTaken(Box<PendingNameEdit<'a>>),
}

struct Confirming {
    caller: ClaimCaller,
    epoch: Epoch,
    receipt: Uuid,
    revision: Uuid,
    account: Account,
    cookies: FactorCookies,
    adoption: tokio::sync::watch::Receiver<bool>,
}
impl Confirming {
    fn with_owner<T>(&self, c: &Coordinator, f: impl FnOnce(&mut Claims) -> Result<T>) -> Result<T> {
        if !Arc::ptr_eq(&self.caller.owner, &c.claims) {
            return Err(stale());
        }
        let _operation = c.operation.try_lock().map_err(|_| busy())?;
        c.check_claim_admission()?;
        let inner = c.inner.try_lock().map_err(|_| busy())?;
        let mut owner = c.claims.lock().expect("claim owner");
        self.epoch.check()?;
        self.caller.check()?;
        owner.confirming(self.receipt, self.revision, &self.account)?;
        if inner.state.account.status != AccountStatus::SignedIn
            || inner.state.account.identity.as_ref() != Some(&self.account)
            || inner.record.account_id.as_ref() != Some(&self.account.id)
            || inner.account.resumes.as_ref() != Some(&self.account.id)
            || inner.account.session.factor_cookies().as_ref() != Some(&self.cookies)
        {
            return Err(stale());
        }
        if !*self.adoption.borrow() || !matches!(self.adoption.has_changed(), Ok(false)) {
            return Err(stale());
        }
        f(&mut owner)
    }
}

/// No editable revision is available until the original document receives the refusal.
/// A lost reply drops only its own unarmed helper and receipt.
#[must_use]
pub(super) struct PendingNameEdit<'a> {
    owner: Confirming,
    claim: HostedClaim,
    borrow: Borrow<'a>,
}
impl PendingNameEdit<'_> {
    pub(super) fn deliver(self, c: &Coordinator) -> Result<Value> {
        let Self {
            owner,
            claim,
            mut borrow,
        } = self;
        let revision = claim.revision();
        owner
            .with_owner(c, |claims| {
                if !claim.machine_live() {
                    return Err(stale());
                }
                claims.active.as_mut().ok_or_else(stale)?.stage = Stage::Reading {
                    idle: Some(claim),
                    interrupt: None,
                    delivery: Delivery::EditOnly,
                };
                borrow.returned = true;
                Ok(json!({"state":"name_taken", "revision":revision.to_string()}))
            })
            .map_err(|_| stale())
    }
}

/// Own the confirming receipt until direct native delivery or drop. Dropping the
/// old waiter after cancellation must not retire a new document or receipt.
#[must_use]
pub(super) struct PendingDecision {
    owner: Confirming,
    result: Result<Option<ObservedOwner>>,
}
impl PendingDecision {
    /// True means verified publication; false only means that the original worker
    /// declined. Neither a private error nor its retry hint leaves without admission.
    pub(super) fn deliver(self, c: &Coordinator) -> Result<bool> {
        self.owner
            .with_owner(c, |_| {
                Ok(match &self.result {
                    Ok(Some(observed)) => observed.check().map(|()| true),
                    Ok(None) => Ok(false),
                    Err(error) => Err(error.clone()),
                })
            })
            .map_err(|_| stale())?
    }
}
impl Drop for PendingDecision {
    fn drop(&mut self) {
        let s = &self.owner;
        let mut owner = s.caller.owner.lock().expect("claim owner");
        if s.caller.check().is_ok() && owner.active.as_ref().is_some_and(|a| a.receipt == s.receipt) {
            owner.retire();
        }
    }
}

impl Coordinator {
    /// Native callers provide only the lifetime and the handles released by this
    /// coordinator. A hosted grant is not permission for a different helper, and
    /// a returned machine outcome is not a verified cloud Owner.
    pub(super) async fn confirm_claim(
        &self,
        caller: &ClaimCaller,
        expected_generation: u64,
        receipt: Uuid,
        revision: Uuid,
    ) -> Result<DecisionOutcome<'_>> {
        self.confirm_review(caller, expected_generation, receipt, revision, None)
            .await
    }
    pub(super) async fn confirm_review(
        &self,
        caller: &ClaimCaller,
        expected_generation: u64,
        receipt: Uuid,
        revision: Uuid,
        continuation: Option<&proof::RereadOrigin>,
    ) -> Result<DecisionOutcome<'_>> {
        if !Arc::ptr_eq(&self.claims, &caller.owner) {
            return Err(stale());
        }
        let epoch = Epoch::new(self.generation.clone(), expected_generation);
        let (wake, mut interrupted) = oneshot::channel();
        let (mut claim, mut flow, state, previous) = {
            let _operation = self.operation.try_lock().map_err(|_| busy())?;
            self.check_claim_admission()?;
            let inner = self.inner.lock().expect("client state");
            let mut owner = self.claims.lock().expect("claim owner");
            epoch.check()?;
            caller.check()?;
            let a = inner.state.account.identity.as_ref().ok_or_else(auth)?;
            if inner.state.account.status != AccountStatus::SignedIn
                || !inner.account.session.is_account()
                || inner.record.account_id.as_ref() != Some(&a.id)
                || inner.account.resumes.as_ref() != Some(&a.id)
            {
                return Err(auth());
            }
            let previous = inner.account.session.factor_cookies().ok_or_else(auth)?;
            let usage = if let Some(origin) = continuation {
                origin.check(self, &inner, a)?;
                UseReview::ContinueConsent { receipt }
            } else {
                UseReview::Confirm { receipt }
            };
            let (_, claim) = owner.borrow_review(revision, a, usage, wake)?;
            (
                claim,
                inner.account.for_claim(epoch.clone()),
                inner.state.account.clone(),
                previous,
            )
        };
        // Keep the original pipe only for a qualified proof offer or a definite
        // refusal of a new name. Neither grants permission to arm or replay.
        let mut borrow = Borrow {
            owner: &self.claims,
            receipt,
            returned: false,
        };
        let mut confirming = Confirming {
            caller: caller.clone(),
            epoch: epoch.clone(),
            receipt,
            revision,
            account: state.identity.clone().ok_or_else(stale)?,
            cookies: previous,
            adoption: claim.adoption()?,
        };
        let deadline = claim.deadline()?;
        let grant = tokio::select! { biased;
            _ = &mut interrupted => return Err(stale()),
            _ = tokio::time::sleep_until(deadline) => return Err(stale()),
            response = claim.confirm(&mut flow, &state, revision) => response,
        };
        // This is an answer to the one dispatched grant, not a new confirmation.
        // Wait only for the background descriptor poll before checking the original
        // owner. Cancellation, ticket expiry and real account contention still win.
        let monitor = tokio::select! { biased;
            _ = &mut interrupted => return Err(stale()),
            _ = tokio::time::sleep_until(deadline) => return Err(stale()),
            guard = self.descriptor_refresh.lock() => guard,
        };
        let grant = match grant {
            Ok(value) => value,
            Err(error) => {
                // The published account jar did not change. Drop the private pipe,
                // but retain the confirming receipt until final delivery or expiry.
                confirming.with_owner(self, |_| Ok(())).map_err(|_| stale())?;
                borrow.returned = true;
                return Ok(DecisionOutcome::Terminal(PendingDecision {
                    owner: confirming,
                    result: Err(error),
                }));
            }
        };
        let (completion, jar) = {
            // A submission has already been dispatched. Contention never permits a replay.
            let _operation = self.operation.try_lock().map_err(|_| stale())?;
            self.check_claim_admission().map_err(|_| stale())?;
            let mut inner = self.inner.lock().expect("client state");
            let mut owner = self.claims.lock().expect("claim owner");
            epoch.check()?;
            caller.check()?;
            let a = inner.state.account.identity.as_ref().ok_or_else(stale)?;
            if inner.state.account.status != AccountStatus::SignedIn
                || !inner.account.session.is_account()
                || a != &confirming.account
            {
                return Err(stale());
            }
            if inner.account.session.factor_cookies().as_ref() != Some(&confirming.cookies)
                || inner.record.account_id.as_ref() != Some(&a.id)
                || inner.account.resumes.as_ref() != Some(&a.id)
            {
                return Err(stale());
            }
            owner.confirming(receipt, revision, a)?;
            let grant = match grant {
                GrantResponse::Matched(grant) => grant,
                GrantResponse::NewNameTaken => {
                    return Ok(DecisionOutcome::NameTaken(Box::new(PendingNameEdit {
                        owner: confirming,
                        claim,
                        borrow,
                    })));
                }
                GrantResponse::ProofRequired(proof) => {
                    let cookies = flow.session.factor_cookies().ok_or_else(stale)?;
                    owner
                        .offer_proof(receipt, revision, a, proof, cookies)
                        .map_err(|_| stale())?;
                    inner.account.session = flow.session;
                    return Ok(DecisionOutcome::ProofOffered(PendingProofOffer::new(
                        self,
                        caller.clone(),
                        epoch,
                        revision,
                        borrow,
                    )));
                }
            };
            // Synchronous, single-use enqueue under the same locks that retire
            // either epoch. The pipe driver rechecks them immediately before writing.
            let cookies = flow.session.factor_cookies().ok_or_else(stale)?;
            let completion = grant.dispatch(revision, a).map_err(|_| stale())?;
            confirming.cookies = cookies;
            let jar = flow.session.clone();
            inner.account.session = flow.session;
            (completion, jar)
        };
        drop(monitor);
        let verify = async {
            match completion.wait().await? {
                MachineOutcome::Declined => {
                    confirming.with_owner(self, |_| Ok(()))?;
                    Ok(None)
                }
                MachineOutcome::Publication(p) => {
                    confirming.with_owner(self, |_| Ok(()))?;
                    let classified = p.classify(jar, &confirming.account, &confirming.cookies).await?;
                    let ack = confirming.with_owner(self, |_| classified.dispatch())?;
                    ack.await.map(Some)
                }
            }
        };
        let observed = tokio::select! {biased;
            _ = &mut interrupted => return Err(stale()),
            _ = tokio::time::sleep_until(deadline) => return Err(stale()),
            r = verify => r,
        };
        confirming
            .with_owner(self, |claims| {
                if let Ok(Some(p)) = &observed {
                    p.check()?;
                    let until = claims.confirming(receipt, revision, &confirming.account)?;
                    *until = (*until).min(p.deadline());
                }
                Ok(())
            })
            .map_err(|_| stale())?;
        borrow.returned = true;
        Ok(DecisionOutcome::Terminal(PendingDecision {
            owner: confirming,
            result: observed,
        }))
    }
}
