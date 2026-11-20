//! AUTH-SETUP-175/177: catalog authority, caller grammar and cookie separation.
//! Existing local bootstrap tests cannot exercise the account/remote boundary.
use async_trait::async_trait;
use avibe_runtime_host::instance_client::{
    enrollment::EnrollmentFlow,
    protocol::{
        account_view, instance_origin, presentation_view, ClientAction, ClientError, ClientRequest, ClientState,
        Instance,
    },
    storage::ConnectionStore,
    transport::{NativeSession, Request, Response, Transport},
};
use reqwest::{
    header::{HeaderMap, SET_COOKIE},
    Method,
};
use serde_json::Value;
use std::sync::{Arc, Mutex};

#[test]
fn shared_protocol_fixtures_roundtrip_and_keep_private_results_out_of_state() {
    let requests: Value =
        serde_json::from_str(include_str!("../../contracts/instance-client-v1/requests.json")).unwrap();
    for request in requests.as_array().unwrap() {
        let _: ClientRequest = serde_json::from_value(request.clone()).unwrap();
    }
    let states: Value = serde_json::from_str(include_str!("../../contracts/instance-client-v1/states.json")).unwrap();
    for sample in states.as_object().unwrap().values() {
        let state: ClientState = serde_json::from_value(sample.clone()).unwrap();
        let full = serde_json::to_value(&state).unwrap();
        assert_eq!(&full["instance_update"], &sample["instance_update"]);
        assert!(full.get("instance_update").is_some());
        // The sample is exactly what the shell's validator produces from it.
        if let Some(snapshot) = sample["instance_update"]["snapshot"].as_object() {
            let snapshot = Value::Object(snapshot.clone());
            assert_eq!(
                avibe_runtime_host::instance_client::instance_update::snapshot(&snapshot).unwrap(),
                snapshot
            );
        }
        // The shell's own facts about its request are full-state only, and
        // `installed_from` is a version or null.
        if let Some(request) = sample["instance_update"]["request"].as_object() {
            assert!(request["following"].is_boolean());
            let from = &request["installed_from"];
            assert!(
                from.is_null()
                    || from
                        .as_str()
                        .is_some_and(avibe_runtime_host::instance_client::instance_update::is_version)
            );
        }
        let projection = state.runtime_projection();
        let projected = projection.to_string();
        for private in ["following", "installed_from"] {
            assert!(!projected.contains(private), "{private}");
        }
        for private in [
            "catalog",
            "consent",
            "enrollment",
            "current_instance",
            "instance_update",
        ] {
            assert!(projection.get(private).is_none());
        }
    }
    // Released producers omit A5 display metadata; absence grants no owner or
    // identity inference and must normalize to the nullable new fields.
    let mut legacy = states["remote_connected"].clone();
    legacy.as_object_mut().unwrap().remove("current_instance");
    legacy["local"].as_object_mut().unwrap().remove("slug");
    let legacy: ClientState = serde_json::from_value(legacy).unwrap();
    assert!(legacy.local.slug.is_none());
    assert!(legacy.current_instance.is_none());
}

// A4: the old single-view grammar rejected locale even though Workbench and
// the bundled page cannot share localStorage. Extra fields remain forbidden.
#[test]
fn account_locale_is_strict_presentation_data() {
    let owned = "tauri://localhost/".parse().unwrap();
    for locale in ["en", "zh"] {
        assert_eq!(
            account_view(
                &format!("tauri://localhost/account/index.html?view=instances&locale={locale}")
                    .parse()
                    .unwrap(),
                &owned
            ),
            Some("instances")
        );
    }
    for query in [
        "view=account&locale=fr",
        "locale=zh&locale=en",
        "view=account&locale=zh&action=sign_out",
    ] {
        assert_eq!(
            account_view(
                &format!("tauri://localhost/account/index.html?{query}").parse().unwrap(),
                &owned
            ),
            None
        );
    }
}

struct Descriptor(serde_json::Value);
#[async_trait]
impl Transport for Descriptor {
    async fn send(&self, request: Request) -> Result<Response, ClientError> {
        assert_eq!(request.url.path(), "/api/desktop/local-instance");
        Ok(Response {
            status: 200,
            headers: HeaderMap::new(),
            body: serde_json::to_vec(&self.0).unwrap(),
        })
    }
}

