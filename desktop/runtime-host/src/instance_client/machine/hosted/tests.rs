//! All identities, cookies, clocks' deadlines and transports here are test-owned. No
//! operating-system credential store, network, Runtime, or WebView participates.
use super::*;
use crate::instance_client::{
    account::CONTROL_PLANE,
    machine::MachineOutcome,
    transport::{Request, Transport},
};
use reqwest::header::{HeaderMap, AUTHORIZATION, CONTENT_TYPE, COOKIE, ORIGIN, SET_COOKIE};
use serde_json::Value;
use std::{
    collections::VecDeque,
    sync::{
        atomic::{AtomicU64, Ordering},
        Arc, Mutex,
    },
    time::Duration,
};
use tokio::{
    sync::{oneshot, watch},
    time::Instant,
};

const READ: &str = "/api/v1/claims/read";
const ID: &str = "a2ea8b2b-b94d-4f19-9d73-495709516627";
const CODE: &str = "ABCD234567";
const EXPIRY: &str = "2098-07-08T15:24:43.123Z";
fn ticket(ack: bool) -> MachineTicket {
    MachineTicket {
        claim_id: ID.into(),
        user_code: CODE.into(),
        device_name: "fixture device".into(),
        expires_at: EXPIRY.into(),
        existing_readback: ack,
        deadline: Instant::now() + Duration::from_secs(60),
    }
}
fn account() -> Account {
    Account {
        id: "fixture-a".into(),
        email: "fixture@example.test".into(),
    }
}
fn envelope(state: Value) -> Value {
    let mut body = json!({"mode":"desktop","locale":"en","expires_at":EXPIRY,"device_name":"fixture device","account_subject":"fixture-a","account_email":"fixture@example.test"});
    body.as_object_mut().unwrap().extend(state.as_object().unwrap().clone());
    body
}
fn new(p: Option<&str>, selection: Option<&str>, taken: bool) -> Value {
    envelope(json!({"state":"new","prefilled_prefix":p,"prefix":selection,"taken":taken}))
}
fn existing() -> Value {
    envelope(
        json!({"state":"existing","prefix":"old-legacy","hostname":"old-legacy-app.avibe.bot","existing_instance_id":"fixed-fixture-id"}),
    )
}
fn holding() -> Value {
    json!({"limit":5,"held_count":6,"active_count":4,"at_limit":true,"reason":"includes_waiting","earliest_release_at":null})
}
fn eligible() -> Value {
    json!([{"prefix":"waiting","hostname":"waiting-app.avibe.bot","release_at":null}])
}
fn parsed(body: Value, requested: Option<&str>) -> Result<Read> {
    match projection::parse(
        &serde_json::to_vec(&body).unwrap(),
        &ticket(true),
        "en",
        &account(),
        requested,
    )? {
        Evidence::Review(r) => Ok(r),
        Evidence::RecordedHostname(_) => Err(changed()),
    }
}
#[test]
fn reads_check_the_machine_and_session_before_projecting_or_freezing_any_target() {
    for (key, value) in [
        ("mode", json!("redirect")),
        ("device_name", json!("other")),
        ("account_subject", json!("same-email-different-id")),
        ("account_email", json!("else@example.test")),
        ("expires_at", json!("2098-07-08T15:24:43.124Z")),
        ("locale", json!("zh")),
        ("claim_id", json!("unsolicited")),
        ("existing_instance_id", json!("injected")),
    ] {
        let mut v = new(Some("pc"), Some("pc"), false);
        v[key] = value;
        assert!(parsed(v, None).is_err());
    }
    let mut missing = new(None, None, false);
    missing.as_object_mut().unwrap().remove("prefilled_prefix");
    assert!(parsed(missing, None).is_err());
    assert!(parsed(new(None, None, false), None).unwrap().target.is_none());
    assert!(parsed(new(Some("server-base"), Some("other"), false), None).is_err());
    assert!(parsed(new(Some("ab--bad"), Some("ab--bad"), false), None).is_err());
    assert!(parsed(new(Some("server-base"), Some("edited"), true), Some("edited"))
        .unwrap()
        .target
        .is_none());
    let t = parsed(new(Some("server-base"), Some("edited"), false), Some("edited"))
        .unwrap()
        .target
        .unwrap();
    assert_eq!(t.hostname, "edited-app.avibe.bot");
    assert!(t.existing_id.is_none());
    assert_eq!(
        parsed(existing(), None).unwrap().target.unwrap().existing_id.as_deref(),
        Some("fixed-fixture-id")
    );
    let mut wrong = existing();
    wrong["hostname"] = json!("old-legacy-app.avibe.bot.example.test");
    assert!(parsed(wrong, None).is_err());
    for hint in ["***", "张***@e***"] {
        let read = parsed(envelope(json!({"state":"other_account","owner_hint":hint})), None).unwrap();
        assert!(matches!(read.screen, Screen::OtherAccount));
        assert!(read.target.is_none());
    }
    for hint in ["secret@example.test", "xx***@e***", "\n***@e***"] {
        assert!(parsed(envelope(json!({"state":"other_account","owner_hint":hint})), None).is_err());
    }
}
#[test]
fn distinct_hosted_capacity_fields_never_turn_a_blocked_name_into_a_target() {
    let mut own = new(Some("waiting"), Some("waiting"), false);
    own["holding"] = holding();
    own["reclaimable_names"] = eligible();
    own["requested_name_state"] = json!("own_reclaimable");
    assert_eq!(
        parsed(own.clone(), None).unwrap().target.unwrap().hostname,
        "waiting-app.avibe.bot"
    );
    for (key, value) in [
        ("reclaimable_names", json!([])),
        ("requested_name_state", json!("public")),
        ("taken", json!(true)),
        ("holding", Value::Null),
    ] {
        let mut v = own.clone();
        v[key] = value;
        assert!(parsed(v, None).is_err());
    }
    let mut cap = json!({"error":"name_holding_limit_reached","limit":5,"held_count":6,"earliest_release_at":null,"holding":holding(),"reclaimable_names":eligible(),"prefilled_prefix":"desired","requested_name_state":"public"});
    let parsed = projection::capacity(&serde_json::to_vec(&cap).unwrap(), None).unwrap();
    assert!(
        matches!(parsed.screen,Screen::Capacity{names:Some(n),..} if n.eligible.len()==1 && n.holding.held_count==6)
    );
    assert!(parsed.target.is_none());
    cap["reclaimable_names"][0]["release_at"] = json!("private_canary");
    assert!(projection::capacity(&serde_json::to_vec(&cap).unwrap(), None).is_err());
}

