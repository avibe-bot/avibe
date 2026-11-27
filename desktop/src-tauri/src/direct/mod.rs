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
pub use credentials::WindowRegistration;
pub use quiet::PublicCensus;
#[cfg(test)]
#[path = "../../tests/direct/cross_network.rs"]
mod cross_network_tests;
#[cfg(test)]
#[path = "../../tests/direct/drain.rs"]
mod drain_tests;
mod faults;
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
mod usage;
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
use transport::{Fault, FaultCode, Stage, Timed};

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

/// A link was withdrawn: said once in the fault record and once in the
/// minute's count, under the kind of path it was on.
struct Withdrawn {
    faults: Arc<faults::Faults>,
    usage: Arc<usage::Usage>,
    strict: bool,
    /// From the link's publication to its withdrawal.
    lived: Duration,
}
impl Withdrawn {
    fn record(self, fault: Fault, path: faults::PathWord) {
        self.usage.retired(path, fault.1);
        self.faults.record(fault, path, self.strict, self.lived);
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
    /// The legs that failed and the links that were withdrawn, for the
    /// instance's log; sent with the rendezvous request.
    faults: Arc<faults::Faults>,
    /// When the link that is up was published, on `clock`.
    published: Option<Duration>,
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
            faults: Arc::default(),
            published: None,
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
        let asked = self.clock.now().saturating_sub(sent);
        self.apply(answer.as_deref(), sent, asked).await.is_ok()
    }

    /// A missing reply is not a withdrawal. Preserve only an already usable
    /// link, under its original monotonic deadline and path monitor; never
    /// extend admission or create an endpoint without a validated offer.
    async fn refresh_reply<F>(&mut self, fetch: impl FnOnce(rendezvous::Asked) -> F) -> RefreshOutcome
    where
        F: std::future::Future<Output = rendezvous::Reply>,
    {
        self.refresh_reply_detailed(fetch).await.0
    }

    /// Same operation and authority as refresh_reply. The reason is independent
    /// of disposition: a failed refresh may safely retain an earlier lease.
    ///
    /// The request also carries this client's report for the instance's
    /// log: the legs that failed and the links that were withdrawn since the
    /// last answered request, and which leg carried the windows'
    /// connections in each minute that is over. Best effort: what a request
    /// did not deliver is kept for the next, for as long as it is retained.
    /// An entry can be lost (pushed out by newer ones, or too old, before
    /// any request is answered; or the session ends), and can arrive more
    /// than once (each time the instance takes a request whose answer is
    /// then lost).
    async fn refresh_reply_detailed<F>(
        &mut self,
        fetch: impl FnOnce(rendezvous::Asked) -> F,
    ) -> (RefreshOutcome, Option<Fault>)
    where
        F: std::future::Future<Output = rendezvous::Reply>,
    {
        let sent = self.clock.now();
        let usage = self.listener.access.usage.clone();
        let (faults, minutes) = (self.faults.take(), usage.take());
        let now = usage.minute();
        let asked = rendezvous::Asked {
            endpoint: self.key.public().to_string(),
            report: rendezvous::Report {
                leg_faults: faults.iter().map(faults::LegFault::json).collect(),
                path_use: minutes.iter().map(|entry| entry.json(now)).collect(),
            },
        };
        let result = tokio::time::timeout(std::time::Duration::from_secs(5), fetch(asked)).await;
        let answered = self.clock.now();
        // Delivered only when the instance answered the request itself.
        if !matches!(result, Ok(rendezvous::Reply::Ready(_))) {
            self.faults.restore(faults);
            usage.restore(minutes);
        }
        // The kind of path the refresh was made on, before its outcome.
        let path = self.path_word();
        let (outcome, failed) = self.settle(result, sent, answered).await;
        if let Some(Timed(fault, ran)) = failed {
            self.faults
                .record(fault, path, self.listener.access.policy.direct_only(), ran);
        }
        (outcome, failed.map(|Timed(fault, _)| fault))
    }

    /// The kind of direct path new connections prefer now.
    fn path_word(&self) -> faults::PathWord {
        faults::PathWord::of(match self.status.borrow().preferred {
            PreferredPath::Direct(path) => Some(path),
            PreferredPath::Tunnel => None,
        })
    }

