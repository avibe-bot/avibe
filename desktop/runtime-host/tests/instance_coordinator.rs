//! AUTH-SETUP-171/176/177: cancellation fences credential mutation and HTTP work.
//! These coordinator tests isolate races; actual producer tests are separate.
use async_trait::async_trait;
use avibe_runtime_host::instance_client::{
    account::Passkeys,
    coordinator::{ClientView, Coordinator},
    protocol::*,
    storage::{ConnectionStore, Credentials},
    transport::{Epoch, Request, Response, Transport},
};
use reqwest::header::{HeaderMap, LOCATION, SET_COOKIE};
use serde_json::{json, Value};
use std::{
    collections::{HashMap, VecDeque},
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc, Mutex,
    },
};
use tokio::sync::{oneshot, Notify};

enum Reply {
    Ready(Response),
    Held(oneshot::Receiver<Response>, Arc<Notify>),
    Failure,
}
struct Http {
    replies: Mutex<VecDeque<(&'static str, Reply)>>,
    calls: Mutex<Vec<String>>,
    cookies: Mutex<Vec<String>>,
    /// Each instance sign-out: its method, `Origin`, CSRF header and cookies.
    logouts: Mutex<Vec<[String; 4]>>,
    /// Instance sign-outs find no network.
    offline: AtomicBool,
}
#[async_trait]
impl Transport for Http {
    async fn send(&self, request: Request) -> Result<Response, ClientError> {
        let path = request.url.path().to_owned();
        self.calls.lock().unwrap().push(path.clone());
        self.cookies.lock().unwrap().push(
            request
                .headers
                .get("cookie")
                .map(|value| value.to_str().unwrap().to_owned())
                .unwrap_or_default(),
        );
        if matches!(
            path.as_str(),
            "/api/update/check"
                | "/api/update/prereleases"
                | "/api/update/auto-update"
                | "/api/upgrade"
                | "/api/desktop/update/request"
        ) {
            assert_eq!(request.method, reqwest::Method::POST);
            assert_eq!(request.headers["origin"], "https://one.avibe.bot");
            assert_eq!(request.headers["x-vibe-csrf-token"], "csrf-from-instance");
            let cookies = request.headers["cookie"].to_str().unwrap();
            assert!(cookies.contains("vibe_csrf_token=csrf-from-instance"));
            assert!(cookies.contains("__Host-vibe_remote_session=rotated-by-instance"));
            assert!(!cookies.contains("private-account"));
            let expected = match path.as_str() {
                "/api/update/check" => json!({}),
                // Both owners install exactly the version on screen.
                "/api/upgrade" | "/api/desktop/update/request" => {
                    json!({"target_version":"3.2.8-rc.4","confirm_interrupt":true})
                }
                _ => json!({"enabled":true}),
            };
            assert_eq!(request.body.as_ref(), Some(&expected));
        }
        if request.method == reqwest::Method::DELETE {
            return Ok(response(200, json!({}), None));
        }
        if path == "/auth/logout" {
            let header = |name: &str| {
                request
                    .headers
                    .get(name)
                    .map(|value| value.to_str().unwrap().to_owned())
                    .unwrap_or_default()
            };
            self.logouts.lock().unwrap().push([
                request.method.to_string(),
                header("origin"),
                header("x-vibe-csrf-token"),
                header("cookie"),
            ]);
            if self.offline.load(Ordering::SeqCst) {
                return Err(ClientError::new("network_unavailable"));
            }
            return Ok(response(200, json!({"ok": true}), None));
        }
        let (expected, reply) = self
            .replies
            .lock()
            .unwrap()
            .pop_front()
            .unwrap_or_else(|| panic!("unexpected HTTP request {path}"));
        assert_eq!(path, expected);
        match reply {
            Reply::Failure => Err(ClientError::new("network_unavailable")),
            Reply::Ready(response) => Ok(response),
            Reply::Held(receiver, entered) => {
                entered.notify_one();
                receiver.await.map_err(|_| ClientError::new("network_unavailable"))
            }
        }
    }
}
fn response(status: u16, body: Value, cookie: Option<&str>) -> Response {
    let mut headers = HeaderMap::new();
    if let Some(cookie) = cookie {
        headers.insert(SET_COOKIE, cookie.parse().unwrap());
    }
    Response {
        status,
        body: serde_json::to_vec(&body).unwrap(),
        headers,
    }
}
fn ok(body: Value) -> Reply {
    Reply::Ready(response(200, body, None))
}

#[derive(Default)]
struct Private(
    Mutex<HashMap<String, Vec<u8>>>,
    Mutex<Option<(oneshot::Sender<()>, std::sync::mpsc::Receiver<()>)>>,
    AtomicBool,
    Mutex<Option<(oneshot::Sender<()>, std::sync::mpsc::Receiver<()>)>>,
    AtomicBool,
);
impl Credentials for Private {
    fn read(&self, id: &str) -> Result<Option<Vec<u8>>, ClientError> {
        let bytes = self.0.lock().unwrap().get(id).cloned();
        if let Some((entered, release)) = self.3.lock().unwrap().take() {
            let _ = entered.send(());
            release.recv().unwrap();
        }
        Ok(bytes)
    }
    fn write(&self, id: &str, bytes: &[u8]) -> Result<(), ClientError> {
        if self.4.swap(false, Ordering::SeqCst) {
            return Err(ClientError::new("credentials_unavailable"));
        }
        self.0.lock().unwrap().insert(id.into(), bytes.into());
        if let Some((entered, release)) = self.1.lock().unwrap().take() {
            let _ = entered.send(());
            release.recv().unwrap();
        }
        Ok(())
    }
    fn erase(&self, id: &str) -> Result<(), ClientError> {
        if self.2.load(Ordering::SeqCst) {
            return Err(ClientError::new("credentials_unavailable"));
        }
        self.0.lock().unwrap().remove(id);
        Ok(())
    }
}
#[derive(Default)]
struct View {
    signout_reached_view: Notify,
    states: Mutex<Vec<ClientState>>,
    committed: Mutex<Vec<ClientTarget>>,
    restored: Mutex<Vec<ClientTarget>>,
    update_calls: Mutex<Vec<(ClientAction, Epoch)>>,
    update_failure: Mutex<Option<String>>,
    retirement_failure: AtomicBool,
    instance_cookies: Mutex<Option<Vec<cookie::Cookie<'static>>>>,
    cookie_reads: std::sync::atomic::AtomicUsize,
    invalidation: Mutex<Option<(oneshot::Sender<()>, std::sync::mpsc::Receiver<()>)>>,
}
#[async_trait]
impl ClientView for View {
    fn desktop_update(&self, action: ClientAction, epoch: Epoch) -> Result<(), ClientError> {
        epoch.check()?;
        self.update_calls.lock().unwrap().push((action, epoch));
        match self.update_failure.lock().unwrap().clone() {
            Some(code) => Err(ClientError::new(code)),
            None => Ok(()),
        }
    }
    fn instance_cookies(&self, epoch: &Epoch, origin: &url::Url) -> Option<Vec<cookie::Cookie<'static>>> {
        epoch.check().ok()?;
        assert_eq!(origin.as_str(), "https://one.avibe.bot/");
        self.cookie_reads.fetch_add(1, Ordering::SeqCst);
        self.instance_cookies.lock().unwrap().clone()
    }
    fn changed(&self, state: &ClientState) {
        self.states.lock().unwrap().push(state.clone());
    }
    fn invalidate(&self, _: u64) {
        let held = self.invalidation.lock().unwrap().take();
        if let Some((entered, release)) = held {
            let _ = entered.send(());
            release.recv_timeout(std::time::Duration::from_secs(5)).unwrap();
        }
    }
    async fn commit(
        &self,
        epoch: Epoch,
        target: ClientTarget,
        _: Vec<cookie::Cookie<'static>>,
    ) -> Result<(), ClientError> {
        epoch.check()?;
        self.committed.lock().unwrap().push(target);
        Ok(())
    }
    async fn restore(&self, _: Epoch, target: ClientTarget, _: bool) -> Result<(), ClientError> {
        self.restored.lock().unwrap().push(target);
        Ok(())
    }
    async fn signed_out(&self, _: Epoch, _: ClientTarget, _: bool) -> Result<(), ClientError> {
        self.signout_reached_view.notify_one();
        if self.retirement_failure.load(Ordering::SeqCst) {
            Err(ClientError::new("window_unavailable"))
        } else {
            Ok(())
        }
    }
}
struct NoPasskeys;
#[async_trait]
impl Passkeys for NoPasskeys {
    async fn assert(&self, _: Value, _: Epoch) -> Result<Value, ClientError> {
        Err(ClientError::new("passkey_unavailable"))
    }
}
struct Fixture {
    client: Arc<Coordinator>,
    http: Arc<Http>,
    private: Arc<Private>,
    view: Arc<View>,
    store: ConnectionStore,
    _root: tempfile::TempDir,
}
fn fixture(replies: Vec<(&'static str, Reply)>) -> Fixture {
    fixture_in(tempfile::tempdir().unwrap(), replies)
}
/// A relaunch: a new coordinator over the same Desktop profile directory.
fn fixture_in(root: tempfile::TempDir, replies: Vec<(&'static str, Reply)>) -> Fixture {
    let store = ConnectionStore::new(root.path().join("connection.json"));
    let record = store.load("小明的 Mac").unwrap();
    let state = ClientState::new("小明的 Mac".into(), "0.1.0".into(), json!({}));
    let http = Arc::new(Http {
        replies: Mutex::new(replies.into()),
        calls: Mutex::new(vec![]),
        cookies: Mutex::new(vec![]),
        logouts: Mutex::new(vec![]),
        offline: AtomicBool::new(false),
    });
    let private = Arc::new(Private::default());
    let view = Arc::new(View::default());
    let client = Arc::new(Coordinator::new(
        state,
        record,
        store.clone(),
        http.clone(),
        private.clone(),
        Arc::new(NoPasskeys),
        view.clone(),
    ));
    Fixture {
        client,
        http,
        private,
        view,
        store,
        _root: root,
    }
}
async fn action(client: &Coordinator, action: ClientAction) -> ClientReply {
    client
        .request(ClientRequest {
            version: 1,
            request_id: "unit".into(),
            expected_generation: client.state().generation,
            action,
        })
        .await
}

#[test]
fn document_claim_receipts_retire_on_real_coordinator_epochs_and_do_not_revive_on_label_reuse() {
    let f = fixture(vec![]);
    let first = f.client.claim_document(f.client.state().generation).unwrap();
    let old = first.caller().unwrap();
    f.client.local_origin(Some(
        avibe_runtime_host::LoopbackOrigin::parse("http://127.0.0.1:1984").unwrap(),
    ));
    assert!(old.check().is_err());
    let second = f.client.claim_document(f.client.state().generation).unwrap();
    let current = second.caller().unwrap();
    drop(first);
    old.retire();
    assert!(current.check().is_ok());
    f.client.window_destroyed();
    assert!(current.check().is_err());
    assert!(f.http.calls.lock().unwrap().is_empty());
}

// The real coordinator (signed in through the synthetic transport), but an
// unadopted Runtime. No probe, helper, keychain or service is allowed here.
#[tokio::test]
async fn open_returns_only_a_revocable_receipt_and_cancel_does_not_queue_behind_account_io() {
    use avibe_runtime_host::instance_client::coordinator::claims::{
        ClaimAction, ClaimCaller, ClaimReply, ClaimRequest,
    };
    use avibe_runtime_host::{
        HealthProbe, LaunchError, LoopbackOrigin, Presence, ResolvedRuntimeLauncher, RuntimeHost, RuntimeHostSettings,
        RuntimeLauncher,
    };
    struct NoRuntime;
    #[async_trait]
    impl HealthProbe for NoRuntime {
        async fn presence(&self, _: &LoopbackOrigin, _: Option<&str>) -> Presence {
            panic!("unexpected probe")
        }
    }
    impl RuntimeLauncher for NoRuntime {
        fn resolve(&self) -> Result<Arc<dyn ResolvedRuntimeLauncher>, LaunchError> {
            panic!("unexpected resolution")
        }
    }
    async fn send<'a>(
        f: &'a Fixture,
        host: &RuntimeHost,
        caller: &ClaimCaller,
        action: ClaimAction,
    ) -> Result<ClaimReply<'a>, ClientError> {
        f.client
            .claim_review(
                host,
                caller,
                "en",
                ClaimRequest {
                    expected_generation: f.client.state().generation,
                    action,
                },
            )
            .await
    }
    let host = RuntimeHost::new(Arc::new(NoRuntime), Arc::new(NoRuntime), RuntimeHostSettings::default());
    let f = fixture(vec![]);
    let doc = f.client.claim_document(f.client.state().generation).unwrap();
    let caller = doc.caller().unwrap();
    assert_eq!(
        send(&f, &host, &caller, ClaimAction::Open {}).await.err().unwrap().code,
        "authentication_required"
    );
    sign_in(&f, catalog("account-a", &["one"])).await;
    let before = f.http.calls.lock().unwrap().len();
    let doc = f.client.claim_document(f.client.state().generation).unwrap();
    let caller = doc.caller().unwrap();
    let open = send(&f, &host, &caller, ClaimAction::Open {})
        .await
        .unwrap()
        .deliver()
        .unwrap()
        .unwrap();
    assert_eq!(open.as_object().unwrap().len(), 1);
    let receipt = open["receipt"].as_str().unwrap().to_owned();
    assert!(uuid::Uuid::parse_str(&receipt).is_ok());
    assert_eq!(before, f.http.calls.lock().unwrap().len());
    assert!(f.client.claim_document(f.client.state().generation).is_err());
    assert!(!serde_json::to_string(&f.client.state()).unwrap().contains(&receipt));
    let entered = Arc::new(Notify::new());
    let (release, wait) = oneshot::channel();
    f.http
        .replies
        .lock()
        .unwrap()
        .push_back(("/api/mobile/session", Reply::Held(wait, entered.clone())));
    let client = f.client.clone();
    let blocked = tokio::spawn(async move { action(&client, ClientAction::RefreshInstances).await });
    entered.notified().await;
    let cancel = tokio::time::timeout(
        std::time::Duration::from_secs(1),
        send(
            &f,
            &host,
            &caller,
            ClaimAction::Cancel {
                receipt: receipt.clone(),
            },
        ),
    )
    .await
    .expect("cancel waited for HTTP");
    // The admitted ordinary transaction retired this receipt before its GET.
    assert_eq!(cancel.err().unwrap().code, "stale_generation");
    assert!(send(
        &f,
        &host,
        &caller,
        ClaimAction::Read {
            receipt: receipt.clone()
        }
    )
    .await
    .is_err());
    release
        .send(response(200, catalog("account-a", &["one"]), None))
        .ok()
        .unwrap();
    assert!(blocked.await.unwrap().ok);
    let next = f.client.claim_document(f.client.state().generation).unwrap();
    let current = next.caller().unwrap();
    let open = send(&f, &host, &current, ClaimAction::Open {})
        .await
        .unwrap()
        .deliver()
        .unwrap()
        .unwrap();
    let new = open["receipt"].as_str().unwrap().to_owned();
    assert_ne!(new, receipt);
    // A delayed message from the same still-loaded page may be admitted with
    // today's caller, not the caller originally paired with its old receipt.
    assert!(send(
        &f,
        &host,
        &current,
        ClaimAction::Cancel {
            receipt: receipt.clone()
        }
    )
    .await
    .is_err());
    assert!(send(&f, &host, &current, ClaimAction::Read { receipt }).await.is_err());
    assert!(current.monitoring());
    let failed = send(&f, &host, &current, ClaimAction::Read { receipt: new.clone() })
        .await
        .unwrap();
    // There is no adopted Runtime. The refusal is private, and the ticket remains
    // single-use while the shell rechecks its caller before delivering the error.
    assert!(send(&f, &host, &current, ClaimAction::Read { receipt: new })
        .await
        .is_err());
    assert_eq!(
        serde_json::to_value(failed.deliver().unwrap_err()).unwrap(),
        json!({"code":"local_runtime_upgrade_required"})
    );
    assert!(!current.monitoring());
    assert_eq!(f.http.calls.lock().unwrap().len(), before + 1);
}

