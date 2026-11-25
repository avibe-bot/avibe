//! The ordinary account owns a dispatched first factor, not the naming sheet.
//! This slot never enters a working Inner, a record, or a bridge projection.
use super::*;
use crate::instance_client::{
    account::{BindingBase, BoundClaimSession, DiscoveredSession, PreparedClaimCode},
    transport::FactorCookies,
};
use uuid::Uuid;

mod recovery;
pub use recovery::{AccountRecoveryRequest, RecoveryDelivery, RecoveryWork};

#[derive(Clone, Copy, PartialEq, Eq)]
pub(super) struct BindingId(Uuid);
#[derive(Default)]
pub(super) struct BindingSlot {
    pending: Option<Pending>,
    // Positive receipt of key readback AND account publication. Never restored.
    committed: Option<(BindingId, FactorCookies)>,
    // Only to qualify a pending private refusal; grants no recovery or replay.
    rejected: Option<BindingId>,
}
struct Pending {
    id: BindingId,
    account: Account,
    base: BindingBase,
    original: FactorCookies,
    automatic: bool,
    clock: Arc<AtomicU64>,
    phase: Phase,
    // The first read/save belongs to the ordinary worker. An explicit retry
    // must not race that first pass or silently inherit another transaction.
    retry: Option<Uuid>,
}
impl Drop for Pending {
    fn drop(&mut self) {
        self.clock.store(0, Ordering::SeqCst);
    }
}
enum Phase {
    Forwarding,
    Bound {
        session: Arc<BoundClaimSession>,
        reader: Arc<tokio::sync::Mutex<()>>,
    },
    Discovered(Arc<DiscoveredSession>),
}
// Recoverable only by the ordinary native owner. Freeze the stage as well as
// the transaction: a queued read may not turn into a write on a successor.
#[derive(Clone, Copy)]
enum Recovery {
    Read(BindingId),
    Save(BindingId),
}
fn unavailable() -> ClientError {
    ClientError::new("credentials_unavailable")
}
fn stale() -> ClientError {
    ClientError::new("stale_generation")
}
fn busy() -> ClientError {
    ClientError::new("claim_in_progress")
}
fn selected(inner: &Inner, a: &Account, base: BindingBase) -> bool {
    inner.record.account_id.as_ref() == Some(&a.id) && base.matches(a, &inner.account, &inner.state.account)
}
impl BindingSlot {
    pub(super) fn retire(&mut self) {
        self.pending.take();
        self.committed = None;
        self.rejected = None;
    }
    pub(super) fn occupied(&self) -> bool {
        self.pending.is_some()
    }
    pub(super) fn accepted(&self, id: BindingId) -> bool {
        self.committed() == Some(id)
            || self
                .pending
                .as_ref()
                .is_some_and(|p| p.id == id && !matches!(p.phase, Phase::Forwarding))
    }
    pub(super) fn publishing_for(&self, id: BindingId, inner: &Inner, account: &Account) -> bool {
        self.pending.as_ref().is_some_and(|p| {
            p.id == id
                && &p.account == account
                && p.automatic
                && !matches!(p.phase, Phase::Forwarding)
                && selected(inner, account, p.base)
                && inner.account.session.factor_cookies().as_ref() == Some(&p.original)
        })
    }
    pub(super) fn committed(&self) -> Option<BindingId> {
        self.committed.as_ref().map(|(id, _)| *id)
    }
    pub(super) fn published_for(&self, id: BindingId, inner: &Inner, account: &Account) -> bool {
        self.committed.as_ref().is_some_and(|(current, cookies)| {
            *current == id
                && inner.account.session.factor_cookies().as_ref() == Some(cookies)
                && [BindingBase::SignedIn, BindingBase::OwedFactor]
                    .into_iter()
                    .any(|base| selected(inner, account, base))
        })
    }
    pub(super) fn rejected(&self, id: BindingId) -> bool {
        self.rejected == Some(id)
    }
    pub(super) fn reserve(
        &mut self,
        inner: &Inner,
        account: &Account,
        code: &mut PreparedClaimCode,
    ) -> Result<BindingId, ClientError> {
        let (id, epoch) = self.start(inner, account, BindingBase::SignedIn)?;
        code.fence(epoch);
        Ok(id)
    }
    pub(in crate::instance_client) fn restore_factor_predecessor(
        &mut self,
        rejected: BindingId,
        predecessor: BindingId,
        cookies: FactorCookies,
    ) -> bool {
        if !self.rejected(rejected) || self.occupied() || self.committed.is_some() {
            return false;
        }
        self.rejected = None;
        self.committed = Some((predecessor, cookies));
        true
    }
    pub(super) fn reserve_factor(
        &mut self,
        inner: &Inner,
        account: &Account,
        previous: BindingId,
    ) -> Result<(BindingId, Epoch), ClientError> {
        if !self.published_for(previous, inner, account) {
            return Err(stale());
        }
        self.start(inner, account, BindingBase::OwedFactor)
    }
    fn start(
        &mut self,
        inner: &Inner,
        account: &Account,
        base: BindingBase,
    ) -> Result<(BindingId, Epoch), ClientError> {
        if !selected(inner, account, base) {
            return Err(stale());
        }
        if self.occupied() {
            return Err(busy());
        }
        let original = inner.account.session.factor_cookies().ok_or_else(stale)?;
        let clock = Arc::new(AtomicU64::new(1));
        let epoch = Epoch::new(clock.clone(), 1);
        let id = BindingId(Uuid::new_v4());
        self.committed = None;
        self.rejected = None;
        self.pending = Some(Pending {
            id,
            account: account.clone(),
            base,
            original,
            automatic: true,
            clock,
            phase: Phase::Forwarding,
            retry: None,
        });
        Ok((id, epoch))
    }
    fn get(&mut self, id: BindingId, inner: &Inner) -> Result<&mut Pending, ClientError> {
        self.pending
            .as_mut()
            .filter(|p| p.id == id && selected(inner, &p.account, p.base))
            .ok_or_else(stale)
    }
}
impl Coordinator {
    // Always invoked under inner, before a working account snapshot is borrowed.
    // Neither a dropped ClaimDocument nor a local-origin-only change comes here.
    pub(super) fn supersede_binding(&self) {
        self.binding.lock().expect("ordinary binding").retire();
    }

