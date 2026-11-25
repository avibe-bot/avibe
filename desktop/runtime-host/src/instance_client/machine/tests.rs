use super::*;

const ID: &str = "a2ea8b2b-b94d-4f19-9d73-495709516627";
fn ticket() -> Value {
    json!({"v":1,"event":"ticket","claim_id":ID,"user_code":"ABCD234567","user_url":"https://avibe.bot/connect/ABCD234567","mode":"desktop","device_name":"fixture computer","expires_at":(utc_now() + chrono::Duration::minutes(5)).to_rfc3339(),"ordinary_decision_readback":true})
}
fn grant() -> ReviewedGrant {
    ReviewedGrant::fixture(ID, "fixture-a", "reviewed.avibe.bot", Some("fixture-instance"))
}
fn parse(v: Value) -> Result<MachineTicket> {
    serde_json::from_value::<Frame>(v).map_err(|_| invalid())?.ticket()
}

pub(crate) struct Peer {
    arm: oneshot::Receiver<ArmRequest>,
    _adoption: watch::Sender<bool>,
    pub(crate) finish: oneshot::Sender<Result<MachineOutcome>>,
}
impl Peer {
    pub(crate) fn published(
        self,
    ) -> (
        watch::Sender<bool>,
        oneshot::Receiver<String>,
        oneshot::Sender<Result<()>>,
    ) {
        let (witness, nonce, ack) = publication::Publication::channel(
            "fixture-published".into(),
            self._adoption.subscribe(),
            Instant::now() + Duration::from_secs(40),
        );
        // The simulated child only supplies its sample and readback, never account ownership.
        let _ = self.finish.send(Ok(MachineOutcome::Publication(witness)));
        (self._adoption, nonce, ack)
    }
    pub(crate) fn assert_unarmed(&mut self) {
        assert!(matches!(self.arm.try_recv(), Err(oneshot::error::TryRecvError::Empty)));
    }
    pub(crate) async fn arm(&mut self) -> Result<Value> {
        (&mut self.arm).await.map_err(|_| retired())?.live_value()
    }
}
/// Stand in only for the private pipe endpoints, not for hosted review or authorization.
pub(crate) fn peer(subject: &str) -> (MachineAttempt, Peer, String) {
    let ticket = parse(ticket()).unwrap();
    let expiry = ticket.expires_at.clone();
    let (arm, request) = oneshot::channel();
    let (finish, result) = oneshot::channel();
    let (sender, adoption) = watch::channel(true);
    (
        MachineAttempt {
            ticket,
            subject: subject.into(),
            locale: "en".into(),
            arm,
            result,
            adoption,
        },
        Peer {
            arm: request,
            _adoption: sender,
            finish,
        },
        expiry,
    )
}

#[test]
fn only_canonical_private_machine_evidence_can_reach_hosted_read() {
    for (key, value) in [
        ("v", json!(2)),
        ("mode", json!("link")),
        ("claim_id", json!(ID.to_uppercase())),
        ("user_url", json!("https://avibe.bot@other.test/connect/ABCD234567")),
        ("user_code", json!("ABCD234561")),
        ("expires_at", json!("2000-01-01T00:00:00Z")),
        ("device_name", json!("x\ny")),
        ("ordinary_decision_readback", json!(1)),
        ("installation_id", json!("fixture-must-not-be-imported")),
    ] {
        let mut v = ticket();
        v[key] = value;
        assert!(parse(v).is_err());
    }
    let machine = parse(ticket()).unwrap();
    let crossed = ReviewedGrant::fixture("another", "fixture-a", "reviewed.avibe.bot", None);
    assert!(machine.arm("fixture-a", crossed).is_err());
    assert!(machine.arm("fixture-b", grant()).is_err());
    assert_eq!(
        machine.arm("fixture-a", grant()).unwrap().live_value().unwrap()["existing_instance_id"],
        "fixture-instance"
    );
    let mut old = ticket();
    old["ordinary_decision_readback"] = json!(false);
    let machine = parse(old).unwrap();
    assert_eq!(
        machine.arm("fixture-a", grant()).err().unwrap().code,
        "local_runtime_upgrade_required"
    );
    let new = ReviewedGrant::fixture(ID, "fixture-a", "reviewed.avibe.bot", None);
    assert_eq!(
        machine.arm("fixture-a", new).unwrap().live_value().unwrap()["kind"],
        "new"
    );
}

