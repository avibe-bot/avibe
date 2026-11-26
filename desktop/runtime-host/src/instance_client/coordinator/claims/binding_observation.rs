//! A read of the existing binding is not a naming receipt or permission to change remote access.
use super::*;
use crate::instance_client::{
    machine::bound::{self, CurrentBinding, LegacyPending},
    transport::{FactorCookies, NativeSession},
};
use tokio::sync::watch;

struct Owner {
    account: Account,
    cookies: FactorCookies,
    caller: ClaimCaller,
    epoch: Epoch,
    receipt: Uuid,
    adoption: watch::Receiver<bool>,
    until: Instant,
}
impl Owner {
    fn check(&self, c: &Coordinator) -> Result<()> {
        c.with_observation_account(&self.caller, &self.epoch, |claims, account, jar| {
            if !claims.active.as_ref().is_some_and(|a| {
                a.receipt == self.receipt
                    && a.account == self.account
                    && matches!(a.stage, Stage::Observing { until } if until == self.until)
            }) || Instant::now() >= self.until
                || !*self.adoption.borrow()
                || !matches!(self.adoption.has_changed(), Ok(false))
                || account != &self.account
                || jar.factor_cookies().as_ref() != Some(&self.cookies)
            {
                return Err(stale());
            }
            Ok(())
        })
    }
}

/// Holds the original observation through physical delivery, also on an admitted
/// helper or hosted error. Dropping it releases only this observation.
#[must_use]
pub(super) struct BindingObservation<'a> {
    owner: Owner,
    _borrow: Borrow<'a>,
    classification: Result<CurrentBinding>,
}
impl BindingObservation<'_> {
    pub(super) fn take(self, c: &Coordinator) -> Result<Value> {
        self.owner.check(c).map_err(|_| stale())?;
        let state = match self.classification? {
            CurrentBinding::NoPublishedBinding => "no_published_binding",
            CurrentBinding::OwnerObserved => "owner_observed",
            CurrentBinding::BoundOwnerNotProven => "bound_owner_unconfirmed",
            CurrentBinding::LegacyPending(pending) => match pending {
                LegacyPending::Wait => "legacy_wait",
                LegacyPending::Publishing => "legacy_publication_pending",
                LegacyPending::NoRequest | LegacyPending::Ended | LegacyPending::Expired => "legacy_review_required",
                LegacyPending::Incompatible => "legacy_recovery_required",
            },
        };
        Ok(json!({"state":state}))
    }
}
impl Coordinator {
    fn with_observation_account<T>(
        &self,
        caller: &ClaimCaller,
        epoch: &Epoch,
        f: impl FnOnce(&mut Claims, &Account, &NativeSession) -> Result<T>,
    ) -> Result<T> {
        if !Arc::ptr_eq(&self.claims, &caller.owner) {
            return Err(stale());
        }
        let _operation = self.operation.claim.try_lock().map_err(|_| busy())?;
        self.check_claim_admission()?;
        let inner = self.inner.try_lock().map_err(|_| busy())?;
        let mut claims = self.claims.lock().expect("claim owner");
        epoch.check()?;
        claims.expire();
        caller.check()?;
        let a = inner.state.account.identity.as_ref().ok_or_else(auth)?;
        if inner.state.account.status != AccountStatus::SignedIn
            || inner.record.account_id.as_ref() != Some(&a.id)
            || inner.account.resumes.as_ref() != Some(&a.id)
            || !inner.account.session.has_discovery_credentials()
        {
            return Err(auth());
        }
        f(&mut claims, a, &inner.account.session)
    }
    pub(super) async fn observe_current_binding(
        &self,
        host: &RuntimeHost,
        caller: &ClaimCaller,
        epoch: Epoch,
    ) -> Result<BindingObservation<'_>> {
        let source = host
            .live_claim_source()
            .await
            .ok_or_else(|| ClientError::new("local_runtime_upgrade_required"))?;
        let until = Instant::now() + Duration::from_secs(60);
        let (stop, mut stopped) = oneshot::channel();
        let (receipt, account, cookies, mut jar) =
            self.with_observation_account(caller, &epoch, |claims, a, jar| {
                if claims.active.is_some() {
                    return Err(busy());
                }
                if claims.opening_until.is_none() {
                    return Err(stale());
                }
                let cookies = jar.factor_cookies().ok_or_else(auth)?;
                let receipt = Uuid::new_v4();
                claims.opening_until = None;
                claims.active = Some(Active {
                    receipt,
                    account: a.clone(),
                    stage: Stage::Observing { until },
                    _stop: Some(stop),
                });
                Ok((receipt, a.clone(), cookies, jar.clone()))
            })?;
        let borrow = Borrow {
            owner: &self.claims,
            receipt,
            returned: false,
        };
        jar.fence(epoch.clone());
        let owner = Owner {
            account,
            cookies,
            caller: caller.clone(),
            epoch,
            receipt,
            adoption: source.live.clone(),
            until,
        };
        let classification = tokio::select! { biased;
            _ = &mut stopped => return Err(stale()),
            _ = tokio::time::sleep_until(until) => return Err(stale()),
            result = bound::observe(source,self.transport.clone(),&owner.account,jar,&owner.cookies,|| owner.check(self)) => result,
        };
        Ok(BindingObservation {
            owner,
            _borrow: borrow,
            classification,
        })
    }
}
