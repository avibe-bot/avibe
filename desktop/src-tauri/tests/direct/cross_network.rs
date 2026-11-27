use super::{
    connect::Authority,
    offer::{Offer, Verdict},
    path::{class, AddressClass, TrialRelay},
    AddressFamily::{Ipv4, Ipv6},
    SelectedPath::{Lan, Punched, Relay},
};
use iroh::{RelayMode, SecretKey, TransportAddr};
use serde_json::{json, Value};
use std::time::Duration;

fn parse(addresses: Value, relay: Value) -> Option<Offer> {
    parse_relay_json(addresses, &relay.to_string())
}

fn parse_relay_json(addresses: Value, relay_json: &str) -> Option<Offer> {
    let client = SecretKey::from_bytes(&[1; 32]).public();
    let peer = SecretKey::from_bytes(&[2; 32]).public();
    let host = Authority::managed("one-app.avibe.bot").unwrap();
    let raw = json!({"version":1,"status":"ready","hostname":host.host(),"instance_id":"one","generation":"g","issued_at":10,"expires_at":40,"alpn":"avibe-direct/1","endpoint":{"id":peer.to_string(),"addresses":addresses,"relay":"RELAY_JSON"},"client":{"endpoint_id":client.to_string(),"admitted_until":30}});
    let text = raw.to_string().replace("\"RELAY_JSON\"", relay_json);
    Offer::parse(&text, &host, "one", client, Duration::ZERO, Duration::ZERO)
}

#[test]
fn only_exact_trial_policy_and_home_url_enable_the_custom_map() {
    let home = "https://aps1-1.relay.n0.iroh.link./";
    let offer = parse(json!([]), json!({"policy":"public-trial-v1","url":home})).unwrap();
    let relay = offer.relay.as_ref().unwrap();
    let target = offer.bootstrap();
    assert_eq!(target.id, offer.id);
    assert_eq!(target.ip_addrs().count(), 0);
    assert_eq!(target.relay_urls().map(|url| url.as_str()).collect::<Vec<_>>(), [home]);
    let RelayMode::Custom(map) = TrialRelay::mode(Some(relay)) else {
        panic!("not a custom map")
    };
    assert_eq!(map, iroh::defaults::prod::default_relay_map());
    assert_eq!(map.urls::<Vec<_>>().len(), 4);
    for url in map.urls::<Vec<_>>() {
        assert_eq!(offer.selected(offer.id, &TransportAddr::Relay(url)), Some(Relay));
    }
    assert_eq!(
        offer.selected(
            offer.id,
            &TransportAddr::Relay("https://other.invalid/".parse().unwrap())
        ),
        None
    );
    for bad in [
        Value::Null,
        json!({"url":home}),
        json!({"policy":"unknown","url":home}),
        json!({"policy":"public-trial-v1"}),
        json!({"policy":"public-trial-v1","url":"https://aps1-1.relay.n0.iroh.link/"}),
        json!({"policy":"public-trial-v1","url":"https://localhost/"}),
    ] {
        assert!(parse(json!([]), bad.clone()).is_none());
        let lan = parse(json!([{"kind":"lan","address":"10.1.1.2:123"}]), bad).unwrap();
        assert_eq!(lan.bootstrap().relay_urls().count(), 0);
        assert!(matches!(TrialRelay::mode(lan.relay.as_ref()), RelayMode::Disabled));
        assert_eq!(lan.selected(lan.id, &TransportAddr::Relay(home.parse().unwrap())), None);
    }
}

