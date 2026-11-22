//! Private native decision owner. Neither the grant nor a local publication notice
//! is deliverable as a successful account binding.
use super::{proof::PendingProofOffer, *};
use crate::instance_client::{
    machine::{hosted::GrantResponse, publication::ObservedOwner},
    transport::FactorCookies,
};

pub(super) enum DecisionOutcome<'a> {
    Published(ConfirmedPublication),
    Declined,
    ProofOffered(PendingProofOffer<'a>),
}

struct Confirming {
    caller: ClaimCaller,
    epoch: Epoch,
    receipt: Uuid,
    revision: Uuid,
    account: Account,
    cookies: FactorCookies,
}
impl Confirming {
    fn with_owner<T>(&self, c: &Coordinator, f: impl FnOnce(&mut Instant) -> Result<T>) -> Result<T> {
        if !Arc::ptr_eq(&self.caller.owner, &c.claims) {
            return Err(stale());
        }
        let _operation = c.operation.try_lock().map_err(|_| busy())?;
        c.check_claim_admission()?;
        let inner = c.inner.lock().expect("client state");
        let mut owner = c.claims.lock().expect("claim owner");
        self.epoch.check()?;
        self.caller.check()?;
        let until = owner.confirming(self.receipt, self.revision, &self.account)?;
        if inner.state.account.status != AccountStatus::SignedIn
            || inner.state.account.identity.as_ref() != Some(&self.account)
            || inner.record.account_id.as_ref() != Some(&self.account.id)
            || inner.account.resumes.as_ref() != Some(&self.account.id)
            || inner.account.session.factor_cookies().as_ref() != Some(&self.cookies)
        {
            return Err(stale());
        }
        f(until)
    }
}

/// Own the confirming receipt until direct native delivery or drop. Dropping the
/// old waiter after cancellation must not retire a new document or receipt.
#[must_use]
pub(super) struct ConfirmedPublication {
    owner: Confirming,
    observed: ObservedOwner,
}
impl ConfirmedPublication {
    pub(super) fn deliver(self, c: &Coordinator) -> Result<()> {
        self.owner.with_owner(c, |_| self.observed.check())
    }
}
impl Drop for ConfirmedPublication {
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
            if inner.state.account.status != AccountStatus::SignedIn || !inner.account.session.is_account() {
                return Err(auth());
            }
            let previous = inner.account.session.factor_cookies().ok_or_else(auth)?;
            let (_, claim) = owner.borrow_review(revision, a, UseReview::Confirm { receipt }, wake)?;
            (
                claim,
                inner.account.for_claim(epoch.clone()),
                inner.state.account.clone(),
                previous,
            )
        };
        // Only an exact proof challenge can return ownership to this receipt.
        // Otherwise losing the caller or the response releases the original pipe.
        let mut borrow = Borrow {
            owner: &self.claims,
            receipt,
            returned: false,
        };
        let deadline = claim.deadline()?;
        let grant = tokio::select! { biased;
            _ = &mut interrupted => return Err(stale()),
            _ = tokio::time::sleep_until(deadline) => return Err(ClientError::new("fresh_claim_required")),
            response = claim.confirm(&mut flow, &state, revision) => response?,
        };
        let (completion, confirming, jar) = {
            // A submission has already been dispatched. Contention never permits a replay.
            let _operation = self
                .operation
                .try_lock()
                .map_err(|_| ClientError::new("fresh_claim_required"))?;
            self.check_claim_admission()
                .map_err(|_| ClientError::new("fresh_claim_required"))?;
            let mut inner = self.inner.lock().expect("client state");
            let mut owner = self.claims.lock().expect("claim owner");
            epoch.check()?;
            caller.check()?;
            let a = inner.state.account.identity.as_ref().ok_or_else(auth)?;
            if inner.state.account.status != AccountStatus::SignedIn || !inner.account.session.is_account() {
                return Err(auth());
            }
            if inner.account.session.factor_cookies().as_ref() != Some(&previous)
                || inner.record.account_id.as_ref() != Some(&a.id)
                || inner.account.resumes.as_ref() != Some(&a.id)
            {
                return Err(stale());
            }
            owner.confirming(receipt, revision, a)?;
            let grant = match grant {
                GrantResponse::Matched(grant) => grant,
                GrantResponse::ProofRequired(proof) => {
                    let cookies = flow.session.factor_cookies().ok_or_else(auth)?;
                    owner.offer_proof(receipt, revision, a, proof, cookies)?;
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
            let confirming = Confirming {
                caller: caller.clone(),
                epoch: epoch.clone(),
                receipt,
                revision,
                account: a.clone(),
                cookies: flow.session.factor_cookies().ok_or_else(auth)?,
            };
            let completion = grant.dispatch(revision, a)?;
            let jar = flow.session.clone();
            inner.account.session = flow.session;
            (completion, confirming, jar)
        };
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
            r = verify => r?,
        };
        let Some(observed) = observed else {
            return Ok(DecisionOutcome::Declined);
        };
        confirming.with_owner(self, |until| {
            observed.check()?;
            *until = (*until).min(observed.deadline()); // a retained, undelivered reply cannot keep this receipt alive
            Ok(())
        })?;
        borrow.returned = true; // ownership transfers, not publication to a page
        Ok(DecisionOutcome::Published(ConfirmedPublication {
            owner: confirming,
            observed,
        }))
    }
}
