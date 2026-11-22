//! Explicit mail send after a hosted proof challenge. No bridge command, no
//! credential publication, and no path from this phase directly to an arm.
mod factor;
use super::super::claim_binding::BindingId;
use super::*;
use crate::instance_client::{
    account::{validate_code, PreparedClaimCode},
    machine::hosted::ProofNeeded,
    transport::FactorCookies,
};

pub(super) struct StepUp {
    proof: ProofNeeded,
    initial_credential: FactorCookies,
    correlation: Uuid,
    phase: Phase,
    // Only the original final POST's native reply can take this; retirement
    // of the document drops any still-undelivered recovery codes.
    enrolled: Option<crate::instance_client::account::EnrolledCodes>,
}
enum OfferState {
    Unreleased,
    Released,
    Sending { _interrupt: oneshot::Sender<()> },
}
enum Phase {
    Offer {
        state: OfferState,
    },
    Delivered(Box<AccountFlow>),
    Preparing {
        _interrupt: oneshot::Sender<()>,
    },
    Prepared,
    PreparingPasskey {
        predecessor: BindingId,
        _interrupt: oneshot::Sender<()>,
    },
    IssuingTotp {
        predecessor: BindingId,
        until: Instant,
        _interrupt: oneshot::Sender<()>,
    },
    TotpSetup {
        predecessor: BindingId,
        until: Instant,
        setup: Box<factor::IssuedTotp>,
    },
    Submitted(BindingId), // correlation only; never evidence the new jar was actually stored
}
/// An outstanding refusal owns the original borrow. Only final delivery to the
/// admitted document may mint a handle for a *separate* mail action.
#[must_use]
pub(super) struct PendingProofOffer<'a> {
    coordinator: &'a Coordinator,
    caller: ClaimCaller,
    epoch: Epoch,
    revision: Uuid,
    borrow: Borrow<'a>,
}
impl<'a> PendingProofOffer<'a> {
    pub(super) fn new(
        coordinator: &'a Coordinator,
        caller: ClaimCaller,
        epoch: Epoch,
        revision: Uuid,
        borrow: Borrow<'a>,
    ) -> Self {
        Self {
            coordinator,
            caller,
            epoch,
            revision,
            borrow,
        }
    }
    pub(super) fn deliver(mut self) -> Result<Uuid> {
        let revision = self.revision;
        let offer = self.coordinator.with_proof(
            &self.caller,
            &self.epoch,
            self.borrow.receipt,
            revision,
            |_, _, step| {
                let Phase::Offer { state, .. } = &mut step.phase else {
                    return Err(changed());
                };
                if !matches!(state, OfferState::Unreleased) || step.proof.refused_revision() != revision {
                    return Err(changed());
                }
                *state = OfferState::Released;
                step.correlation = Uuid::new_v4();
                Ok(step.correlation)
            },
        )?;
        self.borrow.returned = true;
        Ok(offer)
    }
}

/// No next-step handle exists while an issuer result is in transit. Dropping
/// this value spends the offer, even if the issuer already sent the message.
#[must_use]
pub(super) struct PendingMail<'a> {
    coordinator: &'a Coordinator,
    caller: ClaimCaller,
    epoch: Epoch,
    offer: Uuid,
    borrow: Borrow<'a>,
    result: Result<Box<AccountFlow>>,
}
impl PendingMail<'_> {
    pub(super) fn deliver(mut self) -> Result<Uuid> {
        let result = self.result;
        let delivery = self
            .coordinator
            .with_proof(
                &self.caller,
                &self.epoch,
                self.borrow.receipt,
                self.offer,
                |_, _, step| {
                    if !matches!(
                        step.phase,
                        Phase::Offer {
                            state: OfferState::Sending { .. }
                        }
                    ) {
                        return Err(stale());
                    }
                    Ok(result.map(|flow| {
                        step.phase = Phase::Delivered(flow);
                        step.correlation = Uuid::new_v4();
                        step.correlation
                    }))
                },
            )
            .map_err(|_| stale())??;
        self.borrow.returned = true;
        Ok(delivery)
    }
}