// A4 requires current local proof, including failure after a successful read.
// A URL validation case alone cannot exercise transport failure publication.
#[tokio::test]
async fn local_refresh_failure_clears_proof_without_reclassifying_enrollment() {
    for failure in [Reply::Failure, Reply::Ready(response(503, json!({}), None))] {
        let f = fixture(vec![
            ("/api/desktop/local-instance", ok(local_proof())),
            ("/api/desktop/local-instance", failure),
        ]);
        f.client.local_origin(Some(
            avibe_runtime_host::LoopbackOrigin::parse("http://127.0.0.1:1984").unwrap(),
        ));
        f.client.refresh_local().await;
        assert_eq!(f.client.state().local.instance_id.as_deref(), Some("local-id"));
        assert_eq!(
            f.client.state().local.remote_access_url.as_deref(),
            Some("https://local.avibe.bot")
        );
        let enrollment = serde_json::to_value(f.client.state().enrollment).unwrap();
        f.client.refresh_local().await;
        let state = f.client.state();
        assert!(state.local.instance_id.is_none() && state.local.remote_access_url.is_none());
        assert_eq!(state.local.remote_access, RemoteAccessState::Unavailable);
        assert_eq!(serde_json::to_value(state.enrollment).unwrap(), enrollment);
    }
}

fn local_proof() -> Value {
    json!({"schema_version":1,"installation_id":"fixture","binding_revision":"one","device_name":"Local",
        "pairing":{"instance_id":"local-id","backend_url":"https://avibe.bot","public_url":"https://local.avibe.bot"},
        "remote_access":{"configured_enabled":true,"requested":true,"running":true}})
}

// A suspended account transaction cloned the old local proof. Its later
// persistence must not resurrect that proof after a failed local refresh.
#[tokio::test]
async fn local_refresh_cannot_be_overwritten_by_an_older_account_transaction() {
    let (release, waiting) = oneshot::channel();
    let entered = Arc::new(Notify::new());
    let f = fixture(vec![
        ("/api/desktop/local-instance", ok(local_proof())),
        (
            "/api/config/auth",
            ok(json!({"supabase_url":"https://provider.test","supabase_anon_key":"fixture"})),
        ),
        ("/auth/v1/token", Reply::Held(waiting, entered.clone())),
        ("/api/desktop/local-instance", Reply::Failure),
    ]);
    f.client.local_origin(Some(
        avibe_runtime_host::LoopbackOrigin::parse("http://127.0.0.1:1984").unwrap(),
    ));
    f.client.refresh_local().await;
    let client = f.client.clone();
    let password = tokio::spawn(async move {
        action(
            &client,
            ClientAction::SignInPassword {
                email: "user@example.test".into(),
                password: "synthetic".into(),
            },
        )
        .await
    });
    entered.notified().await;
    let client = f.client.clone();
    let mut refresh = tokio::spawn(async move {
        client.refresh_local().await;
    });
    // Old behavior completes the refresh while the older account clone is held.
    // Serialized refresh legitimately waits for that operation to finish.
    let finished = tokio::time::timeout(std::time::Duration::from_millis(50), &mut refresh)
        .await
        .is_ok();
    release
        .send(response(400, json!({"error":"invalid_credentials"}), None))
        .ok()
        .unwrap();
    assert!(!password.await.unwrap().ok);
    if !finished {
        refresh.await.unwrap();
    }
    assert!(
        f.client.state().local.instance_id.is_none(),
        "old account clone resurrected local proof"
    );
    assert!(f.client.state().local.remote_access_url.is_none());
}

// A4 first factor must use the existing cookie/2FA owner; the prior OTP-only
// cases never exercised password dispatch or a delayed password provider reply.
#[tokio::test]
async fn password_first_factor_continues_second_factor_and_fences_late_provider() {
    let password = || {
        serde_json::from_value::<ClientAction>(json!({
            "type":"sign_in_password","email":"user@example.test","password":"synthetic-password"
        }))
        .expect("A4 password action")
    };
    for cancel in [false, true] {
        let (release, waiting) = oneshot::channel();
        let entered = Arc::new(Notify::new());
        let f = fixture(vec![
            (
                "/api/config/auth",
                ok(json!({"supabase_url":"https://provider.test","supabase_anon_key":"public-fixture"})),
            ),
            ("/auth/v1/token", Reply::Held(waiting, entered.clone())),
            (
                "/api/desktop/session",
                Reply::Ready(response(
                    403,
                    json!({"error":"two_factor_required","email":"user@example.test","gate":"challenge","methods":["totp"]}),
                    Some("account=fixture; Path=/; Secure; HttpOnly"),
                )),
            ),
            ("/api/two-factor/verify", ok(json!({}))),
            ("/api/mobile/session", ok(signed_in())),
        ]);
        let client = f.client.clone();
        let input = password();
        assert!(!format!("{input:?}").contains("synthetic-password"));
        let pending = tokio::spawn(async move { action(&client, input).await });
        entered.notified().await;
        assert_eq!(f.client.state().generation, 1);
        if cancel {
            assert!(action(&f.client, ClientAction::Cancel).await.ok);
        }
        assert!(release
            .send(response(
                200,
                json!({"access_token":"synthetic-access","refresh_token":"synthetic-refresh"}),
                None
            ))
            .is_ok());
        let reply = pending.await.unwrap();
        if cancel {
            assert_eq!(reply.error.unwrap().code, "stale_generation");
            assert_eq!(
                f.http.calls.lock().unwrap().as_slice(),
                ["/api/config/auth", "/auth/v1/token"]
            );
            assert!(f.private.0.lock().unwrap().is_empty());
        } else {
            assert!(reply.ok);
            assert_eq!(reply.state.account.status, AccountStatus::SecondFactorRequired);
            assert!(
                action(
                    &f.client,
                    ClientAction::VerifySecondFactor {
                        method: CodeMethod::Recovery,
                        code: "fixture-code".into()
                    }
                )
                .await
                .ok
            );
            assert_eq!(f.client.state().account.status, AccountStatus::SignedIn);
        }
        assert!(!serde_json::to_string(&f.client.state()).unwrap().contains("synthetic-"));
    }
}
// AUTH-SETUP-177/178: local updater intents must not become selected-Runtime
// actions or account transactions. Existing OTP/selection tests do not dispatch
// these four new bridge actions or fence their delayed native completion.
#[tokio::test]
async fn update_actions_keep_local_ownership_and_envelope_generation_guards() {
    let actions = [
        json!({"type":"update_check"}),
        json!({"type":"update_set_prereleases","enabled":true}),
        json!({"type":"update_set_auto_update","enabled":false}),
        json!({"type":"update_install","target_version":"3.2.7"}),
    ];
    for remote in [false, true] {
        let f = fixture(vec![]);
        let mut state = f.client.state();
        state.generation = 7;
        state.desktop.update = json!({"phase":"available","latest":"3.2.7"});
        if remote {
            state.connection.committed = ClientTarget::Remote {
                name: "Studio".into(),
                instance_id: "studio-1".into(),
                canonical_origin: "https://studio.avibe.bot".into(),
            };
        }
        let client = Coordinator::new(
            state,
            f.store.load("Local").unwrap(),
            f.store.clone(),
            f.http.clone(),
            f.private.clone(),
            Arc::new(NoPasskeys),
            f.view.clone(),
        );
        let original = serde_json::to_value(client.state()).unwrap();
        for action in &actions {
            for (version, id, generation, error) in [
                (2, "invalid", 7, "invalid_request"),
                (1, "", 7, "invalid_request"),
                (1, "stale", 6, "stale_generation"),
                (1, "future", 8, "stale_generation"),
            ] {
                let before = f.view.update_calls.lock().unwrap().len();
                let reply = client
                    .request(ClientRequest {
                        version,
                        request_id: id.into(),
                        expected_generation: generation,
                        action: serde_json::from_value(action.clone()).unwrap(),
                    })
                    .await;
                assert_eq!(reply.error.unwrap().code, error);
                assert_eq!(f.view.update_calls.lock().unwrap().len(), before);
            }
            let reply = client
                .request(ClientRequest {
                    version: 1,
                    request_id: "update".into(),
                    expected_generation: 7,
                    action: serde_json::from_value(action.clone()).unwrap(),
                })
                .await;
            assert!(reply.ok);
            assert_eq!(reply.request_id, "update");
            assert_eq!(serde_json::to_value(reply.state).unwrap(), original);
            let calls = f.view.update_calls.lock().unwrap();
            assert_eq!(serde_json::to_value(&calls.last().unwrap().0).unwrap(), *action);
            assert_eq!(calls.last().unwrap().1.value, 7);
        }
        *f.view.update_failure.lock().unwrap() = Some("update_in_progress".into());
        assert_eq!(
            action(&client, ClientAction::UpdateCheck {}).await.error.unwrap().code,
            "update_in_progress"
        );
        assert!(f.http.calls.lock().unwrap().is_empty());
        assert!(f.private.0.lock().unwrap().is_empty());
        assert_eq!(serde_json::to_value(client.state()).unwrap(), original);
    }
}

#[tokio::test]
async fn update_actions_cannot_overtake_an_account_operation_and_escape_fences_completion() {
    let (release, waiting) = oneshot::channel();
    let entered = Arc::new(Notify::new());
    let f = fixture(vec![
        ("/api/config/auth", Reply::Held(waiting, entered.clone())),
        ("/auth/v1/otp", ok(json!({}))),
    ]);
    let client = f.client.clone();
    let pending = tokio::spawn(async move {
        action(
            &client,
            ClientAction::RequestOtp {
                email: "user@example.test".into(),
            },
        )
        .await
    });
    entered.notified().await;
    assert_eq!(
        action(&f.client, ClientAction::UpdateCheck {})
            .await
            .error
            .unwrap()
            .code,
        "busy"
    );
    assert!(f.view.update_calls.lock().unwrap().is_empty());
    release
        .send(response(
            200,
            json!({"supabase_url":"https://fixture.supabase.test","supabase_anon_key":"public-fixture"}),
            None,
        ))
        .ok()
        .unwrap();
    assert!(pending.await.unwrap().ok);
    assert!(action(&f.client, ClientAction::UpdateCheck {}).await.ok);
    let epoch = f.view.update_calls.lock().unwrap()[0].1.clone();
    assert!(epoch.check().is_ok());
    assert!(action(&f.client, ClientAction::Cancel).await.ok);
    assert_eq!(epoch.check().unwrap_err().code, "stale_generation");
}

fn code_start() -> (&'static str, Reply) {
    (
        "/api/auth/first-party/start",
        Reply::Ready(response(
            200,
            json!({"transaction":"t".repeat(43)}),
            Some("__Host-avibe-sign-in-start=fixture; Secure; HttpOnly; Path=/; SameSite=Lax; Max-Age=600"),
        )),
    )
}
fn code_bound() -> (&'static str, Reply) {
    (
        "/api/auth/first-party/email-code",
        Reply::Ready(response(
            200,
            json!({"bound":true}),
            Some("avibe_session=bound; Secure; HttpOnly; Path=/"),
        )),
    )
}
fn first_factor() -> Vec<(&'static str, Reply)> {
    vec![
        (
            "/api/config/auth",
            ok(json!({"supabase_url":"https://fixture.supabase.test","supabase_anon_key":"public-fixture"})),
        ),
        ("/auth/v1/otp", ok(json!({}))),
        code_start(),
        code_bound(),
    ]
}
fn signed_in() -> Value {
    serde_json::from_str::<Value>(include_str!("../../contracts/instance-client-v1/transport.json")).unwrap()
        ["account_session"]
        .clone()
}

