//! AUTH-SETUP-171. The actual coordinator owns the document, the revision and
//! the HTTP grant. Hold the two external responses independently. No UI,
//! platform credentials, actual helper process, network, or user profile.
use super::super::{decision::DecisionOutcome, *};
use crate::instance_client::{
    account::Passkeys,
    coordinator::ClientView,
    machine::tests::peer,
    protocol::{ClientState, ClientTarget},
    storage::{ConnectionStore, Credentials},
    transport::{NativeSession, Request, Response, Transport},
};
mod enroll;
mod factor;
mod observation;
mod output;
mod passkey;
mod recovery;
mod review;
mod save;
use tokio::sync::Notify;

struct NoPlatform;
impl Credentials for NoPlatform {
    fn read(&self, _: &str) -> Result<Option<Vec<u8>>> {
        panic!("unexpected credential read")
    }
    fn write(&self, _: &str, _: &[u8]) -> Result<()> {
        panic!("unexpected credential write")
    }
    fn erase(&self, _: &str) -> Result<()> {
        panic!("unexpected credential erase")
    }
}
#[async_trait::async_trait]
impl Passkeys for NoPlatform {
    async fn assert(&self, _: Value, _: Epoch) -> Result<Value> {
        panic!("unexpected passkey request")
    }
}
#[async_trait::async_trait]
impl ClientView for NoPlatform {
    fn changed(&self, _: &ClientState) {
        panic!("private decision broadcast")
    }
    fn invalidate(&self, _: u64) {}
    async fn commit(&self, _: Epoch, _: ClientTarget, _: Vec<cookie::Cookie<'static>>) -> Result<()> {
        panic!("unexpected view commit")
    }
    async fn restore(&self, _: Epoch, _: ClientTarget, _: bool) -> Result<()> {
        panic!("unexpected view restore")
    }
    async fn signed_out(&self, _: Epoch, _: ClientTarget, _: bool) -> Result<()> {
        panic!("unexpected view change")
    }
}
struct Hosted {
    expiry: String,
    grant: Mutex<Option<oneshot::Receiver<()>>>,
    sent: Notify,
    require_proof: bool,
    config: Mutex<Option<oneshot::Receiver<()>>>,
    discovering: Notify,
    mails: AtomicU64,
    start: Mutex<Option<oneshot::Receiver<()>>>,
    starting: Notify,
    forward: Mutex<Option<oneshot::Receiver<()>>>,
    forwarding: Notify,
    reads: AtomicU64,
    unknown: bool,
    review: Option<&'static str>,
    grants: AtomicU64,
    publication_case: &'static str,
    owner_reads: AtomicU64,
    observing: Notify,
    resume_observation: Notify,
    ticket_reads: AtomicU64,
    factors: AtomicU64,
    factor_entered: Notify,
    factor_release: Notify,
    setups: AtomicU64,
    setup_entered: Notify,
    setup_release: Notify,
    platform: passkey::Platform,
}
#[async_trait::async_trait]
impl Transport for Hosted {
    async fn send(&self, req: Request) -> Result<Response> {
        let mut status = 200;
        let mut headers = reqwest::header::HeaderMap::new();
        let raced_name = self.publication_case.starts_with("name_race_");
        let owed = self.review.is_some_and(|s| s.starts_with("factor_"));
        let verified = owed && self.factors.load(Ordering::SeqCst) != 0;
        let ordinary = if verified {
            "ordinary=after-factor"
        } else {
            "ordinary=new"
        };
        let publication = req.url.path() == "/api/mobile/session"
            && self.grants.load(Ordering::SeqCst) >= if self.require_proof { 2 } else { 1 };
        if publication {
            assert_eq!(self.owner_reads.fetch_add(1, Ordering::SeqCst), 0);
            assert_eq!(req.url.as_str(), "https://avibe.bot/api/mobile/session");
            assert_eq!(req.method, reqwest::Method::GET);
            assert!(req.body.is_none());
            assert_eq!(
                req.headers[reqwest::header::COOKIE],
                if self.require_proof {
                    ordinary
                } else {
                    "session=synthetic"
                }
            );
            assert!(!req.headers.contains_key(reqwest::header::AUTHORIZATION));
            headers.insert(
                reqwest::header::SET_COOKIE,
                "ordinary=discard; Secure; Path=/".parse().unwrap(),
            );
            if self.publication_case == "publication_read_changed" {
                self.observing.notify_one();
                self.resume_observation.notified().await;
            }
            let rows = match self.publication_case {
                "publication_offline" | "publication_error_replaced" => {
                    return Err(ClientError::new("network_unavailable"))
                }
                "publication_shared" => {
                    json!([{ "id":"fixture-published", "relationship":"shared" }, {"id":"other", "relationship":"owner", "host":"desk-app.avibe.bot"}])
                }
                "publication_absent" => json!([]),
                _ => json!([{ "id":"fixture-published", "relationship":"owner" }]),
            };
            let body = json!({"account":{"id":"fixture-a","email":"fixture@example.test"},"instances":rows});
            return Ok(Response {
                status: 200,
                headers,
                body: serde_json::to_vec(&body).unwrap(),
            });
        }
        let claims_cookie = match req.url.path() {
            "/api/mobile/session" if self.publication_case.starts_with("read_refresh_") => {
                assert_eq!(req.url.as_str(), "https://avibe.bot/api/mobile/session");
                assert_eq!(req.method, reqwest::Method::GET);
                assert!(req.body.is_none());
                assert_eq!(self.reads.fetch_add(1, Ordering::SeqCst), 0);
                assert_eq!(
                    req.headers[reqwest::header::COOKIE],
                    if self.publication_case == "read_refresh_before_reply" {
                        "session=synthetic"
                    } else {
                        "session=from-review"
                    }
                );
                headers.insert(
                    reqwest::header::SET_COOKIE,
                    "session=successor; Path=/; Secure".parse().unwrap(),
                );
                return Ok(Response {
                    status: 200,
                    headers,
                    body: serde_json::to_vec(
                        &json!({"account":{"id":"fixture-a","email":"fixture@example.test"},"instances":[]}),
                    )
                    .unwrap(),
                });
            }
            "/api/v1/claims/read" => Some(
                if self.ticket_reads.fetch_add(1, Ordering::SeqCst) == 0
                    || raced_name
                    || self.publication_case.starts_with("check_error_")
                {
                    "session=synthetic"
                } else {
                    ordinary
                },
            ),
            "/api/v1/claims/grant" => Some(if self.grants.fetch_add(1, Ordering::SeqCst) == 0 || raced_name {
                "session=synthetic"
            } else if verified {
                "ordinary=after-factor; __Host-avibe-sign-in-proof=fixture.proof"
            } else {
                "ordinary=new; __Host-avibe-sign-in-proof=fixture.proof"
            }),
            _ => None,
        };
        if let Some(expected) = claims_cookie {
            assert_eq!(req.headers[reqwest::header::COOKIE], expected);
        } else if req.url.path() == "/api/auth/first-party/email-code" {
            assert_eq!(
                req.headers[reqwest::header::COOKIE],
                "__Host-avibe-sign-in-start=fixture"
            );
        } else if req.url.path() == "/api/mobile/session" {
            assert_eq!(req.method, reqwest::Method::GET);
            assert!(req.body.is_none());
            assert_eq!(req.headers[reqwest::header::COOKIE], ordinary);
        } else if matches!(
            req.url.path(),
            "/api/two-factor/verify"
                | "/api/two-factor/passkey-challenge"
                | "/api/two-factor/totp/setup"
                | "/api/two-factor/totp"
        ) {
            assert_eq!(req.headers[reqwest::header::COOKIE], "ordinary=new");
        } else {
            assert!(!req.headers.contains_key(reqwest::header::COOKIE));
        }
        let body = match req.url.as_str() {
            "https://avibe.bot/api/v1/claims/read" => {
                let again = self.ticket_reads.load(Ordering::SeqCst) == 2;
                assert_eq!(
                    req.body,
                    Some(if again {
                        json!({"user_code":"ABCD234567","prefix":if raced_name {"spare"}else{"desk"}})
                    } else {
                        json!({"user_code":"ABCD234567"})
                    })
                );
                if again && self.publication_case.starts_with("check_error_") {
                    // Valid refusal with unprojectable extra fields, or malformed hosted JSON.
                    return Ok(Response {
                        status: 403,
                        headers,
                        body: if self.publication_case == "check_error_malformed" {
                            b"fixture-private-diagnostic: not JSON".to_vec()
                        } else {
                            serde_json::to_vec(&json!({"error":"two_factor_required", "detail":"fixture-private-diagnostic", "retry_after_seconds":47})).unwrap()
                        },
                    });
                }
                if again {
                    assert!(raced_name || self.review.is_some());
                }
                let mut projection = json!({"mode":"desktop","locale":"en","expires_at":self.expiry,"device_name":"fixture computer","account_subject":"fixture-a","account_email":"fixture@example.test","state":"new","prefilled_prefix":"desk","prefix":"desk","taken":false});
                if again && raced_name {
                    projection["prefix"] = json!("spare");
                }
                if self.publication_case.starts_with("read_refresh_") {
                    headers.insert(
                        reqwest::header::SET_COOKIE,
                        "session=from-review; Path=/; Secure".parse().unwrap(),
                    );
                    if self.publication_case == "read_refresh_before_reply" {
                        self.observing.notify_one();
                        self.resume_observation.notified().await;
                    }
                }
                if self.publication_case.starts_with("conflict_") {
                    headers.insert(
                        reqwest::header::SET_COOKIE,
                        "session=discard; Path=/; Secure".parse().unwrap(),
                    );
                    return Ok(Response { status, headers, body: serde_json::to_vec(&json!({
                        "mode":"desktop", "locale":"en", "expires_at":self.expiry, "device_name":"fixture computer",
                        "account_subject":"fixture-a", "account_email":"fixture@example.test", "state":"other_account",
                        "owner_hint":"x***@y***"
                    })).unwrap() });
                }
                if again && self.review == Some("target") {
                    let v = projection.as_object_mut().unwrap();
                    v.remove("prefilled_prefix");
                    v.remove("taken");
                    v.insert("state".into(), json!("existing"));
                    v.insert("hostname".into(), json!("desk-app.avibe.bot"));
                    v.insert("existing_instance_id".into(), json!("other-fixture"));
                }
                projection
            }
            "https://avibe.bot/api/v1/claims/grant" => {
                let first = self.grants.load(Ordering::SeqCst) == 1;
                assert_eq!(
                    req.body,
                    Some(
                        json!({"user_code":"ABCD234567","mode":"desktop","confirm_device":true,"prefix":if raced_name && !first {"spare"}else{"desk"},"expected_subject":"fixture-a","existing_instance_id":null})
                    )
                );
                if first {
                    let wait = self.grant.lock().unwrap().take().expect("first grant");
                    self.sent.notify_one();
                    wait.await.map_err(|_| stale())?;
                } else {
                    assert!(
                        raced_name
                            || matches!(
                                self.review,
                                Some("ok" | "refused" | "factor_ok" | "factor_recovery" | "factor_pk_ok")
                            )
                            || self.review.is_some_and(|r| r.starts_with("factor_enroll_"))
                    );
                    assert_eq!(self.grants.load(Ordering::SeqCst), 2);
                }
                if first && raced_name {
                    status = 409;
                    headers.insert(
                        reqwest::header::SET_COOKIE,
                        "session=unattributed; Secure; Path=/".parse().unwrap(),
                    );
                    json!({"error":"taken"})
                } else if self.publication_case.starts_with("grant_denied_") {
                    status = 409;
                    json!({"error":"binding_unavailable", "reason":"paired_installation_loopback_required"})
                } else if (first && self.require_proof) || self.review == Some("refused") {
                    status = 403;
                    if self.publication_case == "proof_offer_refreshed" {
                        headers.insert(
                            reqwest::header::SET_COOKIE,
                            "session=from-refusal; Secure; Path=/".parse().unwrap(),
                        );
                    }
                    json!({"error":"first_factor_reauthentication_required"})
                } else {
                    json!({"status":"granted","hostname":if raced_name {"spare-app.avibe.bot"}else{"desk-app.avibe.bot"}})
                }
            }
            "https://avibe.bot/api/config/auth" if self.require_proof => {
                let wait = self.config.lock().unwrap().take().expect("replayed discovery");
                self.discovering.notify_one();
                wait.await.map_err(|_| stale())?;
                json!({"supabase_url":"https://issuer.test","supabase_anon_key":"fixture"})
            }
            _ if self.require_proof
                && req.url.origin().ascii_serialization() == "https://issuer.test"
                && req.url.path() == "/auth/v1/otp" =>
            {
                assert_eq!(
                    req.body,
                    Some(json!({"email":"fixture@example.test","create_user":false}))
                );
                assert_eq!(self.mails.fetch_add(1, Ordering::SeqCst), 0);
                if self.publication_case.starts_with("proof_mail_limited") {
                    status = 429;
                    headers.insert(reqwest::header::RETRY_AFTER, "47".parse().unwrap());
                }
                json!({})
            }
            "https://avibe.bot/api/auth/first-party/start" => {
                assert_eq!(
                    req.body,
                    Some(json!({"kind":"email_code", "email":"fixture@example.test", "expected_subject":"fixture-a"}))
                );
                let wait = self.start.lock().unwrap().take().expect("start replayed");
                self.starting.notify_one();
                wait.await.map_err(|_| stale())?;
                headers.insert(
                    reqwest::header::SET_COOKIE,
                    "__Host-avibe-sign-in-start=fixture; Secure; HttpOnly; SameSite=Lax; Path=/; Max-Age=600"
                        .parse()
                        .unwrap(),
                );
                json!({"transaction":"t".repeat(43)})
            }
            "https://avibe.bot/api/auth/first-party/email-code" => {
                assert_eq!(
                    req.body,
                    Some(json!({"email":"fixture@example.test","transaction":"t".repeat(43),"code":"000001"}))
                );
                let wait = self.forward.lock().unwrap().take().expect("code replayed");
                self.forwarding.notify_one();
                wait.await.map_err(|_| stale())?;
                if self.unknown {
                    return Err(ClientError::new("network_unavailable"));
                }
                headers.insert(
                    reqwest::header::SET_COOKIE,
                    "ordinary=new; Path=/; Secure; HttpOnly".parse().unwrap(),
                );
                headers.append(
                    reqwest::header::SET_COOKIE,
                    "__Host-avibe-sign-in-proof=fixture.proof; Secure; HttpOnly; SameSite=Lax; Path=/; Max-Age=600"
                        .parse()
                        .unwrap(),
                );
                json!({"bound":true})
            }
            "https://avibe.bot/api/two-factor/totp" => {
                assert_eq!(req.method, reqwest::Method::POST);
                assert_eq!(req.body, Some(json!({"token":"fixture-setup-token", "code":"000002"})));
                assert_eq!(self.factors.fetch_add(1, Ordering::SeqCst), 0, "enrollment replay");
                self.factor_entered.notify_one();
                self.factor_release.notified().await;
                if matches!(
                    self.review,
                    Some("factor_enroll_unknown" | "factor_enroll_unknown_late")
                ) {
                    return Err(ClientError::new("network_unavailable"));
                }
                status = 201;
                headers.insert(
                    reqwest::header::SET_COOKIE,
                    "ordinary=after-factor; Path=/; Secure; HttpOnly".parse().unwrap(),
                );
                let mut result = json!({"method":{"kind":"totp", "id":"tfm_fixture", "name":"Authenticator app", "createdAt":"2026-11-22T00:00:00Z", "lastUsedAt":null}});
                if self.review != Some("factor_enroll_absent") {
                    result["recoveryCodes"] = if self.review == Some("factor_enroll_bad_codes") {
                        json!(["unexpected upstream data"])
                    } else {
                        json!(enroll::codes())
                    };
                }
                result
            }
            "https://avibe.bot/api/two-factor/totp/setup" => {
                assert_eq!(req.method, reqwest::Method::POST);
                assert_eq!(req.body, Some(json!({})));
                assert_eq!(self.setups.fetch_add(1, Ordering::SeqCst), 0);
                self.setup_entered.notify_one();
                self.setup_release.notified().await;
                headers.insert(
                    reqwest::header::SET_COOKIE,
                    "ordinary=setup-only; Path=/; Secure; HttpOnly".parse().unwrap(),
                );
                json!({"token":"fixture-setup-token", "secret": if self.review == Some("factor_setup_invalid") {"not a base32 key"} else {enroll::KEY},
                    "uri":"https://unused.invalid/untrusted-setup-uri", "qrSvg":"<svg>untrusted-setup-markup</svg>"})
            }
            "https://avibe.bot/api/two-factor/passkey-challenge" => {
                assert_eq!(req.method, reqwest::Method::POST);
                assert_eq!(req.body, Some(json!({})));
                assert_eq!(self.platform.challenges.fetch_add(1, Ordering::SeqCst), 0);
                self.platform.challenging.notify_one();
                self.platform.issue.notified().await;
                // Challenge cookies must never replace the ordinary credential.
                headers.insert(
                    reqwest::header::SET_COOKIE,
                    "ordinary=unused; Path=/; Secure; HttpOnly".parse().unwrap(),
                );
                let mut options = passkey::options();
                if self.review == Some("factor_pk_nolist") {
                    options.as_object_mut().unwrap().remove("allowCredentials");
                }
                json!({"options": options, "token":"fixture-factor-token"})
            }
            "https://avibe.bot/api/two-factor/verify" => {
                assert!(owed);
                assert_eq!(req.method, reqwest::Method::POST);
                assert_eq!(self.factors.fetch_add(1, Ordering::SeqCst), 0, "MFA replay");
                assert_eq!(
                    req.body,
                    Some(if self.review.is_some_and(|r| r.starts_with("factor_pk_")) {
                        json!({"method":"passkey","token":"fixture-factor-token","response":passkey::assertion()})
                    } else if self.review == Some("factor_recovery") {
                        json!({"method":"recovery","code":"fixture-backup"})
                    } else {
                        json!({"method":"totp","code":"000002"})
                    })
                );
                self.factor_entered.notify_one();
                self.factor_release.notified().await;
                if matches!(self.review, Some("factor_unknown" | "factor_unknown_late")) {
                    return Err(ClientError::new("network_unavailable"));
                }
                if self.review == Some("factor_malformed") {
                    return Ok(Response {
                        status: 200,
                        headers,
                        body: br#"{"ok":true,"error":"unexpected"}"#.to_vec(),
                    });
                }
                headers.insert(
                    reqwest::header::SET_COOKIE,
                    "ordinary=after-factor; Path=/; Secure; HttpOnly".parse().unwrap(),
                );
                json!({"ok":true})
            }
            "https://avibe.bot/api/mobile/session" => {
                let n = self.reads.fetch_add(1, Ordering::SeqCst);
                if n == 0 && !self.publication_case.starts_with("proof_start_auto_save") {
                    return Err(ClientError::new("network_unavailable"));
                }
                if owed && n == 1 {
                    status = 403;
                    if self
                        .review
                        .is_some_and(|r| r.starts_with("factor_setup_") || r.starts_with("factor_enroll_"))
                    {
                        if self.review == Some("factor_setup_path") {
                            headers.insert(
                                reqwest::header::SET_COOKIE,
                                "narrow=fixture; Path=/api/two-factor/totp/setup; Secure; HttpOnly"
                                    .parse()
                                    .unwrap(),
                            );
                        }
                        return Ok(Response { status, headers, body: serde_json::to_vec(&json!({"error":"two_factor_required", "email":"fixture@example.test", "gate":"enroll", "methods":[]})).unwrap() });
                    }
                    if self.review == Some("factor_pk_path") {
                        headers.insert(
                            reqwest::header::SET_COOKIE,
                            "narrow=fixture; Path=/api/two-factor/passkey-challenge; Secure; HttpOnly"
                                .parse()
                                .unwrap(),
                        );
                    } else if self.review == Some("factor_path") {
                        headers.insert(
                            reqwest::header::SET_COOKIE,
                            "narrow=fixture; Path=/api/two-factor; Secure; HttpOnly"
                                .parse()
                                .unwrap(),
                        );
                    }
                    return Ok(Response { status, headers, body: serde_json::to_vec(&json!({"error":"two_factor_required","email":"fixture@example.test","gate":"challenge","methods": if self.review.is_some_and(|r| r == "factor_recovery" || r.starts_with("factor_pk_")) {json!(["passkey"])} else {json!(["totp"])}})).unwrap() });
                }
                if owed && n == 2 {
                    return Err(ClientError::new("network_unavailable"));
                }
                if n == 2 && matches!(self.review, Some("mfa" | "mfa_late_credential" | "mfa_late_successor")) {
                    return Ok(Response {
                        status: 403,
                        headers,
                        body: serde_json::to_vec(&json!({"error":"two_factor_required"})).unwrap(),
                    });
                }
                let subject = if (n == 2 && self.review == Some("subject"))
                    || (n == 3 && self.review == Some("factor_subject"))
                {
                    "other-fixture"
                } else {
                    "fixture-a"
                };
                json!({"account":{"id":subject,"email":"fixture@example.test"},"instances":[]})
            }
            _ => panic!("unexpected route"),
        };
        Ok(Response {
            status,
            headers,
            body: serde_json::to_vec(&body).unwrap(),
        })
    }
}
#[tokio::test]
async fn only_a_released_review_can_submit_and_neither_external_wait_owns_a_coordinator_lock() {
    for cut in [
        "http",
        "busy_after_grant",
        "no_saved_owner_at_confirm",
        "machine",
        "publication_offline",
        "name_race_edit",
        "name_race_drop",
        "name_race_credential",
        "name_race_replaced",
        "grant_denied_live",
        "grant_denied_replaced",
        "decline_live",
        "decline_replaced",
        "decline_changed_jar",
        "publication_error_replaced",
        "publication_absent",
        "publication_shared",
        "publication_read_changed",
        "publication_cancel",
        "publication_changed_jar",
        "publication_ack_failed",
        "publication_drop",
        "delivery_changed_document",
        "delivery_successor",
        "delivery_changed_adoption",
        "complete",
        "proof_send",
        "proof_cancel",
        "proof_busy",
        "proof_start_cancel",
        "proof_start_busy",
        "proof_start_drop",
        "proof_start_no_owner",
        "proof_start_retired",
        "proof_start_bound",
        "proof_start_auto_save",
        "proof_start_auto_save_refused",
        "proof_start_save_drop",
        "proof_start_save_stale",
        "proof_start_save_mismatch",
        "proof_start_save_absent_prior",
        "proof_start_save_invalid_prior",
        "proof_start_save_rollback_late_error",
        "proof_start_save_changed_before_undo",
        "proof_start_save_rollback_error",
        "proof_start_unknown",
        "proof_start_unknown_live",
        "proof_start_unknown_credential",
        "proof_start_unknown_successor",
        "proof_start_auto_save_direct",
        "proof_start_auto_save_direct_changed",
        "proof_start_review_unsaved",
        "proof_start_review_ok",
        "proof_start_review_refused",
        "proof_start_review_subject",
        "proof_start_review_mfa",
        "proof_start_review_mfa_late_credential",
        "proof_start_review_mfa_late_successor",
        "proof_start_review_target",
        "proof_start_review_close",
        "proof_start_review_replaced",
        "proof_start_review_factor_ok",
        "proof_start_review_factor_recovery",
        "proof_start_review_factor_close",
        "proof_start_review_factor_subject",
        "proof_start_review_factor_unknown",
        "proof_start_review_factor_unknown_late",
        "proof_start_review_factor_malformed",
        "proof_start_review_factor_path",
        "proof_start_review_factor_pk_ok",
        "proof_start_review_factor_pk_cancel",
        "proof_start_review_factor_pk_drop",
        "proof_start_review_factor_pk_replaced",
        "proof_start_review_factor_pk_nolist",
        "proof_start_review_factor_pk_path",
        "proof_start_review_factor_pk_before_sheet",
        "proof_start_review_factor_pk_wrong_id",
        "proof_start_review_factor_pk_close",
        "proof_start_review_factor_setup_ok",
        "proof_start_review_factor_setup_cancel",
        "proof_start_review_factor_setup_replaced",
        "proof_start_review_factor_setup_drop",
        "proof_start_review_factor_setup_invalid",
        "proof_start_review_factor_setup_path",
        "proof_start_review_factor_enroll_ok",
        "proof_start_review_factor_enroll_absent",
        "proof_start_review_factor_enroll_bad_codes",
        "proof_start_review_factor_enroll_unknown",
        "proof_start_review_factor_enroll_unknown_late",
        "proof_start_review_factor_enroll_close",
        "proof_start_review_factor_enroll_drop",
        "proof_start_review_factor_enroll_replaced",
    ] {
        tokio::time::timeout(Duration::from_secs(5), check_cut(cut))
            .await
            .expect("decision did not release");
    }
}
#[tokio::test]
async fn ordinary_read_refresh_stays_with_its_account_transaction_and_document() {
    for cut in [
        "read_refresh_before_reply",
        "read_refresh_before_delivery",
        "read_refresh_delivered",
        "read_refresh_cancel_replaced",
    ] {
        tokio::time::timeout(Duration::from_secs(5), check_cut(cut))
            .await
            .unwrap();
    }
}

