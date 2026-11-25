//! Exercise the real document dispatch, resolved adoption, subprocess pipe and final
//! delivery. Only the subprocess peer and HTTP transport are synthetic.
use super::*;
use crate::{
    launcher::ClaimHelperCommand, BootstrapPhase, BootstrapTrigger, DiscardStatus, HealthProbe, LaunchError,
    LaunchedRuntime, LoopbackOrigin, Presence, ResolvedRuntimeLauncher, RuntimeHostSettings, RuntimeLauncher,
};
use reqwest::{
    header::{HeaderMap, AUTHORIZATION, COOKIE, SET_COOKIE},
    Method,
};
use std::{
    path::PathBuf,
    process::{Command, Stdio},
};

const ORIGIN: &str = "http://127.0.0.1:40123";
#[derive(Clone)]
struct Fixture {
    root: PathBuf,
    case: &'static str,
}
#[async_trait::async_trait]
impl HealthProbe for Fixture {
    async fn presence(&self, origin: &LoopbackOrigin, _: Option<&str>) -> Presence {
        assert_eq!(origin.as_str(), ORIGIN);
        Presence::Unmanaged
    }
}
impl RuntimeLauncher for Fixture {
    fn resolve(&self) -> std::result::Result<Arc<dyn ResolvedRuntimeLauncher>, LaunchError> {
        Ok(Arc::new(self.clone()))
    }
}
impl ResolvedRuntimeLauncher for Fixture {
    fn endpoint(&self) -> std::result::Result<LoopbackOrigin, LaunchError> {
        Ok(LoopbackOrigin::parse(ORIGIN).unwrap())
    }
    fn launch(&self, _: bool) -> std::result::Result<LaunchedRuntime, LaunchError> {
        panic!("observation must not start a Runtime")
    }
    fn claim_helper(&self) -> Option<ClaimHelperCommand> {
        eprintln!("observation fixture: resolving inspect peer");
        let exe = if cfg!(windows) { "python.exe" } else { "python3" };
        let interpreter = std::env::split_paths(&std::env::var_os("PATH").unwrap())
            .filter(|p| p.is_absolute())
            .map(|p| p.join(exe))
            .find(|p| p.is_file())
            .expect("test Python");
        let mut cmd = Command::new(interpreter);
        cmd.env_clear()
            .current_dir(&self.root)
            .args(["-I", "-S", "-B", "peer.py", self.case])
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null());
        #[cfg(windows)]
        cmd.env("SystemRoot", std::env::var_os("SystemRoot").unwrap());
        Some(ClaimHelperCommand(cmd))
    }
}
struct Wire {
    case: &'static str,
    calls: AtomicU64,
    reading: Notify,
}
#[async_trait::async_trait]
impl Transport for Wire {
    async fn send(&self, r: Request) -> Result<Response> {
        let mut headers = HeaderMap::new();
        assert!(!r.headers.contains_key(AUTHORIZATION));
        let call = self.calls.fetch_add(1, Ordering::SeqCst);
        eprintln!("observation fixture: transport call {call}");
        let (status, body) = match call {
            0 => {
                assert_eq!(
                    (r.method, r.url.as_str()),
                    (Method::GET, "http://127.0.0.1:40123/api/csrf-token")
                );
                assert!(!r.headers.contains_key(COOKIE) && r.body.is_none());
                headers.insert(SET_COOKIE, "vibe_csrf_token=fixture; Path=/".parse().unwrap());
                (200, br#"{"csrf_token":"fixture"}"#.to_vec())
            }
            1 => {
                assert_eq!(
                    (r.method, r.url.as_str()),
                    (Method::POST, "http://127.0.0.1:40123/api/desktop/claim/v1/admit")
                );
                assert_eq!(r.headers[COOKIE], "vibe_csrf_token=fixture");
                assert_eq!(r.body, Some(json!({"challenge":"e".repeat(64)})));
                (204, vec![])
            }
            2 => {
                assert_eq!(
                    (r.method, r.url.as_str()),
                    (Method::GET, "https://avibe.bot/api/mobile/session")
                );
                assert_eq!(r.headers[COOKIE], "ordinary=fixture");
                assert!(r.body.is_none());
                if self.case == "during" {
                    self.reading.notify_one();
                    return std::future::pending().await;
                }
                if matches!(self.case, "offline" | "late_error") {
                    return Err(ClientError::new("fixture_private_transport_error"));
                }
                headers.insert(
                    SET_COOKIE,
                    "ordinary=discard; Secure; HttpOnly; Path=/".parse().unwrap(),
                );
                let relationship = if self.case == "shared" { "shared" } else { "owner" };
                (200,serde_json::to_vec(&json!({"account":{"id":"fixture-a","email":"fixture@example.test"},"instances":[{"id":"fixture-local","relationship":relationship}]})).unwrap())
            }
            _ => panic!("unexpected request during binding inspection"),
        };
        Ok(Response { status, body, headers })
    }
}
fn request(generation: u64, action: Value) -> ClaimRequest {
    serde_json::from_value(json!({"expected_generation":generation,"action":action})).unwrap()
}
async fn exercise(case: &'static str, root: &tempfile::TempDir) {
    std::fs::write(root.path().join("peer.py"), include_bytes!("observation_peer.py")).unwrap();
    let fixture = Arc::new(Fixture {
        root: root.path().to_owned(),
        case,
    });
    let host = RuntimeHost::new(fixture.clone(), fixture, RuntimeHostSettings::default());
    if case != "no_source" {
        assert_eq!(
            host.bootstrap(&DiscardStatus, BootstrapTrigger::Recovery).await.phase,
            BootstrapPhase::Ready
        );
    }
    eprintln!("observation fixture: bootstrap complete");
    let wire = Arc::new(Wire {
        case,
        calls: AtomicU64::new(0),
        reading: Notify::new(),
    });
    let path = root.path().join("connection.json");
    let store = ConnectionStore::new(path.clone());
    let mut record = store.load("fixture computer").unwrap();
    record.account_id = Some("fixture-a".into());
    store.save(&record).unwrap();
    let stored = std::fs::read(&path).unwrap();
    let mut state = ClientState::new("fixture computer".into(), "0.1.0".into(), json!({}));
    state.account.status = AccountStatus::SignedIn;
    state.account.identity = Some(Account {
        id: "fixture-a".into(),
        email: "fixture@example.test".into(),
    });
    let gen = state.generation;
    let c = Arc::new(Coordinator::new(
        state,
        record,
        store,
        wire.clone(),
        Arc::new(NoPlatform),
        Arc::new(NoPlatform),
        Arc::new(NoPlatform),
    ));
    {
        let mut inner = c.inner.lock().unwrap();
        inner.account.resumes = Some("fixture-a".into());
        let url = inner.account.session.origin.clone();
        let mut seed = NativeSession::new(url.clone(), wire.clone());
        seed.store_cookies(vec![cookie::Cookie::new("ordinary", "fixture")], &url);
        inner.account.session = NativeSession::restore_account(wire.clone(), &seed.persist().unwrap()).unwrap();
    }
    let doc = c.claim_document(gen).unwrap();
    let caller = doc.caller().unwrap();
    assert!(caller.monitoring() && c.claim_document(gen).is_err()); // even before dispatch
    let observe = || request(gen, json!({"type":"observe_current_binding"}));
    if case == "naming" {
        let opened = c
            .claim_review(&host, &caller, "en", request(gen, json!({"type":"open"})))
            .await
            .unwrap()
            .deliver()
            .unwrap()
            .unwrap();
        assert!(c.claim_review(&host, &caller, "en", observe()).await.is_err());
        c.claim_review(
            &host,
            &caller,
            "en",
            request(gen, json!({"type":"cancel","receipt":opened["receipt"]})),
        )
        .await
        .unwrap()
        .deliver()
        .unwrap();
        assert_eq!(wire.calls.load(Ordering::SeqCst), 0);
        return;
    }
    eprintln!("observation fixture: dispatch");
    let work = c.claim_review(&host, &caller, "en", observe());
    tokio::pin!(work);
    if case == "during" {
        tokio::select! { _=wire.reading.notified()=>(), _=&mut work=>panic!("inspection returned before document closed") };
        assert!(c.claim_document(gen).is_err());
        drop(doc); // synchronous shell close or replacement while hosted GET waits
        let next = c.claim_document(gen).unwrap();
        assert!(work.await.is_err() && next.caller().unwrap().monitoring());
        return;
    }
    let pending = work.await;
    eprintln!("observation fixture: dispatch returned");
    if case == "no_source" {
        assert!(pending.is_err());
        assert_eq!(wire.calls.load(Ordering::SeqCst), 0);
        return;
    }
    let pending = pending.unwrap();
    assert!(caller.monitoring() && c.claim_document(gen).is_err());
    assert!(c
        .claim_review(&host, &caller, "en", request(gen, json!({"type":"open"})))
        .await
        .is_err());
    assert!(root.path().join("inspect-finished").is_file());
    assert_eq!(std::fs::read(path).unwrap(), stored);
    assert_eq!(
        c.inner
            .lock()
            .unwrap()
            .account
            .session
            .cookie_value("ordinary")
            .as_deref(),
        Some("fixture")
    );
    assert_eq!(
        wire.calls.load(Ordering::SeqCst),
        if matches!(case, "absent" | "legacy" | "helper_error") {
            2
        } else {
            3
        }
    );
    match case {
        "drop" => drop(pending),
        "late_error" => {
            drop(doc);
            let next = c.claim_document(gen).unwrap();
            assert_eq!(pending.deliver().unwrap_err().code, "stale_generation");
            assert!(next.caller().unwrap().monitoring());
        }
        "credential" => {
            c.inner.lock().unwrap().account.session = NativeSession::account(wire);
            assert_eq!(pending.deliver().unwrap_err().code, "stale_generation");
        }
        "contended" => {
            let _locked = c.inner.lock().unwrap();
            assert_eq!(pending.deliver().unwrap_err().code, "stale_generation");
        }
        "readiness" => {
            host.release_after_readiness_loss();
            assert_eq!(pending.deliver().unwrap_err().code, "stale_generation");
        }
        "offline" | "helper_error" => assert_eq!(
            pending.deliver().unwrap_err().code,
            if case == "offline" {
                "account_observation_unavailable"
            } else {
                "local_binding_changed"
            }
        ),
        _ => {
            let state = match case {
                "owner" => "owner_observed",
                "shared" => "bound_owner_unconfirmed",
                "absent" => "no_published_binding",
                "legacy" => "legacy_review_required",
                _ => unreachable!(),
            };
            assert_eq!(pending.deliver().unwrap(), Some(json!({"state":state})));
        }
    }
    assert!(!caller.monitoring());
}

/// How long the Python peer may take to start, apart from each case's own budget.
const PEER_START_LIMIT: Duration = Duration::from_secs(120);

#[tokio::test]
async fn current_binding_is_read_only_and_only_the_original_document_receives_its_result() {
    for case in [
        "owner",
        "shared",
        "absent",
        "legacy",
        "helper_error",
        "offline",
        "during",
        "late_error",
        "credential",
        "contended",
        "readiness",
        "drop",
        "naming",
        "no_source",
    ] {
        let root = tempfile::tempdir().unwrap();
        let run = exercise(case, &root);
        tokio::pin!(run);
        // Starting the Python peer is not what is measured: a loaded runner can
        // take longer than the whole case to start an interpreter. The case's
        // own budget starts once the peer says it has started, or the case
        // finishes without one (`naming`, `no_source`).
        let finished = tokio::time::timeout(PEER_START_LIMIT, async {
            loop {
                if root.path().join("peer-started").is_file() {
                    return false;
                }
                tokio::select! {
                    _ = &mut run => return true,
                    _ = tokio::time::sleep(Duration::from_millis(50)) => {}
                }
            }
        })
        .await
        .unwrap_or_else(|_| {
            panic!(
                "binding observation: the peer did not start, and the case did not end, within {} s: {case}",
                PEER_START_LIMIT.as_secs()
            )
        });
        if !finished && tokio::time::timeout(Duration::from_secs(15), &mut run).await.is_err() {
            // Peer-side progress only: no frame, URL, credential, nonce or child stderr.
            let phase = [
                "inspect-finished",
                "unchanged",
                "recheck",
                "binding",
                "begin",
                "challenge",
                "peer-started",
            ]
            .into_iter()
            .find(|phase| root.path().join(phase).is_file())
            .unwrap_or("before-peer-start");
            panic!("binding observation timed out: {case}, peer stage: {phase}");
        }
    }
}
