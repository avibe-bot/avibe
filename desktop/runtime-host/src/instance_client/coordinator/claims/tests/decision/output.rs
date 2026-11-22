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
    c: &Coordinator,
    caller: &ClaimCaller,
    generation: u64,
    receipt: Uuid,
    revision: Uuid,
    cut: &str,
) {
    let host = RuntimeHost::new(
        Arc::new(NoPlatform),
        Arc::new(NoPlatform),
        RuntimeHostSettings::default(),
    );
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