// The link is received before its native task runs. A lifecycle-only change
// must preserve that captured action authority; a newer explicit action or a
// real cleanup failure must not. The window tests cover the separate delivery
// callback boundary; this exercises the actual admission and durable owner.
#[tokio::test]
async fn native_link_admission_preserves_only_its_received_action_authority() {
    use avibe_runtime_host::{deep_link::DeepLinks, BootstrapNoticeCode, BootstrapStatus, LoopbackOrigin};

    for interruption in [
        "lifecycle",
        "lifecycle_twice",
        "explicit",
        "new_link",
        "cleanup_failure",
    ] {
        let f = fixture(first_factor());
        if interruption == "cleanup_failure" {
            f.view.retirement_failure.store(true, Ordering::SeqCst);
            assert_eq!(
                action(&f.client, ClientAction::SignOut { intent_id: None })
                    .await
                    .error
                    .unwrap()
                    .code,
                "window_unavailable"
            );
        }
        let mut links = DeepLinks::default();
        let owner = f.client.presentation_owner();
        let generation = f.client.state().generation;
        links.receive_owned(["avibe://settings/shortcuts"], Some(owner));
        let ticket = links.claim_local_return(generation, owner).unwrap();
        let origin = LoopbackOrigin::parse("http://127.0.0.1:1984").unwrap();
        f.client.local_origin(Some(origin.clone()));
        if interruption == "lifecycle_twice" {
            f.client.local_origin(None);
            f.client.local_origin(Some(origin.clone()));
        }
        let target = if interruption == "new_link" {
            links.receive_owned(["avibe://settings/computer-use"], Some(owner));
            "/settings/computer-use"
        } else {
            "/settings/shortcuts"
        };
        if interruption == "explicit" {
            assert!(
                action(
                    &f.client,
                    ClientAction::RequestOtp {
                        email: "later@example.test".into()
                    }
                )
                .await
                .ok
            );
        }
        assert!(links
            .claim_local_return(f.client.state().generation, f.client.presentation_owner())
            .is_none());
        let before = serde_json::to_value(f.client.state()).unwrap();
        let record = std::fs::read(f._root.path().join("connection.json")).unwrap();
        let restores = f.view.restored.lock().unwrap().len();
        let result = f
            .client
            .return_local_if_current(ticket.expected_generation, ticket.action_owner)
            .await;
        if interruption == "explicit" {
            assert_eq!(result.unwrap_err().code, "stale_generation");
            assert_eq!(serde_json::to_value(f.client.state()).unwrap(), before);
            assert_eq!(std::fs::read(f._root.path().join("connection.json")).unwrap(), record);
            assert_eq!(f.view.restored.lock().unwrap().len(), restores);
            links.finish_local_return(&ticket, None);
            assert!(!links.pending_for_owner(Some(f.client.presentation_owner())));
        } else if interruption == "cleanup_failure" {
            assert_eq!(result.unwrap_err().code, "window_unavailable");
            links.finish_local_return(&ticket, None);
            assert!(!links.pending_for_owner(Some(f.client.presentation_owner())));
        } else {
            assert!(
                result.is_ok(),
                "same received action lost to {interruption}: {result:?}"
            );
            let accepted_owner = result.unwrap();
            links.finish_local_return(&ticket, Some(accepted_owner));
            assert_eq!(accepted_owner, f.client.presentation_owner());
            assert_eq!(accepted_owner, before["generation"].as_u64().unwrap() + 1);
            assert!(links.pending_for_owner(Some(accepted_owner)));
            assert_eq!(f.view.restored.lock().unwrap().len(), restores + 1);
            assert!(f.store.load("unused").unwrap().last_committed.is_local());
            let ready = BootstrapStatus::ready(&origin, 1, BootstrapNoticeCode::Ready);
            let bootstrap = links.bootstrap_navigation_owned(&ready, Some(accepted_owner)).unwrap();
            let navigation = links.workbench_navigation(&origin, &origin.navigation_url()).unwrap();
            assert_eq!(bootstrap.url().path(), target);
            assert_eq!(navigation.url().path(), target);
            assert!(!links.commit_navigation(&navigation, false, 1, 1));
            assert!(links.has_pending());
            assert!(links.commit_navigation(&navigation, true, 1, 1));
            assert!(!links.commit_navigation(&bootstrap, true, 1, 1));
            assert!(!links.has_pending());
        }
    }
}

// Admission must reject obsolete native work without changing anything. Once
// accepted, lifecycle can interrupt callbacks, but only a newer explicit
// action may replace the accepted state/record. Existing credential tests hold
// a later boundary; this holds invalidate between acceptance and publication.
#[tokio::test]
async fn native_return_admission_and_state_commit_keep_their_action_owner() {
    for explicit in [false, true] {
        let f = fixture(first_factor());
        let record = std::fs::read(f._root.path().join("connection.json")).unwrap();
        let before = f.client.state();
        assert_eq!(
            f.client
                .return_local_if_current(before.generation + 1, f.client.presentation_owner())
                .await
                .unwrap_err()
                .code,
            "stale_generation"
        );
        assert_eq!(
            serde_json::to_value(f.client.state()).unwrap(),
            serde_json::to_value(before).unwrap()
        );
        assert!(f.view.states.lock().unwrap().is_empty());
        assert!(f.view.restored.lock().unwrap().is_empty());
        assert_eq!(std::fs::read(f._root.path().join("connection.json")).unwrap(), record);
        let (entered, reached) = oneshot::channel();
        let (release, released) = std::sync::mpsc::channel();
        *f.view.invalidation.lock().unwrap() = Some((entered, released));
        let client = f.client.clone();
        let runtime = tokio::runtime::Handle::current();
        let returning = tokio::task::spawn_blocking(move || runtime.block_on(client.return_local_if_current(0, 0)));
        reached.await.unwrap();
        if explicit {
            assert!(
                action(
                    &f.client,
                    ClientAction::RequestOtp {
                        email: "newer@example.test".into()
                    }
                )
                .await
                .ok
            );
        } else {
            f.client.local_origin(Some(
                avibe_runtime_host::LoopbackOrigin::parse("http://127.0.0.1:1984").unwrap(),
            ));
        }
        let accepted = f.client.state();
        release.send(()).unwrap();
        let result = returning.await.unwrap();
        if explicit {
            assert_eq!(result.unwrap_err().code, "stale_generation");
            assert_eq!(
                serde_json::to_value(f.client.state()).unwrap(),
                serde_json::to_value(accepted).unwrap()
            );
            assert!(f.view.restored.lock().unwrap().is_empty());
            assert_eq!(std::fs::read(f._root.path().join("connection.json")).unwrap(), record);
        } else {
            assert!(result.is_ok());
            assert!(f.client.state().connection.committed.is_local());
            assert!(f.store.load("unused").unwrap().last_committed.is_local());
            assert_eq!(f.view.restored.lock().unwrap().len(), 1);
        }
    }
}

// Initial binding may invalidate credential reads or discovery. Only that
// lifecycle event may continue startup restoration; explicit escape still wins.
#[tokio::test]
async fn startup_restore_continues_only_after_local_lifecycle_invalidation() {
    for held_read in [true, false] {
        for (escape, erase_failure) in [
            (None, false),
            (Some(ClientAction::Cancel), false),
            (Some(ClientAction::ReturnLocal), false),
            (Some(ClientAction::SignOut { intent_id: None }), false),
            (Some(ClientAction::SignOut { intent_id: None }), true),
        ] {
            let mut replies = first_factor();
            replies.push((
                "/api/mobile/session",
                Reply::Ready(response(
                    200,
                    signed_in(),
                    Some("avibe_session=saved; Secure; HttpOnly; Path=/"),
                )),
            ));
            let f = fixture(replies);
            assert!(
                action(
                    &f.client,
                    ClientAction::RequestOtp {
                        email: "xiaoming@example.test".into()
                    }
                )
                .await
                .ok
            );
            assert!(
                action(&f.client, ClientAction::VerifyOtp { code: "123456".into() })
                    .await
                    .ok
            );
            let restored = Arc::new(Coordinator::new(
                ClientState::new("小明的 Mac".into(), "0.1.0".into(), json!({})),
                f.store.load("unused").unwrap(),
                f.store.clone(),
                f.http.clone(),
                f.private.clone(),
                Arc::new(NoPasskeys),
                f.view.clone(),
            ));
            let (read_entered, reading) = oneshot::channel();
            let (read_release, read_receiver) = std::sync::mpsc::channel();
            let (http_release, http_held) = oneshot::channel();
            let http_entered = Arc::new(Notify::new());
            if held_read {
                *f.private.3.lock().unwrap() = Some((read_entered, read_receiver));
            } else {
                f.http
                    .replies
                    .lock()
                    .unwrap()
                    .push_back(("/api/mobile/session", Reply::Held(http_held, http_entered.clone())));
            }
            if escape.is_none() {
                f.http
                    .replies
                    .lock()
                    .unwrap()
                    .push_back(("/api/mobile/session", ok(signed_in())));
            }
            let client = restored.clone();
            let restore = tokio::spawn(async move { client.restore_account().await });
            if held_read {
                reading.await.unwrap();
            } else {
                http_entered.notified().await;
            }
            restored.local_origin(Some(
                avibe_runtime_host::LoopbackOrigin::parse("http://127.0.0.1:1984").unwrap(),
            ));
            if let Some(action_kind) = escape.clone() {
                f.private.2.store(erase_failure, Ordering::SeqCst);
                let escaped = action(&restored, action_kind).await;
                if erase_failure {
                    assert_eq!(escaped.error.unwrap().code, "credentials_unavailable");
                } else {
                    assert!(escaped.ok);
                }
            }
            if held_read {
                read_release.send(()).unwrap();
            } else {
                http_release.send(response(200, signed_in(), None)).ok().unwrap();
            }
            let result = restore.await.unwrap();
            if escape.is_none() {
                result.unwrap();
                assert_eq!(restored.state().account.status, AccountStatus::SignedIn);
            } else {
                assert_eq!(result.unwrap_err().code, "stale_generation");
                assert_eq!(restored.state().account.status, AccountStatus::SignedOut);
            }
            if erase_failure {
                assert!(f.store.load("unused").unwrap().account_id.is_none());
                assert_eq!(
                    action(
                        &restored,
                        ClientAction::RequestOtp {
                            email: "next@example.test".into()
                        }
                    )
                    .await
                    .error
                    .unwrap()
                    .code,
                    "credentials_unavailable"
                );
                f.private.2.store(false, Ordering::SeqCst);
                assert!(action(&restored, ClientAction::SignOut { intent_id: None }).await.ok);
                assert!(f.private.0.lock().unwrap().is_empty());
            }
            assert!(f.http.replies.lock().unwrap().is_empty());
        }
    }
}

#[tokio::test]
async fn signout_during_session_establishment_discards_late_cookie_and_stops_enrollment() {
    let (release, held) = oneshot::channel();
    let entered = Arc::new(Notify::new());
    let mut replies = first_factor();
    replies.push(("/api/mobile/session", Reply::Held(held, entered.clone())));
    let f = fixture(replies);
    assert!(
        action(
            &f.client,
            ClientAction::RequestOtp {
                email: "xiaoming@example.test".into()
            }
        )
        .await
        .ok
    );
    let client = f.client.clone();
    let verification =
        tokio::spawn(async move { action(&client, ClientAction::VerifyOtp { code: "123456".into() }).await });
    entered.notified().await;
    assert!(action(&f.client, ClientAction::SignOut { intent_id: None }).await.ok);
    release
        .send(response(
            200,
            signed_in(),
            Some("avibe_session=private-account; Secure; HttpOnly; Path=/"),
        ))
        .ok()
        .unwrap();
    let reply = verification.await.unwrap();
    assert_eq!(reply.error.unwrap().code, "stale_generation");
    assert_eq!(f.client.state().account.status, AccountStatus::SignedOut);
    assert!(f.private.0.lock().unwrap().is_empty());
    assert!(f.store.load("unused").unwrap().account_id.is_none());
    assert!(f.view.committed.lock().unwrap().is_empty());
    assert!(!f
        .http
        .calls
        .lock()
        .unwrap()
        .iter()
        .any(|path| path.contains("ensure-local")));
}

#[tokio::test]
async fn cancel_during_catalog_refresh_never_sends_remote_authorization() {
    let (release, held) = oneshot::channel();
    let entered = Arc::new(Notify::new());
    let mut replies = first_factor();
    replies.extend([
        (
            "/api/mobile/session",
            Reply::Ready(response(
                200,
                signed_in(),
                Some("avibe_session=private-account; Secure; HttpOnly; Path=/"),
            )),
        ),
        ("/api/mobile/session", Reply::Held(held, entered.clone())),
    ]);
    let f = fixture(replies);
    assert!(
        action(
            &f.client,
            ClientAction::RequestOtp {
                email: "xiaoming@example.test".into()
            }
        )
        .await
        .ok
    );
    assert!(
        action(&f.client, ClientAction::VerifyOtp { code: "123456".into() })
            .await
            .ok
    );
    let client = f.client.clone();
    let selection = tokio::spawn(async move {
        action(
            &client,
            ClientAction::SelectInstance {
                instance_id: "studio-1".into(),
            },
        )
        .await
    });
    entered.notified().await;
    let cancel = action(&f.client, ClientAction::Cancel).await;
    assert!(cancel.ok);
    release.send(response(200, signed_in(), None)).ok().unwrap();
    assert_eq!(selection.await.unwrap().error.unwrap().code, "stale_generation");
    assert!(f.client.state().connection.committed.is_local());
    assert!(f.view.committed.lock().unwrap().is_empty());
    assert!(!f.http.calls.lock().unwrap().iter().any(|path| path == "/auth/login"));
    assert!(!f.private.0.lock().unwrap().is_empty());
}

#[tokio::test]
async fn second_factor_blocks_enrollment_and_direct_totp_material_never_broadcasts() {
    let mut replies = first_factor();
    replies.extend([
        ("/api/mobile/session",Reply::Ready(response(403,json!({
            "error":"two_factor_required","email":"xiaoming@example.test","gate":"enroll","methods":["totp"]
        }),Some("avibe_session=first-factor-private; Secure; HttpOnly; Path=/")))),
        ("/api/two-factor/totp/setup",ok(json!({"token":"private-enrollment-token","uri":"otpauth://fixture","qrSvg":"<svg/>","secret":"private-seed"}))),
        ("/api/two-factor/totp",ok(json!({"method":{"id":"totp-1","kind":"totp","name":"Authenticator","createdAt":"2026-11-13","lastUsedAt":null},"recoveryCodes":["private-recovery"]}))),
        ("/api/mobile/session",Reply::Ready(response(503,json!({"error":"unavailable"}),None))),
    ]);
    let f = fixture(replies);
    assert!(
        action(
            &f.client,
            ClientAction::RequestOtp {
                email: "xiaoming@example.test".into()
            }
        )
        .await
        .ok
    );
    assert!(
        action(&f.client, ClientAction::VerifyOtp { code: "123456".into() })
            .await
            .ok
    );
    assert_eq!(f.client.state().account.status, AccountStatus::SecondFactorRequired);
    assert!(!action(&f.client, ClientAction::RetryEnrollment).await.ok);
    let direct = action(&f.client, ClientAction::BeginTotpEnrollment).await;
    assert!(matches!(direct.result, Some(DirectResult::TotpEnrollment { .. })));
    let done = action(&f.client, ClientAction::FinishTotpEnrollment { code: "123456".into() }).await;
    assert!(matches!(done.result, Some(DirectResult::TotpEnrolled { .. })));
    let public = serde_json::to_string(&*f.view.states.lock().unwrap()).unwrap();
    for secret in [
        "private-enrollment-token",
        "private-seed",
        "private-recovery",
        "first-factor-private",
    ] {
        assert!(!public.contains(secret));
    }
    assert!(f.private.0.lock().unwrap().is_empty());
}