// A4: registration status is not local identity. A remote-selected client must
// still expose its actual local pairing, and failed refresh cannot retain proof.
#[tokio::test]
async fn local_descriptor_projects_identity_independently_of_enrollment() {
    let mut state = ClientState::new("Local".into(), "test".into(), serde_json::json!({}));
    state.connection.committed = serde_json::from_value(serde_json::json!({
        "kind":"remote","name":"Studio","instance_id":"studio","canonical_origin":"https://studio.avibe.bot"
    }))
    .unwrap();
    for (pairing, requested, running, expected_id, expected_url, succeeds) in [
        (
            serde_json::json!({"instance_id":"local-id","backend_url":"https://avibe.bot","public_url":"https://local.avibe.bot"}),
            true,
            true,
            Some("local-id"),
            Some("https://local.avibe.bot"),
            true,
        ),
        (
            serde_json::json!({"instance_id":"local-id","backend_url":"https://avibe.bot","public_url":"https://local.avibe.bot"}),
            false,
            false,
            Some("local-id"),
            None,
            true,
        ),
        (
            serde_json::json!({"instance_id":"local-id","backend_url":"https://avibe.bot","public_url":"https://local.avibe.bot"}),
            true,
            false,
            Some("local-id"),
            None,
            true,
        ),
        (
            serde_json::json!({"instance_id":"local-id","backend_url":"https://avibe.bot","public_url":"https://evil.test"}),
            true,
            true,
            None,
            None,
            false,
        ),
        (Value::Null, true, false, None, None, true),
    ] {
        let transport = Arc::new(Descriptor(serde_json::json!({
            "schema_version":1,"installation_id":"installation","device_name":"Local",
            "binding_revision":"1","pairing":pairing,
            "remote_access":{"configured_enabled":requested,"requested":requested,"running":running}
        })));
        let mut local = NativeSession::new("http://127.0.0.1:1984/".parse().unwrap(), transport);
        assert_eq!(EnrollmentFlow::read(&mut local, &mut state).await.is_ok(), succeeds);
        let projection = state.runtime_projection();
        assert_eq!(projection["local"]["instance_id"], serde_json::json!(expected_id));
        assert_eq!(
            projection["local"]["remote_access_url"],
            serde_json::json!(expected_url)
        );
        assert!(state.enrollment.instance_id.is_none());
        assert_eq!(projection["connection"]["committed"]["instance_id"], "studio");
    }
}

#[test]
fn catalog_requires_exact_tuple_and_bundle_requires_exact_owned_entry() {
    let states: Value = serde_json::from_str(include_str!("../../contracts/instance-client-v1/states.json")).unwrap();
    let instance: Instance = serde_json::from_value(states["signed_in"]["catalog"]["instances"][0].clone()).unwrap();
    assert_eq!(
        instance_origin(&instance).unwrap().as_str(),
        "https://studio.avibe.bot/"
    );
    for url in [
        "http://studio.avibe.bot",
        "https://studio.avibe.bot:8443",
        "https://studio.avibe.bot.attacker.test",
        "https://child.studio.avibe.bot",
        "https://studio.avibe.bot/account",
        "https://user@studio.avibe.bot",
        "https://studio.avibe.bot/?callback=evil",
        "https://studio.avibe.bot/#a",
    ] {
        let mut candidate = instance.clone();
        candidate.url = url.into();
        assert!(instance_origin(&candidate).is_err(), "{url}");
    }
    let owned = "tauri://localhost/".parse().unwrap();
    assert_eq!(
        account_view(
            &"tauri://localhost/account/index.html?view=instances".parse().unwrap(),
            &owned
        ),
        Some("instances")
    );
    for origin in ["tauri://localhost/", "http://tauri.localhost/"] {
        let owned = origin.parse().unwrap();
        let updates = format!("{origin}account/index.html?view=updates");
        assert_eq!(account_view(&updates.parse().unwrap(), &owned), Some("updates"));
        assert_eq!(
            account_view(&format!("{updates}&target=https://evil.test").parse().unwrap(), &owned),
            None
        );
    }
    assert_eq!(
        presentation_view(&"avibe://desktop/updates".parse().unwrap()),
        Some("updates")
    );
    for url in [
        "avibe://desktop/updates?install=true",
        "avibe://desktop/updates#install",
        "avibe://evil/updates",
    ] {
        assert_eq!(presentation_view(&url.parse().unwrap()), None);
    }
    for url in [
        "tauri://localhost/account/index.html?view=delete",
        "tauri://localhost/account/index.html?view=account&view=instances",
        "tauri://localhost/account/index.html#account",
        "tauri://localhost/index.html",
        "https://studio.avibe.bot/account/index.html",
    ] {
        assert_eq!(account_view(&url.parse().unwrap(), &owned), None);
    }
}

// The bridge wire shape must not turn a setting save or an absent clicked
// target into an install. Generic account fixtures did not cover these actions.
#[test]
fn update_action_payloads_require_exact_typed_fields() {
    for action in [
        serde_json::json!({"type":"update_check","target_version":"3.2.7"}),
        serde_json::json!({"type":"update_set_prereleases"}),
        serde_json::json!({"type":"update_set_auto_update","enabled":"true"}),
        serde_json::json!({"type":"update_install"}),
        serde_json::json!({"type":"update_install","target_version":327}),
        serde_json::json!({"type":"update_install","target_version":"3.2.7","url":"https://evil.test"}),
    ] {
        assert!(
            serde_json::from_value::<ClientAction>(action.clone()).is_err(),
            "{action}"
        );
    }
}

