//! Drive the ordinary recovery wire request. No claim caller, OTP, account ID,
//! platform credential, or machine ticket is supplied by this client.
use super::*;
use crate::instance_client::{
    coordinator::{AccountRecoveryRequest, RecoveryWork},
    protocol::{ClientAction, ClientRequest},
};

fn admit(c: &Arc<Coordinator>, generation: u64, action: Value) -> Result<RecoveryWork> {
    let req: AccountRecoveryRequest =
        serde_json::from_value(json!({"expected_generation":generation,"action":action})).unwrap();
    c.admit_account_recovery(req)
}
async fn request(c: &Arc<Coordinator>, generation: u64, action: Value) -> Result<Value> {
    admit(c, generation, action)?.wait().await?.deliver()
}
pub(super) async fn inspect(c: &Arc<Coordinator>) -> Value {
    request(c, c.state().generation, json!({"type":"inspect"}))
        .await
        .unwrap()
}
pub(super) async fn offer(c: &Arc<Coordinator>) -> String {
    tokio::time::timeout(Duration::from_secs(4), async {
        loop {
            let reply = request(c, c.state().generation, json!({"type":"inspect"})).await;
            if let Ok(r) = reply {
                if r["state"] == "available" {
                    return r["receipt"].as_str().unwrap().to_owned();
                }
                assert_eq!(r, json!({"state":"unavailable"}));
            }
            tokio::time::sleep(Duration::from_millis(1)).await;
        }
    })
    .await
    .expect("account-owned pass did not yield a recovery receipt")
}
pub(super) async fn resume(c: &Arc<Coordinator>, token: &str) -> Result<Value> {
    request(c, c.state().generation, json!({"type":"resume","receipt":token})).await
}
pub(super) async fn read_once(c: &Arc<Coordinator>) -> Value {
    let token = offer(c).await;
    let generation = c.state().generation;
    let delayed = admit(c, generation, json!({"type":"inspect"}))
        .unwrap()
        .wait()
        .await
        .unwrap();
    let general = c
        .request(ClientRequest {
            version: 1,
            request_id: "fixture-state".into(),
            expected_generation: generation,
            action: ClientAction::GetState,
        })
        .await;
    assert!(general.ok);
    assert!(!serde_json::to_string(&general).unwrap().contains(&token));
    assert_eq!(inspect(c).await["receipt"], token); // GetState did not spend the offer
    assert!(
        request(c, c.state().generation + 1, json!({"type":"resume","receipt":token}))
            .await
            .is_err()
    );
    let reply = resume(c, &token).await.unwrap();
    assert!(
        delayed.deliver().is_err(),
        "a late inspection cannot publish a spent offer"
    );
    assert!(resume(c, &token).await.is_err(), "an old GET must not become a write");
    reply
}
pub(super) async fn auto(c: &Arc<Coordinator>, hosted: &Hosted, memory: &save::Memory, refused: bool) {
    if refused {
        let token = offer(c).await;
        assert_eq!(hosted.reads.load(Ordering::SeqCst), 1);
        assert_eq!(memory.changed.load(Ordering::SeqCst), 0);
        memory.allow_one_save();
        assert_eq!(resume(c, &token).await.unwrap(), json!({"state":"saved"}));
        assert!(resume(c, &token).await.is_err());
    } else {
        tokio::time::timeout(Duration::from_secs(4), async {
            while memory.changed.load(Ordering::SeqCst) == 0 {
                tokio::time::sleep(Duration::from_millis(1)).await;
            }
        })
        .await
        .expect("closing naming cancelled the ordinary owner");
    }
    assert_eq!(hosted.reads.load(Ordering::SeqCst), 1);
    assert_eq!(hosted.mails.load(Ordering::SeqCst), 1);
    assert_eq!(hosted.grants.load(Ordering::SeqCst), 1);
    assert_eq!(memory.changed.load(Ordering::SeqCst), 1);
    assert_eq!(
        request(c, c.state().generation, json!({"type":"inspect"}))
            .await
            .unwrap(),
        json!({"state":"unavailable"})
    );
    let bytes = memory.read("fixture-a").unwrap().unwrap();
    let stored = NativeSession::restore_account(c.transport.clone(), &bytes).unwrap();
    assert_eq!(stored.cookie_value("ordinary").as_deref(), Some("new"));
}