// Credential persistence can complete before the account becomes public. A
// logout must erase that key even when it captured no committed account ID.
#[tokio::test]
async fn signout_owns_and_erases_a_first_credential_write_before_public_commit() {
    for drop_logout_waiter in [false, true] {
        let mut replies = first_factor();
        replies.push((
            "/api/mobile/session",
            Reply::Ready(response(
                200,
                signed_in(),
                Some("avibe_session=private-account; Secure; HttpOnly; Path=/"),
            )),
        ));
        let f = fixture(replies);
        assert!(
            action(
                &f.client,
                ClientAction::RequestOtp {
                    email: "xiaoming@example.test".into()
                }
            )
            .await
            .ok
        );
        let (entered, writing) = oneshot::channel();
        let (release, receiver) = std::sync::mpsc::channel();
        *f.private.1.lock().unwrap() = Some((entered, receiver));
        let client = f.client.clone();
        let verification =
            tokio::spawn(async move { action(&client, ClientAction::VerifyOtp { code: "123456".into() }).await });
        writing.await.unwrap();
        assert!(f.client.state().account.identity.is_none());
        assert!(!f.private.0.lock().unwrap().is_empty());
        let client = f.client.clone();
        let logout = tokio::spawn(async move { action(&client, ClientAction::SignOut { intent_id: None }).await });
        tokio::time::timeout(std::time::Duration::from_secs(2), async {
            while f.client.state().account.status != AccountStatus::SignedOut {
                tokio::task::yield_now().await;
            }
        })
        .await
        .unwrap();
        if drop_logout_waiter {
            // Sign-out has fenced the account but the credential writer is still
            // blocked. Its eventual erasure AND native store retirement are owed.
            logout.abort();
            assert!(logout.await.unwrap_err().is_cancelled());
            release.send(()).unwrap();
            tokio::time::timeout(
                std::time::Duration::from_secs(5),
                f.view.signout_reached_view.notified(),
            )
            .await
            .unwrap();
        } else {
            release.send(()).unwrap();
            assert!(logout.await.unwrap().ok);
        }
        assert_eq!(verification.await.unwrap().error.unwrap().code, "stale_generation");
        assert!(f.private.0.lock().unwrap().is_empty());
        assert!(f.store.load("unused").unwrap().account_id.is_none());
    }
}

#[tokio::test]
async fn old_signout_cannot_erase_a_newer_generation() {
    let f = fixture(vec![]);
    assert!(action(&f.client, ClientAction::ReturnLocal).await.ok);
    let reply = f
        .client
        .request(ClientRequest {
            version: 1,
            request_id: "old".into(),
            expected_generation: 0,
            action: ClientAction::SignOut { intent_id: None },
        })
        .await;
    assert_eq!(reply.error.unwrap().code, "stale_generation");
    assert_eq!(f.client.state().generation, 1);
}

// The actual-window cases interrupt navigation. This earlier boundary holds
// credential IO: lifecycle must not discard an accepted local presentation
// before it reaches the window owner, while a later explicit action must win.
#[tokio::test]
async fn local_escape_reaches_its_view_owner_after_lifecycle_during_credential_io() {
    for superseded in [false, true] {
        let mut replies = first_factor();
        replies.push((
            "/api/mobile/session",
            Reply::Ready(response(
                200,
                signed_in(),
                Some("avibe_session=private-account; Secure; HttpOnly; Path=/"),
            )),
        ));
        let f = fixture(replies);
        assert!(
            action(
                &f.client,
                ClientAction::RequestOtp {
                    email: "xiaoming@example.test".into()
                }
            )
            .await
            .ok
        );
        assert!(
            action(&f.client, ClientAction::VerifyOtp { code: "123456".into() })
                .await
                .ok
        );
        let mut redirect = response(302, json!({}), None);
        redirect.headers.insert(
            LOCATION,
            "https://avibe.bot/oauth/authorize?state=owned&redirect_uri=https%3A%2F%2Fstudio.avibe.bot%2Fauth%2Fcallback"
                .parse()
                .unwrap(),
        );
        f.http.replies.lock().unwrap().extend([
            ("/api/mobile/session", ok(signed_in())),
            ("/auth/login", Reply::Ready(redirect)),
            (
                "/api/desktop/instance-authorization",
                ok(json!({
                    "status":"authorized",
                    "redirect_url":"https://studio.avibe.bot/auth/callback?code=fixture-code&state=owned"
                })),
            ),
            (
                "/auth/callback",
                Reply::Ready(response(
                    302,
                    json!({}),
                    Some("__Host-vibe_remote_session=instance-only; Secure; HttpOnly; Path=/"),
                )),
            ),
            (
                "/api/session",
                ok(json!({
                    "remote":true,"authenticated":true,"authorization_state":"current",
                    "sub":"account-xiaoming","instance_id":"studio-1","capabilities":{"can_read_instance":true}
                })),
            ),
            ("/api/mobile/session", ok(signed_in())),
        ]);
        assert!(
            action(
                &f.client,
                ClientAction::SelectInstance {
                    instance_id: "studio-1".into()
                }
            )
            .await
            .ok
        );
        let before = f.store.load("unused").unwrap();
        assert!(matches!(before.last_committed, ClientTarget::Remote { .. }));
        assert!(before.account_id.is_some());
        let (entered, writing) = oneshot::channel();
        let (release, receiver) = std::sync::mpsc::channel();
        *f.private.1.lock().unwrap() = Some((entered, receiver));
        let client = f.client.clone();
        let verification = tokio::spawn(async move { action(&client, ClientAction::RefreshInstances).await });
        writing.await.unwrap();
        let generation = f.client.state().generation;
        let client = f.client.clone();
        let escape = tokio::spawn(async move {
            action(
                &client,
                if superseded {
                    ClientAction::Cancel
                } else {
                    ClientAction::ReturnLocal
                },
            )
            .await
        });
        tokio::time::timeout(std::time::Duration::from_secs(2), async {
            while f.client.state().generation == generation {
                tokio::task::yield_now().await;
            }
        })
        .await
        .unwrap();
        f.client.local_origin(Some(
            avibe_runtime_host::LoopbackOrigin::parse("http://127.0.0.1:1984").unwrap(),
        ));
        let later = if superseded {
            let generation = f.client.state().generation;
            let client = f.client.clone();
            let later = tokio::spawn(async move { action(&client, ClientAction::ReturnLocal).await });
            tokio::time::timeout(std::time::Duration::from_secs(2), async {
                while f.client.state().generation == generation {
                    tokio::task::yield_now().await;
                }
            })
            .await
            .unwrap();
            Some(later)
        } else {
            None
        };
        release.send(()).unwrap();
        assert_eq!(verification.await.unwrap().error.unwrap().code, "stale_generation");
        let result = escape.await.unwrap();
        if let Some(later) = later {
            assert_eq!(result.error.unwrap().code, "stale_generation");
            assert!(later.await.unwrap().ok);
        } else {
            assert!(result.ok, "{:?}", result.error);
        }
        assert_eq!(f.view.restored.lock().unwrap().len(), 1);
        assert!(f.view.restored.lock().unwrap()[0].is_local());
        let after = f.store.load("unused").unwrap();
        assert!(after.last_committed.is_local());
        assert_eq!(after.account_id, before.account_id);
        assert_eq!(after.last_committed, f.client.state().connection.committed);
    }
}

// A first key write can finish after Cancel. An existing session refresh can
// race the same way. Neither may leave credentials from an uncommitted epoch.
#[tokio::test]
async fn cancellation_rolls_back_unpublished_credential_writes() {
    for (refresh_existing, erase_failure, drop_waiter) in [
        (false, false, false),
        (true, false, false),
        (false, true, false),
        (true, false, true),
    ] {
        let mut replies = first_factor();
        replies.push((
            "/api/mobile/session",
            Reply::Ready(response(
                200,
                signed_in(),
                Some("avibe_session=original; Secure; HttpOnly; Path=/"),
            )),
        ));
        if refresh_existing {
            replies.push((
                "/api/mobile/session",
                Reply::Ready(response(
                    200,
                    signed_in(),
                    Some("avibe_session=replaced; Secure; HttpOnly; Path=/"),
                )),
            ));
        }
        let f = fixture(replies);
        assert!(
            action(
                &f.client,
                ClientAction::RequestOtp {
                    email: "xiaoming@example.test".into()
                }
            )
            .await
            .ok
        );
        if refresh_existing {
            assert!(
                action(&f.client, ClientAction::VerifyOtp { code: "123456".into() })
                    .await
                    .ok
            );
        }
        f.private.2.store(erase_failure, Ordering::SeqCst);
        let before = f.private.0.lock().unwrap().clone();
        let before_record = f.store.load("unused").unwrap().account_id;
        let (entered, writing) = oneshot::channel();
        let (release, receiver) = std::sync::mpsc::channel();
        *f.private.1.lock().unwrap() = Some((entered, receiver));
        let client = f.client.clone();
        let verification = tokio::spawn(async move {
            action(
                &client,
                if refresh_existing {
                    ClientAction::RefreshInstances
                } else {
                    ClientAction::VerifyOtp { code: "123456".into() }
                },
            )
            .await
        });
        writing.await.unwrap();
        let generation = f.client.state().generation;
        let client = f.client.clone();
        let cancel = tokio::spawn(async move { action(&client, ClientAction::Cancel).await });
        tokio::time::timeout(std::time::Duration::from_secs(2), async {
            while f.client.state().generation == generation {
                tokio::task::yield_now().await;
            }
        })
        .await
        .unwrap();
        // Cancelling the invoke is different from an epoch change. The actual
        // OS writer must still own rollback and exclude the escaped account.
        if drop_waiter {
            verification.abort();
            assert!(verification.await.unwrap_err().is_cancelled());
            release.send(()).unwrap();
        } else {
            release.send(()).unwrap();
            assert_eq!(
                verification.await.unwrap().error.unwrap().code,
                if erase_failure {
                    "credentials_unavailable"
                } else {
                    "stale_generation"
                }
            );
        }
        let cancelled = cancel.await.unwrap();
        if erase_failure {
            assert_eq!(cancelled.error.unwrap().code, "credentials_unavailable");
            assert_eq!(
                f.client.state().enrollment.error_code.as_deref(),
                Some("credentials_unavailable")
            );
            f.private.2.store(false, Ordering::SeqCst);
            assert!(action(&f.client, ClientAction::SignOut { intent_id: None }).await.ok);
        } else {
            assert!(cancelled.ok);
        }
        assert_eq!(*f.private.0.lock().unwrap(), before);
        assert_eq!(f.store.load("unused").unwrap().account_id, before_record);
    }
}

#[tokio::test]
async fn restore_rejects_expired_or_different_account_and_erases_owned_credential() {
    // The native store owns its erase callback. A failure returned by that
    // owner must block account work just like a credential-store failure.
    for (changed_account, erase_failure, retirement_failure) in [
        (false, false, false),
        (true, false, false),
        (false, true, false),
        (false, false, true),
    ] {
        let mut replies = first_factor();
        replies.push((
            "/api/mobile/session",
            Reply::Ready(response(
                200,
                signed_in(),
                Some("avibe_session=original; Secure; HttpOnly; Path=/"),
            )),
        ));
        let f = fixture(replies);
        assert!(
            action(
                &f.client,
                ClientAction::RequestOtp {
                    email: "xiaoming@example.test".into()
                }
            )
            .await
            .ok
        );
        assert!(
            action(&f.client, ClientAction::VerifyOtp { code: "123456".into() })
                .await
                .ok
        );
        let mut other = signed_in();
        other["account"]["id"] = json!("other-account");
        f.http.replies.lock().unwrap().push_back((
            "/api/mobile/session",
            if changed_account {
                ok(other)
            } else {
                Reply::Ready(response(401, json!({"error":"signed_out"}), None))
            },
        ));
        f.private.2.store(erase_failure, Ordering::SeqCst);
        f.view.retirement_failure.store(retirement_failure, Ordering::SeqCst);
        let restored = Coordinator::new(
            ClientState::new("小明的 Mac".into(), "0.1.0".into(), json!({})),
            f.store.load("unused").unwrap(),
            f.store.clone(),
            f.http.clone(),
            f.private.clone(),
            Arc::new(NoPasskeys),
            f.view.clone(),
        );
        let error = restored.restore_account().await.unwrap_err();
        assert_eq!(
            error.code,
            if erase_failure {
                "credentials_unavailable"
            } else if retirement_failure {
                "window_unavailable"
            } else if changed_account {
                "account_changed"
            } else {
                "signed_out"
            }
        );
        assert_eq!(restored.state().account.status, AccountStatus::SignedOut);
        assert!(restored.state().connection.committed.is_local());
        assert_eq!(
            restored.state().enrollment.error_code.as_deref(),
            Some(error.code.as_str())
        );
        if erase_failure || retirement_failure {
            let snapshot = action(&restored, ClientAction::GetState).await;
            assert_eq!(snapshot.error.unwrap().code, error.code);
            assert!(snapshot.result.is_none());
            assert_eq!(!f.private.0.lock().unwrap().is_empty(), erase_failure);
            f.http
                .replies
                .lock()
                .unwrap()
                .extend(first_factor().into_iter().take(2));
            let calls = f.http.calls.lock().unwrap().len();
            let blocked = action(
                &restored,
                ClientAction::RequestOtp {
                    email: "other@example.test".into(),
                },
            )
            .await;
            assert_eq!(
                f.http.calls.lock().unwrap().len(),
                calls,
                "an unfinished native erase must block new account requests"
            );
            assert_eq!(blocked.error.unwrap().code, error.code);
            f.private.2.store(false, Ordering::SeqCst);
            f.view.retirement_failure.store(false, Ordering::SeqCst);
            assert!(action(&restored, ClientAction::SignOut { intent_id: None }).await.ok);
            assert!(restored.state().enrollment.error_code.is_none());
            assert!(action(&restored, ClientAction::GetState).await.ok);
        }
        assert!(f.private.0.lock().unwrap().is_empty());
        assert!(f.store.load("unused").unwrap().account_id.is_none());
    }
}