    async fn settle(
        &mut self,
        result: Result<rendezvous::Reply, tokio::time::error::Elapsed>,
        sent: Duration,
        answered: Duration,
    ) -> (RefreshOutcome, Option<Timed>) {
        use rendezvous::Reply;
        use FaultCode::*;
        // A fault of the request itself ran for as long as the request did.
        let asked = answered.saturating_sub(sent);
        let code = match result {
            Ok(Reply::Ready(body)) => {
                return match self.apply(Some(&body), sent, asked).await {
                    Ok(()) => (RefreshOutcome::Renewed, None),
                    Err(reason) => (RefreshOutcome::Retired, Some(reason)),
                }
            }
            // The outer deadline, or the adapter's own a second before it.
            Err(_) | Ok(Reply::TimedOut) => Timeout,
            Ok(Reply::NotReady) => NotReady,
            Ok(Reply::Failed) => Other,
            Ok(Reply::Refused(408 | 429 | 500..=599)) => Status,
            Ok(reply) => {
                let code = match reply {
                    Reply::SelectionChanged => {
                        if let Some(task) = self.withdraw(false, Superseded) {
                            let _ = task.await;
                        }
                        Superseded
                    }
                    other => {
                        let code = match other {
                            Reply::Controlled | Reply::Refused(401 | 403) => Denied,
                            _ => Refused,
                        };
                        self.retire(code).await;
                        code
                    }
                };
                return (
                    RefreshOutcome::Retired,
                    Some(Timed(Fault(Stage::Rendezvous, code), asked)),
                );
            }
        };
        // One look at the link decides both: whether it is kept, and if not,
        // the loss it is withdrawn for. The request only failed to renew it.
        let (remaining, lost) = match self.links.borrow().as_ref() {
            None => (Duration::ZERO, None),
            Some(link) => match link.check_path(Stage::Handover) {
                Ok(()) => (link.remaining(), None),
                Err(Fault(_, lost)) => (Duration::ZERO, Some(lost)),
            },
        };
        let disposition = if remaining.is_zero() {
            // Usable a moment ago with nothing left now: the lease ran out.
            self.retire(lost.unwrap_or(Expired)).await;
            RefreshOutcome::Retired
        } else {
            RefreshOutcome::Retained {
                retry_before: (remaining / 2).min(Duration::from_secs(5)),
            }
        };
        (disposition, Some(Timed(Fault(Stage::Rendezvous, code), asked)))
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

    /// `reason` is what the caller decided on, and is recorded as given:
    /// `superseded` only for a replacement by a newer offer, a changed
    /// selection or the end of the session; the answer's code for a refresh
    /// the instance refused or answered with no offer; the loss the caller
    /// observed for a link that had nothing left. Nothing is read from the
    /// link again here: it may say something else by now.
    fn withdraw(&mut self, involuntary: bool, reason: FaultCode) -> Option<JoinHandle<()>> {
        let _writer = self.status_writer.lock().unwrap();
        if let Some(l) = self.links.send_replace(None) {
            // Withdraw immediately; a zero deadline also wakes the monitor.
            let closed = l.claim_loss();
            // The kind of path the link was last on, whether or not new
            // connections still preferred it.
            let path = faults::PathWord::of(l.last_path());
            l.retire();
            let fault = Fault(Stage::Handover, reason);
            Withdrawn {
                faults: self.faults.clone(),
                usage: self.listener.access.usage.clone(),
                strict: self.listener.access.policy.direct_only(),
                lived: self
                    .published
                    .take()
                    .map(|at| self.clock.now().saturating_sub(at))
                    .unwrap_or_default(),
            }
            .record(fault, path);
            PathStatus::publish(&self.status, None, involuntary && closed);
        }
        self.monitor.take()
    }
    async fn retire(&mut self, reason: FaultCode) {
        if let Some(task) = self.withdraw(true, reason) {
            let _ = task.await;
        }
    }
    /// Only the native isolated-world adapter may supply a completion. No
    /// command or message handler accepts a rendezvous answer from a page.
    ///
    /// `asked` is how long the request took. A failure says how long its own
    /// stage ran: the request for an answer that is no offer, nothing for a
    /// check, the bind or the dial for those.
    async fn apply(&mut self, answer: Option<&str>, sent: Duration, asked: Duration) -> Result<(), Timed> {
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
            let code = answer.map_or(FaultCode::InvalidOffer, unusable_answer);
            self.retire(code).await;
            return Err(Timed(Fault(Stage::Rendezvous, code), asked));
        };
        if self.links.borrow().as_ref().is_some_and(|link| link.renew(&offer)) {
            return Ok(());
        }
        // Retire previous authority before starting a replacement. Never use a
        // stale result to make a new stream while waiting for the next probe.
        self.retire(FaultCode::Superseded).await;
        if !self.listener.is_alive() {
            return Err(Timed(Fault(Stage::Handover, FaultCode::ListenerClosed), Duration::ZERO));
        }
        let begun = self.clock.now();
        if begun >= offer.until {
            return Err(Timed(Fault(Stage::Dial, FaultCode::Expired), Duration::ZERO));
        }
        // A non-renewable grant gets a fresh endpoint. In particular an old
        // relay map or iroh peer-address cache cannot cross a policy change.
        let bound = tokio::time::timeout(
            offer.until.saturating_sub(begun),
            transport::endpoint_builder(self.key.clone(), offer.relay.as_ref()).bind(),
        )
        .await;
        let endpoint = match bound {
            Ok(Ok(endpoint)) => endpoint,
            failed => {
                let code = if failed.is_err() {
                    FaultCode::Timeout
                } else {
                    FaultCode::Other
                };
                return Err(Timed(Fault(Stage::Dial, code), self.clock.now().saturating_sub(begun)));
            }
        };
        self.probe_detailed(endpoint, offer).await
    }