struct Step(&'static str, Value, u16, Value);
struct Issuer {
    steps: Mutex<VecDeque<Step>>,
    retire_on_grant: Option<Arc<AtomicU64>>,
}
#[async_trait::async_trait]
impl Transport for Issuer {
    async fn send(&self, r: Request) -> Result<Response> {
        let Step(path, body, status, reply) = self.steps.lock().unwrap().pop_front().expect("unscheduled request");
        assert_eq!(r.url.as_str(), format!("https://avibe.bot{path}"));
        assert_eq!(r.headers[COOKIE], "session=private-fixture");
        assert!(!r.headers.contains_key(AUTHORIZATION));
        if path == "/api/mobile/session" {
            assert_eq!(r.method, Method::GET);
            assert!(r.body.is_none());
        } else {
            assert_eq!(r.method, Method::POST);
            assert_eq!(r.headers[ORIGIN], "https://avibe.bot");
            assert_eq!(r.headers[CONTENT_TYPE], "application/json");
            assert_eq!(r.body, Some(body));
        }
        if path == GRANT {
            if let Some(e) = &self.retire_on_grant {
                e.fetch_add(1, Ordering::SeqCst);
            }
        }
        let mut headers = HeaderMap::new();
        if path == READ && status == 409 && reply["error"] == "name_holding_limit_reached" {
            headers.insert(
                SET_COOKIE,
                "session=fixture-refresh; Secure; HttpOnly; Path=/".parse().unwrap(),
            );
        } else if path == SESSION {
            headers.insert(
                SET_COOKIE,
                "session=discovery-only; Secure; HttpOnly; Path=/".parse().unwrap(),
            );
        }
        Ok(Response {
            status,
            headers,
            body: serde_json::to_vec(&reply).unwrap(),
        })
    }
}
struct Setup {
    claim: HostedClaim,
    flow: AccountFlow,
    state: AccountState,
    issuer: Arc<Issuer>,
    arm: oneshot::Receiver<super::super::ArmRequest>,
    finish: oneshot::Sender<Result<MachineOutcome>>,
    adoption: watch::Sender<bool>,
    epoch: Arc<AtomicU64>,
}
fn setup(steps: Vec<Step>, ack: bool, retire: bool) -> Setup {
    let live = Arc::new(AtomicU64::new(7));
    let e = Epoch::new(live.clone(), 7);
    let issuer = Arc::new(Issuer {
        steps: Mutex::new(steps.into()),
        retire_on_grant: retire.then(|| live.clone()),
    });
    let mut flow = AccountFlow::new(issuer.clone(), false);
    let origin = flow.session.origin.clone();
    let mut ordinary = NativeSession::new(origin.clone(), issuer.clone());
    ordinary.store_cookies(
        vec![
            cookie::Cookie::parse("session=private-fixture; Secure; HttpOnly; Path=/")
                .unwrap()
                .into_owned(),
        ],
        &origin,
    );
    flow.session = NativeSession::restore_account(issuer.clone(), &ordinary.persist().unwrap()).unwrap();
    let state = AccountState {
        status: AccountStatus::SignedIn,
        email: Some(account().email),
        identity: Some(account()),
        gate: None,
        methods: vec![],
    };
    let (send, arm) = oneshot::channel();
    let (finish, result) = oneshot::channel();
    let (adoption, adoption_rx) = watch::channel(true);
    let m = MachineAttempt {
        ticket: ticket(ack),
        subject: account().id,
        locale: "en".into(),
        adoption: adoption_rx,
        arm: send,
        result,
    };
    let claim = HostedClaim::new(m, e.clone(), e, &state).unwrap();
    Setup {
        claim,
        flow,
        state,
        issuer,
        arm,
        finish,
        adoption,
        epoch: live,
    }
}
fn read_step(v: Value) -> Step {
    Step(READ, json!({"user_code":CODE}), 200, v)
}
fn grant_body(p: &str, id: Option<&str>) -> Value {
    json!({"user_code":CODE,"prefix":p,"mode":"desktop","confirm_device":true,"expected_subject":"fixture-a","existing_instance_id":id})
}
impl Setup {
    async fn read(&mut self, r: Uuid) -> Result<()> {
        self.claim.read(&mut self.flow, &self.state, r, None).await
    }
    async fn confirm(&mut self, r: Uuid) -> Result<GrantResponse> {
        self.claim.confirm(&mut self.flow, &self.state, r).await
    }
    fn empty(&self) {
        assert!(self.issuer.steps.lock().unwrap().is_empty());
    }
}
#[tokio::test]
async fn only_the_latest_affirmed_revision_can_dispatch_and_arm_once_for_its_exact_target() {
    for (read, p, id, ack) in [
        (
            new(Some("server-prefill"), Some("server-prefill"), false),
            "server-prefill",
            None,
            false,
        ),
        (existing(), "old-legacy", Some("fixed-fixture-id"), true),
    ] {
        let h = format!("{p}-app.avibe.bot");
        let mut s = setup(
            vec![
                read_step(read),
                Step(GRANT, grant_body(p, id), 200, json!({"status":"granted","hostname":h})),
            ],
            ack,
            false,
        );
        let start = s.claim.revision();
        s.read(start).await.unwrap();
        let revision = s.claim.revision();
        assert!(s.confirm(start).await.is_err());
        let GrantResponse::Matched(matched) = s.confirm(revision).await.unwrap() else {
            panic!("expected hosted grant");
        };
        assert!(matches!(s.arm.try_recv(), Err(oneshot::error::TryRecvError::Empty)));
        assert!(s.confirm(revision).await.is_err());
        let completion = matched.dispatch(revision, &account()).unwrap();
        s.empty();
        let frame = s.arm.await.unwrap().live_value().unwrap();
        // The helper hasn't finished. Both the grant and the arm enqueue already returned.
        s.finish.send(Ok(MachineOutcome::Declined)).ok().unwrap();
        assert!(matches!(completion.wait().await.unwrap(), MachineOutcome::Declined));
        assert_eq!(
            frame,
            json!({"v":1,"op":"arm","claim_id":ID,"kind":if id.is_some(){"existing"}else{"new"},"hostname":h,"existing_instance_id":id})
        );
    }
}
#[tokio::test(start_paused = true)]
async fn matched_http_cannot_arm_a_retired_expired_or_retargeted_review() {
    for change in [
        "revision",
        "account",
        "expiry",
        "adoption",
        "before_enqueue",
        "before_write",
    ] {
        let mut s = setup(
            vec![
                read_step(existing()),
                Step(
                    GRANT,
                    grant_body("old-legacy", Some("fixed-fixture-id")),
                    200,
                    json!({"status":"granted","hostname":"old-legacy-app.avibe.bot"}),
                ),
            ],
            true,
            false,
        );
        s.read(s.claim.revision()).await.unwrap();
        let revision = s.claim.revision();
        let GrantResponse::Matched(matched) = s.confirm(revision).await.unwrap() else {
            panic!("expected hosted grant");
        };
        let mut a = account();
        let mut r = revision;
        match change {
            "revision" => r = Uuid::new_v4(),
            "account" => a.id = "other-with-same-email".into(),
            "adoption" => {
                s.adoption.send_replace(false);
            }
            "expiry" => tokio::time::advance(Duration::from_secs(61)).await,
            "before_enqueue" => {
                s.epoch.fetch_add(1, Ordering::SeqCst);
            }
            _ => (),
        }
        s.empty();
        let queued = matched.dispatch(r, &a);
        if change == "before_write" {
            let _completion = queued.unwrap();
            s.epoch.fetch_add(1, Ordering::SeqCst);
            assert!(s.arm.await.unwrap().live_value().is_err());
        } else {
            assert!(queued.is_err());
            assert!(matches!(s.arm.try_recv(), Err(oneshot::error::TryRecvError::Closed)));
        }
    }
}
#[tokio::test]
async fn missing_ack_changed_generation_and_unproven_recorded_target_never_arm_or_repost() {
    let mut s = setup(vec![read_step(existing())], false, false);
    s.read(s.claim.revision()).await.unwrap();
    assert_eq!(
        s.confirm(s.claim.revision()).await.err().unwrap().code,
        "local_runtime_upgrade_required"
    );
    s.empty();
    assert!(s.arm.try_recv().is_err());
    for (status, response, retire) in [
        (
            200,
            json!({"status":"granted","hostname":"old-legacy-app.avibe.bot"}),
            true,
        ),
        (503, json!({"error":"private_canary"}), false),
        (
            403,
            json!({"error":"first_factor_reauthentication_required", "unexpected":true}),
            false,
        ),
        (
            200,
            json!({"status":"bound","hostname":"old-legacy-app.avibe.bot"}),
            false,
        ),
    ] {
        let mut steps = vec![
            read_step(existing()),
            Step(
                GRANT,
                grant_body("old-legacy", Some("fixed-fixture-id")),
                status,
                response,
            ),
        ];
        if !retire {
            steps.push(read_step(envelope(
                json!({"state":"granted","hostname":"old-legacy-app.avibe.bot"}),
            )));
        }
        let mut s = setup(steps, true, retire);
        s.read(s.claim.revision()).await.unwrap();
        assert_eq!(
            s.confirm(s.claim.revision()).await.err().unwrap().code,
            if retire {
                "stale_generation"
            } else {
                "fresh_claim_required"
            }
        );
        assert!(s.confirm(s.claim.revision()).await.is_err());
        assert!(s.arm.try_recv().is_err());
        s.empty();
    }
}
#[tokio::test]
async fn origin_subject_and_unsolicited_grant_cannot_supply_a_review() {
    let mut s = setup(vec![], true, false);
    s.flow.session.origin = "https://avibe.bot.example.test/".parse().unwrap();
    assert!(s.read(s.claim.revision()).await.is_err());
    s.flow.session.origin = CONTROL_PLANE.parse().unwrap();
    s.state.identity.as_mut().unwrap().id = "different-id".into();
    assert!(s.read(s.claim.revision()).await.is_err());
    s.empty();
    let mut s = setup(
        vec![read_step(envelope(
            json!({"state":"granted","hostname":"old-legacy-app.avibe.bot"}),
        ))],
        true,
        false,
    );
    assert!(s.read(s.claim.revision()).await.is_err());
    assert!(s.confirm(s.claim.revision()).await.is_err());
    s.empty();
}
#[tokio::test]
async fn a_capacity_inventory_has_no_account_id_so_check_the_original_cookie_principal() {
    let cap = json!({"error":"name_holding_limit_reached","limit":5,"held_count":6,"earliest_release_at":null,"holding":holding(),"reclaimable_names":eligible(),"prefilled_prefix":"public","requested_name_state":"public"});
    for (subject, success) in [("fixture-a", true), ("other-id", false)] {
        let mut s = setup(
            vec![
                Step(READ, json!({"user_code":CODE}), 409, cap.clone()),
                Step(
                    "/api/mobile/session",
                    Value::Null,
                    200,
                    json!({"account":{"id":subject,"email":"fixture@example.test"},"instances":[]}),
                ),
            ],
            true,
            false,
        );
        assert_eq!(s.read(s.claim.revision()).await.is_ok(), success);
        assert_eq!(
            s.flow.session.cookie_value("session").as_deref(),
            Some(if success { "fixture-refresh" } else { "private-fixture" })
        );
        if success {
            let view = s.claim.screen(&s.flow, &s.state).unwrap();
            assert!(!view.can_confirm);
            assert!(matches!(view.screen,Screen::Capacity{names:Some(n),..} if n.eligible[0].prefix=="waiting"));
        } else {
            assert!(s.claim.screen(&s.flow, &s.state).is_err());
        }
        assert!(s.confirm(s.claim.revision()).await.is_err());
        s.empty();
    }
    // Two snapshots of the same account origin model an ordinary cookie expiring
    // between POST and GET without sleeping on the wall clock. The first POST is
    // sent; the GET must refuse before transport, even though both paths now agree.
    let mut s = setup(
        vec![Step(READ, json!({"user_code":CODE}), 409, cap.clone())],
        true,
        false,
    );
    let mut posting = s.flow.session.clone();
    let sent = posting.claim_read_cookies().unwrap();
    s.flow.session = NativeSession::account(s.issuer.clone());
    assert_eq!(
        s.claim
            .read_ticket(&mut posting, &s.flow, &s.state, None, Some(&sent))
            .await
            .err()
            .unwrap()
            .code,
        "decision_changed"
    );
    s.empty();

    // Neither an unrecognized body nor a discovery-only cookie permits holder details
    // or adopting the error's refresh. Only the single original POST is scheduled.
    for different_paths in [false, true] {
        let reply = if different_paths {
            cap.clone()
        } else {
            json!({"error":"name_holding_limit_reached"})
        };
        let mut s = setup(vec![Step(READ, json!({"user_code":CODE}), 409, reply)], true, false);
        if different_paths {
            let origin = s.flow.session.origin.clone();
            let mut ordinary =
                NativeSession::restore(origin.clone(), s.issuer.clone(), &s.flow.session.persist().unwrap()).unwrap();
            ordinary.store_cookies(
                vec![cookie::Cookie::parse("narrow=fixture; Secure; Path=/api/mobile")
                    .unwrap()
                    .into_owned()],
                &origin,
            );
            s.flow.session = NativeSession::restore_account(s.issuer.clone(), &ordinary.persist().unwrap()).unwrap();
        }
        s.read(s.claim.revision()).await.unwrap();
        let view = s.claim.screen(&s.flow, &s.state).unwrap();
        assert!(
            matches!(
                view.screen,
                Screen::Capacity {
                    names: None,
                    prefilled: None
                }
            ) && !view.can_confirm
        );
        assert_eq!(
            s.flow.session.cookie_value("session").as_deref(),
            Some("private-fixture")
        );
        assert!(s.confirm(s.claim.revision()).await.is_err());
        s.empty();
    }
}
#[tokio::test]
async fn older_edit_and_second_click_cannot_submit_even_after_a_definite_proof_refusal() {
    let mut s = setup(
        vec![
            read_step(new(Some("first"), Some("first"), false)),
            Step(
                READ,
                json!({"user_code":CODE,"prefix":"second"}),
                200,
                new(Some("first"), Some("second"), false),
            ),
            Step(
                GRANT,
                grant_body("second", None),
                403,
                json!({"error":"first_factor_reauthentication_required"}),
            ),
        ],
        false,
        false,
    );
    s.read(s.claim.revision()).await.unwrap();
    let first = s.claim.revision();
    s.claim
        .read(&mut s.flow, &s.state, first, Some("second"))
        .await
        .unwrap();
    let second = s.claim.revision();
    assert!(s.confirm(first).await.is_err());
    assert!(s.claim.read(&mut s.flow, &s.state, first, Some("stale")).await.is_err());
    let GrantResponse::ProofRequired(proof) = s.confirm(second).await.unwrap() else {
        panic!("refusal is not a grant");
    };
    assert_eq!(proof.refused_revision(), second);
    assert_eq!(proof.target.prefix, "second");
    assert!(proof.target.existing_id.is_none());
    assert!(proof.check(&account()).is_ok());
    let mut other = account();
    other.id = "same-email-different-id".into();
    assert!(proof.check(&other).is_err());
    assert!(matches!(s.arm.try_recv(), Err(oneshot::error::TryRecvError::Empty)));
    assert!(s.confirm(second).await.is_err());
    assert!(s.confirm(s.claim.revision()).await.is_err());
    assert!(s.arm.try_recv().is_err());
    s.empty();
}
#[test]
fn legacy_binding_refusals_and_private_diagnostics_are_not_actionable_evidence() {
    let reason = json!({"error":"binding_unavailable","reason":"paired_installation_loopback_required"});
    for (body, grant, expected) in [
        (reason.clone(), true, Some("paired_installation_loopback_required")),
        (reason, false, Some("binding_unavailable")),
        (
            json!({"error":"binding_unavailable"}),
            true,
            Some("binding_unavailable"),
        ),
        (
            json!({"error":"binding_unavailable","reason":"paired_installation_loopback_required","extra":"private_canary"}),
            true,
            Some("binding_unavailable"),
        ),
        (json!({"error":"other_account"}), true, Some("binding_unavailable")),
        (json!({"error":"private_canary"}), true, None),
    ] {
        let r = Response {
            status: 409,
            headers: HeaderMap::new(),
            body: serde_json::to_vec(&body).unwrap(),
        };
        assert_eq!(refusal(&r, grant).as_ref().map(|e| e.code.as_str()), expected);
    }
}
