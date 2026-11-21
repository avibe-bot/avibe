//! Public fallback dials only a checked numeric answer from the resolver. In
//! particular a second DNS lookup cannot redirect it into a private network.
use super::connect::Authority;
use std::net::{IpAddr, Ipv4Addr};
use tokio::net::{lookup_host, TcpStream};

fn public_v4(ip: Ipv4Addr) -> bool {
    let [a, b, c, _] = ip.octets();
    !(matches!(a, 0 | 10 | 127 | 224..=255)
        || a == 100 && (64..=127).contains(&b)
        || a == 169 && b == 254
        || a == 172 && (16..=31).contains(&b)
        || a == 192 && (b == 168 || b == 0 && matches!(c, 0 | 2))
        || a == 198 && (matches!(b, 18 | 19) || b == 51 && c == 100)
        || a == 203 && b == 0 && c == 113)
}

fn public_ip(ip: IpAddr) -> bool {
    match ip {
        IpAddr::V4(ip) => public_v4(ip),
        IpAddr::V6(ip) => {
            if let Some(v4) = ip.to_ipv4_mapped() {
                return public_v4(v4);
            }
            let bytes = ip.octets();
            if ip.segments()[..6] == [0x64, 0xff9b, 0, 0, 0, 0] {
                return public_v4(Ipv4Addr::new(bytes[12], bytes[13], bytes[14], bytes[15]));
            }
            bytes[0] & 0xe0 == 0x20
                && ip.segments()[0] != 0x2002
                && !(ip.segments()[0] == 0x2001 && matches!(ip.segments()[1], 0 | 0xdb8))
        }
    }
}

pub(super) async fn open(authority: &Authority) -> Option<TcpStream> {
    let addrs = lookup_host((authority.host(), 443)).await.ok()?;
    // Like DirectPublicBackend, give IPv4 and IPv6 a chance independently.
    let mut dials = tokio::task::JoinSet::new();
    for addr in addrs.filter(|a| public_ip(a.ip())).take(16) {
        dials.spawn(TcpStream::connect(addr));
    }
    while let Some(result) = dials.join_next().await {
        if let Ok(Ok(stream)) = result {
            return Some(stream);
        }
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn never_dial_a_local_or_translated_private_answer_as_public() {
        for ip in [
            "0.0.0.0",
            "127.0.0.1",
            "192.168.0.1",
            "100.64.0.1",
            "198.19.0.1",
            "::1",
            "fd00::1",
            "fe80::1",
            "::ffff:10.0.0.1",
            "64:ff9b::c0a8:0001",
            "2001:db8::1",
            "2002:7f00:1::1",
        ] {
            assert!(!public_ip(ip.parse().unwrap()));
        }
        for ip in ["8.8.8.8", "2606:4700:4700::1111", "64:ff9b::808:808"] {
            assert!(public_ip(ip.parse().unwrap()));
        }
    }
}