#[tokio::test]
async fn hostile_or_truncated_frames_have_only_fixed_errors() {
    let mut large = vec![b' '; MAX_FRAME];
    large.push(b'\n');
    for b in [
        large,
        b"{\"event\":\"challenge\",\"v\":1,\"v\":1,\"challenge\":\"secret-canary\"}\n".to_vec(),
        b"{\"v\":1,\"event\":\"challenge\",\"challenge\":\"fixture\"}".to_vec(),
    ] {
        assert_eq!(read(&mut &b[..]).await.err().unwrap().code, "invalid_machine_response");
    }
    let bytes = b"{\"event\":\"result\",\"v\":1,\"outcome\":\"error\",\"code\":\"secret_canary\"}\n";
    assert_eq!(
        read(&mut &bytes[..]).await.unwrap().outcome().err().unwrap().code,
        "invalid_machine_response"
    );
}

#[tokio::test]
async fn a_notice_needs_a_publication_sample_from_this_claim_and_exact_existing_target() {
    let local = "fixture-instance";
    let generation = "c".repeat(64);
    for (event, claim, target, version, marker, accept) in [
        ("publication", ID, local, 1, generation.as_str(), true),
        ("publication", "another-claim", local, 1, generation.as_str(), false),
        ("publication", ID, "another-instance", 1, generation.as_str(), false),
        ("publication", ID, local, 2, generation.as_str(), false),
        ("publication", ID, local, 1, "invalid", false),
        ("binding", ID, local, 1, generation.as_str(), false),
        ("eof", ID, local, 1, generation.as_str(), false),
    ] {
        let mut frame = json!({"v":version,"event":event,"instance_id":target,"generation":marker});
        if event == "publication" {
            frame["claim_id"] = json!(claim);
        }
        let mut response = Vec::new();
        if event != "eof" {
            write(&mut response, frame).await.unwrap();
        }
        let mut commands = Vec::new();
        let r = publication::Sample::original(&mut response.as_slice(), &mut commands, ID, Some(local)).await;
        assert_eq!(r.is_ok(), accept);
        assert_eq!(
            serde_json::from_slice::<Value>(&commands).unwrap(),
            json!({"v":1,"op":"observe_publication"})
        );
    }
}

#[cfg(unix)]
mod child {
    use super::*;
    use crate::instance_client::transport::{Request, Response};
    use reqwest::{
        header::{HeaderMap, COOKIE, ORIGIN, SET_COOKIE},
        Method,
    };
    use std::{
        process::Stdio,
        sync::atomic::{AtomicUsize, Ordering},
    };

