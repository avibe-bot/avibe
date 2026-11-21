use super::{connect::Authority, offer::Offer};
use iroh::SecretKey;
use serde_json::json;
use std::time::Duration;
use tokio::time::Instant;

#[tokio::test(start_paused = true)]
async fn only_a_current_bound_offer_can_bootstrap_a_lan_path() {
    let client = SecretKey::from_bytes(&[1; 32]).public();
    let peer = SecretKey::from_bytes(&[2; 32]).public();
    let host = Authority::managed("one-app.avibe.bot").unwrap();
    let sent = Instant::now();
    let answer = json!({"version":1,"status":"ready","hostname":host.host(),"instance_id":"one","generation":"boot:1","issued_at":10,"expires_at":500,"alpn":"avibe-direct/1","endpoint":{"id":peer.to_string(),"addresses":[{"kind":"lan","address":"192.168.1.4:345"},{"kind":"ipv6","address":"[2606:4700::1]:345"},{"kind":"lan","address":"127.0.0.1:345"},{"kind":"lan","address":"[fd00::1]:345"}]},"client":{"endpoint_id":client.to_string(),"admitted_until":50}});
    let parse = |v: &serde_json::Value| Offer::parse(&v.to_string(), &host, "one", client, sent);
    let offer = parse(&answer).unwrap();
    assert_eq!(
        offer.candidates,
        vec![
            "192.168.1.4:345".parse::<std::net::SocketAddr>().unwrap(),
            "[fd00::1]:345".parse().unwrap()
        ]
    );
    assert_eq!(offer.until, sent + Duration::from_secs(40));
    for (path, bad) in [
        ("/hostname", json!("two-app.avibe.bot")),
        ("/instance_id", json!("two")),
        ("/status", json!("unavailable")),
        ("/client/endpoint_id", json!(peer.to_string())),
        ("/alpn", json!("wrong")),
        ("/generation", json!("")),
        ("/endpoint/addresses", json!([])),
    ] {
        let mut v = answer.clone();
        *v.pointer_mut(path).unwrap() = bad;
        assert!(parse(&v).is_none());
    }
    tokio::time::advance(Duration::from_secs(40)).await;
    assert!(parse(&answer).is_none());
}
