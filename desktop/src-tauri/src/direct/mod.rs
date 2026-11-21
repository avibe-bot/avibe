//! One controller per native-selected remote origin and signed-in session.
//! Dropping it on a selection change retires the proxy and the peer. Never
//! identify an instance or session by its installation ID.
mod connect;
#[cfg(test)]
#[path = "../../tests/direct/sidecar.rs"]
mod interop;
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
use iroh::{Endpoint, TransportAddr};
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
    endpoint: Endpoint,
    links: watch::Sender<Option<Arc<Link>>>,
    listener: Option<Listener>,
    monitor: Option<JoinHandle<()>>,
}
impl Client {
    /// Create only for the current managed origin, once per signed-in native
    /// session. No TCP listener or browser routing on this initial path.
    pub async fn new(host: &str, instance: String) -> Option<Self> {
        let host = Authority::managed(host)?;
        if instance.is_empty() {
            return None;
        }
        Some(Self {
            host,
            instance,
            endpoint: transport::bind().await?,
            links: watch::channel(None).0,
            listener: None,
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
        let answer = tokio::time::timeout(std::time::Duration::from_secs(5), fetch(self.endpoint.id().to_string()))
            .await
            .ok()
            .flatten();
        let ready = self.apply(answer.as_deref(), sent).await;
        if !ready {
            self.listener.take();
        }
        ready
    }

    /// No route before a successful probe, or after expiry or loss. A listener
    /// already referenced by a browser may still serve public fallback until
    /// the native caller detaches the route and drops this session's client.
    pub fn route(&self) -> Option<Route<'_>> {
        if !self.links.borrow().as_ref().is_some_and(|link| link.usable()) {
            return None;
        }
        let listener = self.listener.as_ref()?;
        let (username, password) = listener.credential.native_login();
        Some(Route {
            host: self.host.host(),
            proxy: std::net::SocketAddrV4::new(std::net::Ipv4Addr::LOCALHOST, listener.port),
            username,
            password,
        })
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
            answer.and_then(|body| Offer::parse(body, &self.host, &self.instance, self.endpoint.id(), sent))
        else {
            self.retire();
            return false;
        };
        if self.links.borrow().as_ref().is_some_and(|link| link.renew(&offer)) {
            return true;
        }
        // Retire previous authority before starting a replacement. Never use a
        // stale result to make a new stream while waiting for the next probe.
        self.retire();
        let Some(link) = Link::dial(&self.endpoint, offer, &self.host).await else {
            return false;
        };
        if self.listener.is_none() {
            self.listener = Listener::bind(self.host.clone(), self.links.subscribe()).await;
        }
        if self.listener.is_none() || !link.usable() {
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
