//! AUTH-SETUP-171. Setup material is direct, revocable and never itself a
//! credential commit. Drive the real coordinator with a synthetic hosted reply.
use super::*;
use crate::instance_client::coordinator::claim_binding::BindingId;

pub(super) const KEY: &str = "ABCD EFGH IJKL MNOP QRST UVWX YZ23 4567";

pub(super) fn codes() -> Vec<String> {
    "abcdefghjk".chars().map(|c| format!("aaaaa-bbbb{c}")).collect()
}

/// A lost direct result does not revoke ordinary discovery. Conversely,
/// successful ordinary publication cannot recreate recovery codes.
pub(super) async fn finish(
    c: &Arc<Coordinator>,
    caller: &ClaimCaller,
    hosted: &Hosted,
    receipt: Uuid,
    predecessor: BindingId,
    cut: &str,
) -> bool {
    use crate::instance_client::account::EnrolledCodes;
    let generation = c.state().generation;
    let submit =
        |code: &'static str| c.finish_claim_totp_enrollment(caller, generation, receipt, predecessor, code.into());
    let issue = c.prepare_claim_totp_setup(caller, generation, receipt, predecessor);
    tokio::pin!(issue);
    tokio::select! { _ = &mut issue => panic!("setup held"), _ = hosted.setup_entered.notified() => () }
    hosted.setup_release.notify_one();
    let setup = issue.await.unwrap();
    assert!(submit("000002").await.is_err());
    assert_eq!(hosted.factors.load(Ordering::SeqCst), 0);
    let _ = setup.deliver().unwrap();
    assert!(submit("０００００２").await.is_err());
    let mut wait = Box::pin(submit("000002"));
    tokio::select! { _ = &mut wait => panic!("final enrollment held"), _ = hosted.factor_entered.notified() => () }
    assert!(c.operation.try_lock().is_ok() && c.credential_io.try_lock().is_ok());
    assert!(submit("000002").await.is_err());
    if cut == "factor_enroll_drop" {
        drop(wait);
        hosted.factor_release.notify_one();
        // The suite bounds this scenario. Wait for the owning worker, not a sleep.
        while c.binding_recovery().is_none() {
            tokio::task::yield_now().await;
        }
        assert!(submit("000002").await.is_err());
        return true;
    }
    if cut == "factor_enroll_replaced" {
        let _inner = c.inner.lock().unwrap();
        c.supersede_binding();
    } else if cut == "factor_enroll_close" {
        caller.retire();
    }
    hosted.factor_release.notify_one();
    let result = wait.await;
    if matches!(cut, "factor_enroll_unknown" | "factor_enroll_replaced") {
        let err = result.err().expect("no direct result");
        if cut == "factor_enroll_unknown" {
            assert_eq!(err.code, "factor_delivery_unknown");
        }
        assert!(c.binding_recovery().is_none() && submit("000002").await.is_err());
        return false;
    }
    let reply = result.unwrap();
    assert!(c.binding_recovery().is_some());
    assert!(!serde_json::to_string(&c.state()).unwrap().contains("aaaaa-bbbb"));
    if cut == "factor_enroll_close" {
        assert!(reply.deliver().is_err());
    } else {
        let held = c.inner.lock().unwrap();
        match (cut, reply.deliver().unwrap()) {
            ("factor_enroll_absent", EnrolledCodes::NotIssued)
            | ("factor_enroll_bad_codes", EnrolledCodes::Unavailable) => (),
            ("factor_enroll_ok", EnrolledCodes::Codes(got)) => assert_eq!(got, codes()),
            _ => panic!("incorrect one-time result"),
        }
        assert!(reply.deliver().is_err());
        drop(held);
    }
    assert!(submit("000002").await.is_err());
    true
}

pub(super) async fn setup(
    c: &Arc<Coordinator>,
    caller: &ClaimCaller,
    hosted: &Hosted,
    receipt: Uuid,
    predecessor: BindingId,
    cut: &str,
) {
    let generation = c.state().generation;
    let issue = || c.prepare_claim_totp_setup(caller, generation, receipt, predecessor);
    let mut request = Box::pin(issue());
    if cut == "factor_setup_path" {
        assert_eq!(request.await.err().unwrap().code, "invalid_action");
        assert_eq!(hosted.setups.load(Ordering::SeqCst), 0);
        return;
    }
    tokio::select! { _ = &mut request => panic!("setup is held"), _ = hosted.setup_entered.notified() => () }
    assert!(c.operation.try_lock().is_ok() && c.credential_io.try_lock().is_ok());
    assert!(issue().await.is_err());
    if cut == "factor_setup_cancel" {
        caller.retire();
        assert!(request.await.is_err()); // cancellation does not wait for hosted
        return;
    }
    hosted.setup_release.notify_one();
    if cut == "factor_setup_invalid" {
        assert_eq!(request.await.err().unwrap().code, "invalid_response");
        return;
    }
    let receipt = request.await.unwrap();
    let public = serde_json::to_string(&c.state()).unwrap();
    assert!(![KEY, "fixture-setup-token", "otpauth://", "untrusted-setup"]
        .iter()
        .any(|s| public.contains(*s)));
    assert!(issue().await.is_err());
    match cut {
        "factor_setup_drop" => {
            drop(receipt);
            assert!(!caller.monitoring());
        }
        "factor_setup_replaced" => {
            {
                let _inner = c.inner.lock().unwrap();
                c.supersede_binding();
            }
            assert!(receipt.deliver().is_err());
        }
        "factor_setup_ok" => {
            // Delivery under the shell's role lock cannot block on inner or
            // accidentally prefer the server-supplied URI/SVG over the seed.
            let held = c.inner.lock().unwrap();
            let direct = receipt.deliver().unwrap();
            drop(held);
            assert_eq!(direct.manual_key, KEY);
            assert_eq!(direct.qr_payload, "otpauth://totp/avibe.bot%3Afixture%40example.test?secret=ABCDEFGHIJKLMNOPQRSTUVWXYZ234567&issuer=avibe.bot&algorithm=SHA1&digits=6&period=30");
        }
        _ => panic!("unknown setup cut"),
    }
    assert!(issue().await.is_err());
    assert_eq!(hosted.setups.load(Ordering::SeqCst), 1);
}
