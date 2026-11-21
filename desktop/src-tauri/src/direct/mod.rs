//! One controller per native-selected remote origin and signed-in session.
//! Dropping it on a selection change retires the proxy and the peer. Never
//! identify an instance or session by its installation ID.
mod connect;
#[cfg(test)]
#[path = "../../tests/direct/sidecar.rs"]
mod interop;
#[cfg(test)]
#[path = "../../tests/direct/lifecycle.rs"]
mod lifecycle_tests;
mod listener;
mod offer;
#[cfg(test)]
#[path = "../../tests/direct/offer.rs"]
mod offer_tests;
mod public;
mod selection;
mod transport;

use connect::Authority;
use futures_util::StreamExt;
use iroh::{Endpoint, SecretKey, TransportAddr};
use listener::Listener;
use offer::Offer;
use std::sync::Arc;
use tokio::{
    sync::watch,
    task::JoinHandle,
    time::{sleep_until, Instant},
};
use transport::Link;

/// Borrowed native route material, deliberately neither serializable nor Debug.
/// Never put the proxy credential in a URL, browser argument, page bridge or log.
pub struct Route<'a> {
    pub host: &'a str,
    pub proxy: std::net::SocketAddrV4,
    pub username: &'a str,
    pub password: &'a str,
}

pub struct Client {
    host: Authority,
    instance: String,
    key: SecretKey,
    endpoint: Option<Endpoint>,
    links: watch::Sender<Option<Arc<Link>>>,
    listener: Listener,
    monitor: Option<JoinHandle<()>>,
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
        let listener = Listener::bind(host.clone(), rx).await?;
        Some(Self {
            host,
            instance,
            key: SecretKey::generate(),
            endpoint: None,
            links,
            listener,
            monitor: None,
        })
    }
    /// Ask the selected session's native adapter for a same-origin reply. The
    /// caller must verify the session/document is still current on completion;
    /// the core binds the body to this instance, hostname, client key and time.
    pub async fn refresh<F>(&mut self, fetch: impl FnOnce(String) -> F) -> bool
    where
        F: std::future::Future<Output = Option<String>>,
    {
        let sent = Instant::now();
        let answer = tokio::time::timeout(std::time::Duration::from_secs(5), fetch(self.key.public().to_string()))
            .await
            .ok()
            .flatten();
        self.apply(answer.as_deref(), sent).await
    }

    /// The route describes the proxy, not direct-path readiness. Its port and
    /// credential survive loss of direct. Drop the client only after native detach.
    pub fn route(&self) -> Option<Route<'_>> {
        let listener = &self.listener;
        if !listener.is_alive() {
            return None;
        }
        let (username, password) = listener.credential.native_login();
        Some(Route {
            host: self.host.host(),
            proxy: std::net::SocketAddrV4::new(std::net::Ipv4Addr::LOCALHOST, listener.port),
            username,
            password,
        })
    }

    /// Subscribe before route installation. `changed().await` returning `Err`
    /// means this listener ended: detach the native route and rebuild if eligible.
    /// Direct-path availability or refresh failure does not close this signal.
    pub fn listener_lifetime(&self) -> watch::Receiver<()> {
        self.listener.health()
    }

    fn retire(&mut self) {
        if let Some(t) = self.monitor.take() {
            t.abort()
        }
        if let Some(l) = self.links.send_replace(None) {
            l.retire()
        }
    }
    /// Only the native isolated-world adapter may supply a completion. No
    /// command or message handler accepts a rendezvous answer from a page.
    async fn apply(&mut self, answer: Option<&str>, sent: Instant) -> bool {
        let Some(offer) =
            answer.and_then(|body| Offer::parse(body, &self.host, &self.instance, self.key.public(), sent))
        else {
            self.retire();
            if let Some(endpoint) = self.endpoint.take() {
                endpoint.close().await;
            }
            return false;
        };
        if self.links.borrow().as_ref().is_some_and(|link| link.renew(&offer)) {
            return true;
        }
        // Retire previous authority before starting a replacement. Never use a
        // stale result to make a new stream while waiting for the next probe.
        self.retire();
        if !self.listener.is_alive() || Instant::now() >= offer.until {
            return false;
        }
        if self.endpoint.is_none() {
            self.endpoint = tokio::time::timeout_at(offer.until, transport::bind(self.key.clone()))
                .await
                .ok()
                .flatten();
        }
        let Some(endpoint) = self.endpoint.as_ref() else {
            return false;
        };
        let Some(link) = Link::dial(endpoint, offer, &self.host).await else {
            return false;
        };
        if !self.listener.is_alive() || !link.usable() {
            return false;
        }
        self.links.send_replace(Some(link.clone()));
        let tx = self.links.clone();
        self.monitor = Some(tokio::spawn(async move {
            let mut paths = link.conn.paths_stream();
            let mut deadlines = link.deadlines();
            loop {
                let bound = *deadlines.borrow_and_update();
                tokio::select! {
                    biased;
                    _=deadlines.changed()=>continue,
                    _=sleep_until(bound)=>break,
                    next=paths.next()=>{let Some(snapshot)=next else{break};if !snapshot.iter().any(|p|p.is_selected()&&matches!(p.remote_addr(),TransportAddr::Ip(a) if link.offer.candidates.contains(a))){break}}
                }
            }
            // The old watcher can finish while a new link is being published.
            tx.send_if_modified(|value| {
                if value.as_ref().is_some_and(|v| Arc::ptr_eq(v, &link)) {
                    *value = None;
                    true
                } else {
                    false
                }
            });
            link.retire();
        }));
        true
    }
}
impl Drop for Client {
    fn drop(&mut self) {
        self.retire()
    }
}