#[test]
fn repeated_relay_grant_fields_do_not_authorize_a_map_or_erase_ips() {
    let policy = r#""policy":"public-trial-v1""#;
    let url = r#""url":"https://aps1-1.relay.n0.iroh.link./""#;
    for fields in [
        format!("{policy},{policy},{url}"),
        format!("{policy},{url},{url}"),
        format!(r#""policy":"unknown",{policy},{url}"#),
        format!(r#"{policy},"policy":"unknown",{url}"#),
        format!(r#"{policy},"url":"https://other.invalid/",{url}"#),
        format!(r#"{policy},{url},"url":"https://other.invalid/""#),
    ] {
        let raw = format!("{{{fields}}}");
        assert!(parse_relay_json(json!([]), &raw).is_none());
        let lan = parse_relay_json(json!([{"kind":"lan","address":"10.1.1.2:123"}]), &raw).unwrap();
        assert!(lan.relay.is_none());
        assert_eq!(
            lan.bootstrap().ip_addrs().copied().collect::<Vec<_>>(),
            ["10.1.1.2:123".parse::<std::net::SocketAddr>().unwrap()]
        );
    }
}

#[test]
fn bootstrap_and_authenticated_migration_have_distinct_authority() {
    let peer = SecretKey::from_bytes(&[2; 32]).public();
    let wrong = SecretKey::from_bytes(&[3; 32]).public();
    let ip = |a: &str| TransportAddr::Ip(a.parse().unwrap());
    let only_lan = parse(json!([{"kind":"lan","address":"10.1.1.2:123"}]), Value::Null).unwrap();
    let wide = parse(json!([{"kind":"ipv6","address":"[2606:4700::1]:123"}]), Value::Null).unwrap();
    for (addr, kind) in [
        ("10.1.1.2:123", Lan(Ipv4)),
        ("[fdab::1]:123", Lan(Ipv6)),
        ("8.8.8.8:123", Punched(Ipv4)),
        ("[2606:4700::2]:123", Punched(Ipv6)),
    ] {
        assert_eq!(wide.selected(peer, &ip(addr)), Some(kind));
        assert_eq!(wide.selected(wrong, &ip(addr)), None);
        assert_eq!(
            only_lan.selected(peer, &ip(addr)),
            (addr == "10.1.1.2:123").then_some(kind)
        );
    }
    for addr in [
        "127.0.0.1:80",
        "169.254.1.2:80",
        "198.18.0.1:80",
        "192.0.2.1:80",
        "224.0.0.1:80",
        "[fe80::1]:80",
        "[2001:db8::1]:80",
        "[2001::1]:80",
        "[3fff::1]:80",
        "[2002::1]:80",
        "[::ffff:8.8.8.8]:80",
        "[64:ff9b::1]:80",
    ] {
        assert_eq!(wide.selected(peer, &ip(addr)), None);
        assert!(parse(json!([{"kind":"ipv6","address":addr}]), Value::Null).is_none());
    }
    for (kind, addr) in [
        ("ipv4", "8.8.8.8:81"),
        ("lan", "8.8.8.8:81"),
        ("ipv6", "8.8.8.8:81"),
        ("ipv6", "[fd00::1]:81"),
        ("lan", "[2606:4700::1]:81"),
        ("ipv6", "[2606:4700::1%1]:81"),
    ] {
        assert!(parse(json!([{"kind":kind,"address":addr}]), Value::Null).is_none());
    }
}

/// Two computers on one overlay network (a tailnet), or behind one carrier's
/// NAT, reach each other in IPv4 shared address space. iroh selects that
/// path as soon as it answers; the link then names it a local one instead of
/// ending. Nothing there is ever dialled first.
#[test]
fn a_selected_path_in_shared_address_space_is_a_local_one() {
    let peer = SecretKey::from_bytes(&[2; 32]).public();
    let wrong = SecretKey::from_bytes(&[3; 32]).public();
    let ip = |a: &str| TransportAddr::Ip(a.parse().unwrap());
    let home = "https://aps1-1.relay.n0.iroh.link./";
    let relayed = parse(json!([]), json!({"policy":"public-trial-v1","url":home})).unwrap();
    let wide = parse(json!([{"kind":"ipv6","address":"[2606:4700::1]:123"}]), Value::Null).unwrap();
    let only_lan = parse(json!([{"kind":"lan","address":"10.1.1.2:123"}]), Value::Null).unwrap();
    for offer in [&relayed, &wide] {
        // The whole of 100.64.0.0/10 and no more: the addresses on either
        // side of it are public ones, and stay hole-punched.
        for (addr, kind) in [
            ("100.63.255.255:80", Punched(Ipv4)),
            ("100.64.0.0:80", Lan(Ipv4)),
            ("100.100.1.2:4433", Lan(Ipv4)),
            ("100.127.255.255:80", Lan(Ipv4)),
            ("100.128.0.0:80", Punched(Ipv4)),
        ] {
            assert_eq!(offer.selected(peer, &ip(addr)), Some(kind), "{addr}");
            // Only on the peer the grant names.
            assert_eq!(offer.selected(wrong, &ip(addr)), None, "{addr}");
        }
        assert_eq!(offer.selected(peer, &ip("100.64.0.1:0")), None);
        // The other classes without a name stay refused.
        for addr in [
            "198.18.0.1:80",
            "169.254.1.2:80",
            "[64:ff9b::6440:1]:80",
            "[::ffff:100.64.0.1]:80",
        ] {
            assert_eq!(offer.selected(peer, &ip(addr)), None, "{addr}");
        }
    }
    // A grant for listed local addresses alone carries only those.
    assert_eq!(only_lan.selected(peer, &ip("100.64.0.1:80")), None);
    // No offer makes such an address one to dial: it is not listed under any
    // kind, and an offer of nothing else is no offer.
    for kind in ["lan", "ipv4", "ipv6"] {
        let listed = json!([{"kind":kind,"address":"100.64.0.1:80"}]);
        assert!(parse(listed.clone(), Value::Null).is_none(), "{kind}");
        let offer = parse(listed, json!({"policy":"public-trial-v1","url":home})).unwrap();
        assert_eq!(offer.bootstrap().ip_addrs().count(), 0, "{kind}");
    }
}

/// A link is judged by the path iroh has selected, which is the one that
/// carries its data. A candidate that is open and not selected never ends a
/// link whose selected path is accepted; a selected path the grant does not
/// permit is refused whatever else is open, the relay included.
#[test]
fn a_link_is_judged_by_its_selected_path_alone() {
    let ip = |a: &str| TransportAddr::Ip(a.parse().unwrap());
    let home = "https://aps1-1.relay.n0.iroh.link./";
    let relay = || TransportAddr::Relay(home.parse().unwrap());
    let offer = parse(json!([]), json!({"policy":"public-trial-v1","url":home})).unwrap();
    let judge = |paths: Vec<(bool, TransportAddr)>| offer.judge(offer.id, paths);

    assert_eq!(judge(vec![(true, relay())]), Verdict::Accepted(Relay));
    // A refused candidate beside the selected relay, or beside a selected
    // local path: the link stays as it is.
    for (refused, named) in [
        ("198.18.0.1:80", AddressClass::Other),
        ("169.254.1.2:80", AddressClass::Linklocal),
        ("[64:ff9b::1]:80", AddressClass::Nat64),
    ] {
        assert_eq!(
            judge(vec![(false, ip(refused)), (true, relay())]),
            Verdict::Accepted(Relay),
            "{refused}"
        );
        assert_eq!(
            judge(vec![(true, ip("10.1.1.2:80")), (false, ip(refused)), (false, relay())]),
            Verdict::Accepted(Lan(Ipv4)),
            "{refused}"
        );
        // Selected, it carries the data: refused, though the relay is open,
        // and the report can say what class of address it was.
        assert_eq!(
            judge(vec![(false, relay()), (true, ip(refused))]),
            Verdict::Refused(named),
            "{refused}"
        );
    }
    // The selection moves from the relay to a path in shared address space:
    // the link goes on, as a local one.
    assert_eq!(
        judge(vec![(false, relay()), (true, ip("100.100.1.2:4433"))]),
        Verdict::Accepted(Lan(Ipv4))
    );
    // Nothing selected is not a refusal.
    assert_eq!(
        judge(vec![(false, relay()), (false, ip("10.1.1.2:80"))]),
        Verdict::Unselected
    );
    assert_eq!(judge(Vec::new()), Verdict::Unselected);
    // Another peer's path is refused, whatever its address.
    let other = SecretKey::from_bytes(&[3; 32]).public();
    assert_eq!(
        offer.judge(other, vec![(true, relay())]),
        Verdict::Refused(AddressClass::Relay)
    );
}

/// The reports name a path's address by a fixed word and nothing more. The
/// word is what was seen; it permits nothing.
#[test]
fn an_address_is_reported_by_its_class_alone() {
    use AddressClass::*;
    let ip = |a: &str| TransportAddr::Ip(a.parse().unwrap());
    let home = "https://aps1-1.relay.n0.iroh.link./";
    assert_eq!(class(&TransportAddr::Relay(home.parse().unwrap())), Relay);
    for (addr, named, word) in [
        ("10.1.1.2:80", Private, "private"),
        ("172.31.255.255:80", Private, "private"),
        ("192.168.0.1:80", Private, "private"),
        ("100.63.255.255:80", Public, "public"),
        ("100.64.0.0:80", Cgnat, "cgnat"),
        ("100.127.255.255:0", Cgnat, "cgnat"),
        ("100.128.0.0:80", Public, "public"),
        ("8.8.8.8:80", Public, "public"),
        ("169.254.1.2:80", Linklocal, "linklocal"),
        ("127.0.0.1:80", Loopback, "loopback"),
        ("198.18.0.1:80", Other, "other"),
        ("192.0.2.1:80", Other, "other"),
        ("224.0.0.1:80", Other, "other"),
        ("[fdab::1]:80", Ula, "ula"),
        ("[fe80::1]:80", Linklocal, "linklocal"),
        ("[::1]:80", Loopback, "loopback"),
        ("[2606:4700::2]:80", Global, "global"),
        ("[64:ff9b::808:808]:80", Nat64, "nat64"),
        ("[::ffff:8.8.8.8]:80", Mapped, "mapped"),
        ("[2001:db8::1]:80", Other, "other"),
        ("[2002::1]:80", Other, "other"),
    ] {
        assert_eq!(class(&ip(addr)), named, "{addr}");
        assert_eq!(serde_json::to_value(named).unwrap(), json!(word), "{addr}");
    }
}