async fn refresh_same_generation(c: &Coordinator, memory: &save::Memory, generation: u64) {
    use crate::instance_client::protocol::{ClientAction, ClientRequest};
    memory.allow_one_save();
    let reply = c
        .request(ClientRequest {
            version: 1,
            request_id: "refresh-fixture".into(),
            expected_generation: generation,
            action: ClientAction::RefreshInstances,
        })
        .await;
    assert!(reply.ok && reply.state.generation == generation);
    assert_eq!(reply.state.account.identity.unwrap().id, "fixture-a");
    let stored = memory.read("fixture-a").unwrap().unwrap();
    assert_eq!(
        NativeSession::restore_account(c.transport.clone(), &stored)
            .unwrap()
            .cookie_value("session")
            .as_deref(),
        Some("successor")
    );
}

#[tokio::test]
async fn owner_conflict_is_only_a_read_by_the_original_credential_in_the_original_document() {
    for cut in [
        "conflict_read_path",
        "conflict_changed_before_finish",
        "conflict_changed_before_delivery",
        "conflict_closed",
        "conflict_delivered",
    ] {
        tokio::time::timeout(Duration::from_secs(5), check_cut(cut))
            .await
            .unwrap();
    }
}

#[tokio::test(start_paused = true)]
async fn a_qualified_proof_refusal_needs_its_original_delivery_and_a_separate_mail_action() {
    for cut in [
        "proof_offer_drop",
        "proof_offer_successor",
        "proof_offer_account",
        "proof_offer_credential",
        "proof_offer_expired",
        "proof_offer_refreshed",
        "proof_offer_before_mail",
        "proof_mail_drop",
        "proof_mail_replaced",
        "proof_mail_credential",
        "proof_mail_limited",
        "proof_mail_limited_replaced",
        "proof_mail_limited_credential",
    ] {
        // The fixture ticket lasts five minutes. Advancing only Tokio's clock
        // exercises its real monotonic deadline, without sleeping or changing the ticket.
        tokio::time::timeout(Duration::from_secs(330), check_cut(cut))
            .await
            .unwrap();
    }
}

