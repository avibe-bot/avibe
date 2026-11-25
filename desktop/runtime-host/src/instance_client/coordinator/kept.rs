//! Instance sessions this run has opened, so that opening an instance again
//! (from the list, back from this computer, or for another kind of path)
//! continues its sign-in instead of starting a new one, and adds no device.
//!
//! In memory only, never saved: a new run signs in again. Each is held for the
//! account, the instance and its exact canonical origin it was opened with,
//! and it is the committed instance's own session (`CommittedUpdate`), so the
//! cookies its replies set are the ones used next time. A kept session is
//! never shown before the instance has said again, on `/api/session`, that it
//! is this account's on this instance (`Coordinator::resume`). Every session
//! that leaves this list is signed out of its instance as far as it can be
//! (`Coordinator::sign_out_sessions`): when the instance refuses it, when
//! another account opens an instance, when the instance is opened at another
//! origin, and when the account signs out.
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
    /// The session kept for this account on this instance at `origin`, and
    /// those let go meanwhile: another account's (one account is signed in at
    /// a time).
    pub(super) fn find(
        &mut self,
        account_id: &str,
        instance_id: &str,
        origin: &Url,
    ) -> (Option<Arc<CommittedUpdate>>, Vec<Arc<CommittedUpdate>>) {
        let dropped = self.drain(|kept| kept.account_id != account_id);
        let origin = origin.origin().ascii_serialization();
        let found = self
            .0
            .iter()
            .find(|kept| kept.instance_id == instance_id && kept.origin == origin)
            .map(|kept| kept.session.clone());
        (found, dropped)
    }

    /// The session an opening has just committed. It continues the one kept
    /// for that instance at the same origin, which it replaces; any other
    /// account's, and the instance's at another origin, are let go.
    pub(super) fn keep(
        &mut self,
        account_id: &str,
        target: &ClientTarget,
        session: Arc<CommittedUpdate>,
    ) -> Vec<Arc<CommittedUpdate>> {
        let ClientTarget::Remote {
            instance_id,
            canonical_origin,
            ..
        } = target
        else {
            return vec![];
        };
        self.0.retain(|kept| {
            !(kept.account_id == account_id && kept.instance_id == *instance_id && kept.origin == *canonical_origin)
        });
        let dropped = self.drain(|kept| kept.account_id != account_id || kept.instance_id == *instance_id);
        self.0.push(Kept {
            account_id: account_id.to_owned(),
            instance_id: instance_id.clone(),
            origin: canonical_origin.clone(),
            session,
        });
        dropped
    }

    /// A session its instance no longer accepts.
    pub(super) fn forget(&mut self, session: &Arc<CommittedUpdate>) -> Vec<Arc<CommittedUpdate>> {
        self.drain(|kept| Arc::ptr_eq(&kept.session, session))
    }

    /// Every kept session, for signing out.
    pub(super) fn take(&mut self) -> Vec<Arc<CommittedUpdate>> {
        self.drain(|_| true)
    }

    fn drain(&mut self, leaves: impl Fn(&Kept) -> bool) -> Vec<Arc<CommittedUpdate>> {
        let (gone, stay): (Vec<Kept>, Vec<Kept>) = self.0.drain(..).partition(leaves);
        self.0 = stay;
        gone.into_iter().map(|kept| kept.session).collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::instance_client::transport::{NativeSession, Request, Response, Transport};

    struct Offline;
    #[async_trait]
    impl Transport for Offline {
        async fn send(&self, _: Request) -> Result<Response, ClientError> {
            Err(ClientError::new("network_unavailable"))
        }
    }
    fn session(origin: &str) -> Arc<CommittedUpdate> {
        Arc::new(CommittedUpdate::new(NativeSession::new(
            Url::parse(origin).unwrap(),
            Arc::new(Offline),
        )))
    }
    fn remote(id: &str, origin: &str) -> ClientTarget {
        ClientTarget::Remote {
            name: id.into(),
            instance_id: id.into(),
            canonical_origin: origin.into(),
        }
    }
    const ONE: &str = "https://one.avibe.bot";
    const RENAMED: &str = "https://one-renamed.avibe.bot";

    /// Every session that leaves the list is returned, to be signed out; a
    /// session that continues at the same origin is not one of them.
    #[test]
    fn every_session_let_go_is_returned_for_signing_out() {
        let mut kept = KeptSessions::default();
        let (first, two) = (session(ONE), session("https://two.avibe.bot"));
        assert!(kept.keep("a", &remote("one", ONE), first.clone()).is_empty());
        assert!(kept
            .keep("a", &remote("two", "https://two.avibe.bot"), two.clone())
            .is_empty());

        // Opened again at the same origin: the same sign-in goes on.
        let again = session(ONE);
        assert!(kept.keep("a", &remote("one", ONE), again.clone()).is_empty());

        // Renamed: the instance opens at another origin; the old one is let go.
        let renamed = session(RENAMED);
        let gone = kept.keep("a", &remote("one", RENAMED), renamed.clone());
        assert!(gone.len() == 1 && Arc::ptr_eq(&gone[0], &again));
        let (found, gone) = kept.find("a", "one", &Url::parse(ONE).unwrap());
        assert!(found.is_none() && gone.is_empty());

        // Another account, without signing out: all of the first one's go.
        let (found, gone) = kept.find("b", "one", &Url::parse(RENAMED).unwrap());
        assert!(found.is_none());
        assert_eq!(gone.len(), 2);
        assert!(gone.iter().any(|each| Arc::ptr_eq(each, &renamed)) && gone.iter().any(|each| Arc::ptr_eq(each, &two)));
        assert!(kept.take().is_empty());

        let refused = session(ONE);
        kept.keep("b", &remote("one", ONE), refused.clone());
        let gone = kept.forget(&refused);
        assert!(gone.len() == 1 && Arc::ptr_eq(&gone[0], &refused));
        assert!(kept.forget(&refused).is_empty());
        assert!(!Arc::ptr_eq(&first, &refused));
    }
}
