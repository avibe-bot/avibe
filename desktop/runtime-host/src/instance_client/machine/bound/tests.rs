use super::*;
use crate::instance_client::{account::CONTROL_PLANE, transport::Request};
use reqwest::{
    header::{HeaderMap, AUTHORIZATION, COOKIE, SET_COOKIE},
    Method,
};
use std::sync::atomic::{AtomicUsize, Ordering};

struct Issuer {
    case: &'static str,
    calls: AtomicUsize,
}
#[async_trait::async_trait]
impl Transport for Issuer {
    async fn send(&self, req: Request) -> Result<Response> {
        assert_eq!(self.calls.fetch_add(1, Ordering::SeqCst), 0);
        assert_eq!(req.url.as_str(), "https://avibe.bot/api/mobile/session");
        assert_eq!(req.method, Method::GET);
        assert_eq!(req.headers[COOKIE], "ordinary=fixture");
        assert!(req.body.is_none() && !req.headers.contains_key(AUTHORIZATION));
        if self.case == "offline" {
            return Err(ClientError::new("private_canary"));
        }
        let row = |id: &str, r: &str| json!({"id":id,"relationship":r,"host":"same.avibe.bot","available":false});
        let rows = match self.case {
            "shared" => vec![row("local", "shared"), row("other", "owner")],
            "missing" => vec![row("other", "owner")],
            "duplicate" => vec![row("local", "owner"), row("local", "shared")],
            "malformed" => vec![row("local", "unknown")],
            _ => vec![row("local", "owner")],
        };
        let mut headers = HeaderMap::new();
        headers.insert(
            SET_COOKIE,
            "ordinary=discard; Secure; HttpOnly; Path=/".parse().unwrap(),
        );
        Ok(Response { status: if self.case == "mfa" {403} else {200}, headers,
            body: serde_json::to_vec(&json!({"account":{"id":if self.case=="crossed"{"B"}else{"A"},"email":"a@fixture.test"},"instances":rows})).unwrap() })
    }
}

#[tokio::test]
async fn only_the_fresh_account_and_post_read_machine_echo_classify_this_binding() {
    for case in [
        "owner",
        "shared",
        "missing",
        "crossed",
        "malformed",
        "duplicate",
        "offline",
        "mfa",
        "unbound",
        "legacy_pairing_wait",
        "legacy_pairing_no_request",
        "legacy_pairing_ended",
        "legacy_pairing_expired",
        "legacy_pairing_publication",
        "legacy_pairing_incompatible",
        "legacy_pairing_unknown",
        "stale_account",
        "stale_generation",
        "prebuffered",
        "changed_after_recheck",
        "wrong_jar",
    ] {
        let issuer = Arc::new(Issuer {
            case,
            calls: AtomicUsize::new(0),
        });
        let origin = url::Url::parse(CONTROL_PLANE).unwrap();
        let jars = |value: &'static str| {
            let mut ordinary = NativeSession::new(origin.clone(), issuer.clone());
            ordinary.store_cookies(vec![cookie::Cookie::new("ordinary", value).into_owned()], &origin);
            NativeSession::restore_account(issuer.clone(), &ordinary.persist().unwrap()).unwrap()
        };
        let owner = jars("fixture");
        let frozen = owner.factor_cookies().unwrap();
        let mut jar = if case == "wrong_jar" {
            jars("other")
        } else {
            owner.clone()
        };
        let account = Account {
            id: "A".into(),
            email: "a@fixture.test".into(),
        };
        let needs_recheck = matches!(
            case,
            "owner" | "shared" | "missing" | "stale_generation" | "prebuffered" | "changed_after_recheck"
        );
        let (native, machine) = tokio::io::duplex(4096);
        let (rx, mut tx) = tokio::io::split(native);
        let (machine_rx, mut machine_tx) = tokio::io::split(machine);
        let checks = AtomicUsize::new(0);
        let result = async {
            let mut rx = BufReader::new(rx);
            let current = || {
                let n = checks.fetch_add(1, Ordering::SeqCst);
                if (case == "stale_account" && n == 1) || (case == "changed_after_recheck" && n == 2) {
                    Err(retired())
                } else {
                    Ok(())
                }
            };
            let r = inspect(&mut rx, &mut tx, &account, &mut jar, &frozen, &current).await;
            drop(tx);
            drop(rx);
            r
        };
        let peer = async {
            let token = "a".repeat(64);
            let binding = if case == "unbound" {
                json!({"v":1,"event":"no_binding"})
            } else if case.starts_with("legacy_pairing_") {
                json!({"v":1,"event":"result","outcome":"error","code":case})
            } else {
                json!({"v":1,"event":"binding","instance_id":"local","generation":token})
            };
            write(&mut machine_tx, binding).await.unwrap();
            if case == "prebuffered" {
                write(
                    &mut machine_tx,
                    json!({"v":1,"event":"unchanged","generation":token,"challenge":"0".repeat(32)}),
                )
                .await
                .unwrap();
            }
            let mut lines = BufReader::new(machine_rx).lines();
            let cmd = lines.next_line().await.unwrap();
            if needs_recheck {
                let cmd: Value = serde_json::from_str(&cmd.unwrap()).unwrap();
                assert_eq!(issuer.calls.load(Ordering::SeqCst), 1);
                let challenge = cmd["challenge"].as_str().unwrap();
                assert_eq!(challenge.len(), 32);
                assert_eq!(
                    cmd,
                    json!({"v":1,"op":"recheck","generation":token,"challenge":challenge})
                );
                if case != "prebuffered" {
                    write(&mut machine_tx,json!({"v":1,"event":"unchanged","generation":if case=="stale_generation" {"b".repeat(64)}else{token},"challenge":challenge})).await.unwrap();
                }
            } else {
                assert!(cmd.is_none());
            }
        };
        let (r, ()) = timeout(Duration::from_secs(3), async { tokio::join!(result, peer) })
            .await
            .unwrap();
        assert_eq!(
            issuer.calls.load(Ordering::SeqCst),
            usize::from(!matches!(case, "unbound" | "wrong_jar") && !case.starts_with("legacy_pairing_"))
        );
        assert_eq!(owner.cookie_value("ordinary").as_deref(), Some("fixture"));
        match case {
            "owner" => assert!(matches!(r, Ok(CurrentBinding::OwnerObserved))),
            "shared" | "missing" => assert!(matches!(r, Ok(CurrentBinding::BoundOwnerNotProven))),
            "unbound" => assert!(matches!(r, Ok(CurrentBinding::NoPublishedBinding))),
            "legacy_pairing_wait" => assert!(matches!(r, Ok(CurrentBinding::LegacyPending(LegacyPending::Wait)))),
            "legacy_pairing_no_request" => {
                assert!(matches!(r, Ok(CurrentBinding::LegacyPending(LegacyPending::NoRequest))))
            }
            "legacy_pairing_ended" => assert!(matches!(r, Ok(CurrentBinding::LegacyPending(LegacyPending::Ended)))),
            "legacy_pairing_expired" => assert!(matches!(r, Ok(CurrentBinding::LegacyPending(LegacyPending::Expired)))),
            "legacy_pairing_publication" => assert!(matches!(
                r,
                Ok(CurrentBinding::LegacyPending(LegacyPending::Publishing))
            )),
            "legacy_pairing_incompatible" => assert!(matches!(
                r,
                Ok(CurrentBinding::LegacyPending(LegacyPending::Incompatible))
            )),
            _ => assert!(r.is_err()),
        }
    }
}