#[tokio::test(start_paused = true)]
async fn an_undelivered_publication_expires_without_holding_the_next_claim() {
    tokio::time::timeout(Duration::from_secs(60), check_cut("delivery_expired"))
        .await
        .unwrap();
}

async fn check_cut(cut: &'static str) {
    let root = tempfile::tempdir().unwrap();
    let store = ConnectionStore::new(root.path().join("connection.json"));
    let mut record = store.load("fixture computer").unwrap();
    let review = cut.strip_prefix("proof_start_review_");
    let adopting = cut == "proof_start_bound"
        || cut.starts_with("proof_start_auto_save")
        || cut.starts_with("proof_start_save_")
        || review.is_some()
        || cut.starts_with("read_refresh_");
    let memory = Arc::new(save::Memory::default());
    let mut state = ClientState::new("fixture computer".into(), "0.1.0".into(), json!({}));
    let a = Account {
        id: "fixture-a".into(),
        email: "fixture@example.test".into(),
    };
    state.account.identity = Some(a.clone());
    state.account.email = Some(a.email.clone());
    state.account.status = AccountStatus::SignedIn;
    let (m, mut peer, expiry) = peer(&a.id);
    let (reply, wait) = oneshot::channel();
    let (config_reply, config_wait) = oneshot::channel();
    let (start_reply, start_wait) = oneshot::channel();
    let (forward_reply, forward_wait) = oneshot::channel();
    let hosted = Arc::new(Hosted {
        expiry,
        grant: Mutex::new(Some(wait)),
        sent: Notify::new(),
        require_proof: cut.starts_with("proof_"),
        config: Mutex::new(Some(config_wait)),
        discovering: Notify::new(),
        mails: AtomicU64::new(0),
        start: Mutex::new(Some(start_wait)),
        starting: Notify::new(),
        forward: Mutex::new(Some(forward_wait)),
        forwarding: Notify::new(),
        reads: AtomicU64::new(0),
        unknown: cut.starts_with("proof_start_unknown"),
        review,
        grants: AtomicU64::new(0),
        publication_case: cut,
        owner_reads: AtomicU64::new(0),
        observing: Notify::new(),
        resume_observation: Notify::new(),
        ticket_reads: AtomicU64::new(0),
        factors: AtomicU64::new(0),
        factor_entered: Notify::new(),
        factor_release: Notify::new(),
        setups: AtomicU64::new(0),
        setup_entered: Notify::new(),
        setup_release: Notify::new(),
        platform: Default::default(),
    });
    record.account_id = Some(a.id.clone());
    store.save(&record).unwrap();
    let credentials: Arc<dyn Credentials> = if adopting { memory.clone() } else { Arc::new(NoPlatform) };
    let view: Arc<dyn ClientView> = if adopting { memory.clone() } else { Arc::new(NoPlatform) };
    let c = Arc::new(Coordinator::new(
        state,
        record,
        store,
        hosted.clone(),
        credentials,
        if review.is_some_and(|r| r.starts_with("factor_pk_")) {
            hosted.clone()
        } else {
            Arc::new(NoPlatform)
        },
        view,
    ));
    let generation = c.state().generation;
    let doc = c.claim_document(generation).unwrap();
    let caller = doc.caller().unwrap();
    let epoch = Epoch::new(c.generation.clone(), generation);
    let (mut flow, state) = {
        let mut i = c.inner.lock().unwrap();
        i.record.account_id = Some(a.id.clone());
        i.account.resumes = Some(a.id.clone());
        let origin = i.account.session.origin.clone();
        let mut fixture = NativeSession::new(origin.clone(), hosted.clone());
        fixture.store_cookies(
            vec![cookie::Cookie::parse("session=synthetic; Path=/; HttpOnly; Secure")
                .unwrap()
                .into_owned()],
            &origin,
        );
        if cut == "conflict_read_path" {
            fixture.store_cookies(
                vec![cookie::Cookie::parse("narrow=fixture; Path=/api/mobile; Secure")
                    .unwrap()
                    .into_owned()],
                &origin,
            );
        }
        let saved = fixture.persist().unwrap();
        i.account.session = NativeSession::restore_account(hosted.clone(), &saved).unwrap();
        if adopting {
            memory.seed(saved);
        }
        (i.account.for_claim(epoch.clone()), i.state.account.clone())
    };
    let open = c
        .reserve_claim(&caller, epoch.clone())
        .unwrap()
        .deliver()
        .unwrap()
        .unwrap();
    let receipt = opaque(open["receipt"].as_str().unwrap()).unwrap();
    let _stop = c
        .claims
        .lock()
        .unwrap()
        .begin(receipt, &a, oneshot::channel().0)
        .unwrap();
    let mut claim = HostedClaim::new(m, epoch.clone(), caller.epoch.clone(), &state).unwrap();
    let read = {
        let pending = claim.read(&mut flow, &state, claim.revision(), None);
        tokio::pin!(pending);
        if cut == "read_refresh_before_reply" {
            tokio::select! { _ = &mut pending => panic!("held review returned"), _ = hosted.observing.notified() => () }
            refresh_same_generation(&c, &memory, generation).await;
            hosted.resume_observation.notify_one();
        }
        pending.await
    };
    if cut == "read_refresh_before_reply" {
        assert!(read.is_err() && caller.check().is_err());
        assert!(c
            .confirm_claim(&caller, generation, receipt, claim.revision())
            .await
            .is_err());
        assert_eq!(
            c.inner
                .lock()
                .unwrap()
                .account
                .session
                .cookie_value("session")
                .as_deref(),
            Some("successor")
        );
        assert_eq!(hosted.grants.load(Ordering::SeqCst), 0);
        peer.assert_unarmed(); // This fixture still holds the borrowed review.
        drop(claim);
        assert!(peer.arm().await.is_err());
        return;
    }
    if cut == "conflict_read_path" {
        assert_eq!(read.err().unwrap().code, "decision_changed");
        peer.assert_unarmed();
        return;
    }
    read.unwrap();
    let revision = claim.revision();
    let replace_credential = || {
        let mut inner = c.inner.lock().unwrap();
        let origin = inner.account.session.origin.clone();
        let mut jar = NativeSession::new(origin.clone(), hosted.clone());
        jar.store_cookies(
            vec![cookie::Cookie::parse("session=successor; Path=/; Secure")
                .unwrap()
                .into_owned()],
            &origin,
        );
        inner.account.session = NativeSession::restore_account(hosted.clone(), &jar.persist().unwrap()).unwrap();
    };
    if cut == "conflict_changed_before_finish" {
        replace_credential();
    }
    let mut borrow = Borrow {
        owner: &c.claims,
        receipt,
        returned: false,
    };
    let read_reply = c.finish_claim_review(&caller, epoch, claim, flow, &mut borrow, None);
    drop(borrow);
    if cut == "conflict_changed_before_finish" {
        assert!(read_reply.is_err());
        assert_eq!(
            c.inner
                .lock()
                .unwrap()
                .account
                .session
                .cookie_value("session")
                .as_deref(),
            Some("successor")
        );
        assert!(peer.arm().await.is_err());
        return;
    }
    let read_reply = read_reply.unwrap();
    if cut.starts_with("read_refresh_") {
        peer.assert_unarmed();
        assert_eq!(
            c.inner
                .lock()
                .unwrap()
                .account
                .session
                .cookie_value("session")
                .as_deref(),
            Some("from-review")
        );
        // Receipt plus revision is not confirmation until this reply passes delivery.
        assert!(c.confirm_claim(&caller, generation, receipt, revision).await.is_err());
        match cut {
            "read_refresh_delivered" => {
                let view = read_reply.deliver().unwrap().unwrap();
                assert_eq!(opaque(view["revision"].as_str().unwrap()).unwrap(), revision);
                assert_eq!(view["screen"]["state"], "new");
                peer.assert_unarmed();
            }
            "read_refresh_cancel_replaced" => {
                c.claims.lock().unwrap().cancel(receipt).unwrap();
                let next_doc = c.claim_document(generation).unwrap();
                let next = next_doc.caller().unwrap();
                let opened = c
                    .reserve_claim(&next, Epoch::new(c.generation.clone(), generation))
                    .unwrap()
                    .deliver()
                    .unwrap()
                    .unwrap();
                let successor = opaque(opened["receipt"].as_str().unwrap()).unwrap();
                assert!(read_reply.deliver().is_err());
                assert!(c.confirm_claim(&next, generation, successor, revision).await.is_err());
                assert!(next.monitoring() && peer.arm().await.is_err());
            }
            "read_refresh_before_delivery" => {
                refresh_same_generation(&c, &memory, generation).await;
                assert!(read_reply.deliver().is_err());
                assert!(c.confirm_claim(&caller, generation, receipt, revision).await.is_err());
                assert!(peer.arm().await.is_err());
                assert_eq!(
                    c.inner
                        .lock()
                        .unwrap()
                        .account
                        .session
                        .cookie_value("session")
                        .as_deref(),
                    Some("successor")
                );
            }
            _ => unreachable!(),
        }
        assert_eq!(hosted.grants.load(Ordering::SeqCst), 0);
        return;
    }
    if cut.starts_with("conflict_") {
        peer.assert_unarmed();
        assert_eq!(
            c.inner
                .lock()
                .unwrap()
                .account
                .session
                .cookie_value("session")
                .as_deref(),
            Some("synthetic")
        );
        if cut == "conflict_changed_before_delivery" {
            replace_credential();
        }
        if cut == "conflict_closed" {
            caller.retire();
        }
        let delivered = read_reply.deliver();
        if cut == "conflict_delivered" {
            let value = delivered.unwrap().unwrap();
            assert_eq!(
                value,
                json!({"revision":revision.to_string(),"account_email":"fixture@example.test",
                                    "device_name":"fixture computer","screen":{"state":"owner_conflict"}})
            );
            assert!(c.confirm_claim(&caller, generation, receipt, revision).await.is_err());
        } else {
            assert!(delivered.is_err());
        }
        assert_eq!(hosted.grants.load(Ordering::SeqCst), 0);
        return;
    }
    if cut.starts_with("check_error_") {
        read_reply.deliver().unwrap().unwrap();
        output::check(&c, &caller, generation, receipt, revision, cut).await;
        assert!(peer.arm().await.is_err());
        assert_eq!(hosted.ticket_reads.load(Ordering::SeqCst), 2);
        assert_eq!(hosted.grants.load(Ordering::SeqCst), 0);
        return;
    }
    let host = output::host();
    let dispatch = |action| {
        c.claim_review(
            &host,
            &caller,
            "en",
            ClaimRequest {
                expected_generation: generation,
                action,
            },
        )
    };
    let confirm = |r: Uuid| {
        dispatch(ClaimAction::Confirm {
            receipt: r.to_string(),
            revision: revision.to_string(),
        })
    };
    assert!(confirm(receipt).await.is_err()); // generated but not released
    assert!(read_reply.deliver().unwrap().is_some());
    assert!(confirm(Uuid::new_v4()).await.is_err());
    if cut == "no_saved_owner_at_confirm" {
        c.inner.lock().unwrap().record.account_id = None;
        assert!(confirm(receipt).await.is_err());
        assert_eq!(hosted.grants.load(Ordering::SeqCst), 0);
        peer.assert_unarmed();
        return;
    }
    let decision = confirm(receipt);
    tokio::pin!(decision);
    tokio::select! { _ = &mut decision => panic!("grant response was held"), _ = hosted.sent.notified() => () }
    let unlocked = || {
        assert!(c.operation.try_lock().is_ok());
        assert!(c.credential_io.try_lock().is_ok());
        assert!(c.inner.try_lock().is_ok());
        assert!(c.claims.try_lock().is_ok());
    };
    unlocked();
    assert!(confirm(receipt).await.is_err());
    if cut == "http" {
        c.claims.lock().unwrap().cancel(receipt).unwrap();
        let next = c.claim_document(generation).unwrap();
        let successor = c.claims.lock().unwrap().reserve(a.clone()).unwrap();
        reply.send(()).unwrap();
        assert!(decision.await.is_err());
        assert!(peer.arm().await.is_err());
        assert_eq!(c.claims.lock().unwrap().active.as_ref().unwrap().receipt, successor);
        drop(next);
        return;
    }
    if cut == "busy_after_grant" {
        let _ordinary_transaction = c.operation.try_lock().unwrap();
        reply.send(()).unwrap();
        assert_eq!(decision.await.err().unwrap().code, "stale_generation");
        assert!(peer.arm().await.is_err());
        assert!(!caller.monitoring());
        return;
    }
    reply.send(()).unwrap();
    if cut.starts_with("name_race_") {
        let pending = decision.await.unwrap();
        peer.assert_unarmed();
        assert!(confirm(receipt).await.is_err());
        assert_eq!(hosted.grants.load(Ordering::SeqCst), 1);
        let check = |r: Uuid| {
            dispatch(ClaimAction::Check {
                revision: r.to_string(),
                prefix: "spare".into(),
            })
        };
        assert!(check(revision).await.is_err());
        if cut == "name_race_drop" {
            drop(pending);
            assert!(peer.arm().await.is_err());
            return;
        }
        if cut == "name_race_replaced" {
            caller.retire();
            let next = c.claim_document(generation).unwrap();
            assert_eq!(pending.deliver().unwrap_err().code, "stale_generation");
            assert!(next.caller().unwrap().monitoring() && peer.arm().await.is_err());
            return;
        }
        if cut == "name_race_credential" {
            replace_credential();
            assert_eq!(pending.deliver().unwrap_err().code, "stale_generation");
            assert!(peer.arm().await.is_err());
            return;
        }
        let taken = pending.deliver().unwrap().unwrap();
        let edit = opaque(taken["revision"].as_str().unwrap()).unwrap();
        assert_eq!(taken, json!({"state":"name_taken","revision":edit.to_string()}));
        assert_ne!(edit, revision);
        let accept = |r: Uuid| {
            dispatch(ClaimAction::Confirm {
                receipt: receipt.to_string(),
                revision: r.to_string(),
            })
        };
        assert!(accept(edit).await.is_err() && check(revision).await.is_err());
        assert_eq!(hosted.ticket_reads.load(Ordering::SeqCst), 1);
        let fresh = check(edit).await.unwrap().deliver().unwrap().unwrap();
        let replacement = opaque(fresh["revision"].as_str().unwrap()).unwrap();
        assert_eq!(fresh["screen"]["prefix"], "spare");
        assert_ne!(replacement, edit);
        assert!(accept(edit).await.is_err());
        assert_eq!(hosted.grants.load(Ordering::SeqCst), 1);
        peer.assert_unarmed();
        let second = accept(replacement);
        tokio::pin!(second);
        let arm = tokio::select! { _=&mut second=>panic!("unreleased machine result"), arm=peer.arm()=>arm.unwrap() };
        assert_eq!(arm["hostname"], "spare-app.avibe.bot");
        peer.finish.send(Ok(MachineOutcome::Declined)).ok().unwrap();
        assert_eq!(
            second.await.unwrap().deliver().unwrap(),
            Some(json!({"state":"declined"}))
        );
        assert_eq!(hosted.grants.load(Ordering::SeqCst), 2);
        return;
    }
    if hosted.require_proof {
        let pending_offer = decision.await.unwrap();
        assert!(caller.monitoring());
        peer.assert_unarmed();
        let send = |id: Uuid| {
            dispatch(ClaimAction::SendProofCode {
                receipt: receipt.to_string(),
                offer: id.to_string(),
            })
        };
        assert!(send(revision).await.is_err());
        assert!(confirm(receipt).await.is_err());
        assert!(hosted.config.lock().unwrap().is_some());
        assert_eq!(hosted.mails.load(Ordering::SeqCst), 0);
        if matches!(
            cut,
            "proof_offer_drop"
                | "proof_offer_successor"
                | "proof_offer_account"
                | "proof_offer_credential"
                | "proof_offer_expired"
        ) {
            match cut {
                "proof_offer_drop" => drop(pending_offer),
                "proof_offer_successor" => {
                    c.claims.lock().unwrap().cancel(receipt).unwrap();
                    let next = c.claim_document(generation).unwrap();
                    let successor = c.claims.lock().unwrap().reserve(a.clone()).unwrap();
                    assert!(pending_offer.deliver().is_err());
                    assert_eq!(c.claims.lock().unwrap().active.as_ref().unwrap().receipt, successor);
                    drop(next);
                }
                _ => {
                    match cut {
                        "proof_offer_account" => {
                            c.inner.lock().unwrap().state.account.identity = Some(Account {
                                id: "fixture-b".into(),
                                email: a.email.clone(), // Same mailbox is not the original account.
                            });
                        }
                        "proof_offer_credential" => replace_credential(),
                        "proof_offer_expired" => tokio::time::advance(Duration::from_secs(301)).await,
                        _ => unreachable!(),
                    }
                    assert!(pending_offer.deliver().is_err());
                }
            }
            assert!(!caller.monitoring());
            assert!(peer.arm().await.is_err());
            assert_eq!(hosted.grants.load(Ordering::SeqCst), 1);
            assert!(hosted.config.lock().unwrap().is_some() && hosted.mails.load(Ordering::SeqCst) == 0);
            return;
        }
        let offered = pending_offer.deliver().unwrap().unwrap();
        let offer = opaque(offered["offer"].as_str().unwrap()).unwrap();
        assert_eq!(
            offered,
            json!({"state":"confirmation_required", "offer":offer.to_string()})
        );
        assert_ne!(offer, revision);
        assert!(hosted.config.lock().unwrap().is_some() && hosted.mails.load(Ordering::SeqCst) == 0);
        if cut == "proof_offer_refreshed" {
            assert_eq!(
                c.inner
                    .lock()
                    .unwrap()
                    .account
                    .session
                    .cookie_value("session")
                    .as_deref(),
                Some("from-refusal")
            );
        }
        let sending = send(offer);
        tokio::pin!(sending);
        tokio::select! { _ = &mut sending => panic!("discovery was held"), _ = hosted.discovering.notified() => () }
        unlocked();
        assert!(send(offer).await.is_err());
        if cut == "proof_cancel" {
            c.claims.lock().unwrap().cancel(receipt).unwrap();
            let next = c.claim_document(generation).unwrap();
            let successor = c.claims.lock().unwrap().reserve(a.clone()).unwrap();
            config_reply.send(()).unwrap();
            assert!(sending.await.is_err());
            assert_eq!(c.claims.lock().unwrap().active.as_ref().unwrap().receipt, successor);
            drop(next);
        } else if cut == "proof_offer_before_mail" {
            replace_credential();
            config_reply.send(()).unwrap();
            assert_eq!(sending.await.unwrap().deliver().unwrap_err().code, "stale_generation");
            assert_eq!(hosted.grants.load(Ordering::SeqCst), 1);
        } else if cut == "proof_busy" {
            let _ordinary_transaction = c.operation.try_lock().unwrap();
            config_reply.send(()).unwrap();
            assert_eq!(sending.await.unwrap().deliver().unwrap_err().code, "stale_generation");
        } else {
            config_reply.send(()).unwrap();
            let mail = sending.await.unwrap();
            // No code submission is admitted on the mail handle, including while
            // the issuer has already answered but the shell has not released it.
            assert!(c
                .prepare_proof_code(&caller, generation, receipt, offer, "000001")
                .await
                .is_err());
            assert!(send(offer).await.is_err());
            if cut.starts_with("proof_mail_") {
                assert!(hosted.start.lock().unwrap().is_some());
                if cut == "proof_mail_drop" {
                    drop(mail);
                } else if cut.ends_with("replaced") {
                    caller.retire();
                    let next = c.claim_document(generation).unwrap();
                    let successor = c.claims.lock().unwrap().reserve(a.clone()).unwrap();
                    let err = mail.deliver().unwrap_err();
                    assert_eq!((err.code.as_str(), err.retry_after_seconds), ("stale_generation", None));
                    assert_eq!(c.claims.lock().unwrap().active.as_ref().unwrap().receipt, successor);
                    drop(next);
                } else {
                    if cut.ends_with("credential") {
                        replace_credential();
                    }
                    let err = mail.deliver().unwrap_err();
                    let expected = if cut == "proof_mail_limited" {
                        ("rate_limited", Some(47))
                    } else {
                        ("stale_generation", None)
                    };
                    assert_eq!((err.code.as_str(), err.retry_after_seconds), expected);
                }
                assert!(send(offer).await.is_err());
                assert!(!caller.monitoring());
                assert!(peer.arm().await.is_err());
                assert_eq!(hosted.mails.load(Ordering::SeqCst), 1);
                return;
            }
            let accepted = mail.deliver().unwrap().unwrap();
            let delivery = opaque(accepted["delivery"].as_str().unwrap()).unwrap();
            assert_eq!(
                accepted,
                json!({"state":"mail_accepted", "delivery":delivery.to_string()})
            );
            assert_ne!(delivery, offer);
            assert!(send(offer).await.is_err());
            assert!(send(delivery).await.is_err());
            assert!(confirm(receipt).await.is_err());
            peer.assert_unarmed();
            if cut.starts_with("proof_start_") {
                if matches!(cut, "proof_start_drop" | "proof_start_no_owner") {
                    let preparing = c.prepare_proof_code(&caller, generation, receipt, delivery, "000001");
                    tokio::pin!(preparing);
                    tokio::select! { _ = &mut preparing => panic!("start was held"), _ = hosted.starting.notified() => () }
                    start_reply.send(()).unwrap();
                    let prepared = preparing.await.unwrap();
                    if cut == "proof_start_no_owner" {
                        c.inner.lock().unwrap().account.resumes = None;
                        assert!(c.forward_proof_code(prepared).await.is_err());
                    } else {
                        drop(prepared);
                    }
                    assert!(hosted.forward.lock().unwrap().is_some());
                    assert_eq!(hosted.mails.load(Ordering::SeqCst), 1);
                    assert!(peer.arm().await.is_err());
                    return;
                }
                let submit = |code: &str| {
                    dispatch(ClaimAction::SubmitProofCode {
                        receipt: receipt.to_string(),
                        delivery: delivery.to_string(),
                        code: code.to_owned(),
                    })
                };
                assert_eq!(submit("０００００１").await.err().unwrap().code, "invalid_or_expired");
                let start = submit("000001");
                tokio::pin!(start);
                tokio::select! { _ = &mut start => panic!("start was held"), _ = hosted.starting.notified() => () }
                unlocked();
                assert!(submit("000001").await.is_err());
                match cut {
                    "proof_start_cancel" => {
                        c.claims.lock().unwrap().cancel(receipt).unwrap();
                        let next = c.claim_document(generation).unwrap();
                        let successor = c.claims.lock().unwrap().reserve(a.clone()).unwrap();
                        start_reply.send(()).unwrap();
                        assert!(start.await.is_err());
                        assert_eq!(c.claims.lock().unwrap().active.as_ref().unwrap().receipt, successor);
                        drop(next);
                    }
                    "proof_start_busy" => {
                        let _ordinary = c.operation.try_lock().unwrap();
                        start_reply.send(()).unwrap();
                        assert!(start.await.is_err());
                    }
                    _ => {
                        start_reply.send(()).unwrap();
                        assert!(confirm(receipt).await.is_err());
                        tokio::select! { _ = &mut start => panic!("code response was held"), _ = hosted.forwarding.notified() => () }
                        unlocked();
                        let late_failure = cut.starts_with("proof_start_unknown_");
                        let after_save = cut.starts_with("proof_start_auto_save_direct");
                        if review.is_none() && !late_failure && !after_save {
                            caller.retire();
                        } // sheet closure cannot cancel a dispatched account transaction
                        if cut == "proof_start_retired" {
                            let _inner = c.inner.lock().unwrap();
                            c.supersede_binding(); // same account, same generation, different transaction
                        }
                        if cut == "proof_start_auto_save_refused" {
                            memory.refuse_one_save();
                        } else if cut.starts_with("proof_start_auto_save") {
                            memory.allow_one_save();
                        }
                        forward_reply.send(()).unwrap();
                        let direct = start.await.unwrap();
                        assert!(dispatch(ClaimAction::ReviewAfterProof {
                            receipt: receipt.to_string(),
                            submission: delivery.to_string()
                        })
                        .await
                        .is_err());
                        if adopting {
                            if after_save {
                                recovery::auto(&c, &hosted, &memory, false).await;
                                if cut.ends_with("_changed") {
                                    replace_credential();
                                    assert_eq!(direct.deliver().unwrap_err().code, "stale_generation");
                                } else {
                                    output::submission(direct.deliver().unwrap());
                                    // its own publication, not the old jar
                                }
                                assert!(submit("000001").await.is_err());
                                return;
                            }
                            let projected = direct.deliver();
                            let submitted = if review.is_some() {
                                Some(output::submission(projected.unwrap()))
                            } else {
                                assert_eq!(projected.unwrap_err().code, "stale_generation");
                                None
                            };
                            if cut.starts_with("proof_start_auto_save") {
                                recovery::auto(&c, &hosted, &memory, cut.ends_with("_refused")).await;
                                assert!(peer.arm().await.is_err());
                                return;
                            }
                            let saved_stage = recovery::read_once(&c).await;
                            assert_eq!(saved_stage["state"], "available");
                            assert_eq!(hosted.reads.load(Ordering::SeqCst), 2);
                            if let Some(cut) = review {
                                review::exercise(
                                    &c,
                                    &memory,
                                    &hosted,
                                    peer,
                                    &caller,
                                    (receipt, revision, submitted.unwrap()),
                                    cut,
                                )
                                .await;
                                return;
                            }
                            save::exercise(&c, &memory, cut).await;
                            assert_eq!(hosted.reads.load(Ordering::SeqCst), 2);
                        } else {
                            if cut.ends_with("_credential") {
                                replace_credential();
                            }
                            let next = (cut == "proof_start_unknown_successor").then(|| {
                                caller.retire();
                                let doc = c.claim_document(generation).unwrap();
                                let next = c.claims.lock().unwrap().reserve(a.clone()).unwrap();
                                (doc, next)
                            });
                            let error = direct.deliver().unwrap_err();
                            assert_eq!(
                                error.code,
                                if cut == "proof_start_unknown_live" {
                                    "code_delivery_unknown"
                                } else {
                                    "stale_generation"
                                }
                            );
                            if let Some((_doc, next)) = next {
                                assert_eq!(c.claims.lock().unwrap().active.as_ref().unwrap().receipt, next);
                            }
                            assert!(submit("000001").await.is_err());
                            assert!(!c.binding.lock().unwrap().occupied());
                            assert_eq!(hosted.reads.load(Ordering::SeqCst), 0);
                        }
                    }
                }
            } else {
                c.claims.lock().unwrap().cancel(receipt).unwrap();
            }
        }
        assert_eq!(
            hosted.mails.load(Ordering::SeqCst),
            if matches!(cut, "proof_cancel" | "proof_busy" | "proof_offer_before_mail") {
                0
            } else {
                1
            }
        );
        assert!(peer.arm().await.is_err());
        return;
    }
    if cut.starts_with("grant_denied_") {
        let pending = decision.await.unwrap();
        assert!(peer.arm().await.is_err());
        assert!(caller.monitoring());
        if cut == "grant_denied_replaced" {
            caller.retire();
            let next = c.claim_document(generation).unwrap();
            let successor = c.claims.lock().unwrap().reserve(a.clone()).unwrap();
            assert_eq!(pending.deliver().unwrap_err().code, "stale_generation");
            assert_eq!(c.claims.lock().unwrap().active.as_ref().unwrap().receipt, successor);
            drop(next);
        } else {
            assert_eq!(
                pending.deliver().unwrap_err().code,
                "paired_installation_loopback_required"
            );
        }
        assert_eq!(hosted.grants.load(Ordering::SeqCst), 1);
        return;
    }
    let arm =
        tokio::select! { _ = &mut decision => panic!("machine result was held"), frame = peer.arm() => frame.unwrap() };
    assert_eq!(arm["hostname"], "desk-app.avibe.bot");
    unlocked();
    if cut == "machine" {
        c.window_destroyed();
    }
    if cut.starts_with("decline_") {
        peer.finish.send(Ok(MachineOutcome::Declined)).ok().unwrap();
        let pending = decision.await.unwrap();
        assert!(caller.monitoring());
        if cut == "decline_replaced" {
            caller.retire();
            let next = c.claim_document(generation).unwrap();
            let successor = c.claims.lock().unwrap().reserve(a.clone()).unwrap();
            assert_eq!(pending.deliver().unwrap_err().code, "stale_generation");
            assert_eq!(c.claims.lock().unwrap().active.as_ref().unwrap().receipt, successor);
            drop(next);
        } else if cut == "decline_changed_jar" {
            replace_credential();
            assert_eq!(pending.deliver().unwrap_err().code, "stale_generation");
        } else {
            assert_eq!(pending.deliver().unwrap().unwrap(), json!({"state":"declined"}));
        }
        assert_eq!(hosted.owner_reads.load(Ordering::SeqCst), 0);
        assert_eq!(hosted.grants.load(Ordering::SeqCst), 1);
        return;
    }
    let (adoption, nonce, ack) = peer.published();
    if cut == "machine" {
        assert!(decision.await.is_err());
        assert_eq!(hosted.owner_reads.load(Ordering::SeqCst), 0);
    } else if matches!(
        cut,
        "publication_offline"
            | "publication_error_replaced"
            | "publication_absent"
            | "publication_shared"
            | "publication_read_changed"
    ) {
        if cut == "publication_read_changed" {
            tokio::select! { _ = &mut decision => panic!("account GET held"), _ = hosted.observing.notified() => () };
            unlocked();
            c.inner.lock().unwrap().account.session = NativeSession::account(hosted.clone());
            hosted.resume_observation.notify_one();
        }
        let outcome = decision.await;
        if cut == "publication_read_changed" {
            assert_eq!(outcome.err().unwrap().code, "stale_generation");
        } else {
            let pending = outcome.unwrap();
            if cut == "publication_error_replaced" {
                caller.retire();
            }
            let expected = match cut {
                "publication_error_replaced" => "stale_generation",
                "publication_offline" => "account_observation_unavailable",
                _ => "binding_owner_unconfirmed",
            };
            assert_eq!(pending.deliver().unwrap_err().code, expected);
        }
        assert!(nonce.await.is_err()); // not even a readback dispatched on ineligible account evidence
        assert_eq!(hosted.owner_reads.load(Ordering::SeqCst), 1);
    } else {
        let challenge =
            tokio::select! { biased; _=&mut decision=>panic!("original check not acknowledged"), n=nonce=>n.unwrap() };
        assert_eq!(challenge.len(), 32);
        unlocked();
        if cut == "publication_cancel" {
            caller.retire();
        }
        if cut == "publication_changed_jar" {
            // Same subject, record, document and public generation; different ordinary request credential.
            c.inner.lock().unwrap().account.session = NativeSession::account(hosted.clone());
        }
        let _ = ack.send(if cut == "publication_ack_failed" {
            Err(stale())
        } else {
            Ok(())
        });
        let result = decision.await;
        if matches!(cut, "publication_cancel" | "publication_changed_jar") {
            assert!(result.is_err());
        } else {
            let reply = result.unwrap();
            if cut == "delivery_expired" {
                tokio::time::advance(Duration::from_secs(41)).await;
                assert!(!caller.monitoring()); // even retaining the reply cannot keep an expired claim active
            }
            if cut == "delivery_successor" {
                caller.retire();
                let next = c.claim_document(generation).unwrap();
                let next_receipt = c.claims.lock().unwrap().reserve(a.clone()).unwrap();
                assert!(reply.deliver().is_err());
                assert_eq!(c.claims.lock().unwrap().active.as_ref().unwrap().receipt, next_receipt);
                drop(next);
                return;
            }
            if cut == "delivery_changed_document" {
                caller.retire();
            }
            if cut == "delivery_changed_adoption" {
                adoption.send_replace(false);
            }
            if cut == "publication_drop" {
                drop(reply);
            } else {
                if cut == "complete" {
                    assert_eq!(reply.deliver().unwrap().unwrap(), json!({"state":"bound"}));
                } else {
                    assert!(reply.deliver().is_err());
                }
            }
        }
    }
    assert_eq!(hosted.grants.load(Ordering::SeqCst), 1);
    assert!(c.claims.lock().unwrap().active.is_none());
}