// TOTP recovery codes are one-time account material. Even with a reachable local Runtime,
// finishing authentication is not permission to prepare, allocate a hostname or start a claim.
#[tokio::test]
async fn totp_completion_returns_codes_without_registering_this_computer() {
    let mut replies = first_factor();
    replies.extend([
        ("/api/mobile/session", Reply::Ready(response(403,json!({"error":"two_factor_required","email":"xiaoming@example.test","gate":"enroll","methods":["totp"]}),None))),
        ("/api/two-factor/totp/setup",ok(json!({"token":"private-token","uri":"otpauth://fixture","qrSvg":"<svg/>","secret":"private-seed"}))),
        ("/api/two-factor/totp",ok(json!({"method":{"id":"totp-1","kind":"totp","name":"Authenticator","createdAt":"2026-11-13","lastUsedAt":null},"recoveryCodes":["one-time-code"]}))),
        ("/api/mobile/session",ok(signed_in())),
    ]);
    let f = fixture(replies);
    f.client.local_origin(Some(
        avibe_runtime_host::LoopbackOrigin::parse("http://127.0.0.1:1984").unwrap(),
    ));
    assert!(
        action(
            &f.client,
            ClientAction::RequestOtp {
                email: "xiaoming@example.test".into()
            }
        )
        .await
        .ok
    );
    assert!(
        action(&f.client, ClientAction::VerifyOtp { code: "123456".into() })
            .await
            .ok
    );
    assert!(action(&f.client, ClientAction::BeginTotpEnrollment).await.ok);
    let reply = action(&f.client, ClientAction::FinishTotpEnrollment { code: "123456".into() }).await;
    assert!(reply.ok, "{:?}", reply.error);
    match reply.result {
        Some(DirectResult::TotpEnrolled { recovery_codes, .. }) => {
            assert_eq!(recovery_codes, Some(vec!["one-time-code".into()]))
        }
        other => panic!("missing direct recovery result: {other:?}"),
    }
    assert!(!serde_json::to_string(&*f.view.states.lock().unwrap())
        .unwrap()
        .contains("one-time-code"));
    assert!(f.http.replies.lock().unwrap().is_empty());
    assert_eq!(f.client.state().account.status, AccountStatus::SignedIn);
    let count = f.http.calls.lock().unwrap().len();
    assert!(!action(&f.client, ClientAction::RetryEnrollment).await.ok);
    let calls = f.http.calls.lock().unwrap();
    assert_eq!(calls.len(), count);
    assert!(!calls.iter().any(|path| path.contains("local-instance")
        || path.contains("ensure-local")
        || path.contains("/claims")
        || path.contains("/pair")));
}

fn catalog(account: &str, ids: &[&str]) -> Value {
    let instances: Vec<_> = ids
        .iter()
        .map(|id| {
            json!({
                "id":id,"name":id,"host":format!("{id}.avibe.bot"),"url":format!("https://{id}.avibe.bot"),
                "relationship":"owner","owner_email":null,"available":true
            })
        })
        .collect();
    json!({"account":{"id":account,"email":format!("{account}@example.test")},"instances":instances})
}
fn order(client: &Coordinator) -> Vec<String> {
    client
        .state()
        .catalog
        .instances
        .into_iter()
        .map(|instance| instance.id)
        .collect()
}
async fn sign_in(f: &Fixture, session: Value) {
    let email = session["account"]["email"].as_str().unwrap().to_owned();
    f.http.replies.lock().unwrap().extend(first_factor());
    f.http.replies.lock().unwrap().push_back((
        "/api/mobile/session",
        Reply::Ready(response(
            200,
            session,
            Some("avibe_session=private-account; Secure; HttpOnly; Path=/"),
        )),
    ));
    let email = ClientAction::RequestOtp { email };
    assert!(action(&f.client, email).await.ok);
    assert!(
        action(&f.client, ClientAction::VerifyOtp { code: "123456".into() })
            .await
            .ok
    );
}
/// Replies for one remote selection up to its readiness read, which is last.
fn selection(session: &Value, id: &str, ready: Reply) -> Vec<(&'static str, Reply)> {
    selection_at(session, id, ready)
}
/// The same, for an instance at `https://{host}.avibe.bot`.
fn selection_at(session: &Value, host: &str, ready: Reply) -> Vec<(&'static str, Reply)> {
    let id = host;
    let mut redirect = response(302, json!({}), None);
    redirect.headers.insert(
        LOCATION,
        format!(
            "https://avibe.bot/oauth/authorize?state=owned&redirect_uri=https%3A%2F%2F{id}.avibe.bot%2Fauth%2Fcallback"
        )
        .parse()
        .unwrap(),
    );
    vec![
        ("/api/mobile/session", ok(session.clone())),
        ("/auth/login", Reply::Ready(redirect)),
        (
            "/api/desktop/instance-authorization",
            ok(json!({
                "status":"authorized",
                "redirect_url":format!("https://{id}.avibe.bot/auth/callback?code=fixture-code&state=owned")
            })),
        ),
        (
            "/auth/callback",
            Reply::Ready(response(
                302,
                json!({}),
                Some("__Host-vibe_remote_session=instance-only; Secure; HttpOnly; Path=/"),
            )),
        ),
        ("/api/session", ready),
    ]
}
fn ready(account: &str, id: &str) -> Response {
    response(
        200,
        json!({
            "remote":true,"authenticated":true,"authorization_state":"current",
            "sub":account,"instance_id":id,"capabilities":{"can_read_instance":true}
        }),
        None,
    )
}
async fn select(f: &Fixture, id: &str) -> ClientReply {
    action(&f.client, ClientAction::SelectInstance { instance_id: id.into() }).await
}

async fn selected_for_update() -> Fixture {
    let f = fixture(vec![]);
    let session = catalog("account-a", &["one"]);
    sign_in(&f, session.clone()).await;
    let mut ready = ready("account-a", "one");
    ready.headers.append(
        SET_COOKIE,
        "vibe_csrf_token=csrf-from-instance; Secure; Path=/".parse().unwrap(),
    );
    f.http
        .replies
        .lock()
        .unwrap()
        .extend(selection(&session, "one", Reply::Ready(ready)));
    assert!(select(&f, "one").await.ok);
    f
}
fn identity(can_manage: bool) -> Value {
    let mut identity: Value = serde_json::from_slice(&ready("account-a", "one").body).unwrap();
    identity["capabilities"]["can_manage_instance"] = json!(can_manage);
    identity
}
/// The two reads of one refresh; the instance rotates its session on the second.
fn update_reads(f: &Fixture, can_manage: bool, snapshot: Value) {
    f.http.replies.lock().unwrap().extend([
        ("/api/session", ok(identity(can_manage))),
        (
            "/api/version",
            Reply::Ready(response(
                200,
                snapshot,
                Some("__Host-vibe_remote_session=rotated-by-instance; HttpOnly; Secure; Path=/"),
            )),
        ),
    ]);
}
fn offer() -> Value {
    json!({
        "current": "3.2.7rc1", "latest": "3.2.8-rc.4", "has_update": true,
        "error": null, "package_spec": "https://mirror.example/private.whl",
        "prereleases": true, "auto_update": false, "build": {"kind": "package"},
        "host_actions": ["check", "install", "prereleases", "auto_update", "unlisted"],
        "install": null,
    })
}
/// Follow-up to B1: the published `following` is exactly what a second action
/// for the instance gets. Refused (and nothing sent) when true; sent when
/// false. A false check sends, so call it where the scenario may move on.
async fn refusal_matches_following(f: &Fixture, expected: bool) {
    let request = update(f)["request"].clone();
    let following = request["following"].as_bool().unwrap_or(false);
    assert_eq!(following, expected, "{request}");
    let calls = f.http.calls.lock().unwrap().len();
    if following {
        let code = action(&f.client, ClientAction::InstanceUpdateSetPrereleases { enabled: true })
            .await
            .error
            .unwrap()
            .code;
        // `busy` while the request is still on its way, `update_in_progress` after.
        assert!(matches!(code.as_str(), "busy" | "update_in_progress"), "{code}");
        assert_eq!(f.http.calls.lock().unwrap().len(), calls);
    } else {
        f.http
            .replies
            .lock()
            .unwrap()
            .push_back(("/api/update/prereleases", ok(json!({"ok": true}))));
        let reply = action(&f.client, ClientAction::InstanceUpdateSetPrereleases { enabled: true }).await;
        assert!(reply.ok, "{:?}", reply.error);
        assert_eq!(f.http.calls.lock().unwrap()[calls..], ["/api/update/prereleases"]);
        assert_eq!(update(f)["request"]["following"], false);
    }
}
async fn refresh(f: &Fixture) -> ClientReply {
    action(&f.client, ClientAction::InstanceUpdateRefresh {}).await
}
fn update(f: &Fixture) -> Value {
    serde_json::to_value(f.client.state().instance_update).unwrap()
}

// B1: the shell acts on the selected instance with the sign-in it made for
// it, through the instance's own endpoints. A desktop-managed host gets the
// exact target; a standalone Runtime gets its released `/api/upgrade`. The
// instance jar is never written with the account credential.
#[tokio::test]
async fn instance_update_uses_the_instance_sign_in_and_never_persists_it() {
    let f = selected_for_update().await;
    assert_eq!(update(&f)["status"], "loading");
    let install = |target: &str| ClientAction::InstanceUpdateInstall {
        target_version: target.into(),
    };
    // A viewer sees the section and cannot act; nothing is sent.
    update_reads(&f, false, offer());
    assert!(refresh(&f).await.ok);
    assert_eq!(update(&f)["can_manage"], false);
    assert_eq!(
        action(&f.client, install("3.2.8-rc.4")).await.error.unwrap().code,
        "instance_unauthorized"
    );
    for managed in [false, true] {
        let mut data = offer();
        if managed {
            // The standalone install above has ended: the instance restarted
            // on another version (now a desktop-managed host).
            data["managed_by"] = json!("desktop");
            data["current"] = json!("3.2.7rc2");
        }
        update_reads(&f, true, data);
        let reply = refresh(&f).await;
        assert!(reply.ok, "{:?}", reply.error);
        let state = update(&f);
        assert_eq!(state["status"], "ready");
        assert_eq!(state["can_manage"], true);
        assert_eq!(
            state["snapshot"]["host_actions"],
            json!(["check", "prereleases", "auto_update", "install"])
        );
        assert!(state["snapshot"].get("package_spec").is_none());
        assert!(reply.state.runtime_projection().get("instance_update").is_none());
        // Only the exact version on screen.
        let calls = f.http.calls.lock().unwrap().len();
        assert_eq!(
            action(&f.client, install("3.2.8")).await.error.unwrap().code,
            "target_mismatch"
        );
        assert_eq!(f.http.calls.lock().unwrap().len(), calls);
        let path = if managed {
            "/api/desktop/update/request"
        } else {
            "/api/upgrade"
        };
        for (action_, path) in [
            (ClientAction::InstanceUpdateCheck {}, "/api/update/check"),
            (
                ClientAction::InstanceUpdateSetPrereleases { enabled: true },
                "/api/update/prereleases",
            ),
            (
                ClientAction::InstanceUpdateSetAutoUpdate { enabled: true },
                "/api/update/auto-update",
            ),
            (install("3.2.8-rc.4"), path),
        ] {
            f.http
                .replies
                .lock()
                .unwrap()
                .push_back((path, ok(json!({"ok": true, "output": "installer log"}))));
            let reply = action(&f.client, action_).await;
            assert!(reply.ok, "{path}: {:?}", reply.error);
            let receipt = &update(&f)["request"];
            assert_eq!(receipt["phase"], "accepted");
            assert!(!receipt.to_string().contains("installer log"));
            // Only an install is followed, and only it says what it started from.
            let install = receipt["action"] == "install";
            assert_eq!(receipt["following"], install);
            let from = if managed { "3.2.7rc2" } else { "3.2.7rc1" };
            assert_eq!(
                receipt["installed_from"],
                if install { json!(from) } else { Value::Null }
            );
        }
        assert_eq!(update(&f)["request"]["target"], "3.2.8-rc.4");
        // A second install of the same target while the first runs is refused.
        assert_eq!(
            action(&f.client, install("3.2.8-rc.4")).await.error.unwrap().code,
            "update_in_progress"
        );
    }
    let saved = String::from_utf8(f.private.read("account-a").unwrap().unwrap()).unwrap();
    for instance_only in ["instance-only", "rotated-by-instance", "csrf-from-instance"] {
        assert!(!saved.contains(instance_only));
    }
    assert!(!std::fs::read_to_string(f._root.path().join("connection.json"))
        .unwrap()
        .contains("rotated-by-instance"));
    // The local updater was never involved.
    assert!(f.view.update_calls.lock().unwrap().is_empty());
}

// A silent release (docs/release.md): a standalone instance's unprompted
// version reads leave it out, the user's own check finds it, later reads keep
// that offer while the instance runs the same version, and the install names it.
#[tokio::test]
async fn instance_update_keeps_the_offer_the_users_check_found() {
    let f = selected_for_update().await;
    let mut announced = offer();
    announced["latest"] = json!("3.2.7rc1");
    announced["has_update"] = json!(false);
    update_reads(&f, true, announced.clone());
    assert!(refresh(&f).await.ok);
    assert_eq!(update(&f)["snapshot"]["has_update"], false);

    f.http
        .replies
        .lock()
        .unwrap()
        .push_back(("/api/update/check", ok(json!({"ok": true, "snapshot": offer()}))));
    let reply = action(&f.client, ClientAction::InstanceUpdateCheck {}).await;
    assert!(reply.ok, "{:?}", reply.error);
    update_reads(&f, true, announced.clone());
    assert!(refresh(&f).await.ok);
    let snapshot = update(&f)["snapshot"].clone();
    assert_eq!(
        (&snapshot["latest"], &snapshot["has_update"]),
        (&json!("3.2.8-rc.4"), &json!(true))
    );

    f.http
        .replies
        .lock()
        .unwrap()
        .push_back(("/api/upgrade", ok(json!({"ok": true}))));
    let install = ClientAction::InstanceUpdateInstall {
        target_version: "3.2.8-rc.4".into(),
    };
    let reply = action(&f.client, install).await;
    assert!(reply.ok, "{:?}", reply.error);

    // Running another version, the instance's own reads decide again.
    let mut moved = announced;
    moved["current"] = json!("3.2.8rc4");
    moved["latest"] = json!("3.2.8rc4");
    update_reads(&f, true, moved);
    assert!(refresh(&f).await.ok);
    assert_eq!(update(&f)["snapshot"]["has_update"], false);
}

