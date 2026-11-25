//! A synthetic hosted transport; no desktop, credential adapter, DNS or HTTP.
use super::*;
use ceremony::{PROOF, START};
use serde_json::json;
use std::{collections::VecDeque, sync::Mutex};

const START_PATH: &str = "/api/auth/first-party/start";
const COMPLETE: &str = "/api/auth/first-party/email-code";
const PASSKEY: &str = "/api/passkey-primary/verify";
const GRANT: &str = "/api/v1/claims/grant";
const ATTR: &str = "Secure; HttpOnly; SameSite=Lax; Path=/";
const TX: &str = "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG";
struct Step(Response, Option<String>);
#[derive(Default)]
struct Wire(Mutex<VecDeque<Step>>);
impl Wire {
    fn next(&self, status: u16, body: Value, cookies: &[String], expected: Option<&str>) {
        let mut headers = HeaderMap::new();
        for value in cookies {
            headers.append(SET_COOKIE, value.parse().unwrap());
        }
        self.0.lock().unwrap().push_back(Step(
            Response {
                status,
                headers,
                body: serde_json::to_vec(&body).unwrap(),
            },
            expected.map(str::to_owned),
        ));
    }
}
#[async_trait]
impl Transport for Wire {
    async fn send(&self, request: Request) -> Result<Response, ClientError> {
        let Step(reply, expected) = self.0.lock().unwrap().pop_front().expect("unexpected dispatch");
        // Don't dump the credential header even on a failing assertion.
        assert!(request.headers.get(COOKIE).map(|h| h.to_str().unwrap()) == expected.as_deref());
        assert!(!request.headers.contains_key(AUTHORIZATION));
        Ok(reply)
    }
}
async fn post(jar: &mut NativeSession, path: &str, body: Value) -> Response {
    jar.request(Method::POST, path, Some(body)).await.unwrap()
}
#[tokio::test]
async fn native_start_is_one_use_and_only_the_accepted_account_response_can_supply_grant_proof() {
    let wire = Arc::new(Wire::default());
    let mut jar = NativeSession::account(wire.clone());
    wire.next(
        200,
        json!({"transaction":TX}),
        &[format!("{START}=start.fixture; {ATTR}; Max-Age=600")],
        None,
    );
    assert!(!post(
        &mut jar,
        START_PATH,
        json!({"kind":"email_code","email":"test@example.test"})
    )
    .await
    .headers
    .contains_key(SET_COOKIE));
    let complete = json!({"transaction":TX,"email":"test@example.test","code":"000001"});
    assert!(jar
        .clone()
        .request(Method::POST, COMPLETE, Some(complete.clone()))
        .await
        .is_err());
    assert!(!String::from_utf8(jar.persist().unwrap())
        .unwrap()
        .contains("start.fixture"));
    wire.next(
        200,
        json!({"bound":true}),
        &[
            format!("{PROOF}=proof.fixture; Max-Age=7776000; {ATTR}"),
            format!("{START}=; Max-Age=0; {ATTR}"),
            "ordinary=account; Path=/; Secure; HttpOnly".into(),
        ],
        Some("__Host-avibe-sign-in-start=start.fixture"),
    );
    post(&mut jar, COMPLETE, complete.clone()).await;
    assert!(jar.request(Method::POST, COMPLETE, Some(complete)).await.is_err());
    wire.next(200, json!({}), &[], Some("ordinary=account"));
    jar.request(Method::GET, "/api/mobile/session", None).await.unwrap();
    wire.next(
        403,
        json!({}),
        &[format!("{PROOF}=; {ATTR}; Max-Age=0")],
        Some("ordinary=account; __Host-avibe-sign-in-proof=proof.fixture"),
    );
    let bytes = jar.persist().unwrap();
    assert!(!String::from_utf8_lossy(&bytes).contains("proof.fixture"));
    assert!(jar.cookie_value(PROOF).is_none());
    post(&mut jar, GRANT, json!({})).await;
    wire.next(403, json!({}), &[], Some("ordinary=account"));
    post(&mut jar, GRANT, json!({})).await;
    let mut restored = NativeSession::restore_account(wire.clone(), &bytes).unwrap();
    wire.next(403, json!({}), &[], Some("ordinary=account"));
    post(&mut restored, GRANT, json!({})).await;
    assert!(wire.0.lock().unwrap().is_empty());
}
#[tokio::test]
async fn prefix_attributes_and_terminal_role_are_checked_before_cookie_narrowing() {
    let wire = Arc::new(Wire::default());
    let issue = |attrs: &str| format!("{PROOF}=test.proof; {attrs}");
    let valid = format!("{ATTR}; Max-Age=20");
    let ok = json!({"ok":true,"next":"/"});
    for (path, status, body, fields, account, issued) in [
        (PASSKEY, 200, ok.clone(), vec![issue(&valid)], true, true),
        (PASSKEY, 200, ok.clone(), vec![issue(&valid)], false, false),
        (PASSKEY, 302, ok.clone(), vec![issue(&valid)], true, false),
        (PASSKEY, 200, json!({"ok":false}), vec![issue(&valid)], true, false),
        (
            "/api/desktop/session",
            200,
            ok.clone(),
            vec![issue(&valid)],
            true,
            false,
        ),
        (
            "/api/two-factor/verify",
            200,
            ok.clone(),
            vec![issue(&valid)],
            true,
            false,
        ),
        (
            PASSKEY,
            200,
            ok.clone(),
            vec![issue(&valid), issue(&valid)],
            true,
            false,
        ),
    ] {
        let mut jar = if account {
            NativeSession::account(wire.clone())
        } else {
            NativeSession::new(CONTROL_PLANE.parse().unwrap(), wire.clone())
        };
        wire.next(status, body, &fields, None);
        assert!(!post(&mut jar, path, json!({})).await.headers.contains_key(SET_COOKIE));
        wire.next(
            403,
            json!({}),
            &[],
            issued.then_some("__Host-avibe-sign-in-proof=test.proof"),
        );
        post(&mut jar, GRANT, json!({})).await;
    }
    for attrs in [
        format!("{valid}; Domain=avibe.bot"),
        format!("{valid}; Domain="),
        format!("{valid}; Path=/"),
        valid.replace("Secure", "Secure=false"),
        valid.replace("HttpOnly", "HttpOnly=1"),
        valid.replace("HttpOnly; ", ""),
        valid.replace("Path=/", "Path=/else"),
        valid.replace("Lax", "None"),
        valid.replace("20", "7776001"),
    ] {
        let mut jar = NativeSession::account(wire.clone());
        wire.next(200, ok.clone(), &[issue(&attrs)], None);
        post(&mut jar, PASSKEY, json!({})).await;
        wire.next(403, json!({}), &[], None);
        post(&mut jar, GRANT, json!({})).await;
    }
}
#[tokio::test]
async fn historical_and_webview_entries_and_injected_headers_cannot_supply_a_ceremony() {
    let wire = Arc::new(Wire::default());
    let origin: Url = CONTROL_PLANE.parse().unwrap();
    let mut historical = CookieStore::default();
    for (name, value) in [(START, "start.old"), (PROOF, "proof.old"), ("ordinary", "yes")] {
        historical.store_response_cookies(
            std::iter::once(cookie::Cookie::parse(format!("{name}={value}; {ATTR}; Max-Age=600")).unwrap()),
            &origin,
        );
    }
    let mut bytes = Vec::new();
    cookie_store::serde::json::save_incl_expired_and_nonpersistent(&historical, &mut bytes).unwrap();
    assert!(String::from_utf8_lossy(&bytes).contains("proof.old"));
    let mut jar = NativeSession::restore_account(wire.clone(), &bytes).unwrap();
    let clean = String::from_utf8(jar.persist().unwrap()).unwrap();
    assert!(clean.contains("ordinary") && !clean.contains(START) && !clean.contains(PROOF));
    jar.store_cookies(
        vec![
            cookie::Cookie::new(PROOF, "webview.fixture"),
            cookie::Cookie::new("ordinary", "changed"),
        ],
        &origin,
    );
    for key in [AUTHORIZATION, COOKIE, HOST] {
        let mut extra = HeaderMap::new();
        extra.insert(key, HeaderValue::from_static("injected"));
        assert!(jar
            .request_with_headers(Method::POST, GRANT, None, extra)
            .await
            .is_err());
    }
    wire.next(403, json!({}), &[], Some("ordinary=yes"));
    post(&mut jar, GRANT, json!({})).await;
    for path in [
        "http://avibe.bot/api/v1/claims/grant",
        "https://avibe.bot:444/api/v1/claims/grant",
        "https://other.test/api/v1/claims/grant",
    ] {
        assert!(jar.request(Method::POST, path, None).await.is_err());
    }
}
