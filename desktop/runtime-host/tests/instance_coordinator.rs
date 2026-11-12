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
use reqwest::header::{HeaderMap, SET_COOKIE};
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
}
struct Http {
    replies: Mutex<VecDeque<(&'static str, Reply)>>,
    calls: Mutex<Vec<String>>,
}
#[async_trait]
impl Transport for Http {
    async fn send(&self, request: Request) -> Result<Response, ClientError> {
        let path = request.url.path().to_owned();
        self.calls.lock().unwrap().push(path.clone());
        if request.method == reqwest::Method::DELETE {
            return Ok(response(200, json!({}), None));
        }
        let (expected, reply) = self
            .replies
            .lock()
            .unwrap()
            .pop_front()
            .expect("unexpected HTTP request");
        assert_eq!(path, expected);
        match reply {
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
    states: Mutex<Vec<ClientState>>,
    committed: Mutex<Vec<ClientTarget>>,
}
#[async_trait]
impl ClientView for View {
    fn changed(&self, state: &ClientState) {
        self.states.lock().unwrap().push(state.clone());
    }
    fn invalidate(&self, _: u64) {}
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
    async fn restore(&self, _: Epoch, _: ClientTarget) -> Result<(), ClientError> {
        Ok(())
    }
    async fn signed_out(&self, _: Epoch, _: ClientTarget) -> Result<(), ClientError> {
        Ok(())
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
    let root = tempfile::tempdir().unwrap();
    let store = ConnectionStore::new(root.path().join("connection.json"));
    let record = store.load("小明的 Mac").unwrap();
    let state = ClientState::new("小明的 Mac".into(), "0.1.0".into(), json!({}));
    let http = Arc::new(Http {
        replies: Mutex::new(replies.into()),
        calls: Mutex::new(vec![]),
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
fn first_factor() -> Vec<(&'static str, Reply)> {
    vec![
        (
            "/api/config/auth",
            ok(json!({"supabase_url":"https://fixture.supabase.test","supabase_anon_key":"public-fixture"})),
        ),
        ("/auth/v1/otp", ok(json!({}))),
        (
            "/auth/v1/verify",
            ok(json!({"access_token":"private-provider","refresh_token":"private-refresh"})),
        ),
    ]
}
fn signed_in() -> Value {
    serde_json::from_str::<Value>(include_str!("../../contracts/instance-client-v1/transport.json")).unwrap()
        ["account_session"]
        .clone()
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
            (Some(ClientAction::SignOut), false),
            (Some(ClientAction::SignOut), true),
        ] {
            let mut replies = first_factor();
            replies.push((
                "/api/desktop/session",
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
                        email: "saved@example.test".into()
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
                assert!(action(&restored, ClientAction::SignOut).await.ok);
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
    replies.push(("/api/desktop/session", Reply::Held(held, entered.clone())));
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
    assert!(action(&f.client, ClientAction::SignOut).await.ok);
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
            "/api/desktop/session",
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
        ("/api/desktop/session",Reply::Ready(response(403,json!({
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
    let mut replies = first_factor();
    replies.push((
        "/api/desktop/session",
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
    let logout = tokio::spawn(async move { action(&client, ClientAction::SignOut).await });
    tokio::time::timeout(std::time::Duration::from_secs(2), async {
        while f.client.state().account.status != AccountStatus::SignedOut {
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap();
    release.send(()).unwrap();
    assert_eq!(verification.await.unwrap().error.unwrap().code, "stale_generation");
    assert!(logout.await.unwrap().ok);
    assert!(f.private.0.lock().unwrap().is_empty());
    assert!(f.store.load("unused").unwrap().account_id.is_none());
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
            action: ClientAction::SignOut,
        })
        .await;
    assert_eq!(reply.error.unwrap().code, "stale_generation");
    assert_eq!(f.client.state().generation, 1);
}

// A first key write can finish after Cancel. An existing session refresh can
// race the same way. Neither may leave credentials from an uncommitted epoch.
#[tokio::test]
async fn cancellation_rolls_back_unpublished_credential_writes() {
    for (refresh_existing, erase_failure) in [(false, false), (true, false), (false, true)] {
        let mut replies = first_factor();
        replies.push((
            "/api/desktop/session",
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
        release.send(()).unwrap();
        assert_eq!(
            verification.await.unwrap().error.unwrap().code,
            if erase_failure {
                "credentials_unavailable"
            } else {
                "stale_generation"
            }
        );
        let cancelled = cancel.await.unwrap();
        if erase_failure {
            assert_eq!(cancelled.error.unwrap().code, "credentials_unavailable");
            assert_eq!(
                f.client.state().enrollment.error_code.as_deref(),
                Some("credentials_unavailable")
            );
            f.private.2.store(false, Ordering::SeqCst);
            assert!(action(&f.client, ClientAction::SignOut).await.ok);
        } else {
            assert!(cancelled.ok);
        }
        assert_eq!(*f.private.0.lock().unwrap(), before);
        assert_eq!(f.store.load("unused").unwrap().account_id, before_record);
    }
}

#[tokio::test]
async fn restore_rejects_expired_or_different_account_and_erases_owned_credential() {
    for (changed_account, erase_failure) in [(false, false), (true, false), (false, true)] {
        let mut replies = first_factor();
        replies.push((
            "/api/desktop/session",
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
        if erase_failure {
            assert!(!f.private.0.lock().unwrap().is_empty());
            assert_eq!(
                action(
                    &restored,
                    ClientAction::RequestOtp {
                        email: "other@example.test".into()
                    }
                )
                .await
                .error
                .unwrap()
                .code,
                "credentials_unavailable"
            );
            f.private.2.store(false, Ordering::SeqCst);
            assert!(action(&restored, ClientAction::SignOut).await.ok);
            assert!(restored.state().enrollment.error_code.is_none());
        }
        assert!(f.private.0.lock().unwrap().is_empty());
        assert!(f.store.load("unused").unwrap().account_id.is_none());
    }
}

// A second catalog fetch follows successful registration. Its failure must not
// consume the only recovery-code response after TOTP enrollment succeeded.
#[tokio::test]
async fn recovery_codes_survive_catalog_failure_after_local_registration() {
    let mut replies = first_factor();
    let descriptor = json!({"schema_version":1,"installation_id":"installation","device_name":"小明的 Mac",
        "binding_revision":"revision","pairing":{"instance_id":"local-1","backend_url":"https://avibe.bot","public_url":"https://local.avibe.bot"},
        "remote_access":{"configured_enabled":false,"requested":false,"running":false},"enrollment":null});
    replies.extend([
        ("/api/desktop/session", Reply::Ready(response(403,json!({"error":"two_factor_required","email":"xiaoming@example.test","gate":"enroll","methods":["totp"]}),None))),
        ("/api/two-factor/totp/setup",ok(json!({"token":"private-token","uri":"otpauth://fixture","qrSvg":"<svg/>","secret":"private-seed"}))),
        ("/api/two-factor/totp",ok(json!({"method":{"id":"totp-1","kind":"totp","name":"Authenticator","createdAt":"2026-11-13","lastUsedAt":null},"recoveryCodes":["one-time-code"]}))),
        ("/api/mobile/session",ok(signed_in())),
        ("/api/mobile/session",ok(signed_in())),
        ("/api/desktop/local-instance",ok(descriptor.clone())),
        ("/api/csrf-token",Reply::Ready(response(200,json!({"csrf_token":"csrf"}),Some("vibe_csrf_token=csrf; Path=/")))),
        ("/api/desktop/local-instance/prepare",ok(json!({"local":descriptor,"operation_id":"operation"}))),
        ("/api/desktop/instances/ensure-local",ok(json!({"outcome":"already_paired","owner_match":true,"instance_id":"local-1"}))),
        ("/api/desktop/local-instance",ok(descriptor)),
        ("/api/mobile/session",Reply::Ready(response(503,json!({"error":"unavailable"}),None))),
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
}
