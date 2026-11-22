//! A code challenge has no OS wait. Hold the actual coordinator at issuance,
//! assertion and verification separately: neither later hop inherits permission
//! just because an earlier one returned. Only synthetic bytes cross these hops.
use super::*;
use crate::instance_client::coordinator::claim_binding::BindingId;
use std::{future::Future, pin::Pin};

#[derive(Default)]
pub(super) struct Platform {
    pub(super) challenges: AtomicU64,
    pub(super) challenging: Notify,
    pub(super) issue: Notify,
    sheets: AtomicU64,
    opening: Notify,
    answer: Notify,
}
pub(super) fn options() -> Value {
    json!({"rpId":"avibe.bot","challenge":"Y2hhbGxlbmdl","userVerification":"required","allowCredentials":[{"type":"public-key","id":"Zml4dHVyZQ"}]})
}
pub(super) fn assertion() -> Value {
    json!({"type":"public-key","id":"Zml4dHVyZQ","rawId":"Zml4dHVyZQ","response":{"signature":"Zml4dHVyZQ"}})
}
#[async_trait::async_trait]
impl Passkeys for Hosted {
    async fn assert(&self, request: Value, epoch: Epoch) -> Result<Value> {
        epoch.check()?;
        assert_eq!(request, options(), "second factor must keep the credential allow-list");
        assert_eq!(self.platform.sheets.fetch_add(1, Ordering::SeqCst), 0);
        self.platform.opening.notify_one();
        self.platform.answer.notified().await;
        // Deliberately return a late result even if the platform did not cancel.
        let mut result = assertion();
        if self.review == Some("factor_pk_wrong_id") {
            result["id"] = json!("b3RoZXI");
            result["rawId"] = result["id"].clone();
        }
        Ok(result)
    }
}

pub(super) type Verify<'a> = Pin<Box<dyn Future<Output = Result<proof::SubmittedReply>> + 'a>>;

pub(super) async fn prepare<'a>(
    c: &'a Arc<Coordinator>,
    caller: &'a ClaimCaller,
    hosted: &Hosted,
    receipt: Uuid,
    predecessor: BindingId,
    cut: &str,
) -> Option<Verify<'a>> {
    let generation = c.state().generation;
    let submit = || c.submit_claim_factor_passkey(caller, generation, receipt, predecessor);
    let mut run = Box::pin(submit());
    let platform = &hosted.platform;
    if cut == "factor_pk_path" {
        assert_eq!(
            run.await.err().expect("preparation did not refuse").code,
            "invalid_action"
        );
        assert_eq!(platform.challenges.load(Ordering::SeqCst), 0);
        assert_eq!(platform.sheets.load(Ordering::SeqCst), 0);
        return None;
    }
    tokio::select! { _ = &mut run => panic!("issuance held"), _ = platform.challenging.notified() => () }
    assert!(submit().await.is_err());
    assert!(c.operation.try_lock().is_ok() && c.credential_io.try_lock().is_ok());
    if cut == "factor_pk_cancel" {
        c.claims.lock().unwrap().cancel(receipt).unwrap();
        assert!(run.await.is_err()); // no challenge response necessary to release
        assert_eq!(platform.sheets.load(Ordering::SeqCst), 0);
        return None;
    }
    if cut == "factor_pk_before_sheet" {
        {
            let _inner = c.inner.lock().unwrap();
            c.supersede_binding();
        }
        platform.issue.notify_one();
        assert!(run.await.is_err());
        assert_eq!(platform.sheets.load(Ordering::SeqCst), 0);
        return None;
    }
    platform.issue.notify_one();
    if cut == "factor_pk_nolist" {
        assert_eq!(
            run.await.err().expect("preparation did not refuse").code,
            "invalid_passkey_challenge"
        );
        assert_eq!(platform.sheets.load(Ordering::SeqCst), 0);
        return None;
    }
    tokio::select! { _ = &mut run => panic!("OS response held"), _ = platform.opening.notified() => () }
    assert!(c.operation.try_lock().is_ok() && c.credential_io.try_lock().is_ok());
    assert!(submit().await.is_err());
    match cut {
        "factor_pk_wrong_id" => {
            platform.answer.notify_one();
            assert_eq!(
                run.await.err().expect("preparation did not refuse").code,
                "invalid_passkey_assertion"
            );
            None
        }
        "factor_pk_drop" => {
            drop(run);
            platform.answer.notify_one();
            assert!(!caller.monitoring());
            assert!(submit().await.is_err());
            None
        }
        "factor_pk_replaced" => {
            // Same subject, same public generation, projection still owes a factor.
            let _inner = c.inner.lock().unwrap();
            c.supersede_binding();
            drop(_inner);
            platform.answer.notify_one();
            assert!(run.await.is_err());
            None
        }
        "factor_pk_expired" => {
            tokio::time::pause();
            tokio::time::advance(Duration::from_secs(301)).await;
            platform.answer.notify_one();
            assert_eq!(run.await.err().expect("preparation did not refuse").code, "expired");
            None
        }
        _ => {
            platform.answer.notify_one();
            Some(run)
        }
    }
}
#[tokio::test]
async fn ticket_expiry_during_the_native_assertion_never_dispatches_verify() {
    check_cut("proof_start_review_factor_pk_expired").await;
}
