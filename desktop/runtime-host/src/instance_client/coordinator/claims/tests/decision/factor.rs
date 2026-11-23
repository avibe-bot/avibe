//! The saved first factor is a valid ordinary account transaction, but is not
//! a verified second factor or permission to use its ticket. All I/O is fake.
use super::*;
use crate::instance_client::protocol::CodeMethod;

pub(super) async fn challenge(
    c: &Arc<Coordinator>,
    memory: &save::Memory,
    hosted: &Hosted,
    caller: &ClaimCaller,
    handles: (Uuid, Uuid),
    cut: &str,
) -> Option<Uuid> {
    let (receipt, first_token) = handles;
    let generation = c.state().generation;
    let predecessor = c.binding.lock().unwrap().committed().unwrap();
    let first = memory.read("fixture-a").unwrap().unwrap();
    assert_eq!(c.state().account.status, AccountStatus::SecondFactorRequired);
    assert!(c.state().account.identity.is_none());
    let again = |token: Uuid| {
        output::dispatch(
            c,
            caller,
            generation,
            json!({"type":"review_after_proof","receipt":receipt.to_string(),"submission":token.to_string()}),
        )
    };
    let submit = |method: CodeMethod, code: &'static str| {
        output::dispatch(
            c,
            caller,
            generation,
            json!({"type":"submit_factor_code","receipt":receipt.to_string(),"submission":first_token.to_string(),"method":method,"code":code}),
        )
    };
    assert!(submit(CodeMethod::Totp, "bad").await.is_err());
    assert_eq!(
        again(first_token).await.unwrap().deliver().unwrap_err().code,
        "authentication_required"
    );
    if cut.starts_with("factor_setup_") {
        assert!(submit(CodeMethod::Totp, "000002").await.is_err());
        enroll::setup(c, caller, hosted, receipt, first_token, cut).await;
        assert_eq!(hosted.factors.load(Ordering::SeqCst), 0);
        assert_eq!(hosted.reads.load(Ordering::SeqCst), 2);
        assert_eq!(memory.read("fixture-a").unwrap().unwrap(), first);
        return None;
    }
    if cut == "factor_path" {
        assert!(submit(CodeMethod::Totp, "000002").await.is_err());
        assert_eq!(hosted.factors.load(Ordering::SeqCst), 0);
        return None;
    }
    let next = if cut.starts_with("factor_enroll_") {
        let Ok(token) = enroll::finish(c, caller, hosted, receipt, first_token, cut).await else {
            assert_eq!(hosted.factors.load(Ordering::SeqCst), 1);
            assert_eq!(hosted.reads.load(Ordering::SeqCst), 2);
            assert_eq!(memory.read("fixture-a").unwrap().unwrap(), first);
            return None;
        };
        token
    } else {
        let recovery = cut.starts_with("factor_recovery");
        if recovery {
            assert!(submit(CodeMethod::Totp, "000002").await.is_err());
        }
        let verify: passkey::Verify<'_> = if cut.starts_with("factor_pk_") {
            let Some(verify) = passkey::prepare(c, caller, hosted, receipt, first_token, cut).await else {
                assert_eq!(hosted.factors.load(Ordering::SeqCst), 0);
                assert_eq!(hosted.reads.load(Ordering::SeqCst), 2);
                assert_eq!(memory.read("fixture-a").unwrap().unwrap(), first);
                return None;
            };
            verify
        } else if recovery {
            Box::pin(submit(
                CodeMethod::Recovery,
                if cut == "factor_recovery_retry" {
                    "fixture-wrong"
                } else {
                    "fixture-backup"
                },
            ))
        } else {
            Box::pin(submit(CodeMethod::Totp, "000002"))
        };
        tokio::pin!(verify);
        tokio::select! { _ = &mut verify => panic!("factor response held"), _ = hosted.factor_entered.notified() => () }
        assert!(c.operation.try_lock().is_ok() && c.credential_io.try_lock().is_ok());
        assert!(submit(CodeMethod::Totp, "000002").await.is_err());
        if matches!(cut, "factor_close" | "factor_pk_close") {
            caller.retire();
        }
        hosted.factor_release.notify_one();
        let direct = verify.await.unwrap();
        if cut == "factor_unknown_late" {
            caller.retire();
        }
        let mut outcome = direct.deliver();
        if matches!(
            cut,
            "factor_pk_denied_retry" | "factor_totp_retry" | "factor_recovery_retry"
        ) {
            assert_eq!(
                outcome.unwrap_err().code,
                if cut == "factor_pk_denied_retry" {
                    "invalid_passkey_assertion"
                } else {
                    "invalid_or_expired"
                }
            );
            assert_eq!(hosted.factors.load(Ordering::SeqCst), 1);
            assert!(!hosted.factor_accepted.load(Ordering::SeqCst));
            assert!(c.binding.lock().unwrap().committed() == Some(predecessor));
            assert_eq!(memory.read("fixture-a").unwrap().unwrap(), first);
            hosted.factor_release.notify_one();
            let retry = if cut == "factor_totp_retry" {
                submit(CodeMethod::Totp, "000003")
            } else {
                submit(CodeMethod::Recovery, "fixture-backup")
            };
            outcome = retry.await.unwrap().deliver();
        }
        if matches!(
            cut,
            "factor_unknown" | "factor_unknown_late" | "factor_malformed" | "factor_pk_unknown"
        ) {
            assert_eq!(
                outcome.unwrap_err().code,
                if cut == "factor_unknown_late" {
                    "stale_generation"
                } else {
                    "factor_delivery_unknown"
                }
            );
            assert_eq!(recovery::inspect(c).await, json!({"state":"unavailable"}));
            assert!(!caller.monitoring());
            assert!(submit(CodeMethod::Recovery, "fixture-backup").await.is_err());
            assert_eq!(hosted.factors.load(Ordering::SeqCst), 1);
            assert_eq!(hosted.reads.load(Ordering::SeqCst), 2);
            assert_eq!(memory.read("fixture-a").unwrap().unwrap(), first);
            return None;
        }
        if matches!(cut, "factor_close" | "factor_pk_close") {
            assert_eq!(outcome.unwrap_err().code, "stale_generation");
            None
        } else {
            Some(output::submission(outcome.unwrap()))
        }
    };
    if cut == "factor_pk_denied_retry" {
        let token = next.expect("accepted recovery submission");
        hosted.factor_publication_read.notified().await;
        assert_eq!(c.state().account.status, AccountStatus::SecondFactorRequired);
        assert!(again(first_token).await.is_err());
        let reads = hosted.reads.load(Ordering::SeqCst);
        let (one, two) = tokio::join!(again(token), again(token));
        for observation in [one.unwrap(), two.unwrap()] {
            assert_eq!(observation.deliver().unwrap(), Some(json!({"state":"publishing"})));
        }
        assert_eq!(hosted.reads.load(Ordering::SeqCst), reads);
        assert_eq!(hosted.ticket_reads.load(Ordering::SeqCst), 1);
        hosted.release_publication_read.notify_one();
    }
    let found = recovery::read_once(c).await;
    if cut == "factor_subject" {
        assert_eq!(found, json!({"state":"unavailable"}));
        assert!(!caller.monitoring(), "terminal discovery must release this ticket");
        assert_eq!(recovery::inspect(c).await, json!({"state":"unavailable"}));
        assert_eq!(memory.read("fixture-a").unwrap().unwrap(), first);
        assert!(again(next.unwrap_or(first_token)).await.is_err());
        return None;
    }
    let save = found["receipt"].as_str().expect("successful GET owes only Save");
    memory.refuse_one_save();
    let refused = recovery::resume(c, save).await.unwrap();
    assert_eq!(refused["state"], "available");
    assert_ne!(refused["receipt"], save);
    assert!(recovery::resume(c, save).await.is_err());
    assert_eq!(memory.read("fixture-a").unwrap().unwrap(), first);
    assert!(again(next.unwrap_or(first_token)).await.is_err());
    memory.allow_one_save();
    assert_eq!(
        recovery::resume(c, refused["receipt"].as_str().unwrap()).await.unwrap(),
        json!({"state":"saved"})
    );
    assert!(c
        .binding
        .lock()
        .unwrap()
        .committed()
        .is_some_and(|id| id != predecessor));
    assert!(submit(CodeMethod::Totp, "000002").await.is_err());
    let bytes = memory.read("fixture-a").unwrap().unwrap();
    let disk = NativeSession::restore_account(c.transport.clone(), &bytes).unwrap();
    assert_eq!(disk.cookie_value("ordinary").as_deref(), Some("after-factor"));
    assert_eq!(c.state().account.identity.unwrap().id, "fixture-a");
    assert_eq!(
        hosted.factors.load(Ordering::SeqCst),
        if matches!(
            cut,
            "factor_pk_denied_retry" | "factor_enroll_retry" | "factor_totp_retry" | "factor_recovery_retry"
        ) {
            2
        } else {
            1
        }
    );
    assert_eq!(hosted.mails.load(Ordering::SeqCst), 1);
    assert_eq!(hosted.grants.load(Ordering::SeqCst), 1);
    if matches!(cut, "factor_close" | "factor_pk_close" | "factor_enroll_close") {
        assert!(again(next.unwrap_or(first_token)).await.is_err());
        return None;
    }
    next
}
