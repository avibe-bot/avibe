//! One controller per native-selected remote origin and signed-in session.
//! Dropping it on a selection change retires the proxy and the peer. Never
//! identify an instance or session by its installation ID.
mod bridge;
#[cfg(test)]
#[path = "../../tests/direct/bridge.rs"]
mod bridge_tests;
pub(crate) mod choices;
mod clock;
mod connect;
#[cfg(test)]
#[path = "../../tests/direct/windows_credentials.rs"]
mod credential_tests;
mod credentials;
mod dead_end;
#[cfg(test)]
#[path = "../../tests/direct/dead_end.rs"]
mod dead_end_tests;
pub use credentials::{WindowCounts, WindowRegistration};
pub use quiet::PublicCensus;
#[cfg(test)]
#[path = "../../tests/direct/cross_network.rs"]
mod cross_network_tests;
#[cfg(test)]
#[path = "../../tests/direct/drain.rs"]
mod drain_tests;
mod handover;
#[cfg(test)]
#[path = "../../tests/direct/handover.rs"]
mod handover_tests;
#[cfg(test)]
#[path = "../../tests/direct/sidecar.rs"]
mod interop;
#[cfg(test)]
#[path = "../../tests/direct/lifecycle.rs"]
mod lifecycle_tests;
mod listener;
mod measure;
pub use measure::{measure, MeasureBusy, MeasureResult};
mod quiet;
pub use handover::{Handover, HandoverResult, PathStatus, PreferredPath};
mod offer;
#[cfg(test)]
#[path = "../../tests/direct/offer.rs"]
mod offer_tests;
mod path;
pub use path::{AddressFamily, SelectedPath};
mod public;
mod registered;
#[cfg(test)]
#[path = "../../tests/direct/registered.rs"]
mod registered_tests;
pub mod rendezvous;
#[cfg(test)]
#[path = "../../tests/direct/rendezvous.rs"]
mod rendezvous_tests;
mod selection;
pub(crate) mod shell;
mod strict;
#[cfg(test)]
#[path = "../../tests/direct/strict.rs"]
mod strict_tests;
// muda builds macOS menus only on the main thread, which a test never runs on.
#[cfg(test)]
#[path = "../../tests/direct/direct_only.rs"]
mod direct_only_tests;
#[cfg(all(test, windows))]
#[path = "../../tests/direct/menu.rs"]
mod menu_tests;
mod transport;
#[cfg(target_os = "macos")]
pub mod webview_macos;
#[cfg(windows)]
pub mod webview_windows;
pub(crate) mod wiring;
#[cfg(test)]
#[path = "../../tests/direct/wiring.rs"]
mod wiring_tests;

use clock::Clock;
use connect::Authority;
use futures_util::StreamExt;
use iroh::{Endpoint, SecretKey};
use listener::Listener;
use offer::Offer;
use std::{sync::Arc, time::Duration};
use tokio::{sync::watch, task::JoinHandle};
use transport::Link;
pub use transport::{Fault, FaultCode, QualificationKind, QualificationStep, Stage};

/// Listener location only. Each routed window supplies its own registration.
pub struct Route<'a> {
    pub host: &'a str,
    pub proxy: std::net::SocketAddrV4,
}

#[derive(Debug, PartialEq, Eq)]
pub enum RefreshOutcome {
    Renewed,
    /// Maximum suggested delay from this result to the next attempt. This
    /// is less than the remaining grant; it is not an extension of the grant.
    Retained {
        retry_before: Duration,
    },
    Retired,
}

/// Once bind succeeds, cancellation must still drain queued QUIC close frames.
/// The worker owns a separate endpoint handle and outlives a cancelled probe,
/// monitor or close waiter. Dropping the sender (including in Drop) starts it.
struct EndpointDrain {
    stop: tokio::sync::oneshot::Sender<()>,
    done: JoinHandle<()>,
}
impl EndpointDrain {
    fn new(endpoint: Endpoint) -> Self {
        let (stop, rx) = tokio::sync::oneshot::channel();
        let done = tokio::spawn(async move {
            let _ = rx.await;
            let _ = tokio::time::timeout(Duration::from_secs(2), endpoint.close()).await;
        });
        Self { stop, done }
    }
    async fn close(self) {
        drop(self.stop);
        let _ = self.done.await;
    }
}

