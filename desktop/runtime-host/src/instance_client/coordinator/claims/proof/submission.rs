//! A response to an already dispatched verification is not a second permission
//! to verify. The ordinary worker settles independently; this receipt only
//! qualifies a native result for the submitting naming document.
use super::*;
use crate::instance_client::account::BindingBase;

#[must_use]
pub(in crate::instance_client::coordinator::claims) struct SubmittedReply {
    coordinator: Arc<Coordinator>,
    caller: ClaimCaller,
    epoch: Epoch,
    receipt: Uuid,
    id: BindingId,
    previous: FactorCookies,
    result: Result<()>,
}
impl SubmittedReply {
    pub(super) fn new(
        coordinator: Arc<Coordinator>,
        caller: ClaimCaller,
        epoch: Epoch,
        receipt: Uuid,
        id: BindingId,
        previous: FactorCookies,
        result: Result<()>,
    ) -> Self {
        Self {
            coordinator,
            caller,
            epoch,
            receipt,
            id,
            previous,
            result,
        }
    }
    pub(in crate::instance_client::coordinator::claims) fn deliver(self) -> Result<Uuid> {
        self.with_result(|step| Ok(step.release(self.id)))
    }
    pub(super) fn check(&self) -> Result<()> {
        self.with_result(|_| Ok(()))
    }
    fn with_result<T>(&self, release: impl FnOnce(&mut StepUp) -> Result<T>) -> Result<T> {
        let c = &self.coordinator;
        // Never wait on account discovery, credential I/O or OS authentication
        // with the shell's physical document lock held.
        let inner = c.inner.try_lock().map_err(|_| stale())?;
        let mut claims = self.caller.owner.lock().expect("claim owner");
        self.epoch.check()?;
        claims.expire();
        self.caller.check()?;
        let active = claims
            .active
            .as_mut()
            .filter(|a| a.receipt == self.receipt)
            .ok_or_else(stale)?;
        let Stage::Proof(step) = &mut active.stage else {
            return Err(stale());
        };
        if !matches!(step.phase, Phase::Submitted(id) if id == self.id) {
            return Err(stale());
        }
        let slot = c.binding.try_lock().map_err(|_| stale())?;
        // Publication changes the ordinary jar by design. Only the positive
        // receipt of this very transaction may witness that replacement.
        if self.result.is_ok() && slot.published_for(self.id, &inner, &active.account) {
            return release(step);
        }
        let original = inner.record.account_id.as_ref() == Some(&active.account.id)
            && [BindingBase::SignedIn, BindingBase::OwedFactor]
                .into_iter()
                .any(|base| base.matches(&active.account, &inner.account, &inner.state.account))
            && inner.account.session.factor_cookies().as_ref() == Some(&self.previous);
        let matching = if self.result.is_ok() {
            slot.accepted(self.id) && slot.committed() != Some(self.id)
        } else {
            slot.rejected(self.id)
        };
        if !original || !matching {
            return Err(stale());
        }
        self.result.clone()?;
        release(step)
    }
}
impl Drop for SubmittedReply {
    fn drop(&mut self) {
        if self.result.is_err() {
            // A rejected submission cannot be re-used. Do not invalidate a
            // later submission even if it has the same receipt or subject.
            self.caller
                .owner
                .lock()
                .expect("claim owner")
                .retire_submission(self.id);
        }
    }
}
