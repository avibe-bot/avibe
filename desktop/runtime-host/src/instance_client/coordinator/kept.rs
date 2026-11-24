//! Instance sessions this run has opened, so that opening an instance again
//! (from the list, back from this computer, or for another kind of path)
//! continues its sign-in instead of starting a new one, and adds no device.
//!
//! In memory only, never saved: a new run signs in again. Each is held for the
//! account, the instance and its exact canonical origin it was opened with,
//! and it is the committed instance's own session (`CommittedUpdate`), so the
//! cookies its replies set are the ones used next time. A kept session is
//! never shown before the instance has said again, on `/api/session`, that it
//! is this account's on this instance (`Coordinator::resume`). Signing out
//! takes them all, and signs each out of its instance as far as it can.
use super::*;
use url::Url;

struct Kept {
    account_id: String,
    instance_id: String,
    origin: String,
    session: Arc<CommittedUpdate>,
}

#[derive(Default)]
pub(super) struct KeptSessions(Vec<Kept>);

impl KeptSessions {
    /// The session kept for this account on this instance at `origin`. Those
    /// of another account are let go: one account is signed in at a time.
    pub(super) fn find(&mut self, account_id: &str, instance_id: &str, origin: &Url) -> Option<Arc<CommittedUpdate>> {
        self.0.retain(|kept| kept.account_id == account_id);
        let origin = origin.origin().ascii_serialization();
        self.0
            .iter()
            .find(|kept| kept.instance_id == instance_id && kept.origin == origin)
            .map(|kept| kept.session.clone())
    }

    /// The session an opening has just committed; it replaces any other kept
    /// for that instance.
    pub(super) fn keep(&mut self, account_id: &str, target: &ClientTarget, session: Arc<CommittedUpdate>) {
        let ClientTarget::Remote {
            instance_id,
            canonical_origin,
            ..
        } = target
        else {
            return;
        };
        self.0
            .retain(|kept| kept.account_id == account_id && kept.instance_id != *instance_id);
        self.0.push(Kept {
            account_id: account_id.to_owned(),
            instance_id: instance_id.clone(),
            origin: canonical_origin.clone(),
            session,
        });
    }

    /// A session its instance no longer accepts.
    pub(super) fn forget(&mut self, session: &Arc<CommittedUpdate>) {
        self.0.retain(|kept| !Arc::ptr_eq(&kept.session, session));
    }

    /// Every kept session, for signing out.
    pub(super) fn take(&mut self) -> Vec<Arc<CommittedUpdate>> {
        self.0.drain(..).map(|kept| kept.session).collect()
    }
}