pub struct Client {
    clock: Clock,
    host: Authority,
    instance: String,
    key: SecretKey,
    links: watch::Sender<Option<Arc<Link>>>,
    listener: Listener,
    status: watch::Sender<PathStatus>,
    // Only writers take this gate. Never nest the public status and link watch locks.
    status_writer: Arc<std::sync::Mutex<()>>,
    monitor: Option<JoinHandle<()>>,
    steps: transport::Steps,
    retirement: tokio::sync::broadcast::Sender<Fault>,
}
impl Client {
    /// The native shell calls this only for a direct-eligible, signed-in managed
    /// instance. Prepare its authenticated loopback proxy for environment creation.
    /// Until an eligible rendezvous succeeds it serves only public fallback.
    pub async fn new(host: &str, instance: String) -> Option<Self> {
        let host = Authority::managed(host)?;
        if instance.is_empty() {
            return None;
        }
        let (links, rx) = watch::channel(None);
        let status = watch::channel(PathStatus::default()).0;
        let listener = Listener::bind(host.clone(), rx, status.clone()).await?;
        Some(Self {
            clock: Clock::default(),
            host,
            instance,
            key: SecretKey::generate(),
            links,
            listener,
            status_writer: Default::default(),
            status,
            monitor: None,
            steps: tokio::sync::broadcast::channel(16).0,
            retirement: tokio::sync::broadcast::channel(8).0,
        })
    }
    /// Ask the selected session's native adapter for a same-origin reply. The
    /// caller must verify the session/document is still current on completion;
    /// the core binds the body to this instance, hostname, client key and time.
    pub async fn refresh<F>(&mut self, fetch: impl FnOnce(String) -> F) -> bool
    where
        F: std::future::Future<Output = Option<String>>,
    {
        let sent = self.clock.now();
        let answer = tokio::time::timeout(std::time::Duration::from_secs(5), fetch(self.key.public().to_string()))
            .await
            .ok()
            .flatten();
        self.apply(answer.as_deref(), sent).await.is_ok()
    }

    /// A missing reply is not a withdrawal. Preserve only an already usable
    /// link, under its original monotonic deadline and path monitor; never
    /// extend admission or create an endpoint without a validated offer.
    pub async fn refresh_reply<F>(&mut self, fetch: impl FnOnce(String) -> F) -> RefreshOutcome
    where
        F: std::future::Future<Output = rendezvous::Reply>,
    {
        self.refresh_reply_detailed(fetch).await.0
    }

    /// Same operation and authority as refresh_reply. The reason is independent
    /// of disposition: a failed refresh may safely retain an earlier lease.
    pub async fn refresh_reply_detailed<F>(
        &mut self,
        fetch: impl FnOnce(String) -> F,
    ) -> (RefreshOutcome, Option<Fault>)
    where
        F: std::future::Future<Output = rendezvous::Reply>,
    {
        use rendezvous::Reply;
        use FaultCode::*;
        let sent = self.clock.now();
        let result =
            tokio::time::timeout(std::time::Duration::from_secs(5), fetch(self.key.public().to_string())).await;
        let code = match result {
            Ok(Reply::Ready(body)) => {
                return match self.apply(Some(&body), sent).await {
                    Ok(()) => (RefreshOutcome::Renewed, None),
                    Err(reason) => (RefreshOutcome::Retired, Some(reason)),
                }
            }
            Err(_) => Timeout,
            Ok(Reply::NotReady) => NotReady,
            Ok(Reply::Failed) => Other,
            Ok(Reply::Refused(408 | 429 | 500..=599)) => Status,
            Ok(reply) => {
                let code = match reply {
                    Reply::SelectionChanged => {
                        if let Some(task) = self.withdraw(false) {
                            let _ = task.await;
                        }
                        Superseded
                    }
                    other => {
                        self.retire().await;
                        match other {
                            Reply::Controlled | Reply::Refused(401 | 403) => Denied,
                            _ => Refused,
                        }
                    }
                };
                return (RefreshOutcome::Retired, Some(Fault(Stage::Rendezvous, code)));
            }
        };
        let remaining = self
            .links
            .borrow()
            .as_ref()
            .filter(|l| l.usable())
            .map(|l| l.remaining())
            .unwrap_or_default();
        let disposition = if remaining.is_zero() {
            self.retire().await;
            RefreshOutcome::Retired
        } else {
            RefreshOutcome::Retained {
                retry_before: (remaining / 2).min(Duration::from_secs(5)),
            }
        };
        (disposition, Some(Fault(Stage::Rendezvous, code)))
    }

