//! Parse only a private, native fetch completion. Unavailable and malformed
//! answers both leave the normal tunnel in use. There is no public API to
//! nominate an address, wall-clock comparison, or DNS for direct bootstrap.
use super::{
    connect::Authority,
    path::{global_v6, punched_family, AddressFamily, SelectedPath, TrialRelay},
};
use iroh::{EndpointAddr, EndpointId, TransportAddr};
use serde::Deserialize;
use std::{
    net::{IpAddr, SocketAddr},
    time::Duration,
};

pub(super) const ALPN: &[u8] = b"avibe-direct/1";
#[derive(Deserialize)]
struct Wire {
    version: u8,
    status: String,
    instance_id: String,
    hostname: String,
    generation: String,
    issued_at: u64,
    expires_at: u64,
    alpn: String,
    endpoint: Endpoint,
    client: Client,
}
#[derive(Deserialize)]
struct Endpoint {
    id: String,
    addresses: Vec<serde_json::Value>,
    #[serde(default)]
    relay: serde_json::Value,
}
#[derive(Deserialize)]
struct Client {
    endpoint_id: String,
    admitted_until: u64,
}

pub(super) struct Offer {
    pub generation: String,
    pub id: EndpointId,
    pub candidates: Vec<SocketAddr>,
    pub relay: Option<TrialRelay>,
    pub until: Duration,
}

pub(super) fn lan(addr: SocketAddr) -> bool {
    addr.port() != 0
        && match addr.ip() {
            IpAddr::V4(ip) => ip.is_private(),
            IpAddr::V6(ip) => ip.is_unique_local() && matches!(addr,SocketAddr::V6(v6) if v6.scope_id()==0),
        }
}
impl Offer {
    pub(super) fn bootstrap(&self) -> EndpointAddr {
        let mut addr = EndpointAddr::new(self.id);
        for candidate in &self.candidates {
            addr = addr.with_ip_addr(*candidate);
        }
        if let Some(r) = &self.relay {
            addr = addr.with_relay_url(r.home());
        }
        addr
    }

    /// Called only with a path snapshot of the authenticated connection, never
    /// to authorize bootstrap. A LAN-only grant still requires exact membership.
    pub(super) fn selected(&self, peer: EndpointId, remote: &TransportAddr) -> Option<SelectedPath> {
        if peer != self.id {
            return None;
        }
        match remote {
            TransportAddr::Relay(url) => self
                .relay
                .as_ref()
                .filter(|r| r.permits(url))
                .map(|_| SelectedPath::Relay),
            TransportAddr::Ip(a) => {
                let listed = self.candidates.contains(a);
                if !listed && self.relay.is_none() && !self.candidates.iter().copied().any(global_v6) {
                    return None;
                }
                if lan(*a) || (listed && a.ip().is_loopback()) {
                    // The private loopback address can be listed only by the confined interop fixture.
                    Some(SelectedPath::Lan(if a.is_ipv4() {
                        AddressFamily::Ipv4
                    } else {
                        AddressFamily::Ipv6
                    }))
                } else {
                    punched_family(*a).map(SelectedPath::Punched)
                }
            }
            _ => None,
        }
    }
    pub(super) fn parse(
        raw: &str,
        origin: &Authority,
        instance: &str,
        client: EndpointId,
        sent: Duration,
        now: Duration,
    ) -> Option<Self> {
        if raw.len() > 16384 || instance.is_empty() {
            return None;
        }
        let w: Wire = serde_json::from_str(raw).ok()?;
        if w.version != 1
            || w.status != "ready"
            || w.alpn.as_bytes() != ALPN
            || w.hostname != origin.host()
            || w.instance_id != instance
            || w.client.endpoint_id != client.to_string()
            || w.generation.is_empty()
            || w.generation.len() > 128
        {
            return None;
        }
        let id: EndpointId = w.endpoint.id.parse().ok()?;
        if id == client || id.to_string() != w.endpoint.id {
            return None;
        }
        let span = w
            .expires_at
            .min(w.client.admitted_until)
            .checked_sub(w.issued_at)?
            .min(300);
        let until = sent.checked_add(Duration::from_secs(span))?;
        if now >= until {
            return None;
        }
        let mut candidates = Vec::new();
        for entry in w.endpoint.addresses.iter().take(32) {
            let kind = entry.get("kind").and_then(|s| s.as_str());
            if let Some(a) = entry
                .get("address")
                .and_then(|s| s.as_str())
                .filter(|s| s.len() <= 64 && !s.contains('%'))
                .and_then(|s| s.parse().ok())
                .filter(|a| matches!(kind, Some("lan") if lan(*a)) || matches!(kind, Some("ipv6") if global_v6(*a)))
            {
                if !candidates.contains(&a) {
                    candidates.push(a);
                    if candidates.len() == 8 {
                        break;
                    }
                }
            }
        }
        let relay = TrialRelay::offered(&w.endpoint.relay);
        (!candidates.is_empty() || relay.is_some()).then_some(Self {
            relay,
            id,
            generation: w.generation,
            candidates,
            until,
        })
    }
}
