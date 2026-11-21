//! Parse only a private, native fetch completion. Unavailable and malformed
//! answers both leave the normal tunnel in use. There is no public API to
//! nominate an address, wall-clock comparison, or DNS for direct bootstrap.
use super::connect::Authority;
use iroh::EndpointId;
use serde::Deserialize;
use std::{
    net::{IpAddr, SocketAddr},
    time::Duration,
};
use tokio::time::Instant;

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
    pub until: Instant,
}

pub(super) fn lan(addr: SocketAddr) -> bool {
    addr.port() != 0
        && match addr.ip() {
            IpAddr::V4(ip) => ip.is_private(),
            IpAddr::V6(ip) => ip.is_unique_local() && matches!(addr,SocketAddr::V6(v6) if v6.scope_id()==0),
        }
}
impl Offer {
    pub(super) fn parse(
        raw: &str,
        origin: &Authority,
        instance: &str,
        client: EndpointId,
        sent: Instant,
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
        if Instant::now() >= until {
            return None;
        }
        let mut candidates = Vec::new();
        for entry in w.endpoint.addresses.iter().take(32) {
            if entry.get("kind").and_then(|s| s.as_str()) != Some("lan") {
                continue;
            }
            if let Some(a) = entry
                .get("address")
                .and_then(|s| s.as_str())
                .filter(|s| s.len() <= 64 && !s.contains('%'))
                .and_then(|s| s.parse().ok())
                .filter(|a| lan(*a))
            {
                if !candidates.contains(&a) {
                    candidates.push(a);
                    if candidates.len() == 8 {
                        break;
                    }
                }
            }
        }
        (!candidates.is_empty()).then_some(Self {
            id,
            generation: w.generation,
            candidates,
            until,
        })
    }
}
