//! The private account owner can observe a current binding, never adopt it from this result.
use super::*;
use crate::instance_client::{
    machine::bound::{self, CurrentBinding},
    transport::{FactorCookies, NativeSession},
};
use tokio::sync::watch;

struct Owner {
    account: Account,
    cookies: FactorCookies,
    caller: ClaimCaller,
    epoch: Epoch,
    adoption: watch::Receiver<bool>,
    until: Instant,
}
impl Owner {
    fn check(&self, c: &Coordinator) -> Result<()> {
        if Instant::now() >= self.until || !*self.adoption.borrow() || !matches!(self.adoption.has_changed(), Ok(false))
        {
            return Err(stale());
        }
        c.with_observation_account(&self.caller, &self.epoch, |account, jar| {
            if account != &self.account || jar.factor_cookies().as_ref() != Some(&self.cookies) {
                return Err(stale());
            }
            Ok(())
        })
    }
}

/// Native-only, single-consumption snapshot. Even a delayed native receiver must
/// check that the original account, document, request credential and adoption survive.
#[must_use]
pub(crate) struct BindingObservation {
    owner: Owner,
    classification: CurrentBinding,
}
impl BindingObservation {
    pub(crate) fn take(self, c: &Coordinator) -> Result<CurrentBinding> {
        self.owner.check(c)?;
        Ok(self.classification)
    }
}
impl Coordinator {
    fn with_observation_account<T>(
        &self,
        caller: &ClaimCaller,
        epoch: &Epoch,
        f: impl FnOnce(&Account, &NativeSession) -> Result<T>,
    ) -> Result<T> {
        if !Arc::ptr_eq(&self.claims, &caller.owner) {
            return Err(stale());
        }
        let _operation = self.operation.try_lock().map_err(|_| busy())?;
        self.check_claim_admission()?;
        let inner = self.inner.lock().expect("client state");
        let owner = self.claims.lock().expect("claim owner");
        epoch.check()?;
        caller.check()?;
        if owner.active.is_some() {
            return Err(busy());
        }
        let a = inner.state.account.identity.as_ref().ok_or_else(auth)?;
        if inner.state.account.status != AccountStatus::SignedIn
            || inner.record.account_id.as_ref() != Some(&a.id)
            || inner.account.resumes.as_ref() != Some(&a.id)
            || !inner.account.session.has_discovery_credentials()
        {
            return Err(auth());
        }
        f(a, &inner.account.session)
    }
    /// No bridge command or public state field. This says nothing about which claim
    /// published this binding. A named operation needs a separate matching receipt.
    pub(crate) async fn observe_current_binding(
        &self,
        host: &RuntimeHost,
        caller: &ClaimCaller,
        expected: u64,
    ) -> Result<BindingObservation> {
        let epoch = Epoch::new(self.generation.clone(), expected);
        let (account, cookies, mut jar) = self.with_observation_account(caller, &epoch, |a, jar| {
            Ok((a.clone(), jar.factor_cookies().ok_or_else(auth)?, jar.clone()))
        })?;
        let source = host
            .claim_source()
            .ok_or_else(|| ClientError::new("local_runtime_upgrade_required"))?;
        jar.fence(epoch.clone());
        let owner = Owner {
            account,
            cookies,
            caller: caller.clone(),
            epoch,
            adoption: source.live.clone(),
            until: Instant::now() + Duration::from_secs(60),
        };
        let classification = bound::observe(
            source,
            self.transport.clone(),
            &owner.account,
            jar,
            &owner.cookies,
            || owner.check(self),
        )
        .await?;
        owner.check(self)?;
        Ok(BindingObservation { owner, classification })
    }
}
