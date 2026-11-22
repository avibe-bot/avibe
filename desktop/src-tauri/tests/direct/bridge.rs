use super::{
    bridge::{self, Ledger, Message, Method, Request},
    choices::Mode,
    AddressFamily, HandoverResult, PathStatus, PreferredPath, SelectedPath,
};
use serde_json::{json, Value};
use url::Url;

const TOKEN: &str = "0123456789abcdef0123456789abcdef";

fn client(preferred: PreferredPath, closure: u64) -> PathStatus {
    PathStatus {
        preferred,
        // The client's own order of events is not the page's.
        sequence: 1,
        closure,
    }
}

fn lan() -> PreferredPath {
    PreferredPath::Direct(SelectedPath::Lan(AddressFamily::Ipv4))
}

fn ask(request: Value) -> Url {
    let mut url = Url::parse("avibe-app://direct").unwrap();
    url.query_pairs_mut().append_pair("request", &request.to_string());
    url
}

#[test]
fn a_window_without_a_route_is_on_the_tunnel_whatever_a_client_prefers() {
    let mut ledger = Ledger::default();
    // Nothing to tell a page that has just been told the tunnel.
    assert!(!ledger.observe(None));
    assert_eq!(
        ledger.status(),
        json!({ "preferred": "tunnel", "sequence": 0, "closure": 0, "path": null })
    );
    // The session's client is direct, but this window was never routed.
    assert!(!ledger.observe(None));
    assert_eq!(ledger.status()["preferred"], "tunnel");
}

#[test]
fn a_page_hears_its_path_by_kind_and_class_only() {
    let mut ledger = Ledger::default();
    assert!(!ledger.observe(Some((1, client(PreferredPath::Tunnel, 0)))));

    assert!(ledger.observe(Some((1, client(lan(), 0)))));
    assert_eq!(
        ledger.status(),
        json!({ "preferred": "direct", "sequence": 1, "closure": 0, "path": "ipv4", "path_scope": "local" })
    );
    let punched = PreferredPath::Direct(SelectedPath::Punched(AddressFamily::Ipv6));
    assert!(ledger.observe(Some((1, client(punched, 0)))));
    assert_eq!(
        ledger.status(),
        json!({ "preferred": "direct", "sequence": 2, "closure": 0, "path": "ipv6", "path_scope": "global" })
    );
    // A relayed path is the relay: no class, and never an address family.
    assert!(ledger.observe(Some((1, client(PreferredPath::Direct(SelectedPath::Relay), 0)))));
    assert_eq!(
        ledger.status(),
        json!({ "preferred": "direct", "sequence": 3, "closure": 0, "path": "relay" })
    );
    // The same again is not news.
    assert!(!ledger.observe(Some((1, client(PreferredPath::Direct(SelectedPath::Relay), 0)))));
    assert!(ledger.observe(Some((1, client(PreferredPath::Tunnel, 0)))));
    assert_eq!(
        ledger.status(),
        json!({ "preferred": "tunnel", "sequence": 4, "closure": 0, "path": null })
    );
}

#[test]
fn a_window_counts_on_across_the_clients_it_outlives() {
    let mut ledger = Ledger::default();
    // Routed to a client that has already lost its path twice: not this
    // window's losses.
    assert!(ledger.observe(Some((1, client(lan(), 2)))));
    assert_eq!(ledger.status()["closure"], 0);
    // That client loses it once more, and comes back on the same path.
    assert!(ledger.observe(Some((1, client(PreferredPath::Tunnel, 3)))));
    assert!(ledger.observe(Some((1, client(lan(), 3)))));
    assert_eq!(
        (ledger.status()["closure"].clone(), ledger.status()["sequence"].clone()),
        (json!(1), json!(3))
    );
    // A loss with the path already back: still told, with nothing else new.
    assert!(ledger.observe(Some((1, client(lan(), 4)))));
    assert_eq!(ledger.status()["closure"], 2);

    // The session ends: the route is gone, and what ran through it with it.
    assert!(ledger.observe(None));
    assert_eq!(
        ledger.status(),
        json!({ "preferred": "tunnel", "sequence": 5, "closure": 3, "path": null })
    );
    assert!(!ledger.observe(None));
    // A new session's client counts from nothing; the page's numbers only grow.
    assert!(ledger.observe(Some((2, client(lan(), 0)))));
    assert_eq!(
        (ledger.status()["closure"].clone(), ledger.status()["sequence"].clone()),
        (json!(3), json!(6))
    );
    // One session's route replaced by another's at once closes the first's.
    assert!(ledger.observe(Some((3, client(lan(), 0)))));
    assert_eq!(ledger.status()["closure"], 4);
}

#[test]
fn a_hand_over_is_answered_in_the_phones_words() {
    assert_eq!(
        bridge::handover(&HandoverResult::Done { closed: 2, kept: 1 }),
        json!({ "closed": 2, "kept": 1 })
    );
    assert_eq!(
        bridge::handover(&HandoverResult::NotUsable),
        json!({ "closed": 0, "kept": 0, "skipped": "not_usable" })
    );
    assert_eq!(
        bridge::handover(&HandoverResult::OtherDocuments),
        json!({ "closed": 0, "kept": 0, "skipped": "other_documents" })
    );
    assert_eq!(
        bridge::preference(Mode::Tunnel),
        json!({ "mode": "tunnel", "reverted": null })
    );
    assert_eq!(
        bridge::preference(Mode::Automatic),
        json!({ "mode": "automatic", "reverted": null })
    );
}

