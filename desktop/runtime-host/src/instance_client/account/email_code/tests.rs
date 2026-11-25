//! The step-up adapter before its claim-owner integration. No OS stores or HTTP.
use super::super::*;
use crate::instance_client::transport::Request;
use reqwest::header::SET_COOKIE;
use std::sync::{atomic::AtomicU64, Mutex};

struct Wire(Mutex<Vec<String>>);
#[async_trait]
impl Transport for Wire {
    async fn send(&self, r: Request) -> Result<Response, ClientError> {
        let path = r.url.path();
        let mut seen = self.0.lock().unwrap();
        let reads = seen.iter().filter(|p| p.as_str() == "/api/mobile/session").count();
        seen.push(path.to_owned());
        let email = "me@example.test";
        let tx = "t".repeat(43);
        let mut headers = HeaderMap::new();
        let (status, body) = match path {
            "/api/config/auth" => (
                200,
                json!({"supabase_url":"https://issuer.test", "supabase_anon_key":"fixture"}),
            ),
            "/auth/v1/otp" => {
                assert_eq!(r.url.host_str(), Some("issuer.test"));
                assert_eq!(r.body, Some(json!({"email":email,"create_user":false})));
                assert!(!r.headers.contains_key("cookie"));
                (200, json!({}))
            }
            "/api/auth/first-party/start" => {
                assert_eq!(
                    r.body,
                    Some(json!({"kind":"email_code","email":email,"expected_subject":"original"}))
                );
                headers.insert(
                    SET_COOKIE,
                    "__Host-avibe-sign-in-start=fixture; Secure; HttpOnly; SameSite=Lax; Path=/; Max-Age=600"
                        .parse()
                        .unwrap(),
                );
                (200, json!({"transaction":tx}))
            }
            "/api/auth/first-party/email-code" => {
                assert_eq!(r.body, Some(json!({"transaction":tx,"email":email,"code":"000001"})));
                headers.insert(SET_COOKIE, "ordinary=new; Secure; HttpOnly; Path=/".parse().unwrap());
                (200, json!({"bound":true}))
            }
            "/api/mobile/session" if reads == 0 => return Err(ClientError::new("network_unavailable")),
            "/api/mobile/session" if reads == 1 => {
                assert!(r.headers["cookie"] == "ordinary=new");
                (
                    403,
                    json!({"error":"two_factor_required","email":email,"gate":"challenge","methods":["totp"]}),
                )
            }
            "/api/two-factor/verify" => (200, json!({})),
            "/api/mobile/session" => (200, json!({"account":{"id":"another","email":email},"instances":[]})),
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
async fn same_account_code_cannot_create_a_user_and_a_post_mfa_email_match_is_not_the_subject() {
    let epoch = Epoch::new(Arc::new(AtomicU64::new(4)), 4);
    let wire = Arc::new(Wire(Mutex::new(Vec::new())));
    let mut flow = AccountFlow::new(wire.clone(), true);
    flow.fence(epoch.clone());
    let owner = Account {
        id: "original".into(),
        email: "Me@example.test".into(),
    };
    let mut state = ClientState::new("fixture".into(), "1".into(), json!({}));
    state.account.status = AccountStatus::SignedIn;
    state.account.identity = Some(owner.clone());
    flow.send_claim_code(&owner, epoch, || Ok(())).await.unwrap();
    // AUTH-SETUP-171: there is no ordinary Verifying state and no OTP in start.
    let prepared = Box::new(flow).prepare_claim_code(&owner, "000001").await.unwrap();
    assert_eq!(wire.0.lock().unwrap().last().unwrap(), "/api/auth/first-party/start");
    let bound = prepared.forward().await.unwrap();
    assert_eq!(
        bound.discover(state.clone()).await.err().unwrap().code,
        "network_unavailable"
    );
    let found = bound.discover(state.clone()).await.unwrap();
    let mut flow = found.flow;
    state.account = found.account;
    state.catalog = found.catalog;
    assert_eq!(flow.resumes.as_deref(), Some("original"));
    assert_eq!(state.account.status, AccountStatus::SecondFactorRequired);
    assert!(state.account.identity.is_none());
    assert!(String::from_utf8(flow.session.persist().unwrap())
        .unwrap()
        .contains("ordinary=new"));
    assert_eq!(
        flow.verify_factor(CodeMethod::Totp, "000002".into(), &mut state)
            .await
            .unwrap_err()
            .code,
        "account_changed"
    );
    assert!(state.account.identity.is_none());
    assert_eq!(
        *wire.0.lock().unwrap(),
        [
            "/api/config/auth",
            "/auth/v1/otp",
            "/api/auth/first-party/start",
            "/api/auth/first-party/email-code",
            "/api/mobile/session",
            "/api/mobile/session",
            "/api/two-factor/verify",
            "/api/mobile/session"
        ]
    );
}