    pub(super) async fn accept_forward(
        self: &Arc<Self>,
        id: BindingId,
        code: PreparedClaimCode,
    ) -> Result<(), ClientError> {
        self.finish_forward(id, code.forward().await)
    }
    pub(super) fn finish_forward(
        self: &Arc<Self>,
        id: BindingId,
        response: Result<BoundClaimSession, ClientError>,
    ) -> Result<(), ClientError> {
        let inner = self.inner.lock().expect("client state");
        let mut slot = self.binding.lock().expect("ordinary binding");
        let p = slot.get(id, &inner)?;
        if !matches!(p.phase, Phase::Forwarding) {
            return Err(stale());
        }
        match response {
            Ok(bound) => {
                p.phase = Phase::Bound {
                    session: Arc::new(bound),
                    reader: Default::default(),
                };
                drop(slot);
                drop(inner);
                // Receipt delivery (including one-time recovery material) does
                // not wait for this GET or keychain. Neither sheet closure nor
                // loss of the submitting waiter cancels this account-owned pass.
                let ordinary = self.clone();
                tokio::spawn(async move { ordinary.settle_accepted(id).await });
                Ok(())
            }
            Err(error) => {
                slot.retire();
                slot.rejected = Some(id);
                Err(error)
            } // uncertain is not permission to submit this delivery again
        }
    }

    async fn settle_accepted(self: &Arc<Self>, id: BindingId) {
        let failed = if self.read_bound_session(id).await.is_err() {
            Recovery::Read(id)
        } else if self.recover_binding(Recovery::Save(id)).await.is_err() {
            Recovery::Save(id)
        } else {
            return;
        };
        // A terminal discovery refusal already retired its slot. A late failure
        // cannot mark a different binding (even at the same public generation).
        let _ = self.pause_recovery(failed);
    }

    fn pause_recovery(&self, failed: Recovery) -> Option<Uuid> {
        let id = match failed {
            Recovery::Read(id) | Recovery::Save(id) => id,
        };
        let inner = self.inner.lock().expect("client state");
        let mut slot = self.binding.lock().expect("ordinary binding");
        let p = slot.get(id, &inner).ok()?;
        if !matches!(
            (&p.phase, failed),
            (Phase::Bound { .. }, Recovery::Read(_)) | (Phase::Discovered(_), Recovery::Save(_))
        ) {
            return None;
        }
        let token = Uuid::new_v4();
        p.automatic = false;
        p.retry = Some(token);
        Some(token)
    }

    async fn recover_binding(self: &Arc<Self>, recovery: Recovery) -> Result<(), ClientError> {
        match recovery {
            Recovery::Read(id) => self.read_bound_session(id).await,
            Recovery::Save(id) => {
                let writer = self.clone();
                let mut owned = self.credential_io.clone().lock_owned().await;
                tokio::task::spawn_blocking(move || {
                    let result = writer.save_discovered(id, &mut owned);
                    drop(owned);
                    if result.is_ok() {
                        writer.view.changed(&writer.state());
                    }
                    result
                })
                .await
                .map_err(|_| unavailable())?
            }
        }
    }