    #[cfg(test)]
    async fn probe(&mut self, endpoint: Endpoint, offer: Offer) -> bool {
        self.probe_detailed(endpoint, offer).await.is_ok()
    }

    async fn probe_detailed(&mut self, endpoint: Endpoint, offer: Offer) -> Result<(), Timed> {
        let drain = EndpointDrain::new(endpoint.clone());
        let link = match Link::dial_detailed(&endpoint, offer, &self.host, self.clock.clone()).await {
            Ok(link) => link,
            // Its duration was taken where it failed, before this drain.
            Err(failed) => {
                drain.close().await;
                return Err(failed);
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
            // A check, not a wait.
            return Err(Timed(fault, Duration::ZERO));
        }
        self.publish(link, drain);
        Ok(())
    }

    fn publish(&mut self, link: Arc<Link>, drain: EndpointDrain) {
        self.links.send_replace(Some(link.clone()));
        let path = link.selected_path();
        // What its withdrawal is reported under, until the monitor below
        // sees the connection move to another permitted path.
        if let Some(path) = path {
            link.remember(path);
        }
        PathStatus::publish(&self.status, path, false);
        let status = self.status.clone();
        let writer = self.status_writer.clone();
        let mut health = self.listener.health();
        let tx = self.links.clone();
        let published = self.clock.now();
        self.published = Some(published);
        let (faults, usage) = (self.faults.clone(), self.listener.access.usage.clone());
        let policy = self.listener.access.policy.clone();
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
                        let path = match link.judged(snapshot) {
                            Ok(path) => path,
                            Err(code) => break code,
                        };
                        link.remember(path);
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
                    let fault = Fault(Stage::Handover, reason);
                    Withdrawn {
                        faults,
                        usage,
                        strict: policy.direct_only(),
                        lived: link.clock.now().saturating_sub(published),
                    }
                    // The kind of path the link was last on: by now it
                    // has none, and new connections may have left it before.
                    .record(fault, faults::PathWord::of(link.last_path()));
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
        drop(self.withdraw(false, FaultCode::Superseded));
    }
}

#[cfg(test)]
mod diagnosis_tests {
    use super::*;

    /// The body of the request a refresh would make, and the reply to it.
    async fn refreshed(client: &mut Client, reply: rendezvous::Reply) -> serde_json::Value {
        let body = Arc::new(std::sync::Mutex::new(String::new()));
        let seen = body.clone();
        client
            .refresh_reply(|asked| async move {
                let request = rendezvous::Request::asked("https://desk-app.avibe.bot", &asked).expect("a request");
                let [_, _, (_, text)] = request.arguments();
                *seen.lock().unwrap() = text.to_owned();
                reply
            })
            .await;
        let text = body.lock().unwrap().clone();
        serde_json::from_str(&text).expect("a JSON body")
    }

