use super::rendezvous::{origin_of, script, Reply, Request, MAX_ANSWER_BYTES, PATH};

const ENDPOINT: &str = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";

#[test]
fn a_documents_origin_is_exact_or_nothing() {
    for (url, origin) in [
        ("https://one-app.avibe.bot/", Some("https://one-app.avibe.bot")),
        ("https://one-app.avibe.bot", Some("https://one-app.avibe.bot")),
        (
            "https://one-app.avibe.bot:443/chat?x=1#y",
            Some("https://one-app.avibe.bot"),
        ),
        (
            "https://one-app.avibe.bot:8443/",
            Some("https://one-app.avibe.bot:8443"),
        ),
        ("http://127.0.0.1:5123/a", Some("http://127.0.0.1:5123")),
        ("http://localhost:80/", Some("http://localhost")),
        // Not a document of any origin a request can be made for.
        ("https://user@one-app.avibe.bot/", None),
        ("https://user:secret@one-app.avibe.bot/", None),
        ("https://One-App.avibe.bot/", None),
        ("https://one-app.avibe.bot:0/", None),
        ("https://one-app.avibe.bot:99999/", None),
        ("https://one-app.avibe.bot:/", None),
        ("https://[::1]:5123/", None),
        ("https:///path", None),
        ("about:blank", None),
        ("file:///tmp/a.html", None),
        ("avibe://one-app.avibe.bot/", None),
        ("", None),
    ] {
        assert_eq!(origin_of(url).as_deref(), origin, "{url}");
    }
}

#[test]
fn a_request_is_made_only_for_a_serialized_origin_and_a_plain_endpoint_id() {
    let request = Request::new("https://one-app.avibe.bot", ENDPOINT).expect("a request");
    assert_eq!(request.origin(), "https://one-app.avibe.bot");
    assert_eq!(
        request.arguments(),
        [
            ("url", format!("https://one-app.avibe.bot{PATH}").as_str()),
            ("origin", "https://one-app.avibe.bot"),
            (
                "body",
                format!(r#"{{"client_endpoint_id":"{ENDPOINT}","version":1}}"#).as_str()
            ),
        ]
    );
    // An origin is given exactly as a document's is compared: no path, no
    // default port, no user information.
    for origin in [
        "https://one-app.avibe.bot/",
        "https://one-app.avibe.bot:443",
        "https://user@one-app.avibe.bot",
        "one-app.avibe.bot",
        "https://one-app.avibe.bot\"+alert(1)+\"",
    ] {
        assert!(Request::new(origin, ENDPOINT).is_none(), "{origin}");
    }
    // Nothing but lowercase letters and digits reaches the body.
    for id in ["", "ABCDEF", "abc\"}", "abc def", &"a".repeat(129)] {
        assert!(Request::new("https://one-app.avibe.bot", id).is_none(), "{id}");
    }
}

#[test]
fn the_expression_is_the_script_called_with_string_literals() {
    let request = Request::new("https://one-app.avibe.bot", ENDPOINT).expect("a request");
    let expression = request.expression();
    assert!(expression.starts_with("(async (url, origin, body) => {\n"));
    assert!(expression.contains(&script()));
    let call = format!(
        "}})(\"https://one-app.avibe.bot{PATH}\", \"https://one-app.avibe.bot\", {})",
        serde_json::Value::from(format!(r#"{{"client_endpoint_id":"{ENDPOINT}","version":1}}"#))
    );
    assert!(expression.ends_with(&call), "{expression}");
    // The bounds are in the script itself, not left as names.
    assert!(!script().contains("DEADLINE_MILLISECONDS") && !script().contains("MAX_ANSWER_BYTES"));
    assert!(script().contains("}, 4000);") && script().contains(&format!("total > {MAX_ANSWER_BYTES}")));
}

#[test]
fn only_a_complete_200_from_the_script_gives_a_body() {
    let ready = r#"{"ok":true,"status":200,"body":"{\"version\":1}"}"#;
    assert_eq!(Reply::read(Some(ready)), Reply::Ready(r#"{"version":1}"#.to_owned()));
    assert_eq!(Reply::read(Some(ready)).body().as_deref(), Some(r#"{"version":1}"#));

    let oversize =
        serde_json::json!({ "ok": true, "status": 200, "body": "x".repeat(MAX_ANSWER_BYTES + 1) }).to_string();
    for (reply, expected) in [
        (None, Reply::Failed),
        (Some(""), Reply::Failed),
        (Some("not json"), Reply::Failed),
        (Some(r#"{"ok":false,"failure":"origin"}"#), Reply::NotReady),
        (Some(r#"{"ok":false,"failure":"csrf"}"#), Reply::NotReady),
        (Some(r#"{"ok":false,"failure":"controlled"}"#), Reply::Controlled),
        (Some(r#"{"ok":false,"failure":"redirect"}"#), Reply::Failed),
        (Some(r#"{"ok":false,"failure":"timeout"}"#), Reply::Failed),
        (Some(r#"{"ok":false}"#), Reply::Failed),
        (Some(r#"{"ok":true,"status":403,"body":""}"#), Reply::Refused(403)),
        (Some(r#"{"ok":true,"status":401,"body":"no"}"#), Reply::Refused(401)),
        // Not the script's shape: no status, no body, a body that is not text.
        (Some(r#"{"ok":true,"body":"x"}"#), Reply::Failed),
        (Some(r#"{"ok":true,"status":200}"#), Reply::Failed),
        (Some(r#"{"ok":true,"status":200,"body":{"version":1}}"#), Reply::Failed),
        (Some(r#"{"ok":"true","status":200,"body":"x"}"#), Reply::Failed),
        (Some(r#"{"ok":true,"status":99999999,"body":"x"}"#), Reply::Failed),
        (Some(oversize.as_str()), Reply::Failed),
    ] {
        assert_eq!(Reply::read(reply), expected, "{reply:?}");
        assert_eq!(Reply::read(reply).body(), None);
    }
    // Every outcome an adapter can reach is delivered for the selection as
    // it is at that moment: unchanged while it is current, and "the
    // selection changed" once it is not, whatever the instance said.
    for outcome in [
        || Reply::Ready("{}".to_owned()),
        || Reply::NotReady,
        || Reply::Refused(403),
        || Reply::Controlled,
        || Reply::Failed,
        || Reply::SelectionChanged,
    ] {
        assert_eq!(outcome().for_selection(true), outcome());
        assert_eq!(outcome().for_selection(false), Reply::SelectionChanged);
    }
    // The script never says that the selection changed: only native code
    // knows the selection, and no reply of a page's world can claim it.
    assert_eq!(Reply::SelectionChanged.body(), None);
    for failure in ["selection", "selection-changed", "SelectionChanged"] {
        let reply = format!(r#"{{"ok":false,"failure":"{failure}"}}"#);
        assert_eq!(Reply::read(Some(&reply)), Reply::Failed, "{failure}");
    }
}