/// Owns a code-free start, not permission to forward. Dropping it releases this
/// receipt; the credential handoff must recheck the original owner before send.
#[must_use]
pub(super) struct ClaimStart<'a> {
    prepared: PreparedClaimCode,
    borrow: Borrow<'a>,
    caller: &'a ClaimCaller,
    epoch: Epoch,
    delivery: Uuid,
}
impl StepUp {
    pub(super) fn live(&self, account: &Account) -> bool {
        self.proof.check(account).is_ok()
            && match &self.phase {
                Phase::IssuingTotp { until, .. } | Phase::TotpSetup { until, .. } => Instant::now() < *until,
                _ => true,
            }
    }
}
impl Claims {
    // A terminal discovery denial is about this exact native attempt. It must
    // release its unarmed pipe without cancelling a later sheet or account.
    pub(in crate::instance_client::coordinator) fn retire_submission(&mut self, id: BindingId) {
        if self.active.as_ref().is_some_and(|a| {
            matches!(&a.stage,
            Stage::Proof(StepUp { phase: Phase::Submitted(current), .. }) if *current == id)
        }) {
            self.retire();
        }
    }
    pub(super) fn offer_proof(
        &mut self,
        receipt: Uuid,
        revision: Uuid,
        account: &Account,
        proof: ProofNeeded,
        cookies: FactorCookies,
    ) -> Result<()> {
        let a = self
            .active
            .as_mut()
            .filter(|a| a.receipt == receipt && &a.account == account)
            .ok_or_else(stale)?;
        if proof.refused_revision() != revision
            || !matches!(&a.stage, Stage::Reading { idle: None, delivery: Delivery::Confirming(r, _), .. } if *r == revision)
        {
            return Err(changed());
        }
        proof.check(account)?;
        a.stage = Stage::Proof(StepUp {
            proof,
            initial_credential: cookies,
            correlation: revision,
            phase: Phase::Offer {
                state: OfferState::Unreleased,
            },
            enrolled: None,
        });
        Ok(())
    }
}
impl Coordinator {
    // All transitions share the retirement lock, but none waits there for HTTP.
    fn with_proof<T>(
        &self,
        caller: &ClaimCaller,
        epoch: &Epoch,
        receipt: Uuid,
        correlation: Uuid,
        f: impl FnOnce(&super::super::Inner, &Account, &mut StepUp) -> Result<T>,
    ) -> Result<T> {
        if !Arc::ptr_eq(&self.claims, &caller.owner) {
            return Err(stale());
        }
        let _operation = self.operation.try_lock().map_err(|_| busy())?;
        self.check_claim_admission()?;
        let inner = self.inner.try_lock().map_err(|_| busy())?;
        let mut owner = self.claims.lock().expect("claim owner");
        epoch.check()?;
        caller.check()?;
        owner.expire();
        caller.check()?;
        let a = inner
            .state
            .account
            .identity
            .as_ref()
            .filter(|_| inner.state.account.status == AccountStatus::SignedIn && inner.account.session.is_account())
            .ok_or_else(auth)?;
        let active = owner
            .active
            .as_mut()
            .filter(|active| active.receipt == receipt && &active.account == a)
            .ok_or_else(stale)?;
        let Stage::Proof(step) = &mut active.stage else {
            return Err(changed());
        };
        if step.correlation != correlation {
            return Err(changed());
        }
        step.proof.check(a)?;
        // This gate is only for the initial signed-in account, up to handoff
        // to the ordinary owner. The refusal's own refresh is already included.
        if inner.record.account_id.as_ref() != Some(&a.id)
            || inner.account.resumes.as_ref() != Some(&a.id)
            || inner.account.session.factor_cookies().as_ref() != Some(&step.initial_credential)
        {
            return Err(stale());
        }
        f(&inner, a, step)
    }
    /// Take this delivery once. The first HTTP request contains only the frozen
    /// subject and mailbox; even a response racing cancellation cannot send the code.
    pub(super) async fn prepare_proof_code<'a>(
        &'a self,
        caller: &'a ClaimCaller,
        generation: u64,
        receipt: Uuid,
        delivery: Uuid,
        code: &str,
    ) -> Result<ClaimStart<'a>> {
        let epoch = Epoch::new(self.generation.clone(), generation);
        let (wake, mut interrupted) = oneshot::channel();
        let (flow, account, deadline) = self.with_proof(caller, &epoch, receipt, delivery, |_, account, step| {
            if !matches!(step.phase, Phase::Delivered(_)) {
                return Err(changed());
            }
            validate_code(code)?;
            let Phase::Delivered(flow) = std::mem::replace(&mut step.phase, Phase::Preparing { _interrupt: wake })
            else {
                unreachable!()
            };
            Ok((flow, account.clone(), step.proof.deadline()))
        })?;
        let borrow = Borrow {
            owner: &self.claims,
            receipt,
            returned: false,
        };
        let prepared = tokio::select! { biased;
            _ = &mut interrupted => return Err(stale()),
            _ = tokio::time::sleep_until(deadline) => return Err(ClientError::new("expired")),
            r = flow.prepare_claim_code(&account, code) => r?,
        };
        self.with_proof(caller, &epoch, receipt, delivery, |_, _, step| {
            if !matches!(step.phase, Phase::Preparing { .. }) {
                return Err(stale());
            }
            step.phase = Phase::Prepared;
            Ok(())
        })?;
        Ok(ClaimStart {
            prepared,
            borrow,
            caller,
            epoch,
            delivery,
        })
    }
    /// The waiting document owns cancellation up to the final admission in the worker.
    /// Once admitted, dropping that waiter may retire the sheet, not the account task.
    pub(super) async fn forward_proof_code(
        self: &Arc<Self>,
        start: ClaimStart<'_>,
    ) -> Result<super::super::claim_binding::BindingId> {
        let ClaimStart {
            mut prepared,
            mut borrow,
            caller,
            epoch,
            delivery,
        } = start;
        let receipt = borrow.receipt;
        let caller = (*caller).clone();
        let ordinary = self.clone();
        let result = tokio::spawn(async move {
            let id = ordinary.with_proof(&caller, &epoch, receipt, delivery, |inner, a, step| {
                if !matches!(step.phase, Phase::Prepared) {
                    return Err(changed());
                }
                let id = ordinary
                    .binding
                    .lock()
                    .expect("ordinary binding")
                    .reserve(inner, a, &mut prepared)?;
                step.phase = Phase::Submitted(id);
                Ok(id)
            })?;
            ordinary.accept_forward(id, prepared).await?;
            Ok(id)
        })
        .await
        .map_err(|_| ClientError::new("code_delivery_unknown"))?;
        borrow.returned = result.is_ok();
        result
    }
    /// No machine open, grant or OTP. Only an exact ordinary commit receipt from
    /// this delivery can release the original unarmed ticket for another review.
    pub(super) async fn reread_after_proof(
        &self,
        caller: &ClaimCaller,
        generation: u64,
        receipt: Uuid,
    ) -> Result<ClaimReply<'static>> {
        if !Arc::ptr_eq(&self.claims, &caller.owner) {
            return Err(stale());
        }
        let epoch = Epoch::new(self.generation.clone(), generation);
        let (wake, mut interrupted) = oneshot::channel();
        let (proof, mut flow, state) = {
            let _operation = self.operation.try_lock().map_err(|_| busy())?;
            self.check_claim_admission()?;
            let inner = self.inner.lock().expect("client state");
            let mut owner = self.claims.lock().expect("claim owner");
            epoch.check()?;
            owner.expire();
            caller.check()?;
            let active = owner
                .active
                .as_mut()
                .filter(|a| a.receipt == receipt)
                .ok_or_else(stale)?;
            let account = &active.account;
            if inner.state.account.status != AccountStatus::SignedIn
                || inner.state.account.identity.as_ref() != Some(account)
                || inner.record.account_id.as_ref() != Some(&account.id)
                || inner.account.resumes.as_ref() != Some(&account.id)
                || !inner.account.session.is_account()
            {
                return Err(auth());
            }
            let Stage::Proof(step) = &active.stage else {
                return Err(changed());
            };
            let Phase::Submitted(id) = &step.phase else {
                return Err(changed());
            };
            if self.binding.lock().expect("ordinary binding").committed() != Some(*id) {
                return Err(changed());
            }
            step.proof.check(account)?;
            let Stage::Proof(step) = std::mem::replace(
                &mut active.stage,
                Stage::Reading {
                    interrupt: Some(wake),
                    idle: None,
                    delivery: Delivery::Unreleased,
                },
            ) else {
                unreachable!()
            };
            (
                step.proof,
                inner.account.for_claim(epoch.clone()),
                inner.state.account.clone(),
            )
        };
        let mut borrow = Borrow {
            owner: &self.claims,
            receipt,
            returned: false,
        };
        let deadline = proof.deadline();
        let claim = tokio::select! { biased;
            _ = &mut interrupted => return Err(stale()),
            _ = tokio::time::sleep_until(deadline) => return Err(ClientError::new("expired")),
            result = proof.reread(&mut flow, &state) => result?,
        };
        self.finish_claim_review(caller, epoch, claim, flow, &mut borrow)
    }

    /// Native only. There is no parameter for an email, subject, target or URL.
    /// The offer is single use even if the issuer's response is lost.
    pub(super) async fn send_proof_code(
        &self,
        caller: &ClaimCaller,
        expected_generation: u64,
        receipt: Uuid,
        offer: Uuid,
    ) -> Result<PendingMail<'_>> {
        let epoch = Epoch::new(self.generation.clone(), expected_generation);
        let (wake, mut interrupted) = oneshot::channel();
        let (account, deadline) = self.with_proof(caller, &epoch, receipt, offer, |_, a, step| {
            let Phase::Offer { state, .. } = &mut step.phase else {
                return Err(changed());
            };
            if !matches!(state, OfferState::Released) {
                return Err(changed());
            }
            *state = OfferState::Sending { _interrupt: wake };
            Ok((a.clone(), step.proof.deadline()))
        })?;
        let borrow = Borrow {
            owner: &self.claims,
            receipt,
            returned: false,
        };
        let mut flow = AccountFlow::new(self.transport.clone(), self.passkeys.supported());
        flow.fence(epoch.clone());
        let before_mail = || {
            self.with_proof(caller, &epoch, receipt, offer, |_, _, step| {
                if matches!(
                    step.phase,
                    Phase::Offer {
                        state: OfferState::Sending { .. },
                        ..
                    }
                ) {
                    Ok(())
                } else {
                    Err(stale())
                }
            })
        };
        let result = tokio::select! { biased;
            _ = &mut interrupted => return Err(stale()),
            _ = tokio::time::sleep_until(deadline) => return Err(stale()),
            sent = flow.send_claim_code(&account, caller.epoch.clone(), before_mail) => sent,
        };
        Ok(PendingMail {
            coordinator: self,
            caller: caller.clone(),
            epoch,
            offer,
            borrow,
            result: result.map(|()| Box::new(flow)),
        })
    }
}
