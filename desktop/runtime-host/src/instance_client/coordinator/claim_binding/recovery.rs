//! Explicit ordinary-account recovery. The document chooses neither a binding
//! nor a phase. An offer is single-use and is never included in GetState.
use super::*;
use serde::Deserialize;
use serde_json::{json, Value};

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AccountRecoveryRequest {
    pub expected_generation: u64,
    pub action: AccountRecoveryAction,
}
#[derive(Deserialize)]
#[serde(tag = "type", rename_all = "snake_case", deny_unknown_fields)]
pub enum AccountRecoveryAction {
    Inspect {},
    Resume { receipt: String },
}

#[derive(Clone, Copy)]
enum Answer {
    None,
    Offer(BindingId, Uuid),
    Saved(BindingId),
}
enum Work {
    Ready(Answer),
    Running(tokio::task::JoinHandle<Result<Answer, ClientError>>),
}
#[must_use]
pub struct RecoveryWork {
    owner: Arc<Coordinator>,
    generation: u64,
    work: Work,
}
#[must_use]
pub struct RecoveryDelivery {
    owner: Arc<Coordinator>,
    generation: u64,
    answer: Answer,
}
impl RecoveryWork {
    pub async fn wait(self) -> Result<RecoveryDelivery, ClientError> {
        let answer = match self.work {
            Work::Ready(value) => value,
            Work::Running(worker) => worker.await.map_err(|_| stale())??,
        };
        Ok(RecoveryDelivery {
            owner: self.owner,
            generation: self.generation,
            answer,
        })
    }
}
impl RecoveryDelivery {
    /// Called inside the same shell document's final admission. No I/O and no
    /// waiting for a busy ordinary transaction while the view lock is held.
    pub fn deliver(self) -> Result<Value, ClientError> {
        let c = self.owner;
        let inner = c.inner.try_lock().map_err(|_| stale())?;
        let slot = c.binding.try_lock().map_err(|_| stale())?;
        if inner.state.generation != self.generation || c.signing_out.load(Ordering::SeqCst) {
            return Err(stale());
        }
        match self.answer {
            Answer::None
                if !slot
                    .pending
                    .as_ref()
                    .is_some_and(|p| p.retry.is_some() && selected(&inner, &p.account, p.base)) =>
            {
                Ok(json!({"state":"unavailable"}))
            }
            Answer::Saved(id) if slot.committed == Some(id) => Ok(json!({"state":"saved"})),
            Answer::Offer(id, token)
                if slot
                    .pending
                    .as_ref()
                    .is_some_and(|p| p.id == id && p.retry == Some(token) && selected(&inner, &p.account, p.base)) =>
            {
                let io = c.credential_io.try_lock().map_err(|_| stale())?;
                if io.cleanup_error.is_some() {
                    return Ok(json!({"state":"storage_blocked"}));
                }
                Ok(json!({"state":"available", "receipt":token.to_string()}))
            }
            _ => Err(stale()),
        }
    }
}
impl Coordinator {
    /// The shell admits this exact document before entering. Consuming the offer
    /// and launching its owner are synchronous; dropping the waiter does not
    /// abandon a keychain commit or start another pass.
    pub fn admit_account_recovery(
        self: &Arc<Self>,
        request: AccountRecoveryRequest,
    ) -> Result<RecoveryWork, ClientError> {
        let inner = self.inner.try_lock().map_err(|_| stale())?;
        let mut slot = self.binding.try_lock().map_err(|_| stale())?;
        if inner.state.generation != request.expected_generation || self.signing_out.load(Ordering::SeqCst) {
            return Err(stale());
        }
        let pending = slot.pending.as_mut().filter(|p| selected(&inner, &p.account, p.base));
        let work = match request.action {
            AccountRecoveryAction::Inspect {} => Work::Ready(
                pending
                    .and_then(|p| p.retry.map(|t| Answer::Offer(p.id, t)))
                    .unwrap_or(Answer::None),
            ),
            AccountRecoveryAction::Resume { receipt } => {
                let p = pending.ok_or_else(stale)?;
                let token = p.retry.ok_or_else(stale)?;
                if receipt != token.to_string() {
                    return Err(stale());
                }
                // A failed readback can latch cleanup_error. No new writer may
                // overwrite an unrecognized keychain value, even on a fresh click.
                let io = self.credential_io.try_lock().map_err(|_| stale())?;
                if io.cleanup_error.is_some() {
                    return Err(stale());
                }
                let recovery = match &p.phase {
                    Phase::Bound { .. } => Recovery::Read(p.id),
                    Phase::Discovered(_) => Recovery::Save(p.id),
                    Phase::Forwarding => return Err(stale()),
                };
                let id = p.id;
                p.retry = None;
                let ordinary = self.clone();
                Work::Running(tokio::spawn(async move {
                    let result = ordinary.recover_binding(recovery).await;
                    if matches!(recovery, Recovery::Save(_)) && result.is_ok() {
                        return Ok(Answer::Saved(id));
                    }
                    // A recovered GET creates a new, save-only offer, not an
                    // implicit keychain write under the old read receipt.
                    let next = if result.is_ok() { Recovery::Save(id) } else { recovery };
                    Ok(ordinary
                        .pause_recovery(next)
                        .map_or(Answer::None, |t| Answer::Offer(id, t)))
                }))
            }
        };
        Ok(RecoveryWork {
            owner: self.clone(),
            generation: request.expected_generation,
            work,
        })
    }
}
