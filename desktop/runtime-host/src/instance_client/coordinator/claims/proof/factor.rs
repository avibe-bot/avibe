//! A challenge response starts a NEW ordinary transaction. The sheet can
//! nominate neither the account nor its saved credential.
mod enroll;
use super::*;
use crate::instance_client::{
    account::{FactorChallenge, PreparedFactor},
    coordinator::{claim_binding::BindingSlot, Inner},
    protocol::CodeMethod,
};
pub(super) use enroll::IssuedTotp;
pub(in crate::instance_client::coordinator::claims) use enroll::{EnrolledReply, TotpSetupReply};

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
pub(in crate::instance_client::coordinator::claims) struct PasskeyRefusal<'a> {
    c: &'a Coordinator,
    owner: FactorOwner,
    borrow: Borrow<'a>,
    cookies: FactorCookies,
    error: ClientError,
}
impl PasskeyRefusal<'_> {
    pub(in crate::instance_client::coordinator::claims) fn deliver(mut self) -> Result<Option<Value>> {
        self.owner.with(self.c, At::Passkey, |inner, a, step, _| {
            if FactorChallenge::credentials(&inner.account, &inner.state.account, a)? != self.cookies {
                return Err(changed());
            }
            step.retry_until = self.error.retry_until;
            step.phase = Phase::Submitted(self.owner.predecessor);
            Ok(())
        })?;
        self.borrow.returned = true;
        Err(self.error)
    }
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
        let inner = c.inner.try_lock().map_err(|_| busy())?;
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
        let mut slot = c.binding.try_lock().map_err(|_| busy())?;
        if *p != self.predecessor
            || !slot.published_for(*p, &inner, &active.account)
            || slot.occupied()
            || inner.record.account_id.as_ref() != Some(&active.account.id)
        {
            return Err(changed());
        }
        step.proof.check(&active.account)?;
        step.check_wait()?;
        f(&inner, &active.account, step, &mut slot)
    }
    async fn forward(
        self,
        c: Arc<Coordinator>,
        at: At,
        prepare: impl FnOnce(&Inner, &Account) -> Result<PreparedFactor> + Send + 'static,
    ) -> Result<SubmittedReply> {
        let (reply, waiting) = oneshot::channel();
        tokio::spawn(async move {
            let run = async {
                let (id, epoch, factor, previous) = self.with(&c, at, |inner, a, step, slot| {
                    let factor = prepare(inner, a)?;
                    let previous = inner.account.session.factor_cookies().ok_or_else(stale)?;
                    if reply.is_closed() {
                        return Err(stale());
                    }
                    let (id, epoch) = slot.reserve_factor(inner, a, self.predecessor)?;
                    step.phase = Phase::Submitted(id);
                    // The committed predecessor, not an OS challenge or a signed assertion,
                    // is what a definite refusal can return to the same document.
                    step.retry_phase = Some(Phase::Submitted(self.predecessor));
                    Ok((id, epoch, factor, previous))
                })?;
                let result = c.finish_forward(id, factor.forward(epoch).await);
                let generation = Epoch::new(c.generation.clone(), self.generation);
                Ok(SubmittedReply::new(
                    c,
                    self.caller,
                    generation,
                    self.receipt,
                    id,
                    previous,
                    result,
                ))
            }
            .await;
            let _ = reply.send(run);
        });
        waiting.await.map_err(|_| ClientError::new("factor_delivery_unknown"))?
    }
}
impl Coordinator {
    pub(in crate::instance_client::coordinator::claims) async fn submit_claim_factor_code(
        self: &Arc<Self>,
        caller: &ClaimCaller,
        generation: u64,
        receipt: Uuid,
        predecessor: BindingId,
        method: CodeMethod,
        code: String,
    ) -> Result<SubmittedReply> {
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
    pub(in crate::instance_client::coordinator::claims) async fn submit_claim_factor_passkey(
        self: &Arc<Self>,
        caller: &ClaimCaller,
        generation: u64,
        receipt: Uuid,
        predecessor: BindingId,
    ) -> Result<ReplyBody<'_>> {
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
        let refusal = |owner, borrow, cookies, error| {
            ReplyBody::PasskeyRefusal(Box::new(PasskeyRefusal {
                c: self,
                owner,
                borrow,
                cookies,
                error,
            }))
        };
        if !self.passkeys.supported() {
            return Ok(refusal(owner, borrow, cookies, ClientError::new("passkey_unavailable")));
        }
        jar.fence(caller.epoch.clone());
        let challenge = tokio::select! {biased;
            _ = &mut interrupted => return Err(stale()),
            _ = tokio::time::sleep_until(deadline) => return Err(ClientError::new("expired")),
            r = FactorChallenge::fetch(jar, &cookies) => r,
        };
        let challenge = match challenge {
            Err(e) if e.retry_until.is_some() => return Ok(refusal(owner, borrow, cookies, e)),
            other => other?,
        };
        owner.with(self, At::Passkey, |inner, a, _, _| {
            (FactorChallenge::credentials(&inner.account, &inner.state.account, a)? == cookies)
                .then_some(())
                .ok_or_else(changed)
        })?;
        let response = tokio::select! {biased;
            _ = &mut interrupted => return Err(stale()),
            _ = tokio::time::sleep_until(deadline) => return Err(ClientError::new("expired")),
            r = challenge.assertion(self.passkeys.as_ref(), caller.epoch.clone()) => r,
        };
        let response = match response {
            Err(e)
                if matches!(
                    e.code.as_str(),
                    "passkey_cancelled" | "passkey_unavailable" | "invalid_passkey_assertion"
                ) =>
            {
                return Ok(refusal(owner, borrow, cookies, e));
            }
            other => other?,
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
        result.map(|r| ReplyBody::Submission(Box::new(r)))
    }
}