// B1 display data: a known field that does not validate makes the answer
// unreachable; upstream text is replaced by a code; unknown fields vanish.
#[tokio::test]
async fn instance_update_snapshot_is_display_data_only() {
    let f = selected_for_update().await;
    let mut data = offer();
    data["error"] = json!("PyPI said <script>https://evil.example/?token=1</script>");
    data["remote_request"] = json!({
        "request_id": "r-1", "target": "3.2.8-rc.4", "phase": "failed", "progress": null,
        "error_code": "Traceback: /Users/alex/.avibe secret", "updated_at": "2026-11-20T07:03:17Z",
        "debug_url": "https://evil.example/",
    });
    data["install"] =
        json!({"phase": "installing", "target": "3.2.8-rc.4", "started_at": "2026-11-20T07:03:17Z", "pid": 4});
    data["control_request"] = Value::Null;
    data["phase"] = json!("available");
    data["unknown_future_field"] = json!({"cookie": "x"});
    data["build"] = json!({"kind": "package", "revision": "1a2b3c4d", "dirty": true});
    update_reads(&f, true, data);
    assert!(refresh(&f).await.ok);
    let snapshot = update(&f)["snapshot"].clone();
    assert_eq!(snapshot["error"], "error");
    assert_eq!(snapshot["build"], json!({"kind": "package"}));
    assert_eq!(snapshot["remote_request"]["error_code"], "request_failed");
    assert!(snapshot["remote_request"].get("debug_url").is_none());
    assert_eq!(
        snapshot["install"],
        json!({"phase": "installing", "target": "3.2.8-rc.4", "started_at": "2026-11-20T07:03:17Z"})
    );
    assert!(snapshot.get("unknown_future_field").is_none());
    let text = snapshot.to_string();
    for leaked in ["evil.example", "Traceback", "token", "cookie"] {
        assert!(!text.contains(leaked), "{leaked}");
    }
    // An install the instance reports as running cannot be started again.
    let reply = action(
        &f.client,
        ClientAction::InstanceUpdateInstall {
            target_version: "3.2.8-rc.4".into(),
        },
    )
    .await;
    assert_eq!(reply.error.unwrap().code, "update_in_progress");
    for (key, bad) in [
        ("current", json!("v3.2.8")),
        ("latest", json!("<b>3.2.9</b>")),
        ("has_update", json!("yes")),
        ("phase", json!("hacked")),
        ("checked_at", json!("yesterday")),
        ("managed_by", json!("someone")),
        (
            "install",
            json!({"phase": "installing", "target": null, "started_at": "now"}),
        ),
        (
            "remote_request",
            json!({"request_id": "r", "target": "3.2.8", "phase": "exploded", "progress": null, "error_code": null, "updated_at": "2026-11-20T07:03:17Z"}),
        ),
        ("host_actions", json!("install")),
    ] {
        let mut data = offer();
        data[key] = bad;
        update_reads(&f, true, data);
        assert!(refresh(&f).await.ok, "{key}");
        let state = update(&f);
        assert_eq!(state["status"], "unreachable", "{key}");
        // Display keeps the last valid answer across a restart; it is never
        // replaced by the invalid one.
        assert_eq!(state["snapshot"]["current"], "3.2.7rc1", "{key}");
    }
    // An account the instance no longer accepts shows nothing.
    f.http.replies.lock().unwrap().push_back((
        "/api/session",
        Reply::Ready(response(401, json!({"error": "auth_required"}), None)),
    ));
    assert!(refresh(&f).await.ok);
    let state = update(&f);
    assert_eq!(
        (
            state["status"].clone(),
            state["snapshot"].clone(),
            state["can_manage"].clone()
        ),
        (json!("unauthorized"), Value::Null, json!(false))
    );
}

// B1 fencing: a standalone install's request stays open for the whole install
// while the shell keeps reading. A reply that arrives after the selection
// changed publishes nothing, even after the same instance is opened again.
#[tokio::test]
async fn a_held_install_allows_reads_and_its_late_reply_changes_nothing() {
    let f = selected_for_update().await;
    update_reads(&f, true, offer());
    assert!(refresh(&f).await.ok);
    let (release, held) = oneshot::channel();
    let sent = Arc::new(Notify::new());
    f.http
        .replies
        .lock()
        .unwrap()
        .push_back(("/api/upgrade", Reply::Held(held, sent.clone())));
    let client = f.client.clone();
    let install = tokio::spawn(async move {
        action(
            &client,
            ClientAction::InstanceUpdateInstall {
                target_version: "3.2.8-rc.4".into(),
            },
        )
        .await
    });
    sent.notified().await;
    assert_eq!(update(&f)["request"]["phase"], "sending");
    assert_eq!(update(&f)["request"]["following"], true);
    // Reads go on while the install request is open; a second send is busy.
    let mut installing = offer();
    installing["install"] =
        json!({"phase": "installing", "target": "3.2.8-rc.4", "started_at": "2026-11-20T07:03:17Z"});
    update_reads(&f, true, installing);
    assert!(refresh(&f).await.ok);
    assert_eq!(update(&f)["snapshot"]["install"]["phase"], "installing");
    assert_eq!(update(&f)["request"]["phase"], "sending");
    assert_eq!(
        action(&f.client, ClientAction::InstanceUpdateCheck {})
            .await
            .error
            .unwrap()
            .code,
        "busy"
    );
    // The user leaves and opens the same instance again: its sign-in goes on
    // (`kept.rs`), in a new section. The old reply then arrives.
    let old_generation = f.client.state().generation;
    assert!(action(&f.client, ClientAction::ReturnLocal).await.ok);
    assert!(f.client.state().instance_update.is_none());
    f.http.replies.lock().unwrap().extend([
        ("/api/mobile/session", ok(catalog("account-a", &["one"]))),
        ("/api/session", ok(identity(true))),
    ]);
    assert!(select(&f, "one").await.ok);
    let fresh = update(&f);
    let revision = f.client.state().revision;
    // The reply arrives late, or the shell already dropped the request when its
    // generation ended (on a slow runner the reselection outlasts that check).
    // Either way it ends stale and changes nothing.
    let _ = release.send(response(200, json!({"ok": true}), None));
    assert_eq!(install.await.unwrap().error.unwrap().code, "stale_generation");
    assert_eq!(update(&f), fresh);
    assert_eq!(f.client.state().revision, revision);
    // Nothing is sent for a generation that is no longer the committed one.
    let calls = f.http.calls.lock().unwrap().len();
    let late = f
        .client
        .request(ClientRequest {
            version: 1,
            request_id: "late".into(),
            expected_generation: old_generation,
            action: ClientAction::InstanceUpdateCheck {},
        })
        .await;
    assert_eq!(late.error.unwrap().code, "stale_generation");
    assert_eq!(f.http.calls.lock().unwrap().len(), calls);
}

// After a generation change that keeps the instance selected (Back to the
// Workbench), the old receipt is marked interrupted from a later read: never
// resent, and no install result is inferred; `current` says what is installed.
#[tokio::test]
async fn an_interrupted_receipt_settles_without_resending() {
    let f = selected_for_update().await;
    update_reads(&f, true, offer());
    assert!(refresh(&f).await.ok);
    let (release, held) = oneshot::channel();
    let sent = Arc::new(Notify::new());
    f.http
        .replies
        .lock()
        .unwrap()
        .push_back(("/api/upgrade", Reply::Held(held, sent.clone())));
    let client = f.client.clone();
    let install = tokio::spawn(async move {
        action(
            &client,
            ClientAction::InstanceUpdateInstall {
                target_version: "3.2.8-rc.4".into(),
            },
        )
        .await
    });
    sent.notified().await;
    // Still in flight: a read leaves it sending.
    update_reads(&f, true, offer());
    f.client.poll_instance_update(false).await;
    assert_eq!(update(&f)["request"]["phase"], "sending");
    assert!(action(&f.client, ClientAction::Cancel).await.ok);
    // The shell drops the request when its generation ends, or its reply
    // arrives late; it ends stale either way.
    let _ = release.send(response(200, json!({"ok": true}), None));
    assert_eq!(install.await.unwrap().error.unwrap().code, "stale_generation");
    let mut done = offer();
    done["current"] = json!("3.2.9");
    done["latest"] = json!("3.2.9");
    done["has_update"] = json!(false);
    update_reads(&f, true, done);
    f.client.poll_instance_update(false).await;
    let state = update(&f);
    assert_eq!(
        (
            state["request"]["phase"].clone(),
            state["request"]["error_code"].clone()
        ),
        (json!("failed"), json!("interrupted"))
    );
    assert_eq!(state["snapshot"]["current"], "3.2.9");
    // Settled: the shell stops rereading on its own, and sent one install.
    let calls = f.http.calls.lock().unwrap().clone();
    f.client.poll_instance_update(false).await;
    assert_eq!(*f.http.calls.lock().unwrap(), calls);
    assert_eq!(calls.iter().filter(|path| *path == "/api/upgrade").count(), 1);
    refusal_matches_following(&f, false).await;
}

// Codex P2: a standalone Runtime answers before its scheduled restart, still
// reporting the old version. The shell keeps following until another version
// is reported, then stops on its own.
#[tokio::test]
async fn an_accepted_install_is_followed_past_the_pre_restart_read() {
    let f = selected_for_update().await;
    update_reads(&f, true, offer());
    assert!(refresh(&f).await.ok);
    f.http
        .replies
        .lock()
        .unwrap()
        .push_back(("/api/upgrade", ok(json!({"ok": true, "restarting": true}))));
    let reply = action(
        &f.client,
        ClientAction::InstanceUpdateInstall {
            target_version: "3.2.8-rc.4".into(),
        },
    )
    .await;
    assert!(reply.ok, "{:?}", reply.error);
    // Before the restart: same version, no install reported.
    update_reads(&f, true, offer());
    f.client.poll_instance_update(false).await;
    refusal_matches_following(&f, true).await;
    // Nothing else may replace the receipt that follows the install.
    let calls = f.http.calls.lock().unwrap().len();
    for other in [
        ClientAction::InstanceUpdateCheck {},
        ClientAction::InstanceUpdateSetAutoUpdate { enabled: true },
    ] {
        assert_eq!(action(&f.client, other).await.error.unwrap().code, "update_in_progress");
    }
    assert_eq!(f.http.calls.lock().unwrap().len(), calls);
    assert_eq!(update(&f)["request"]["action"], "install");
    // Restarting: the instance does not answer.
    f.http
        .replies
        .lock()
        .unwrap()
        .push_back(("/api/session", Reply::Failure));
    f.client.poll_instance_update(false).await;
    assert_eq!(update(&f)["status"], "unreachable");
    // Still followed while it restarts (a click is refused first because the
    // instance is not answering).
    assert_eq!(update(&f)["request"]["following"], true);
    let mut done = offer();
    done["current"] = json!("3.2.8-rc.4");
    done["has_update"] = json!(false);
    update_reads(&f, true, done);
    f.client.poll_instance_update(false).await;
    assert_eq!(update(&f)["snapshot"]["current"], "3.2.8-rc.4");
    let calls = f.http.calls.lock().unwrap().len();
    f.client.poll_instance_update(false).await;
    assert_eq!(f.http.calls.lock().unwrap().len(), calls);
    refusal_matches_following(&f, false).await;
}

// Codex P2: an install request whose generation ends is dropped promptly, so
// the selected session is not left `busy` until the transport gives up.
#[tokio::test]
async fn an_install_request_is_dropped_when_its_generation_ends() {
    let f = selected_for_update().await;
    update_reads(&f, true, offer());
    assert!(refresh(&f).await.ok);
    let (_never, held) = oneshot::channel::<Response>();
    let sent = Arc::new(Notify::new());
    f.http
        .replies
        .lock()
        .unwrap()
        .push_back(("/api/upgrade", Reply::Held(held, sent.clone())));
    let client = f.client.clone();
    let install = tokio::spawn(async move {
        action(
            &client,
            ClientAction::InstanceUpdateInstall {
                target_version: "3.2.8-rc.4".into(),
            },
        )
        .await
    });
    sent.notified().await;
    assert!(action(&f.client, ClientAction::Cancel).await.ok);
    let ended = tokio::time::timeout(std::time::Duration::from_secs(5), install).await;
    assert_eq!(ended.unwrap().unwrap().error.unwrap().code, "stale_generation");
    // The session is free again: a check under the new generation is sent.
    update_reads(&f, true, offer());
    assert!(refresh(&f).await.ok);
    assert_eq!(update(&f)["request"]["following"], false);
    f.http
        .replies
        .lock()
        .unwrap()
        .push_back(("/api/update/check", ok(json!({"ok": true}))));
    let reply = action(&f.client, ClientAction::InstanceUpdateCheck {}).await;
    assert!(reply.ok, "{:?}", reply.error);
}

// An instance that answers but refuses the app's sign-in ends `unauthorized`:
// no timer retries, no new sign-in; opening the popup reads it once more.
#[tokio::test]
async fn an_unauthorized_instance_is_not_retried_on_a_timer() {
    let f = selected_for_update().await;
    f.http.replies.lock().unwrap().extend([
        ("/api/session", ok(identity(true))),
        (
            "/api/version",
            Reply::Ready(response(403, json!({"error": "instance_access_forbidden"}), None)),
        ),
    ]);
    assert!(refresh(&f).await.ok);
    assert_eq!(update(&f)["status"], "unauthorized");
    let calls = f.http.calls.lock().unwrap().len();
    f.client.poll_instance_update(true).await;
    assert_eq!(f.http.calls.lock().unwrap().len(), calls);
    f.http
        .replies
        .lock()
        .unwrap()
        .push_back(("/api/session", Reply::Ready(response(401, json!({}), None))));
    f.client.refresh_instance_update().await;
    assert_eq!(f.http.calls.lock().unwrap()[calls..], ["/api/session"]);
    assert_eq!(update(&f)["status"], "unauthorized");
    assert_eq!(f.view.cookie_reads.load(Ordering::SeqCst), 2);
    // No account or instance sign-in was started.
    let after = f.http.calls.lock().unwrap()[calls..].to_vec();
    assert!(!after
        .iter()
        .any(|path| path.starts_with("/auth/") || path.contains("authorization")));
    assert_eq!(f.view.committed.lock().unwrap().len(), 1);
}