    struct Local(AtomicUsize);
    #[async_trait::async_trait]
    impl Transport for Local {
        async fn send(&self, r: Request) -> Result<Response> {
            assert_eq!(r.url.origin().ascii_serialization(), "http://127.0.0.1:5123");
            match self.0.fetch_add(1, Ordering::SeqCst) {
                0 => {
                    assert_eq!((r.method, r.url.path()), (Method::GET, "/api/csrf-token"));
                    assert!(r.headers.get(COOKIE).is_none());
                    let mut headers = HeaderMap::new();
                    headers.insert(SET_COOKIE, "vibe_csrf_token=fixture; Path=/".parse().unwrap());
                    Ok(Response {
                        status: 200,
                        headers,
                        body: br#"{"csrf_token":"fixture"}"#.to_vec(),
                    })
                }
                1 => {
                    assert_eq!((r.method, r.url.path()), (Method::POST, ADMIT));
                    assert_eq!(r.headers[ORIGIN], "http://127.0.0.1:5123");
                    assert_eq!(r.headers[COOKIE], "vibe_csrf_token=fixture");
                    assert_eq!(r.headers["x-vibe-csrf-token"], "fixture");
                    assert_eq!(r.body.unwrap(), json!({"challenge":"e".repeat(64)}));
                    Ok(Response {
                        status: 204,
                        headers: HeaderMap::new(),
                        body: vec![],
                    })
                }
                _ => panic!("unexpected request"),
            }
        }
    }
    #[tokio::test]
    async fn one_pipe_admits_and_only_its_owner_can_release_or_arm_it() {
        for cancel in [false, true] {
            let tmp = tempfile::tempdir().unwrap();
            let script = tmp.path().join("fixture.sh");
            // This test-owned peer deliberately has no Runtime, credentials or network.
            std::fs::write(
                &script,
                format!(
                    r#"IFS= read -r open || exit 1
printf '%s\n' '{{"v":1,"event":"challenge","challenge":"{}"}}'
IFS= read -r begin || exit 1
printf '%s\n' '{}'
IFS= read -r arm || {{ : > closed; exit 0; }}
printf '%s' "$arm" > submitted
printf '%s\n' '{{"v":1,"event":"result","outcome":"publication_finished"}}'
IFS= read -r observe || exit 1
printf '%s' "$observe" > observed
printf '%s\n' '{{"v":1,"event":"publication","claim_id":"{ID}","instance_id":"fixture-instance","generation":"{}"}}'
IFS= read -r recheck || exit 0
"#,
                    "e".repeat(64),
                    ticket(),
                    "d".repeat(64)
                ),
            )
            .unwrap();
            let mut cmd = std::process::Command::new("/bin/sh");
            cmd.arg(script)
                .env_clear()
                .current_dir(tmp.path())
                .stdin(Stdio::piped())
                .stdout(Stdio::piped())
                .stderr(Stdio::null());
            let (adoption, live) = watch::channel(true);
            let (_document, document_retired) = oneshot::channel();
            let local = Arc::new(Local(AtomicUsize::new(0)));
            let attempt = MachineAttempt::open_source(
                ClaimSource {
                    command: ClaimHelperCommand(cmd),
                    origin: LoopbackOrigin::parse("http://127.0.0.1:5123").unwrap(),
                    live,
                },
                local.clone(),
                "fixture-a".into(),
                "en",
                document_retired,
            )
            .await
            .unwrap();
            assert_eq!(local.0.load(Ordering::SeqCst), 2);
            if cancel {
                adoption.send_replace(false);
                if let Ok(completion) = attempt.dispatch(grant()) {
                    assert!(completion.wait().await.is_err());
                }
                timeout(Duration::from_secs(3), async {
                    while !tmp.path().join("closed").exists() {
                        tokio::time::sleep(Duration::from_millis(10)).await;
                    }
                })
                .await
                .unwrap();
                assert!(!tmp.path().join("submitted").exists());
            } else {
                assert!(matches!(
                    attempt.dispatch(grant()).unwrap().wait().await.unwrap(),
                    MachineOutcome::Publication(_)
                ));
                let observe: Value =
                    serde_json::from_slice(&std::fs::read(tmp.path().join("observed")).unwrap()).unwrap();
                assert_eq!(observe, json!({"op":"observe_publication","v":1}));
                let arm: Value = serde_json::from_slice(&std::fs::read(tmp.path().join("submitted")).unwrap()).unwrap();
                assert_eq!(
                    arm,
                    json!({"op":"arm","v":1,"claim_id":ID,"kind":"existing","existing_instance_id":"fixture-instance","hostname":"reviewed.avibe.bot"})
                );
            }
        }
    }
}
