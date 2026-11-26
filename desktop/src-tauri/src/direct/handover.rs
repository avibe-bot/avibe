use super::{
    connect::Authority,
    credentials::{WindowCounts, WindowRegistration},
    listener::ListenerAccess,
    quiet::PublicCensus,
    transport::Link,
    SelectedPath,
};
use std::{sync::Arc, time::Duration};
use tokio::{sync::watch, time::Instant};

/// Preference for *new* connections. An admitted probe can prefer direct even
/// while the page is still on a pooled tunnel connection. No address is exposed.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum PreferredPath {
    #[default]
    Tunnel,
    Direct(SelectedPath),
}
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct PathStatus {
    pub preferred: PreferredPath,
    pub sequence: u64,
    pub closure: u64,
    /// Cumulative failed strict CONNECT opens, not closures of live streams.
    pub strict_failures: u64,
}
impl PathStatus {
    pub(super) fn publish(tx: &watch::Sender<Self>, path: Option<SelectedPath>, closed: bool) {
        let preferred = path.map(PreferredPath::Direct).unwrap_or_default();
        tx.send_if_modified(|s| {
            if s.preferred == preferred && !closed {
                return false;
            }
            s.preferred = preferred;
            s.sequence += 1;
            s.closure += u64::from(closed);
            true
        });
    }
}
#[derive(Debug, PartialEq, Eq)]
pub enum HandoverResult {
    NotUsable,
    OtherDocuments,
    Done { closed: usize, kept: usize },
}
/// One selected native session/listener; retaining this handle cannot retain its
/// direct authority. Window operations require the original registration.
#[derive(Clone)]
pub struct Handover {
    pub(super) host: Authority,
    pub(super) port: u16,
    pub(super) links: watch::Receiver<Option<Arc<Link>>>,
    pub(super) health: watch::Receiver<()>,
    pub(super) listener: std::sync::Weak<ListenerAccess>,
}
impl Handover {
    fn owned_listener(&self) -> Option<Arc<ListenerAccess>> {
        self.listener.upgrade().filter(|l| l.owned())
    }
    /// Select the policy for subsequent authenticated CONNECTs. Pending opens
    /// retain their policy and established streams are never migrated.
    pub fn set_direct_only(&self, enabled: bool) {
        if let Some(listener) = self.owned_listener() {
            listener.policy.set_direct_only(enabled);
        }
    }
    /// Retire all public legs of this session (including setup and busy ones),
    /// and fence late public commits. The count is local, not peer acknowledgement.
    pub fn close_public(&self) -> usize {
        self.owned_listener().map_or(0, |l| l.policy.close_public())
    }
    /// Independently usable while the session task holds its Client for refresh.
    pub fn window_credential(&self, window: &str) -> Option<WindowRegistration> {
        self.listener.upgrade()?.window_credential(window)
    }
    pub fn revoke_window(&self, registration: &WindowRegistration) {
        // An accept fault stops issuance, not revocation of existing streams.
        if let Some(listener) = self.owned_listener() {
            listener.windows.revoke(registration);
        }
    }
    /// A delayed call retains the originating registration, even before its first poll.
    pub async fn close_quiet_public_for_window(&self, host: &str, registration: &WindowRegistration) -> HandoverResult {
        let Some(grant) = self.owned_listener().and_then(|l| l.windows.window(registration)) else {
            return HandoverResult::NotUsable;
        };
        if host != self.host.host() {
            return HandoverResult::NotUsable;
        }
        let Some(start) = self.links.borrow().clone() else {
            return HandoverResult::NotUsable;
        };
        let deadline = Instant::now() + Duration::from_secs(3);
        loop {
            {
                let valid = grant.valid.borrow();
                if !*valid {
                    return HandoverResult::NotUsable;
                }
                let current = self.links.borrow();
                if self.owned_listener().is_none()
                    || self.health.has_changed().is_err()
                    || !current.as_ref().is_some_and(|l| Arc::ptr_eq(l, &start))
                    || !matches!(
                        start.selected_path(),
                        Some(SelectedPath::Lan(_) | SelectedPath::Punched(_))
                    )
                {
                    return HandoverResult::NotUsable;
                }
                if let Some((closed, kept)) = grant.legs.claim(deadline) {
                    return HandoverResult::Done { closed, kept };
                }
            }
            tokio::time::sleep_until(deadline.min(Instant::now() + Duration::from_millis(100))).await;
        }
    }
    /// What this registration's authenticated CONNECTs came to so far.
    /// `None` once it is revoked or the listener is gone.
    pub fn window_counts(&self, registration: &WindowRegistration) -> Option<WindowCounts> {
        let grant = self.owned_listener()?.windows.window(registration)?;
        let valid = grant.valid.borrow();
        (*valid).then(|| grant.counts.read())
    }
    /// Connections to the listener that ended without an authenticated
    /// CONNECT, whoever made them. `None` once the listener is gone.
    pub fn unadmitted(&self) -> Option<u64> {
        Some(self.owned_listener()?.windows.unadmitted())
    }
    pub fn window_census(&self, registration: &WindowRegistration) -> Option<PublicCensus> {
        let grant = self.owned_listener()?.windows.window(registration)?;
        let valid = grant.valid.borrow();
        (*valid).then(|| grant.legs.census())
    }
}