#[test]
fn a_request_is_read_only_with_the_windows_token() {
    let request = |method: &str, params: Value| {
        Request::parse(
            &ask(json!({ "token": TOKEN, "id": "7c1e-a", "method": method, "params": params })),
            TOKEN,
        )
        .map(|request| request.method)
    };
    assert_eq!(request("direct.handover", json!({})), Some(Method::Handover));
    assert_eq!(request("direct.preference", json!({})), Some(Method::Preference));
    assert_eq!(
        request("direct.setPreference", json!({ "mode": "tunnel" })),
        Some(Method::SetPreference(Mode::Tunnel))
    );
    assert_eq!(
        request("direct.setPreference", json!({ "mode": "automatic" })),
        Some(Method::SetPreference(Mode::Automatic))
    );
    // Not a choice the app has, a method it does not answer, or a
    // parameter a method does not take.
    assert_eq!(request("direct.setPreference", json!({ "mode": "direct" })), None);
    assert_eq!(request("direct.setPreference", json!({})), None);
    assert_eq!(request("direct.handover", json!({ "mode": "tunnel" })), None);
    assert_eq!(request("direct.status", json!({})), None);
    assert_eq!(request("push.status", json!({})), None);
    assert_eq!(request("direct.handover", json!({ "host": "one-app.avibe.bot" })), None);

    let valid = json!({ "token": TOKEN, "id": "7c1e-a", "method": "direct.handover", "params": {} });
    assert!(Request::parse(&ask(valid.clone()), TOKEN).is_some());
    // Another window's token, a prefix of this one's, or none: a frame
    // inside the page can follow a link, and knows no token.
    for token in ["fedcba9876543210fedcba9876543210", &TOKEN[..31], ""] {
        let mut other = valid.clone();
        other["token"] = json!(token);
        assert_eq!(Request::parse(&ask(other), TOKEN), None);
    }
    let mut missing = valid.clone();
    missing.as_object_mut().unwrap().remove("token");
    assert_eq!(Request::parse(&ask(missing), TOKEN), None);
    // An ID the answer could not be written back with.
    for id in ["", "a\"b", &"a".repeat(65)] {
        let mut other = valid.clone();
        other["id"] = json!(id);
        assert_eq!(Request::parse(&ask(other), TOKEN), None);
    }
    // More than was asked for, or not the bridge's address at all.
    let mut padded = valid.clone();
    padded["pad"] = json!("x".repeat(1024));
    assert_eq!(Request::parse(&ask(padded), TOKEN), None);
    let query = ask(valid).query().unwrap().to_owned();
    for other in [
        format!("avibe-app://launch?{query}"),
        format!("avibe-app://direct/handover?{query}"),
        format!("avibe-app://direct?{query}&request=1"),
        format!("avibe-app://direct?{query}#now"),
        format!("https://direct/?{query}"),
        "avibe-app://direct".to_owned(),
    ] {
        assert_eq!(Request::parse(&Url::parse(&other).unwrap(), TOKEN), None, "{other}");
    }
    // Every link to the bridge's address is the bridge's to refuse.
    assert!(bridge::is_request(&Url::parse("avibe-app://direct?anything").unwrap()));
    assert!(!bridge::is_request(
        &Url::parse("avibe-app://launch?request=1").unwrap()
    ));
    assert!(!bridge::is_request(&Url::parse("avibe-app://close").unwrap()));
}

#[test]
fn a_delivery_runs_only_in_the_instances_own_top_document() {
    let state = Message::State {
        status: Ledger::default().status(),
        preference: bridge::preference(Mode::Automatic),
    };
    let script = bridge::delivery("https://one-app.avibe.bot", &state);
    assert!(script.starts_with(
        "(()=>{if(window.self!==window.top||window.location.origin!==\"https://one-app.avibe.bot\")return;"
    ));
    // A state that arrives before the bridge waits for it; an answer does not.
    assert!(script.contains("else window.__AVIBE_DIRECT_EARLY__=m;"));
    let reply = Message::Reply {
        id: "7c1e-a".to_owned(),
        result: bridge::handover(&HandoverResult::NotUsable),
        preference: None,
    };
    let script = bridge::delivery("https://one-app.avibe.bot", &reply);
    assert!(!script.contains("__AVIBE_DIRECT_EARLY__"));
    assert!(
        script.contains(r#"const m={"reply":{"id":"7c1e-a","result":{"closed":0,"kept":0,"skipped":"not_usable"}}};"#)
    );
}

#[test]
fn the_bridge_script_is_given_the_token_and_the_version_as_data() {
    let script = bridge::script("to\"ken", "3.2.7", true);
    assert!(script.starts_with("((token, version, asksBeforeLeaving) => {"));
    assert!(script.ends_with(")(\"to\\\"ken\",\"3.2.7\",true);"));
    assert!(bridge::script("token", "3.2.7", false).ends_with(")(\"token\",\"3.2.7\",false);"));
    // It names nothing of a listener, and offers `direct` alone.
    for word in ["127.0.0.1", "password", "Proxy-Authorization", "__TAURI"] {
        assert!(!script.contains(word), "{word}");
    }
    assert!(script.contains("capabilities: Object.freeze({ direct: 3, directPreference: 1 })"));
    assert!(script.contains("platform: 'desktop'"));
}
