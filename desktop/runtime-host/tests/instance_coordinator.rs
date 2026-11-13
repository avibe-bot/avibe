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
    restored: Mutex<Vec<ClientTarget>>,
    update_calls: Mutex<Vec<(ClientAction, Epoch)>>,
    update_failure: Mutex<Option<String>>,
    retirement_failure: AtomicBool,
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
    async fn restore(&self, _: Epoch, target: ClientTarget) -> Result<(), ClientError> {
        self.restored.lock().unwrap().push(target);
        Ok(())
    }
    async fn signed_out(&self, _: Epoch, _: ClientTarget) -> Result<(), ClientError> {
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
                action(&f.client, ClientAction::SignOut).await.error.unwrap().code,
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

// The actual-window cases interrupt navigation. This earlier boundary holds
// credential IO: lifecycle must not discard an accepted local presentation
// before it reaches the window owner, while a later explicit action must win.
#[tokio::test]
async fn local_escape_reaches_its_view_owner_after_lifecycle_during_credential_io() {
    for superseded in [false, true] {
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
                    email: "held@example.test".into()
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
