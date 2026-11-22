use super::{
    connect::Authority,
    offer::Offer,
    path::TrialRelay,
    AddressFamily::{Ipv4, Ipv6},
    SelectedPath::{Lan, Punched, Relay},
};
use iroh::{RelayMode, SecretKey, TransportAddr};
use serde_json::{json, Value};
use std::time::Duration;

fn parse(addresses: Value, relay: Value) -> Option<Offer> {
    let client = SecretKey::from_bytes(&[1; 32]).public();
    let peer = SecretKey::from_bytes(&[2; 32]).public();
    let host = Authority::managed("one-app.avibe.bot").unwrap();
    let raw = json!({"version":1,"status":"ready","hostname":host.host(),"instance_id":"one","generation":"g","issued_at":10,"expires_at":40,"alpn":"avibe-direct/1","endpoint":{"id":peer.to_string(),"addresses":addresses,"relay":relay},"client":{"endpoint_id":client.to_string(),"admitted_until":30}});
    Offer::parse(&raw.to_string(), &host, "one", client, Duration::ZERO, Duration::ZERO)
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
        "100.64.0.1:80",
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
