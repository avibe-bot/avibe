//! The ordinary account owns a dispatched first factor, not the naming sheet.
//! This slot never enters a working Inner, a record, or a bridge projection.
use super::*;
use crate::instance_client::account::{BindingBase, BoundClaimSession, DiscoveredSession, PreparedClaimCode};
use uuid::Uuid;

#[derive(Clone, Copy, PartialEq, Eq)]
pub(super) struct BindingId(Uuid);
#[derive(Default)]
pub(super) struct BindingSlot {
    pending: Option<Pending>,
    // Positive receipt of key readback AND account publication. Never restored.
    committed: Option<BindingId>,
}
struct Pending {
    id: BindingId,
    account: Account,
    base: BindingBase,
    clock: Arc<AtomicU64>,
    phase: Phase,
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
pub(super) enum Recovery {
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
    }
    pub(super) fn occupied(&self) -> bool {
        self.pending.is_some()
    }
    pub(super) fn accepted(&self, id: BindingId) -> bool {
        self.committed == Some(id)
            || self
                .pending
                .as_ref()
                .is_some_and(|p| p.id == id && !matches!(p.phase, Phase::Forwarding))
    }
    pub(super) fn committed(&self) -> Option<BindingId> {
        self.committed
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
    pub(super) fn reserve_factor(
        &mut self,
        inner: &Inner,
        account: &Account,
        previous: BindingId,
    ) -> Result<(BindingId, Epoch), ClientError> {
        if self.committed != Some(previous) {
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
        let clock = Arc::new(AtomicU64::new(1));
        let epoch = Epoch::new(clock.clone(), 1);
        let id = BindingId(Uuid::new_v4());
        self.committed = None;
        self.pending = Some(Pending {
            id,
            account: account.clone(),
            base,
            clock,
            phase: Phase::Forwarding,
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

    pub(super) async fn accept_forward(&self, id: BindingId, code: PreparedClaimCode) -> Result<(), ClientError> {
        self.finish_forward(id, code.forward().await)
    }
    pub(super) fn finish_forward(
        &self,
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
                Ok(())
            }
            Err(error) => {
                slot.retire();
                Err(error)
            } // uncertain is not permission to submit this delivery again
        }
    }

    pub(super) fn binding_recovery(&self) -> Option<Recovery> {
        let inner = self.inner.lock().expect("client state");
        let slot = self.binding.lock().expect("ordinary binding");
        let p = slot.pending.as_ref().filter(|p| selected(&inner, &p.account, p.base))?;
        match &p.phase {
            Phase::Forwarding => None,
            Phase::Bound { .. } => Some(Recovery::Read(p.id)),
            Phase::Discovered(_) => Some(Recovery::Save(p.id)),
        }
    }

    pub(super) async fn recover_binding(self: &Arc<Self>, recovery: Recovery) -> Result<(), ClientError> {
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
        let mut flow = found.flow.clone();
        flow.fence(Epoch::new(self.generation.clone(), inner.state.generation));
        inner.account = flow;
        inner.state.account = found.account.clone();
        inner.state.catalog = found.catalog.clone();
        refresh_display(&mut inner);
        order_catalog(&mut inner);
        inner.state.revision += 1;
        slot.retire();
        slot.committed = Some(id);
        Ok(())
    }

    /// A separate native recovery entry. It can never start or forward a code.
    pub(super) async fn read_bound_session(&self, id: BindingId) -> Result<(), ClientError> {
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
