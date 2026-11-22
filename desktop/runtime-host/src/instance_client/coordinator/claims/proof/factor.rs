//! A challenge response starts a NEW ordinary transaction. The sheet can
//! nominate neither the account nor its saved credential. No JS entry point.
mod enroll;
use super::*;
use crate::instance_client::{
    account::{FactorChallenge, PreparedFactor},
    coordinator::{claim_binding::BindingSlot, Inner},
    protocol::CodeMethod,
};
pub(super) use enroll::IssuedTotp;

#[derive(Clone, Copy)]
enum At {
    Submitted,
    Passkey,
    IssuingTotp,
    DeliveredTotp,
}

#[derive(Clone)]
struct FactorOwner {
    caller: ClaimCaller,
    generation: u64,
    receipt: Uuid,
    predecessor: BindingId,
}
impl FactorOwner {
    fn with<T>(
        &self,
        c: &Coordinator,
        at: At,
        f: impl FnOnce(&Inner, &Account, &mut StepUp, &mut BindingSlot) -> Result<T>,
    ) -> Result<T> {
        if !Arc::ptr_eq(&c.claims, &self.caller.owner) {
            return Err(stale());
        }
        let _operation = c.operation.try_lock().map_err(|_| busy())?;
        c.check_claim_admission()?;
        let inner = c.inner.lock().expect("client state");
        let mut owner = c.claims.lock().expect("claim owner");
        Epoch::new(c.generation.clone(), self.generation).check()?;
        owner.expire();
        self.caller.check()?;
        let active = owner
            .active
            .as_mut()
            .filter(|a| a.receipt == self.receipt)
            .ok_or_else(stale)?;
        let Stage::Proof(step) = &mut active.stage else {
            return Err(changed());
        };
        let p = match (at, &step.phase) {
            (At::Submitted, Phase::Submitted(id)) => id,
            (At::Passkey, Phase::PreparingPasskey { predecessor, .. }) => predecessor,
            (At::IssuingTotp, Phase::IssuingTotp { predecessor, .. }) => predecessor,
            (At::DeliveredTotp, Phase::TotpSetup { predecessor, .. }) => predecessor,
            _ => return Err(changed()),
        };
        let mut slot = c.binding.lock().expect("ordinary binding");
        if *p != self.predecessor
            || slot.committed() != Some(*p)
            || slot.occupied()
            || inner.record.account_id.as_ref() != Some(&active.account.id)
        {
            return Err(changed());
        }
        step.proof.check(&active.account)?;
        f(&inner, &active.account, step, &mut slot)
    }
    async fn forward(
        self,
        c: Arc<Coordinator>,
        at: At,
        prepare: impl FnOnce(&Inner, &Account) -> Result<PreparedFactor> + Send + 'static,
    ) -> Result<()> {
        let (reply, waiting) = oneshot::channel();
        tokio::spawn(async move {
            let run = async {
                let (id, epoch, factor) = self.with(&c, at, |inner, a, step, slot| {
                    let factor = prepare(inner, a)?;
                    if reply.is_closed() {
                        return Err(stale());
                    }
                    let (id, epoch) = slot.reserve_factor(inner, a, self.predecessor)?;
                    step.phase = Phase::Submitted(id);
                    Ok((id, epoch, factor))
                })?;
                let mut naming = Borrow {
                    owner: &c.claims,
                    receipt: self.receipt,
                    returned: false,
                };
                let result = c.finish_forward(id, factor.forward(epoch).await);
                naming.returned = result.is_ok();
                result
            }
            .await;
            let _ = reply.send(run);
        });
        waiting.await.map_err(|_| ClientError::new("factor_delivery_unknown"))?
    }
}
impl Coordinator {
    pub(in crate::instance_client::coordinator) async fn submit_claim_factor_code(
        self: &Arc<Self>,
        caller: &ClaimCaller,
        generation: u64,
        receipt: Uuid,
        predecessor: BindingId,
        method: CodeMethod,
        code: String,
    ) -> Result<()> {
        let owner = FactorOwner {
            caller: caller.clone(),
            generation,
            receipt,
            predecessor,
        };
        owner
            .forward(self.clone(), At::Submitted, move |inner, a| {
                PreparedFactor::code(&inner.account, &inner.state.account, a, method, code)
            })
            .await
    }
    pub(in crate::instance_client::coordinator) async fn submit_claim_factor_passkey(
        self: &Arc<Self>,
        caller: &ClaimCaller,
        generation: u64,
        receipt: Uuid,
        predecessor: BindingId,
    ) -> Result<()> {
        if !self.passkeys.supported() {
            return Err(ClientError::new("passkey_unavailable"));
        }
        let owner = FactorOwner {
            caller: caller.clone(),
            generation,
            receipt,
            predecessor,
        };
        let (wake, mut interrupted) = oneshot::channel();
        let (mut jar, cookies, deadline) = owner.with(self, At::Submitted, |inner, a, step, _| {
            let cookies = FactorChallenge::credentials(&inner.account, &inner.state.account, a)?;
            step.phase = Phase::PreparingPasskey {
                predecessor,
                _interrupt: wake,
            };
            Ok((inner.account.session.clone(), cookies, step.proof.deadline()))
        })?;
        let mut borrow = Borrow {
            owner: &self.claims,
            receipt,
            returned: false,
        };
        jar.fence(caller.epoch.clone());
        let challenge = tokio::select! {biased;
            _ = &mut interrupted => return Err(stale()),
            _ = tokio::time::sleep_until(deadline) => return Err(ClientError::new("expired")),
            r = FactorChallenge::fetch(jar, &cookies) => r?,
        };
        owner.with(self, At::Passkey, |inner, a, _, _| {
            (FactorChallenge::credentials(&inner.account, &inner.state.account, a)? == cookies)
                .then_some(())
                .ok_or_else(changed)
        })?;
        let response = tokio::select! {biased;
            _ = &mut interrupted => return Err(stale()),
            _ = tokio::time::sleep_until(deadline) => return Err(ClientError::new("expired")),
            r = challenge.assertion(self.passkeys.as_ref(), caller.epoch.clone()) => r?,
        };
        // No detached preparation. Only the ordinary worker may cross verify;
        // recheck in that worker, not merely when the OS returned an assertion.
        let result = owner
            .forward(self.clone(), At::Passkey, move |inner, a| {
                if FactorChallenge::credentials(&inner.account, &inner.state.account, a)? != cookies {
                    return Err(changed());
                }
                Ok(challenge.prepare(&inner.account, a, cookies, response))
            })
            .await;
        borrow.returned = result.is_ok();
        result
    }
}
