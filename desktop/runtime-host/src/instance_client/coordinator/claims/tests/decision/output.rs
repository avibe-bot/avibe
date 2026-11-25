//! Start with the synthetic helper's delivered review, then exercise the real
//! Check entry point and the exact ClientError serialized by the direct command.
use super::*;
use crate::{
    HealthProbe, LaunchError, LoopbackOrigin, Presence, ResolvedRuntimeLauncher, RuntimeHostSettings, RuntimeLauncher,
};

#[async_trait::async_trait]
impl HealthProbe for NoPlatform {
    async fn presence(&self, _: &LoopbackOrigin, _: Option<&str>) -> Presence {
        panic!("Check cannot probe a Runtime")
    }
}
impl RuntimeLauncher for NoPlatform {
    fn resolve(&self) -> std::result::Result<Arc<dyn ResolvedRuntimeLauncher>, LaunchError> {
        panic!("Check cannot launch a second helper")
    }
}

pub(super) fn host() -> RuntimeHost {
    RuntimeHost::new(
        Arc::new(NoPlatform),
        Arc::new(NoPlatform),
        RuntimeHostSettings::default(),
    )
}

pub(super) async fn dispatch<'a>(
    c: &'a Arc<Coordinator>,
    caller: &ClaimCaller,
    generation: u64,
    action: Value,
) -> Result<ClaimReply<'a>> {
    let request = serde_json::from_value(json!({"expected_generation":generation, "action":action})).unwrap();
    let host = host();
    let review = c.claim_review(&host, caller, "en", request);
    // This is the production entry future, not a larger stack for the test runner.
    assert!(
        std::mem::size_of_val(&review) <= 4096,
        "claim entry future: {} bytes",
        std::mem::size_of_val(&review)
    );
    review.await
}

pub(super) fn submission(result: Option<Value>) -> Uuid {
    let value = result.unwrap();
    let token = opaque(value["submission"].as_str().unwrap()).unwrap();
    assert_eq!(
        value,
        json!({"state":"verification_accepted", "submission":token.to_string()})
    );
    token
}

#[tokio::test]
async fn a_hosted_check_refusal_is_private_until_delivered_to_its_original_document() {
    for cut in [
        "check_error_live",
        "check_error_malformed",
        "check_error_cancel",
        "check_error_drop",
    ] {
        tokio::time::timeout(Duration::from_secs(5), check_cut(cut))
            .await
            .unwrap();
    }
}

pub(super) async fn check(
    c: &Arc<Coordinator>,
    caller: &ClaimCaller,
    generation: u64,
    receipt: Uuid,
    revision: Uuid,
    cut: &str,
) {
    let host = host();
    let pending = c
        .claim_review(
            &host,
            caller,
            "en",
            ClaimRequest {
                expected_generation: generation,
                action: ClaimAction::Check {
                    revision: revision.to_string(),
                    prefix: "desk".into(),
                },
            },
        )
        .await
        .unwrap();
    assert!(c.confirm_claim(caller, generation, receipt, revision).await.is_err());
    if cut == "check_error_drop" {
        drop(pending);
        assert!(!caller.monitoring());
        return;
    }
    if cut == "check_error_cancel" {
        let ack = c
            .claim_review(
                &host,
                caller,
                "en",
                ClaimRequest {
                    expected_generation: generation,
                    action: ClaimAction::Cancel {
                        receipt: receipt.to_string(),
                    },
                },
            )
            .await
            .unwrap();
        assert!(ack.deliver().unwrap().is_none());
        let successor = c.claim_document(generation).unwrap();
        let next = successor.caller().unwrap();
        c.reserve_claim(&next, Epoch::new(c.generation.clone(), generation))
            .unwrap()
            .deliver()
            .unwrap()
            .unwrap();
        assert_eq!(
            serde_json::to_value(pending.deliver().unwrap_err()).unwrap(),
            json!({"code":"stale_generation"})
        );
        assert!(next.monitoring());
        return;
    }
    let expected = if cut == "check_error_malformed" {
        "invalid_claim_response"
    } else {
        "two_factor_required"
    };
    assert_eq!(
        serde_json::to_value(pending.deliver().unwrap_err()).unwrap(),
        json!({"code":expected})
    );
    assert!(!caller.monitoring());
}