struct Responses {
    cookie: Mutex<Option<String>>,
    received: Mutex<Vec<Request>>,
}
#[async_trait]
impl Transport for Responses {
    async fn send(&self, request: Request) -> Result<Response, ClientError> {
        self.received.lock().unwrap().push(request);
        let mut headers = HeaderMap::new();
        if let Some(cookie) = self.cookie.lock().unwrap().take() {
            headers.insert(SET_COOKIE, cookie.parse().unwrap());
        }
        Ok(Response {
            status: 200,
            headers,
            body: b"{}".to_vec(),
        })
    }
}

#[tokio::test]
async fn native_cookie_jar_rejects_cross_authority_and_preserves_private_host_cookie() {
    let transport = Arc::new(Responses {
        cookie: Mutex::new(Some(
            "__Host-vibe_remote_session=private; Path=/; Secure; HttpOnly; SameSite=Lax".into(),
        )),
        received: Mutex::new(vec![]),
    });
    let origin = "https://studio.avibe.bot/".parse().unwrap();
    let mut instance = NativeSession::new(origin, transport.clone());
    instance.request(Method::GET, "/auth/callback", None).await.unwrap();
    let restored =
        NativeSession::restore(instance.origin.clone(), transport.clone(), &instance.persist().unwrap()).unwrap();
    let cookies = restored.runtime_cookies().unwrap();
    assert_eq!(cookies.len(), 1);
    assert_eq!(cookies[0].domain(), Some("studio.avibe.bot"));
    assert_eq!(cookies[0].same_site(), Some(cookie::SameSite::Lax));
    assert_eq!(cookies[0].http_only(), Some(true));
    assert!(NativeSession::restore(
        "https://other.avibe.bot".parse().unwrap(),
        transport.clone(),
        &instance.persist().unwrap()
    )
    .is_err());
    assert!(instance
        .request(Method::GET, "https://other.avibe.bot/", None)
        .await
        .is_err());
    assert_eq!(transport.received.lock().unwrap().len(), 1);
    *transport.cookie.lock().unwrap() = Some("account=private; Domain=avibe.bot; Path=/; Secure; HttpOnly".into());
    assert_eq!(
        instance
            .request(Method::GET, "/api/session", None)
            .await
            .err()
            .unwrap()
            .code,
        "invalid_cookie_scope"
    );
}

#[test]
fn unreadable_connection_record_is_preserved_instead_of_overwritten() {
    let root = tempfile::tempdir().unwrap();
    let path = root.path().join("connection.json");
    let store = ConnectionStore::new(path.clone());
    let record = store.load("小明的 Mac").unwrap();
    assert_eq!(store.load("Different name").unwrap().profile_id, record.profile_id);
    std::fs::write(&path, b"{future-format").unwrap();
    assert!(store.load("小明的 Mac").is_err());
    assert_eq!(std::fs::read(&path).unwrap(), b"{future-format");
}

// Instance order is a device-local preference beside the connection record.
// A newer Desktop's file, or a damaged connection store, is never overwritten.
#[test]
fn instance_recency_is_per_account_and_preserves_newer_files() {
    let newer = br#"{"schema_version":2,"accounts":{"account-a":[{"id":"one"}]}}"#;
    for (existing, writable, replaced) in [
        (None, true, true),
        (Some(&b"{damaged"[..]), true, true),
        (Some(&newer[..]), true, false),
        (None, false, false),
    ] {
        let root = tempfile::tempdir().unwrap();
        let path = root.path().join("instance-recency.json");
        if let Some(bytes) = existing {
            std::fs::write(&path, bytes).unwrap();
        }
        let mut store = ConnectionStore::new(root.path().join("connection.json"));
        if !writable {
            store = store.read_only();
        }
        let mut recency = store.load_recency();
        assert!(recency.for_account("account-a").is_empty());
        for (account, instance) in [
            ("account-a", "one"),
            ("account-a", "two"),
            ("account-b", "three"),
            ("account-a", "one"),
        ] {
            recency.opened(account, instance);
        }
        assert_eq!(recency.for_account("account-a"), ["one", "two"]);
        assert_eq!(recency.for_account("account-b"), ["three"]);
        store.save_recency(&recency).unwrap();
        let reloaded = ConnectionStore::new(root.path().join("connection.json")).load_recency();
        if replaced {
            assert_eq!(reloaded.for_account("account-a"), ["one", "two"]);
            assert_eq!(reloaded.for_account("account-b"), ["three"]);
        } else {
            assert!(reloaded.for_account("account-a").is_empty());
            assert_eq!(std::fs::read(&path).ok().as_deref(), existing);
        }
    }
}