    #[tokio::test]
    async fn a_request_reports_each_fault_once_and_keeps_what_the_instance_did_not_answer() {
        use rendezvous::Reply;
        let mut client = Client::new("desk-app.avibe.bot", "fixture".into()).await.unwrap();
        let unavailable = || {
            Reply::Ready(
                r#"{"version":1,"instance_id":"fixture","hostname":"desk-app.avibe.bot","status":"unavailable","reason":"not_running","retry_after_seconds":60}"#
                    .into(),
            )
        };
        // Nothing to report: the request is the two keys it always was.
        let first = refreshed(&mut client, Reply::Failed).await;
        assert_eq!(first.as_object().unwrap().len(), 2);
        // That request failed: said with the next one. It fails too, so the
        // entry is kept, and the same fault again is counted on it.
        let second = refreshed(&mut client, Reply::Failed).await;
        assert_eq!(
            second["leg_faults"],
            serde_json::json!([{
                "stage": "rendezvous", "code": "other", "path": "none",
                "strict": false, "ms": second["leg_faults"][0]["ms"], "count": 1,
            }])
        );
        assert!(second.get("path_use").is_none());
        // The instance answers: the kept entry is delivered with it, once.
        let third = refreshed(&mut client, unavailable()).await;
        assert_eq!(third["leg_faults"].as_array().unwrap().len(), 1);
        assert_eq!(third["leg_faults"][0]["count"], 2);
        // What that answer said is the next request's only entry.
        client.handover().set_direct_only(true);
        let fourth = refreshed(&mut client, unavailable()).await;
        assert_eq!(
            fourth["leg_faults"]
                .as_array()
                .unwrap()
                .iter()
                .map(|fault| (fault["code"].as_str().unwrap(), fault["strict"].as_bool().unwrap()))
                .collect::<Vec<_>>(),
            [("not_ready_not_running", false)]
        );
        // And the one after it says the leg ran in direct-only mode.
        let fifth = refreshed(&mut client, unavailable()).await;
        assert_eq!(fifth["leg_faults"][0]["strict"], true);
    }

    /// A fault's duration is its own stage's. The request's time is the
    /// rendezvous stage's alone: a later stage does not inherit it.
    #[tokio::test]
    async fn a_fault_says_how_long_its_own_stage_ran() {
        use rendezvous::Reply;
        use std::sync::atomic::{AtomicU64, Ordering};
        let mut client = Client::new("desk-app.avibe.bot", "fixture".into()).await.unwrap();
        let millis = Arc::new(AtomicU64::new(10_000));
        let now = millis.clone();
        client.clock = Clock(Arc::new(move || Duration::from_millis(now.load(Ordering::SeqCst))));
        let peer = SecretKey::generate().public();
        let offer = serde_json::json!({
            "version": 1, "status": "ready", "hostname": "desk-app.avibe.bot", "instance_id": "fixture",
            "generation": "a", "issued_at": 10, "expires_at": 300, "alpn": "avibe-direct/1",
            "endpoint": {"id": peer.to_string(), "addresses": [{"kind": "lan", "address": "192.168.1.4:345"}]},
            "client": {"endpoint_id": client.key.public().to_string(), "admitted_until": 300},
        })
        .to_string();
        let unavailable = r#"{"version":1,"instance_id":"fixture","hostname":"desk-app.avibe.bot","status":"unavailable","reason":"not_running","retry_after_seconds":60}"#;
        // Each request takes as long as its row says before it is answered.
        for (took, reply, stage, code, ms) in [
            // An answer that is no offer: the request's own time.
            (
                3_000,
                Reply::Ready(unavailable.into()),
                "rendezvous",
                "not_ready_not_running",
                3_000,
            ),
            // The adapter's own deadline is a time-out, as the outer one is.
            (4_000, Reply::TimedOut, "rendezvous", "timeout", 4_000),
            (1_500, Reply::Failed, "rendezvous", "other", 1_500),
            // A good offer after a slow request, with the listener ended:
            // what failed is a check, and it took no time.
            (3_000, Reply::Ready(offer), "handover", "listener_closed", 0),
        ] {
            if stage == "handover" {
                client.listener.retire();
            }
            let clock = millis.clone();
            let outcome = client
                .refresh_reply(|_| async move {
                    clock.fetch_add(took, Ordering::SeqCst);
                    reply
                })
                .await;
            // No link is up: nothing is retained, whatever failed.
            assert_eq!(outcome, RefreshOutcome::Retired);
            // Taken here, so that the next request carries nothing of it.
            let recorded: Vec<_> = client.faults.take().iter().map(faults::LegFault::json).collect();
            assert_eq!(
                recorded,
                [serde_json::json!({
                    "stage": stage, "code": code, "path": "none", "strict": false, "ms": ms, "count": 1,
                })]
            );
        }
    }

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
            (Reply::TimedOut, FaultCode::Timeout),
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