    // No connection-record write: before/during/after this operation restore
    // selects the SAME subject. A process loss before key replacement leaves
    // the old jar; after replacement, startup discovers the new ordinary jar
    // and checks its subject. Neither point restores a ticket, OTP or proof.
    fn save_discovered(&self, id: BindingId, owned: &mut CredentialWrites) -> Result<(), ClientError> {
        if let Some(error) = &owned.cleanup_error {
            return Err(error.clone());
        }
        let (key, found, fence) = {
            let inner = self.inner.lock().expect("client state");
            let mut slot = self.binding.lock().expect("ordinary binding");
            let p = slot.get(id, &inner)?;
            let Phase::Discovered(found) = &p.phase else {
                return Err(stale());
            };
            if !found.belongs_to(&p.account) || !owned.ids.contains(&p.account.id) {
                return Err(stale());
            }
            (p.account.id.clone(), found.clone(), Epoch::new(p.clock.clone(), 1))
        };
        let bytes = found.flow.session.persist()?;
        // This is a replacement, not a fresh sign-in. Read a present, decodable prior
        // from the checkpoint that will own the write, never by reacquiring its OS lock.
        let mut change = store_call(|| self.credentials.clone().begin_write(&key))?;
        let previous = store_call(|| change.observe()?.into_prior())?;
        NativeSession::restore_account(self.transport.clone(), &previous)?;
        fence.check()?;
        let attempted = store_call(|| change.publish(&bytes));
        match store_call(|| change.observe()) {
            // A positive readback is evidence of the durable jar, not that we
            // changed its contents. An OS error can follow the actual write.
            Ok(v) if v.is_attempted(&bytes) => (),
            Ok(v) if v.is_prior(&previous) => return Err(attempted.err().unwrap_or_else(unavailable)),
            // Includes a different manifest with the same decoded jar, an absent
            // root, and a recognized manifest whose bank no longer decodes exactly.
            _ => {
                let error = unavailable();
                owned.cleanup_error = Some(error.clone());
                return Err(error);
            }
        }
        // Hold the same handle through the live-slot decision. Cooperating Windows
        // writers cannot interleave; legacy raw writers do not participate in its lock.
        let outcome = self.publish_discovered(id, &found);
        if outcome.is_err() && store_call(|| change.rollback_existing(&previous, &bytes)).is_err() {
            let error = unavailable();
            owned.cleanup_error = Some(error.clone());
            return Err(error);
        }
        outcome
    }

    fn publish_discovered(&self, id: BindingId, found: &Arc<DiscoveredSession>) -> Result<(), ClientError> {
        let mut inner = self.inner.lock().expect("client state");
        let mut slot = self.binding.lock().expect("ordinary binding");
        let p = slot.get(id, &inner)?;
        if !matches!(&p.phase, Phase::Discovered(current) if Arc::ptr_eq(current, found))
            || !found.belongs_to(&p.account)
        {
            return Err(stale());
        }
        let cookies = found.flow.session.factor_cookies().ok_or_else(stale)?;
        let mut flow = found.flow.clone();
        flow.fence(Epoch::new(self.generation.clone(), inner.state.generation));
        inner.account = flow;
        inner.state.account = found.account.clone();
        inner.state.catalog = found.catalog.clone();
        refresh_display(&mut inner);
        order_catalog(&mut inner);
        inner.state.revision += 1;
        slot.retire();
        slot.committed = Some((id, cookies));
        Ok(())
    }

    /// A separate native recovery entry. It can never start or forward a code.
    async fn read_bound_session(&self, id: BindingId) -> Result<(), ClientError> {
        let (bound, state, reader) = {
            let inner = self.inner.lock().expect("client state");
            let mut slot = self.binding.lock().expect("ordinary binding");
            let p = slot.get(id, &inner)?;
            let Phase::Bound { session, reader } = &p.phase else {
                return Err(stale());
            };
            (
                session.clone(),
                inner.state.clone(),
                reader.clone().try_lock_owned().map_err(|_| busy())?,
            )
        };
        let response = bound.discover(state).await;
        let inner = self.inner.lock().expect("client state");
        let mut slot = self.binding.lock().expect("ordinary binding");
        let p = slot.get(id, &inner)?;
        if !matches!(&p.phase, Phase::Bound {session, ..} if Arc::ptr_eq(session, &bound)) {
            return Err(stale());
        }
        drop(reader);
        match response {
            Ok(found) => {
                p.phase = Phase::Discovered(Arc::new(found));
                Ok(())
            }
            Err(e)
                if matches!(
                    e.code.as_str(),
                    "fresh_code_required" | "account_changed" | "signed_out"
                ) =>
            {
                slot.retire();
                drop(slot);
                self.claims.lock().expect("claim owner").retire_submission(id);
                Err(e)
            }
            Err(e) => Err(e), // retain the same bound jar; only a later GET can recover it
        }
    }
}