// The instance page renews or rotates its sign-in in the WebView; the shell's
// jar recovers from that exact store once, rechecks identity, and only reads.
#[tokio::test]
async fn an_expired_instance_jar_recovers_from_the_webview_store() {
    let f = selected_for_update().await;
    *f.view.instance_cookies.lock().unwrap() = Some(vec![
        cookie::Cookie::parse(
            "__Host-vibe_remote_session=from-webview; Secure; HttpOnly; Path=/; Domain=one.avibe.bot",
        )
        .unwrap()
        .into_owned(),
        cookie::Cookie::parse("unrelated=1; Path=/").unwrap().into_owned(),
    ]);
    f.http.replies.lock().unwrap().extend([
        ("/api/session", Reply::Ready(response(401, json!({}), None))),
        ("/api/session", ok(identity(true))),
        ("/api/version", ok(offer())),
    ]);
    assert!(refresh(&f).await.ok);
    assert_eq!(update(&f)["status"], "ready");
    let headers = f.http.cookies.lock().unwrap().clone();
    assert!(headers[headers.len() - 2].contains("__Host-vibe_remote_session=from-webview"));
    assert!(!headers.iter().any(|cookie| cookie.contains("unrelated")));
    assert_eq!(f.view.cookie_reads.load(Ordering::SeqCst), 1);
}

// Desktop lists the current remote, then instances this account opened as the
// selected runtime on this device, newest first, then avibe.bot's order. Only
// a published selection counts: a failed or fenced one leaves order and file.
#[tokio::test]
async fn instance_order_follows_committed_opens_per_account_on_this_device() {
    let session = catalog("account-a", &["one", "two", "three"]);
    let f = fixture(vec![]);
    sign_in(&f, session.clone()).await;
    assert_eq!(order(&f.client), ["one", "two", "three"]);
    for (id, expected) in [("three", ["three", "one", "two"]), ("two", ["two", "three", "one"])] {
        f.http
            .replies
            .lock()
            .unwrap()
            .extend(selection(&session, id, Reply::Ready(ready("account-a", id))));
        let reply = select(&f, id).await;
        assert!(reply.ok, "{:?}", reply.error);
        assert_eq!(order(&f.client), expected);
    }
    let recency = f._root.path().join("instance-recency.json");
    let saved = std::fs::read(&recency).unwrap();

    f.http
        .replies
        .lock()
        .unwrap()
        .extend(selection(&session, "one", Reply::Ready(ready("account-a", "other"))));
    assert_eq!(
        select(&f, "one").await.error.unwrap().code,
        "instance_identity_mismatch"
    );
    let (finish, held) = oneshot::channel();
    let entered = Arc::new(Notify::new());
    f.http
        .replies
        .lock()
        .unwrap()
        .extend(selection(&session, "one", Reply::Held(held, entered.clone())));
    let client = f.client.clone();
    let fenced = tokio::spawn(async move {
        action(
            &client,
            ClientAction::SelectInstance {
                instance_id: "one".into(),
            },
        )
        .await
    });
    entered.notified().await;
    assert!(action(&f.client, ClientAction::Cancel).await.ok);
    assert!(finish.send(ready("account-a", "one")).is_ok());
    assert_eq!(fenced.await.unwrap().error.unwrap().code, "stale_generation");
    assert_eq!(order(&f.client), ["two", "three", "one"]);
    assert_eq!(std::fs::read(&recency).unwrap(), saved);

    // The remainder follows avibe.bot's latest order once the local runtime is current.
    assert!(action(&f.client, ClientAction::ReturnLocal).await.ok);
    f.http.replies.lock().unwrap().push_back((
        "/api/mobile/session",
        ok(catalog("account-a", &["four", "one", "three", "two"])),
    ));
    assert!(action(&f.client, ClientAction::RefreshInstances).await.ok);
    assert_eq!(order(&f.client), ["two", "three", "four", "one"]);
    assert!(f.http.replies.lock().unwrap().is_empty());

    // Relaunch on the same device: another account keeps server order, and
    // this account keeps its own.
    let Fixture { _root: root, .. } = f;
    let other = fixture_in(root, vec![]);
    sign_in(&other, catalog("account-b", &["one", "two", "three"])).await;
    assert_eq!(order(&other.client), ["one", "two", "three"]);
    let Fixture { _root: root, .. } = other;
    let again = fixture_in(root, vec![]);
    sign_in(&again, session).await;
    assert_eq!(order(&again.client), ["two", "three", "one"]);
}

// Recency is bounded. An instance that falls out of it returns to avibe.bot's
// order, even when the open (here, after consent) reuses an already ordered
// catalog instead of a fresh one.
#[tokio::test]
async fn an_instance_dropped_from_recency_returns_to_server_order() {
    let root = tempfile::tempdir().unwrap();
    let mut opened: Vec<String> = (1..64).map(|n| format!("gone-{n}")).collect();
    opened.push("three".into());
    std::fs::write(
        root.path().join("instance-recency.json"),
        serde_json::to_vec(&json!({"schema_version":1,"accounts":{"account-a":opened}})).unwrap(),
    )
    .unwrap();
    let session = catalog("account-a", &["one", "two", "three"]);
    let f = fixture_in(root, vec![]);
    sign_in(&f, session.clone()).await;
    assert_eq!(order(&f.client), ["three", "one", "two"]);
    // Inspect asks for consent first; approval then sends the authorized reply.
    let mut replies = selection(&session, "two", Reply::Ready(ready("account-a", "two")));
    replies.insert(
        2,
        (
            "/api/desktop/instance-authorization",
            ok(json!({
                "status":"consent_required","scope":"openid",
                "account":{"id":"account-a"},"instance":{"id":"two","host":"two.avibe.bot"}
            })),
        ),
    );
    f.http.replies.lock().unwrap().extend(replies);
    assert!(select(&f, "two").await.ok);
    assert!(f.client.state().consent.is_some());
    assert_eq!(order(&f.client), ["three", "one", "two"]);
    let reply = action(&f.client, ClientAction::DecideConsent { approve: true }).await;
    assert!(reply.ok, "{:?}", reply.error);
    assert_eq!(order(&f.client), ["two", "one", "three"]);
    assert!(f.http.replies.lock().unwrap().is_empty());
}

// A rejected replacement first factor changes nothing (#4034, main 2026-11-20: a first factor
// commits only on success). The email code it would have replaced, or the second factor that
// code owes, stays exactly as it was and still works; nothing of the rejection is kept. (Before,
// the rejection reset to signed out, so that no unusable old projection was left; now the old
// one is never made unusable.)
#[tokio::test]
async fn rejected_password_keeps_the_previous_first_factor_usable() {
    for factor in [false, true] {
        let mut replies = first_factor();
        replies.truncate(2);
        if factor {
            replies.extend([
                code_start(), code_bound(),
                ("/api/mobile/session", Reply::Ready(response(403,json!({"error":"two_factor_required","email":"xiaoming@example.test","gate":"challenge","methods":["totp"]}),Some("old_factor=private; Path=/; Secure; HttpOnly")))),
            ]);
        }
        replies.extend([
            ("/api/config/auth", ok(json!({"supabase_url":"https://provider.test","supabase_anon_key":"public"}))),
            ("/auth/v1/token", Reply::Ready(response(400,json!({"error":"invalid_grant","error_code":"invalid_credentials","error_description":"synthetic-private-detail"}),None))),
        ]);
        // The previous attempt, used after the rejection.
        if factor {
            replies.extend([
                ("/api/two-factor/verify", ok(json!({}))),
                ("/api/mobile/session", ok(signed_in())),
            ]);
        } else {
            replies.extend([
                code_start(),
                code_bound(),
                (
                    "/api/mobile/session",
                    Reply::Ready(response(
                        200,
                        signed_in(),
                        Some("avibe_session=private-account; Secure; HttpOnly; Path=/"),
                    )),
                ),
            ]);
        }
        let f = fixture(replies);
        assert!(
            action(
                &f.client,
                ClientAction::RequestOtp {
                    email: "xiaoming@example.test".into()
                }
            )
            .await
            .ok
        );
        if factor {
            assert!(
                action(&f.client, ClientAction::VerifyOtp { code: "123456".into() })
                    .await
                    .ok
            );
        }
        let before = f.client.state();
        let stored = f.private.0.lock().unwrap().clone();
        let reply = action(
            &f.client,
            serde_json::from_value(
                json!({"type":"sign_in_password","email":"new@example.test","password":"synthetic-password"}),
            )
            .unwrap(),
        )
        .await;
        assert_eq!(reply.error.unwrap().code, "invalid_credentials");
        assert_eq!(
            serde_json::to_value(&reply.state.account).unwrap(),
            serde_json::to_value(&before.account).unwrap()
        );
        assert_eq!(
            serde_json::to_value(&reply.state.catalog).unwrap(),
            serde_json::to_value(&before.catalog).unwrap()
        );
        assert_eq!(*f.private.0.lock().unwrap(), stored);
        assert!(!serde_json::to_string(&f.client.state())
            .unwrap()
            .contains("synthetic-private"));
        // Still usable: the same code, or the second factor it owes, signs in.
        let next = if factor {
            serde_json::from_value(json!({"type":"verify_second_factor","method":"totp","code":"123456"})).unwrap()
        } else {
            ClientAction::VerifyOtp { code: "123456".into() }
        };
        let done = action(&f.client, next).await;
        assert!(done.ok, "{:?}", done.error);
        assert_eq!(f.client.state().account.status, AccountStatus::SignedIn);
    }
}

fn auth_config() -> Reply {
    ok(json!({"supabase_url":"https://fixture.supabase.test","supabase_anon_key":"public-fixture"}))
}
fn account_session() -> Reply {
    Reply::Ready(response(
        200,
        signed_in(),
        Some("avibe_session=private-account; Secure; HttpOnly; Path=/"),
    ))
}
fn email(address: &str) -> ClientAction {
    ClientAction::RequestOtp { email: address.into() }
}

/// AUTH-SETUP-182: a resend avibe.bot refuses (rate limited, or no answer at all) leaves the code
/// already sent, its attempt and the published state as they were; that code still signs in, and
/// no broadcast ever shows signed out (#4034, main 2026-11-20: a first factor commits only on
/// success).
#[tokio::test]
async fn a_refused_resend_keeps_the_code_already_sent() {
    for answered in [true, false] {
        let refusal = if answered {
            Reply::Ready(response(429, json!({"error_code":"over_email_send_rate_limit"}), None))
        } else {
            Reply::Failure
        };
        let f = fixture(vec![
            ("/api/config/auth", auth_config()),
            ("/auth/v1/otp", ok(json!({}))),
            ("/api/config/auth", auth_config()),
            ("/auth/v1/otp", refusal),
            code_start(),
            code_bound(),
            ("/api/mobile/session", account_session()),
        ]);
        assert!(action(&f.client, email("xiaoming@example.test")).await.ok);
        let before = f.client.state();
        let seen = f.view.states.lock().unwrap().len();
        let resend = action(&f.client, email("xiaoming@example.test")).await;
        assert!(!resend.ok);
        assert_eq!(
            serde_json::to_value(&resend.state.account).unwrap(),
            serde_json::to_value(&before.account).unwrap()
        );
        assert!(f.view.states.lock().unwrap()[seen..]
            .iter()
            .all(|state| state.account.status == AccountStatus::Verifying));
        let done = action(&f.client, ClientAction::VerifyOtp { code: "123456".into() }).await;
        assert!(done.ok, "{:?}", done.error);
        assert_eq!(f.client.state().account.status, AccountStatus::SignedIn);
    }
}

/// A first factor publishes only after it commits. While a new email's request is still out, the
/// state and every broadcast are the previous attempt's; then one commit moves straight from the
/// old email to the new one, never through signed out.
#[tokio::test]
async fn a_first_factor_publishes_only_its_commit() {
    let (release, waiting) = oneshot::channel();
    let entered = Arc::new(Notify::new());
    let f = fixture(vec![
        ("/api/config/auth", auth_config()),
        ("/auth/v1/otp", ok(json!({}))),
        ("/api/config/auth", auth_config()),
        ("/auth/v1/otp", Reply::Held(waiting, entered.clone())),
    ]);
    assert!(action(&f.client, email("old@example.test")).await.ok);
    let seen = f.view.states.lock().unwrap().len();
    let client = f.client.clone();
    let pending = tokio::spawn(async move { action(&client, email("new@example.test")).await });
    entered.notified().await;
    assert_eq!(f.client.state().account.email.as_deref(), Some("old@example.test"));
    assert!(f.view.states.lock().unwrap()[seen..]
        .iter()
        .all(|state| state.account.email.as_deref() == Some("old@example.test")));
    assert!(release.send(response(200, json!({}), None)).is_ok());
    let done = pending.await.unwrap();
    assert!(done.ok, "{:?}", done.error);
    let states = f.view.states.lock().unwrap()[seen..].to_vec();
    assert!(states
        .iter()
        .all(|state| state.account.status == AccountStatus::Verifying));
    assert_eq!(
        states.last().unwrap().account.email.as_deref(),
        Some("new@example.test")
    );
    assert_eq!(f.client.state().account.email.as_deref(), Some("new@example.test"));
}

/// An escape while a first factor is still out (cancel, back to this computer, sign out) wins:
/// that attempt commits nothing even when avibe.bot then accepts it, and the escape's own state
/// is what stays published.
#[tokio::test]
async fn an_escape_during_a_first_factor_commits_nothing_of_it() {
    for escape in [
        ClientAction::Cancel,
        ClientAction::ReturnLocal,
        ClientAction::SignOut { intent_id: None },
    ] {
        let (release, waiting) = oneshot::channel();
        let entered = Arc::new(Notify::new());
        let f = fixture(vec![
            ("/api/config/auth", auth_config()),
            ("/auth/v1/otp", Reply::Held(waiting, entered.clone())),
        ]);
        let client = f.client.clone();
        let pending = tokio::spawn(async move { action(&client, email("new@example.test")).await });
        entered.notified().await;
        let escaped = action(&f.client, escape).await;
        assert!(escaped.ok, "{:?}", escaped.error);
        assert!(release.send(response(200, json!({}), None)).is_ok());
        let late = pending.await.unwrap();
        assert!(!late.ok);
        assert_eq!(f.client.state().account.status, AccountStatus::SignedOut);
        assert!(f.client.state().account.email.is_none());
        assert!(f
            .view
            .states
            .lock()
            .unwrap()
            .iter()
            .all(|state| state.account.status != AccountStatus::Verifying));
    }
}

