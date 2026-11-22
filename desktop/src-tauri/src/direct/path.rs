//! The wire grants an identity, not arbitrary UDP destinations or relay URLs.
//! Before authentication only the offer's `lan` and `ipv6` sockets are dialled.
use iroh::{RelayMap, RelayMode, RelayUrl};
use std::net::{IpAddr, Ipv4Addr, SocketAddr};

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum AddressFamily {
    Ipv4,
    Ipv6,
}

/// Observed selected iroh path. A relay URL doesn't identify the IP family of
/// the relay connection. `Punched` describes a selected cross-network IP path,
/// not proof of the NAT topology or which packet established reachability.
/// No peer, interface, address, or URL leaves the core.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum SelectedPath {
    Lan(AddressFamily),
    Punched(AddressFamily),
    Relay,
}

const TRIAL: [&str; 4] = [
    "https://use1-1.relay.n0.iroh.link./",
    "https://usw1-1.relay.n0.iroh.link./",
    "https://euc1-1.relay.n0.iroh.link./",
    "https://aps1-1.relay.n0.iroh.link./",
];

#[derive(Clone, PartialEq, Eq)]
pub(super) struct TrialRelay(RelayUrl);
impl TrialRelay {
    pub fn offered(value: &serde_json::Value) -> Option<Self> {
        let url = value.get("url")?.as_str()?;
        (value.get("policy")?.as_str()? == "public-trial-v1" && TRIAL.contains(&url))
            .then(|| Self(url.parse().expect("static trial URL")))
    }
    pub fn home(&self) -> RelayUrl {
        self.0.clone()
    }
    pub fn permits(&self, url: &RelayUrl) -> bool {
        TRIAL.contains(&url.as_str())
    }
    pub fn mode(relay: Option<&Self>) -> RelayMode {
        match relay {
            Some(_) => RelayMode::Custom(
                TRIAL
                    .iter()
                    .map(|url| url.parse::<RelayUrl>().expect("static trial URL"))
                    .collect::<RelayMap>(),
            ),
            None => RelayMode::Disabled,
        }
    }
}

pub(super) fn global_v6(a: SocketAddr) -> bool {
    let SocketAddr::V6(v) = a else { return false };
    let [hi, next, ..] = v.ip().segments();
    v.port() != 0
        && v.scope_id() == 0
        && hi & 0xe000 == 0x2000
        && !(hi == 0x2001 && (next < 0x200 || next == 0xdb8))
        && hi != 0x2002
        && !(hi == 0x3fff && next < 0x1000)
}

// Public IPv4 may be *observed* after peer authentication, never bootstrapped.
fn global_v4(ip: Ipv4Addr) -> bool {
    let [a, b, c, _] = ip.octets();
    !ip.is_private()
        && !ip.is_loopback()
        && !ip.is_link_local()
        && !ip.is_documentation()
        && a != 0
        && a < 224
        && !(a == 100 && (64..128).contains(&b))
        && !(a == 192 && ((b == 0 && c == 0) || (b == 88 && c == 99)))
        && !(a == 198 && matches!(b, 18 | 19))
}

pub(super) fn punched_family(a: SocketAddr) -> Option<AddressFamily> {
    match a.ip() {
        IpAddr::V4(ip) if a.port() != 0 && global_v4(ip) => Some(AddressFamily::Ipv4),
        IpAddr::V6(_) if global_v6(a) => Some(AddressFamily::Ipv6),
        _ => None,
    }
}