    /// The route describes the proxy, not direct-path readiness. Its port
    /// survives loss of direct. Drop the client only after native detach.
    pub fn route(&self) -> Option<Route<'_>> {
        let listener = &self.listener;
        if !listener.is_alive() {
            return None;
        }
        Some(Route {
            host: self.host.host(),
            proxy: std::net::SocketAddrV4::new(std::net::Ipv4Addr::LOCALHOST, listener.port),
        })
    }

    /// Subscribe before route installation. `changed().await` returning `Err`
    /// means this listener ended: detach the native route and rebuild if eligible.
    /// Direct-path availability or refresh failure does not close this signal.
    pub fn listener_lifetime(&self) -> watch::Receiver<()> {
        self.listener.health()
    }

    /// A verified direct CONNECT stream is live on this link. An idle probe,
    /// public fallback alone, a withdrawn grant or an unavailable path is None.
    pub fn selected_path(&self) -> Option<SelectedPath> {
        let slot = self.links.borrow();
        let link = slot.as_ref()?;
        if !self.listener.is_alive() || !link.has_stream() {
            return None;
        }
        link.selected_path()
    }

    pub fn path_changes(&self) -> watch::Receiver<PathStatus> {
        self.status.subscribe()
    }
    pub fn handover(&self) -> Handover {
        Handover {
            host: self.host.clone(),
            links: self.links.subscribe(),
            health: self.listener.health(),
            listener: Arc::downgrade(&self.listener.access),
            port: self.listener.port,
        }
    }

    pub fn qualification_steps(&self) -> tokio::sync::broadcast::Receiver<QualificationStep> {
        self.steps.subscribe()
    }

    /// Live, bounded stream of link withdrawals; never contains peer identifiers.
    pub fn retirements(&self) -> tokio::sync::broadcast::Receiver<Fault> {
        self.retirement.subscribe()
    }

    fn withdraw(&mut self, involuntary: bool) -> Option<JoinHandle<()>> {
        let _writer = self.status_writer.lock().unwrap();
        if let Some(l) = self.links.send_replace(None) {
            // Withdraw immediately; a zero deadline also wakes the monitor.
            let closed = l.claim_loss();
            l.retire();
            let _ = self.retirement.send(Fault(Stage::Handover, FaultCode::Superseded));
            PathStatus::publish(&self.status, None, involuntary && closed);
        }
        self.monitor.take()
    }
    async fn retire(&mut self) {
        if let Some(task) = self.withdraw(true) {
            let _ = task.await;
        }
    }
    /// Only the native isolated-world adapter may supply a completion. No
    /// command or message handler accepts a rendezvous answer from a page.
    async fn apply(&mut self, answer: Option<&str>, sent: Duration) -> Result<(), Fault> {
        let Some(offer) = answer.and_then(|body| {
            Offer::parse(
                body,
                &self.host,
                &self.instance,
                self.key.public(),
                sent,
                self.clock.now(),
            )
        }) else {
            self.retire().await;
            return Err(Fault(
                Stage::Rendezvous,
                answer.map_or(FaultCode::InvalidOffer, unusable_answer),
            ));
        };
        if self.links.borrow().as_ref().is_some_and(|link| link.renew(&offer)) {
            return Ok(());
        }
        // Retire previous authority before starting a replacement. Never use a
        // stale result to make a new stream while waiting for the next probe.
        self.retire().await;
        if !self.listener.is_alive() {
            return Err(Fault(Stage::Handover, FaultCode::ListenerClosed));
        }
        if self.clock.now() >= offer.until {
            return Err(Fault(Stage::Dial, FaultCode::Expired));
        }
        // A non-renewable grant gets a fresh endpoint. In particular an old
        // relay map or iroh peer-address cache cannot cross a policy change.
        let endpoint = tokio::time::timeout(
            offer.until.saturating_sub(self.clock.now()),
            transport::endpoint_builder(self.key.clone(), offer.relay.as_ref()).bind(),
        )
        .await
        .map_err(|_| Fault(Stage::Dial, FaultCode::Timeout))?
        .map_err(|_| Fault(Stage::Dial, FaultCode::Other))?;
        self.probe_detailed(endpoint, offer).await
    }

    #[cfg(test)]
    async fn probe(&mut self, endpoint: Endpoint, offer: Offer) -> bool {
        self.probe_detailed(endpoint, offer).await.is_ok()
    }

    async fn probe_detailed(&mut self, endpoint: Endpoint, offer: Offer) -> Result<(), Fault> {
        let drain = EndpointDrain::new(endpoint.clone());
        let link = match Link::dial_detailed(&endpoint, offer, &self.host, self.clock.clone(), Some(&self.steps)).await
        {
            Ok(link) => link,
            Err(fault) => {
                drain.close().await;
                return Err(fault);
            }
        };
        let check = if self.listener.is_alive() {
            link.check_path(Stage::Handover)
        } else {
            Err(Fault(Stage::Handover, FaultCode::ListenerClosed))
        };
        if let Err(fault) = check {
            link.retire();
            drain.close().await;
            return Err(fault);
        }
        self.publish(link, drain);
        Ok(())
    }

    fn publish(&mut self, link: Arc<Link>, drain: EndpointDrain) {
        self.links.send_replace(Some(link.clone()));
        transport::step(Some(&self.steps), QualificationKind::LinkPublished);
        PathStatus::publish(&self.status, link.selected_path(), false);
        let status = self.status.clone();
        let writer = self.status_writer.clone();
        let mut health = self.listener.health();
        let tx = self.links.clone();
        let retirement = self.retirement.clone();
        self.monitor = Some(tokio::spawn(async move {
            let mut accepting = true;
            let mut paths = link.conn.paths_stream();
            let mut deadlines = link.deadlines();
            let reason = loop {
                let bound = *deadlines.borrow_and_update();
                let remaining = bound.saturating_sub(link.clock.now());
                if remaining.is_zero() {
                    break FaultCode::Expired;
                }
                // Tokio's timer may pause during suspend. Bound idle teardown
                // latency after wake; stream admissions and I/O check directly.
                tokio::select! {
                    biased;
                    _=deadlines.changed()=>continue,
                    _=tokio::time::sleep(remaining.min(Duration::from_millis(250)))=>continue,
                    _=health.changed(), if accepting => {
                        accepting=false;
                        let _writer=writer.lock().unwrap();
                        if tx.borrow().as_ref().is_some_and(|l|Arc::ptr_eq(l,&link)) {
                            PathStatus::publish(&status,None,false);
                        }
                    },
                    next=paths.next()=>{
                        let Some(snapshot) = next else {break FaultCode::ConnectionEnded};
                        let Some(path) = link.selected_in(snapshot) else {break FaultCode::NoPermittedPath};
                        let _writer=writer.lock().unwrap();
                        if !tx.borrow().as_ref().is_some_and(|current|Arc::ptr_eq(current,&link)) {break FaultCode::Superseded;}
                        if accepting && health.has_changed().is_ok() {PathStatus::publish(&status,Some(path),false);}
                    }
                }
            };
            // The old watcher can finish while a new link is being published.
            {
                let _writer = writer.lock().unwrap();
                let mut closed = false;
                let removed = tx.send_if_modified(|value| {
                    if !value.as_ref().is_some_and(|v| Arc::ptr_eq(v, &link)) {
                        return false;
                    }
                    closed = link.claim_loss();
                    *value = None;
                    true
                });
                if removed {
                    let _ = retirement.send(Fault(Stage::Handover, reason));
                    PathStatus::publish(&status, None, closed);
                }
            }
            link.retire();
            drain.close().await;
        }));
    }
}
/// What an answer that is not a usable offer says. The instance's own
/// "unavailable" is not a malformed offer: it is classified by the family of
/// the reason the instance gave, and nothing of the answer is kept. What is
/// done about it does not change: the link is retired either way.
fn unusable_answer(body: &str) -> FaultCode {
    #[derive(serde::Deserialize)]
    struct Unavailable {
        version: u8,
        status: String,
        #[serde(default)]
        reason: String,
    }
    use FaultCode::*;
    if body.len() > rendezvous::MAX_ANSWER_BYTES {
        return InvalidOffer;
    }
    let Ok(answer) = serde_json::from_str::<Unavailable>(body) else {
        return InvalidOffer;
    };
    if answer.version != 1 || answer.status != "unavailable" {
        return InvalidOffer;
    }
    match answer.reason.as_str() {
        "certificate_not_ready" => NotReadyCertificate,
        "owner_required" => NotReadyOwnerRequired,
        "not_running" | "starting" | "stopped" | "state_changed" | "sidecar_exited" => NotReadyNotRunning,
        "permission_denied" | "permission_required" | "permission_expired" => NotReadyPermission,
        "transport_installing" | "transport_install_failed" | "transport_not_installed" | "transport_start_failed" => {
            NotReadyTransport
        }
        "setting_off" | "unpaired" | "hostname_not_direct" => NotReadyDisabled,
        "no_reachable_address" => NotReadyNoAddress,
        _ => NotReadyOther,
    }
}

