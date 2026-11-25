//! AUTH-SETUP-171. Setup material is direct, revocable and never itself a
//! credential commit. Drive the real coordinator with a synthetic hosted reply.
use super::*;

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
    submission: Uuid,
    cut: &str,
) -> std::result::Result<Option<Uuid>, ()> {
    let generation = c.state().generation;
    let finish = |handle: Uuid, code: &str| {
        output::dispatch(
            c,
            caller,
            generation,
            json!({"type":"finish_totp_setup","receipt":receipt.to_string(),"setup":handle.to_string(),"code":code}),
        )
    };
    let issue = output::dispatch(
        c,
        caller,
        generation,
        json!({"type":"prepare_totp_setup","receipt":receipt.to_string(),"submission":submission.to_string()}),
    );
    tokio::pin!(issue);
    tokio::select! { _ = &mut issue => panic!("setup held"), _ = hosted.setup_entered.notified() => () }
    hosted.setup_release.notify_one();
    let setup = issue.await.unwrap();
    assert!(finish(submission, "000002").await.is_err());
    assert_eq!(hosted.factors.load(Ordering::SeqCst), 0);
    let model = setup.deliver().unwrap().unwrap();
    let handle = opaque(model["setup"].as_str().unwrap()).unwrap();
    assert_ne!(submission, handle);
    assert!(finish(submission, "000002").await.is_err());
    let submit = |code: &'static str| finish(handle, code);
    assert!(submit("０００００２").await.is_err());
    let mut wait = Box::pin(submit("000002"));
    tokio::select! { _ = &mut wait => panic!("final enrollment held"), _ = hosted.factor_entered.notified() => () }
    assert!(c.operation.try_lock().is_ok() && c.credential_io.try_lock().is_ok());
    assert!(submit("000002").await.is_err());
    if cut == "factor_enroll_drop" {
        drop(wait);
        hosted.factor_release.notify_one();
        // The suite bounds this scenario. Wait for the owning worker, not a sleep.
        recovery::offer(c).await;
        assert!(submit("000002").await.is_err());
        return Ok(None);
    }
    if cut == "factor_enroll_replaced" {
        let _inner = c.inner.lock().unwrap();
        c.supersede_binding();
    } else if cut == "factor_enroll_close" {
        caller.retire();
    }
    hosted.factor_release.notify_one();
    let mut reply = wait.await.unwrap();
    if matches!(cut, "factor_enroll_retry" | "factor_enroll_expired") {
        assert!(submit("000003").await.is_err(), "refusal has not been delivered");
        let err = reply.deliver().unwrap_err();
        if cut == "factor_enroll_expired" {
            assert_ne!(err.code, "invalid_code");
            assert!(submit("000003").await.is_err());
            assert_eq!(hosted.setups.load(Ordering::SeqCst), 1);
            return Err(());
        }
        assert_eq!(err.code, "invalid_code");
        assert_eq!(hosted.factors.load(Ordering::SeqCst), 1);
        hosted.factor_release.notify_one();
        reply = submit("000003").await.unwrap();
        assert_eq!(hosted.setups.load(Ordering::SeqCst), 1);
    }
    if matches!(
        cut,
        "factor_enroll_unknown" | "factor_enroll_unknown_late" | "factor_enroll_replaced"
    ) {
        if cut == "factor_enroll_unknown_late" {
            caller.retire();
        }
        let Err(err) = reply.deliver() else {
            panic!("unexpected enrollment success");
        };
        if cut == "factor_enroll_unknown" {
            assert_eq!(err.code, "factor_delivery_unknown");
        } else {
            assert_eq!(err.code, "stale_generation");
        }
        assert_eq!(recovery::inspect(c).await, json!({"state":"unavailable"}));
        assert!(submit("000002").await.is_err());
        return Err(());
    }
    recovery::offer(c).await; // ordinary recovery became available independently of this reply
    assert!(!serde_json::to_string(&c.state()).unwrap().contains("aaaaa-bbbb"));
    let next = if cut == "factor_enroll_close" {
        assert!(reply.deliver().is_err());
        None
    } else {
        let held = c.inner.lock().unwrap();
        let value = reply.deliver().unwrap().unwrap();
        drop(held);
        let token = opaque(value["submission"].as_str().unwrap()).unwrap();
        let expected = match cut {
            "factor_enroll_absent" => json!({"state":"not_issued"}),
            "factor_enroll_bad_codes" => json!({"state":"unavailable"}),
            "factor_enroll_ok" | "factor_enroll_retry" => json!({"state":"issued","codes":codes()}),
            _ => panic!("unexpected enrollment result"),
        };
        assert_eq!(
            value,
            json!({"state":"enrollment_accepted","submission":token.to_string(),"recovery":expected})
        );
        Some(token)
    };
    assert!(submit("000002").await.is_err());
    Ok(next)
}

pub(super) async fn setup(
    c: &Arc<Coordinator>,
    caller: &ClaimCaller,
    hosted: &Hosted,
    receipt: Uuid,
    submission: Uuid,
    cut: &str,
) {
    let generation = c.state().generation;
    let issue = || {
        output::dispatch(
            c,
            caller,
            generation,
            json!({"type":"prepare_totp_setup","receipt":receipt.to_string(),"submission":submission.to_string()}),
        )
    };
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
            let direct = receipt.deliver().unwrap().unwrap();
            drop(held);
            let setup = opaque(direct["setup"].as_str().unwrap()).unwrap();
            assert_ne!(setup, submission);
            assert_eq!(
                direct,
                json!({"state":"totp_setup", "setup":setup.to_string(), "manual_key":KEY, "qr_payload":"otpauth://totp/avibe.bot%3Afixture%40example.test?secret=ABCDEFGHIJKLMNOPQRSTUVWXYZ234567&issuer=avibe.bot&algorithm=SHA1&digits=6&period=30"})
            );
        }
        _ => panic!("unknown setup cut"),
    }
    assert!(issue().await.is_err());
    assert_eq!(hosted.setups.load(Ordering::SeqCst), 1);
}
