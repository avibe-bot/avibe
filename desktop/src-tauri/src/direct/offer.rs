//! Parse only a private, native fetch completion. Unavailable and malformed
//! answers both leave the normal tunnel in use. There is no public API to
//! nominate an address, wall-clock comparison, or DNS for direct bootstrap.
use super::{
    connect::Authority,
    path::{class, global_v6, punched_family, shared_v4, AddressClass, AddressFamily, SelectedPath, TrialRelay},
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
    relay: Option<WireRelay>,
}
// Decode the object before converting anything to a JSON map. The struct
// variant rejects duplicate fields; malformed relay metadata alone must not
// discard otherwise valid IP candidates.
#[derive(Deserialize)]
#[serde(untagged)]
enum WireRelay {
    Fields { policy: String, url: String },
    Invalid(serde::de::IgnoredAny),
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

/// What the open paths of an authenticated connection come to under a grant.
#[derive(Debug, PartialEq, Eq)]
pub(super) enum Verdict {
    /// No path is selected in this snapshot.
    Unselected,
    /// The selected path is one the grant does not permit: the class of its
    /// address, for the report.
    Refused(AddressClass),
    Accepted(SelectedPath, AddressClass),
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

    /// Judges an authenticated connection by its selected path alone: iroh
    /// sends application data on that one. A path that is open and not
    /// selected, whatever its address, neither ends the link nor names it.
    pub(super) fn judge(&self, peer: EndpointId, paths: impl IntoIterator<Item = (bool, TransportAddr)>) -> Verdict {
        let Some((_, remote)) = paths.into_iter().find(|(selected, _)| *selected) else {
            return Verdict::Unselected;
        };
        let class = class(&remote);
        self.selected(peer, &remote)
            .map_or(Verdict::Refused(class), |path| Verdict::Accepted(path, class))
    }

    /// Called only with a path snapshot of the authenticated connection, never
    /// to authorize bootstrap. A LAN-only grant still requires exact membership.
    ///
    /// Under a grant that reaches across networks, a path the authenticated
    /// peer answers on in IPv4 shared address space (100.64/10) is a local
    /// one, as an unlisted private address is: two computers on one overlay
    /// network (a tailnet) or behind one carrier's NAT. `parse` lists no such
    /// address, so none is dialled before the peer is authenticated.
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
                if lan(*a) || shared_v4(*a) || (listed && a.ip().is_loopback()) {
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
        let relay = match w.endpoint.relay {
            Some(WireRelay::Fields { policy, url }) => TrialRelay::offered(&policy, &url),
            _ => None,
        };
        (!candidates.is_empty() || relay.is_some()).then_some(Self {
            relay,
            id,
            generation: w.generation,
            candidates,
            until,
        })
    }
}