impl Drop for Client {
    fn drop(&mut self) {
        self.listener.retire();
        // Detach rather than abort: the signalled monitor and drain own shutdown.
        drop(self.withdraw(false));
    }
}

#[cfg(test)]
mod diagnosis_tests {
    use super::*;

    #[tokio::test]
    async fn refresh_keeps_disposition_and_returns_only_classified_failures() {
        use rendezvous::Reply;
        let mut client = Client::new("desk-app.avibe.bot", "fixture".into()).await.unwrap();
        let unavailable = |reason: &str| {
            Reply::Ready(format!(
                r#"{{"version":1,"instance_id":"fixture","hostname":"desk-app.avibe.bot","status":"unavailable","reason":"{reason}","retry_after_seconds":60}}"#
            ))
        };
        for (reply, code) in [
            (Reply::Ready("not a wire offer".into()), FaultCode::InvalidOffer),
            // The instance's own "nothing to dial" is not a malformed offer.
            (unavailable("certificate_not_ready"), FaultCode::NotReadyCertificate),
            (unavailable("owner_required"), FaultCode::NotReadyOwnerRequired),
            (unavailable("not_running"), FaultCode::NotReadyNotRunning),
            (unavailable("stopped"), FaultCode::NotReadyNotRunning),
            (unavailable("permission_expired"), FaultCode::NotReadyPermission),
            (unavailable("transport_not_installed"), FaultCode::NotReadyTransport),
            (unavailable("hostname_not_direct"), FaultCode::NotReadyDisabled),
            (unavailable("no_reachable_address"), FaultCode::NotReadyNoAddress),
            (unavailable("a reason of a later instance"), FaultCode::NotReadyOther),
            // Another version, or a status that is neither, stays unreadable.
            (
                Reply::Ready(r#"{"version":2,"status":"unavailable","reason":"not_running"}"#.into()),
                FaultCode::InvalidOffer,
            ),
            (
                Reply::Ready(r#"{"version":1,"status":"later","reason":"not_running"}"#.into()),
                FaultCode::InvalidOffer,
            ),
            (Reply::NotReady, FaultCode::NotReady),
            (Reply::Refused(403), FaultCode::Denied),
            (Reply::Refused(503), FaultCode::Status),
            (Reply::SelectionChanged, FaultCode::Superseded),
        ] {
            assert_eq!(
                client.refresh_reply_detailed(|_| async move { reply }).await,
                (RefreshOutcome::Retired, Some(Fault(Stage::Rendezvous, code)))
            );
        }
    }
}
