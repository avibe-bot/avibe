//! One final POST. A definite receipt belongs to ordinary account recovery;
//! only the originating, still-live naming document owns its direct result.
use super::*;
use crate::instance_client::account::{BoundClaimSession, EnrolledCodes};

#[must_use]
pub(in crate::instance_client::coordinator) struct EnrolledReply {
    c: Arc<Coordinator>,
    owner: FactorOwner,
    id: BindingId,
    refusal: Option<Box<SubmittedReply>>,
}
impl EnrolledReply {
    fn exact<'a>(&self, claims: &'a mut Claims) -> Result<&'a mut StepUp> {
        let active = claims
            .active
            .as_mut()
            .filter(|a| a.receipt == self.owner.receipt)
            .ok_or_else(stale)?;
        let Stage::Proof(step) = &mut active.stage else {
            return Err(changed());
        };
        if !matches!(step.phase, Phase::Submitted(id) if id == self.id) {
            return Err(changed());
        }
        Ok(step)
    }
    fn with_document<T>(&self, action: impl FnOnce(&mut StepUp) -> Result<T>) -> Result<T> {
        let mut claims = self.owner.caller.owner.lock().expect("claim owner");
        Epoch::new(self.c.generation.clone(), self.owner.generation).check()?;
        claims.expire();
        self.owner.caller.check()?;
        action(self.exact(&mut claims)?)
    }
    // Called only under the original physical view-role admission. A receipt
    // contains no codes: closing the document destroys them, even if the reply
    // waits elsewhere. No ordinary I/O, lock on Inner or second chance here.
    pub(in crate::instance_client::coordinator) fn deliver(&self) -> Result<(Uuid, EnrolledCodes)> {
        if let Some(refused) = &self.refusal {
            refused.check()?;
        }
        self.with_document(|step| {
            // Transient lock contention does not consume this original receipt.
            let slot = self.c.binding.try_lock().map_err(|_| busy())?;
            if !slot.accepted(self.id) {
                return Err(stale());
            }
            let codes = step.enrolled.take().ok_or_else(changed)?;
            Ok((step.release(self.id), codes))
        })
    }
}
impl Drop for EnrolledReply {
    fn drop(&mut self) {
        let mut claims = self.owner.caller.owner.lock().expect("claim owner");
        if let Ok(step) = self.exact(&mut claims) {
            step.enrolled.take();
        }
    }
}
impl Coordinator {
    pub(in crate::instance_client::coordinator) async fn finish_claim_totp_enrollment(
        self: &Arc<Self>,
        caller: &ClaimCaller,
        generation: u64,
        receipt: Uuid,
        predecessor: BindingId,
        code: String,
    ) -> Result<EnrolledReply> {
        if code.len() != 6 || !code.bytes().all(|b| b.is_ascii_digit()) {
            return Err(ClientError::new("invalid_action"));
        }
        let owner = FactorOwner {
            caller: caller.clone(),
            generation,
            receipt,
            predecessor,
        };
        let c = self.clone();
        let (tx, rx) = oneshot::channel();
        tokio::spawn(async move {
            let result =
                async {
                    let (id, epoch, setup, flow, account, previous) =
                        owner.with(&c, At::DeliveredTotp, |inner, a, step, slot| {
                            let Phase::TotpSetup { setup, until, .. } = &step.phase else {
                                return Err(changed());
                            };
                            let previous = credentials(inner, a)?;
                            if setup.material.is_some()
                                || previous != setup.cookies
                                || Instant::now() >= *until
                                || tx.is_closed()
                            {
                                return Err(stale());
                            }
                            let (id, epoch) = slot.reserve_factor(inner, a, predecessor)?;
                            let Phase::TotpSetup { setup, .. } =
                                std::mem::replace(&mut step.phase, Phase::Submitted(id))
                            else {
                                unreachable!()
                            };
                            Ok((id, epoch, setup, Box::new(inner.account.clone()), a.clone(), previous))
                        })?;
                    let mut naming = Borrow {
                        owner: &c.claims,
                        receipt,
                        returned: false,
                    };
                    let (bound, codes) =
                        match BoundClaimSession::enroll_totp(flow, account, setup.cookies, setup.token, code, epoch)
                            .await
                        {
                            Ok((bound, codes)) => (Ok(bound), Some(codes)),
                            Err(e) => (Err(e), None),
                        };
                    let refusal = c.finish_forward(id, bound).err().map(|error| {
                        Box::new(SubmittedReply::new(
                            c.clone(),
                            owner.caller.clone(),
                            Epoch::new(c.generation.clone(), generation),
                            receipt,
                            id,
                            previous,
                            Err(error),
                        ))
                    });
                    naming.returned = true;
                    let direct = EnrolledReply {
                        c: c.clone(),
                        owner,
                        id,
                        refusal,
                    };
                    // Loss of the waiter, its document, or its separate display
                    // material never undoes acceptance or dispatches enrollment again.
                    if direct.refusal.is_none() && !tx.is_closed() {
                        let _ = direct.with_document(|step| {
                            step.enrolled = codes;
                            Ok(())
                        });
                    }
                    Ok(direct)
                }
                .await;
            let _ = tx.send(result);
        });
        rx.await.map_err(|_| ClientError::new("factor_delivery_unknown"))?
    }
}