// A5: display proof comes from the same account's catalog, never the OS name or
// URL. A selected shared row remains displayable when a later catalog omits it.
#[tokio::test]
async fn display_proof_is_account_and_current_identity_bound() {
    let mut session = catalog("account-a", &["local-id", "shared"]);
    session["instances"][0]["name"] = json!("authoritative-local");
    session["instances"][1]["relationship"] = json!("shared");
    session["instances"][1]["owner_email"] = json!("owner@example.test");
    let f = fixture(vec![]);
    sign_in(&f, session.clone()).await;
    f.client.local_origin(Some(
        avibe_runtime_host::LoopbackOrigin::parse("http://127.0.0.1:1984").unwrap(),
    ));
    f.http
        .replies
        .lock()
        .unwrap()
        .push_back(("/api/desktop/local-instance", ok(local_proof())));
    f.client.refresh_local().await;
    assert_eq!(
        serde_json::to_value(f.client.state()).unwrap()["local"]["slug"],
        "authoritative-local"
    );
    f.http.replies.lock().unwrap().extend(selection(
        &session,
        "shared",
        Reply::Ready(ready("account-a", "shared")),
    ));
    assert!(select(&f, "shared").await.ok);
    let selected = serde_json::to_value(f.client.state()).unwrap()["current_instance"].clone();
    assert_eq!(selected["relationship"], "shared");
    assert!(!f
        .client
        .state()
        .runtime_projection()
        .as_object()
        .unwrap()
        .contains_key("current_instance"));
    f.http
        .replies
        .lock()
        .unwrap()
        .push_back(("/api/mobile/session", ok(catalog("account-a", &[]))));
    assert!(action(&f.client, ClientAction::RefreshInstances).await.ok);
    let state = serde_json::to_value(f.client.state()).unwrap();
    assert_eq!(state["current_instance"], selected);
    assert!(state["local"]["slug"].is_null());
    f.http
        .replies
        .lock()
        .unwrap()
        .push_back(("/api/desktop/local-instance", Reply::Failure));
    f.client.refresh_local().await;
    assert!(serde_json::to_value(f.client.state()).unwrap()["local"]["slug"].is_null());
    assert!(action(&f.client, ClientAction::SignOut { intent_id: None }).await.ok);
    let state = serde_json::to_value(f.client.state()).unwrap();
    assert!(state["current_instance"].is_null() && state["local"]["slug"].is_null());
}

// A5: source/native staging alone is not a successful open. A failed durable
// write must leave the published target and account-specific recency unchanged.
#[tokio::test]
async fn failed_selection_persistence_never_publishes_success_or_recency() {
    let session = catalog("account-a", &["one", "two"]);
    let f = fixture(vec![]);
    sign_in(&f, session.clone()).await;
    let record = f.store.load("fixture").unwrap();
    f.http
        .replies
        .lock()
        .unwrap()
        .extend(selection(&session, "two", Reply::Ready(ready("account-a", "two"))));
    f.private.4.store(true, Ordering::SeqCst);
    let reply = select(&f, "two").await;
    assert_eq!(reply.error.unwrap().code, "credentials_unavailable");
    assert!(
        reply.state.connection.committed.is_local(),
        "failed selection was publicly committed"
    );
    assert_eq!(
        serde_json::to_value(f.store.load("fixture").unwrap().last_committed).unwrap(),
        serde_json::to_value(record.last_committed).unwrap()
    );
    assert!(!f._root.path().join("instance-recency.json").exists());
    assert_eq!(order(&f.client), ["one", "two"]);
}

/// The requests made since `from`, and the cookies each sent. Instance
/// sign-outs are sent without waiting, so they can land anywhere in this
/// order; they are read from `Http::logouts` instead.
fn since(f: &Fixture, from: usize) -> (Vec<String>, Vec<String>) {
    let calls = f.http.calls.lock().unwrap()[from..].to_vec();
    let cookies = f.http.cookies.lock().unwrap()[from..].to_vec();
    calls
        .into_iter()
        .zip(cookies)
        .filter(|(path, _)| path != "/auth/logout")
        .unzip()
}

/// Opening an instance again in the same run (from the list, back from this
/// computer, or when the path choice opens it again) goes on with the sign-in
/// it has: the instance is asked again before anything is shown, and no new
/// sign-in is made, so the instance lists no new device. A cookie the
/// instance sets meanwhile is the one sent next time.
#[tokio::test]
async fn opening_an_instance_again_in_one_run_goes_on_with_its_sign_in() {
    let f = selected_for_update().await;
    for (leave_first, sent) in [(false, "instance-only"), (true, "rotated-by-instance")] {
        if leave_first {
            assert!(action(&f.client, ClientAction::ReturnLocal).await.ok);
        }
        let from = f.http.calls.lock().unwrap().len();
        let committed = f.view.committed.lock().unwrap().len();
        let mut again = ready("account-a", "one");
        again.headers.append(
            SET_COOKIE,
            "__Host-vibe_remote_session=rotated-by-instance; Secure; HttpOnly; Path=/"
                .parse()
                .unwrap(),
        );
        f.http.replies.lock().unwrap().extend([
            ("/api/mobile/session", ok(catalog("account-a", &["one"]))),
            ("/api/session", Reply::Ready(again)),
        ]);
        let reply = select(&f, "one").await;
        assert!(reply.ok, "{:?}", reply.error);
        let (calls, cookies) = since(&f, from);
        assert_eq!(
            calls,
            ["/api/mobile/session", "/api/session"],
            "leave first: {leave_first}"
        );
        assert!(
            cookies[1].contains(&format!("__Host-vibe_remote_session={sent}")),
            "{cookies:?}"
        );
        assert_eq!(f.view.committed.lock().unwrap().len(), committed + 1);
        assert!(matches!(
            &f.client.state().connection.committed,
            ClientTarget::Remote { instance_id, .. } if instance_id == "one"
        ));
    }
    assert!(f.http.replies.lock().unwrap().is_empty());
}

/// A kept sign-in the instance no longer takes as this account's there (it
/// expired or was signed out, the instance was bound again, or it answers for
/// another account or instance) is let go, and the opening signs in anew;
/// that new sign-in is then the one kept.
#[tokio::test]
async fn a_kept_sign_in_the_instance_no_longer_takes_is_replaced_by_a_new_one() {
    let refusals: Vec<(&str, Response)> = vec![
        (
            "expired or signed out",
            response(200, json!({"remote": true, "authenticated": false}), None),
        ),
        (
            "refused",
            response(401, json!({"error": "authentication_required"}), None),
        ),
        ("another account", ready("account-b", "one")),
        ("bound again", ready("account-a", "another-binding")),
    ];
    for (case, refusal) in refusals {
        let f = selected_for_update().await;
        let session = catalog("account-a", &["one"]);
        let from = f.http.calls.lock().unwrap().len();
        let reads = f.view.cookie_reads.load(Ordering::SeqCst);
        let mut replies = vec![
            ("/api/mobile/session", ok(session.clone())),
            ("/api/session", Reply::Ready(refusal)),
        ];
        replies.extend(
            selection(&session, "one", Reply::Ready(ready("account-a", "one")))
                .into_iter()
                .skip(1),
        );
        f.http.replies.lock().unwrap().extend(replies);
        let reply = select(&f, "one").await;
        assert!(reply.ok, "{case}: {:?}", reply.error);
        let (calls, _) = since(&f, from);
        assert_eq!(
            calls,
            [
                "/api/mobile/session",
                "/api/session",
                "/auth/login",
                "/api/desktop/instance-authorization",
                "/auth/callback",
                "/api/session"
            ],
            "{case}"
        );
        // The instance's page was asked once for a newer cookie; it had none.
        assert_eq!(f.view.cookie_reads.load(Ordering::SeqCst), reads + 1, "{case}");
        // The refused sign-in is signed out of the instance once.
        let logouts = logouts(&f, 1).await;
        assert_eq!(logouts.len(), 1, "{case}: {logouts:?}");
        assert_eq!(logouts[0][1], "https://one.avibe.bot", "{case}");
        assert!(
            logouts[0][3].contains("__Host-vibe_remote_session=instance-only"),
            "{case}"
        );
        // The new sign-in is kept.
        let from = f.http.calls.lock().unwrap().len();
        f.http.replies.lock().unwrap().extend([
            ("/api/mobile/session", ok(session.clone())),
            ("/api/session", Reply::Ready(ready("account-a", "one"))),
        ]);
        assert!(select(&f, "one").await.ok, "{case}");
        assert_eq!(since(&f, from).0, ["/api/mobile/session", "/api/session"], "{case}");
    }
}

/// The instance sign-outs attempted so far, once there are at least `count`.
async fn logouts(f: &Fixture, count: usize) -> Vec<[String; 4]> {
    tokio::time::timeout(std::time::Duration::from_secs(5), async {
        while f.http.logouts.lock().unwrap().len() < count {
            tokio::task::yield_now().await;
        }
    })
    .await
    .expect("the instance sign-out was attempted");
    // Anything more would come from the same turn of the runtime.
    tokio::task::yield_now().await;
    f.http.logouts.lock().unwrap().clone()
}

/// When the instance gives no answer about a kept sign-in (unreachable, a
/// server error, or it cannot check the account with avibe.bot just now),
/// the sign-in is kept: the opening fails, nothing is signed out, and no new
/// sign-in is made. The next opening goes on with it.
#[tokio::test]
async fn a_kept_sign_in_survives_an_instance_that_cannot_answer() {
    let unanswered: Vec<(&str, Reply)> = vec![
        ("unreachable", Reply::Failure),
        (
            "server error",
            Reply::Ready(response(503, json!({"error": "unavailable"}), None)),
        ),
        (
            "account check unavailable",
            Reply::Ready(response(
                200,
                json!({"remote": true, "authenticated": true, "authorization_state": "unavailable"}),
                None,
            )),
        ),
    ];
    for (case, answer) in unanswered {
        let f = selected_for_update().await;
        let session = catalog("account-a", &["one"]);
        let from = f.http.calls.lock().unwrap().len();
        let reads = f.view.cookie_reads.load(Ordering::SeqCst);
        f.http
            .replies
            .lock()
            .unwrap()
            .extend([("/api/mobile/session", ok(session.clone())), ("/api/session", answer)]);
        let reply = select(&f, "one").await;
        assert!(!reply.ok, "{case}");
        assert_eq!(since(&f, from).0, ["/api/mobile/session", "/api/session"], "{case}");
        assert_eq!(f.view.cookie_reads.load(Ordering::SeqCst), reads, "{case}");
        tokio::task::yield_now().await;
        assert!(f.http.logouts.lock().unwrap().is_empty(), "{case}");

        let from = f.http.calls.lock().unwrap().len();
        f.http.replies.lock().unwrap().extend([
            ("/api/mobile/session", ok(session.clone())),
            ("/api/session", Reply::Ready(ready("account-a", "one"))),
        ]);
        let reply = select(&f, "one").await;
        assert!(reply.ok, "{case}: {:?}", reply.error);
        let (calls, cookies) = since(&f, from);
        assert_eq!(calls, ["/api/mobile/session", "/api/session"], "{case}");
        assert!(
            cookies[1].contains("__Host-vibe_remote_session=instance-only"),
            "{case}"
        );
    }
}

/// An instance renamed meanwhile opens at its new origin with a sign-in of
/// its own; the one kept at the old origin is signed out there.
#[tokio::test]
async fn a_renamed_instance_signs_in_at_its_new_origin_and_ends_the_old_sign_in() {
    let f = selected_for_update().await;
    let mut renamed = catalog("account-a", &["one"]);
    renamed["instances"][0]["host"] = json!("one-renamed.avibe.bot");
    renamed["instances"][0]["url"] = json!("https://one-renamed.avibe.bot");
    let from = f.http.calls.lock().unwrap().len();
    let mut replies = vec![("/api/mobile/session", ok(renamed.clone()))];
    replies.extend(
        selection_at(&renamed, "one-renamed", Reply::Ready(ready("account-a", "one")))
            .into_iter()
            .skip(1),
    );
    f.http.replies.lock().unwrap().extend(replies);
    let reply = select(&f, "one").await;
    assert!(reply.ok, "{:?}", reply.error);
    assert!(since(&f, from).0.contains(&"/auth/callback".to_owned()));
    assert!(matches!(
        &f.client.state().connection.committed,
        ClientTarget::Remote { canonical_origin, .. } if canonical_origin == "https://one-renamed.avibe.bot"
    ));
    let logouts = logouts(&f, 1).await;
    assert_eq!(logouts.len(), 1, "{logouts:?}");
    assert_eq!(logouts[0][1], "https://one.avibe.bot");
    assert!(logouts[0][3].contains("__Host-vibe_remote_session=instance-only"));

    // The new origin's sign-in is the one kept.
    let from = f.http.calls.lock().unwrap().len();
    f.http.replies.lock().unwrap().extend([
        ("/api/mobile/session", ok(renamed.clone())),
        ("/api/session", Reply::Ready(ready("account-a", "one"))),
    ]);
    assert!(select(&f, "one").await.ok);
    assert_eq!(since(&f, from).0, ["/api/mobile/session", "/api/session"]);
}

/// Signing out signs each instance sign-in of this run out of its instance,
/// as its page would (`Origin`, CSRF), and keeps none of them, whether or not
/// the instance can be reached. The next account opens the instance with a
/// sign-in of its own.
#[tokio::test]
async fn signing_out_ends_the_kept_sign_ins_even_offline() {
    for offline in [false, true] {
        let f = selected_for_update().await;
        f.http.offline.store(offline, Ordering::SeqCst);
        assert!(action(&f.client, ClientAction::SignOut { intent_id: None }).await.ok);
        assert_eq!(f.client.state().account.status, AccountStatus::SignedOut);
        tokio::time::timeout(std::time::Duration::from_secs(5), async {
            while f.http.logouts.lock().unwrap().is_empty() {
                tokio::task::yield_now().await;
            }
        })
        .await
        .expect("the instance sign-out was attempted");
        let logouts = f.http.logouts.lock().unwrap().clone();
        assert_eq!(logouts.len(), 1, "{logouts:?}");
        let [method, origin, csrf, cookies] = &logouts[0];
        assert_eq!(
            (method.as_str(), origin.as_str(), csrf.as_str()),
            ("POST", "https://one.avibe.bot", "csrf-from-instance")
        );
        assert!(
            cookies.contains("__Host-vibe_remote_session=instance-only"),
            "{cookies}"
        );
        assert!(!cookies.contains("private-account"), "{cookies}");

        let other = catalog("account-b", &["one"]);
        sign_in(&f, other.clone()).await;
        let from = f.http.calls.lock().unwrap().len();
        f.http
            .replies
            .lock()
            .unwrap()
            .extend(selection(&other, "one", Reply::Ready(ready("account-b", "one"))));
        let reply = select(&f, "one").await;
        assert!(reply.ok, "offline {offline}: {:?}", reply.error);
        assert!(
            since(&f, from).0.contains(&"/auth/callback".to_owned()),
            "offline {offline}"
        );
    }
}
