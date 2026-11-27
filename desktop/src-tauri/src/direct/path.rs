//! The wire grants an identity, not arbitrary UDP destinations or relay URLs.
//! Before authentication only the offer's `lan` and `ipv6` sockets are dialled.
use iroh::{RelayMap, RelayMode, RelayUrl, TransportAddr};
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
    pub fn offered(policy: &str, url: &str) -> Option<Self> {
        (policy == "public-trial-v1" && TRIAL.contains(&url)).then(|| Self(url.parse().expect("static trial URL")))
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

/// IPv4 shared address space, 100.64.0.0/10 (RFC 6598): a carrier's NAT, or
/// an overlay network such as a tailnet. A selected path there is a local
/// one (`Offer::selected`), as the phone App and the instance's own report
/// have it. Never a destination to dial first: no offer lists such an
/// address.
pub(super) fn shared_v4(a: SocketAddr) -> bool {
    matches!(a.ip(), IpAddr::V4(ip) if a.port() != 0 && ip.octets()[0] == 100 && ip.octets()[1] & 0xc0 == 64)
}

/// The class of the address a path runs to, for the reports: a fixed word,
/// never the address. It names what was seen and decides nothing; what a
/// grant permits is `Offer::selected`'s to say.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(super) enum AddressClass {
    Relay,
    /// 10/8, 172.16/12, 192.168/16.
    Private,
    /// 100.64/10, shared address space.
    Cgnat,
    /// fc00::/7.
    Ula,
    /// 169.254/16, fe80::/10.
    Linklocal,
    Loopback,
    /// A public IPv4 address.
    Public,
    /// A global unicast IPv6 address.
    Global,
    /// 64:ff9b::/96.
    Nat64,
    /// An IPv4 address written as an IPv6 one.
    Mapped,
    Other,
}

pub(super) fn class(remote: &TransportAddr) -> AddressClass {
    use AddressClass::*;
    let ip = match remote {
        TransportAddr::Relay(_) => return Relay,
        TransportAddr::Ip(a) => a.ip(),
        _ => return Other,
    };
    match ip {
        IpAddr::V4(ip) if ip.is_loopback() => Loopback,
        IpAddr::V4(ip) if ip.is_private() => Private,
        IpAddr::V4(ip) if ip.is_link_local() => Linklocal,
        IpAddr::V4(ip) if shared_v4(SocketAddr::new(ip.into(), 1)) => Cgnat,
        IpAddr::V4(ip) if global_v4(ip) => Public,
        IpAddr::V4(_) => Other,
        IpAddr::V6(ip) if ip.to_ipv4_mapped().is_some() => Mapped,
        IpAddr::V6(ip) if ip.segments()[..6] == [0x64, 0xff9b, 0, 0, 0, 0] => Nat64,
        IpAddr::V6(ip) if ip.is_loopback() => Loopback,
        IpAddr::V6(ip) if ip.is_unicast_link_local() => Linklocal,
        IpAddr::V6(ip) if ip.is_unique_local() => Ula,
        IpAddr::V6(ip) if global_v6(SocketAddr::new(ip.into(), 1)) => Global,
        IpAddr::V6(_) => Other,
    }
}

pub(super) fn punched_family(a: SocketAddr) -> Option<AddressFamily> {
    match a.ip() {
        IpAddr::V4(ip) if a.port() != 0 && global_v4(ip) => Some(AddressFamily::Ipv4),
        IpAddr::V6(_) if global_v6(a) => Some(AddressFamily::Ipv6),
        _ => None,
    }
}
