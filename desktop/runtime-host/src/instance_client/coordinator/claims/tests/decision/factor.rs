//! The saved first factor is a valid ordinary account transaction, but is not
//! a verified second factor or permission to use its ticket. All I/O is fake.
use super::*;
use crate::instance_client::protocol::CodeMethod;

pub(super) async fn challenge(
    c: &Arc<Coordinator>,
    memory: &save::Memory,
    hosted: &Hosted,
    caller: &ClaimCaller,
    receipt: Uuid,
    cut: &str,
) -> bool {
    let generation = c.state().generation;
    let predecessor = c.binding.lock().unwrap().committed().unwrap();
    let first = memory.read("fixture-a").unwrap().unwrap();
    assert_eq!(c.state().account.status, AccountStatus::SecondFactorRequired);
    assert!(c.state().account.identity.is_none());
    assert!(c.reread_after_proof(caller, generation, receipt).await.is_err());
    let submit = |method, code: &'static str| {
        c.submit_claim_factor_code(caller, generation, receipt, predecessor, method, code.into())
    };
    assert!(submit(CodeMethod::Totp, "bad").await.is_err());
    if cut.starts_with("factor_setup_") {
        assert!(submit(CodeMethod::Totp, "000002").await.is_err());
        enroll::setup(c, caller, hosted, receipt, predecessor, cut).await;
        assert_eq!(hosted.factors.load(Ordering::SeqCst), 0);
        assert_eq!(hosted.reads.load(Ordering::SeqCst), 2);
        assert_eq!(memory.read("fixture-a").unwrap().unwrap(), first);
        return false;
    }
    if cut == "factor_path" {
        assert!(submit(CodeMethod::Totp, "000002").await.is_err());
        assert_eq!(hosted.factors.load(Ordering::SeqCst), 0);
        return false;
    }
    if cut.starts_with("factor_enroll_") {
        if !enroll::finish(c, caller, hosted, receipt, predecessor, cut).await {
            assert_eq!(hosted.factors.load(Ordering::SeqCst), 1);
            assert_eq!(hosted.reads.load(Ordering::SeqCst), 2);
            assert_eq!(memory.read("fixture-a").unwrap().unwrap(), first);
            return false;
        }
    } else {
        let recovery = cut == "factor_recovery";
        if recovery {
            assert!(submit(CodeMethod::Totp, "000002").await.is_err());
        }
        let verify: passkey::Verify<'_> = if cut.starts_with("factor_pk_") {
            let Some(verify) = passkey::prepare(c, caller, hosted, receipt, predecessor, cut).await else {
                assert_eq!(hosted.factors.load(Ordering::SeqCst), 0);
                assert_eq!(hosted.reads.load(Ordering::SeqCst), 2);
                assert_eq!(memory.read("fixture-a").unwrap().unwrap(), first);
                return false;
            };
            verify
        } else if recovery {
            Box::pin(submit(CodeMethod::Recovery, "fixture-backup"))
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
        let outcome = verify.await;
        if matches!(cut, "factor_unknown" | "factor_malformed") {
            assert_eq!(outcome.unwrap_err().code, "factor_delivery_unknown");
            assert!(c.binding_recovery().is_none() && !caller.monitoring());
            assert!(submit(CodeMethod::Totp, "000002").await.is_err());
            assert_eq!(hosted.factors.load(Ordering::SeqCst), 1);
            assert_eq!(hosted.reads.load(Ordering::SeqCst), 2);
            assert_eq!(memory.read("fixture-a").unwrap().unwrap(), first);
            return false;
        }
        outcome.unwrap();
    }
    let read = c.binding_recovery().unwrap();
    assert_eq!(c.recover_binding(read).await.unwrap_err().code, "network_unavailable");
    let found = c.recover_binding(read).await;
    if cut == "factor_subject" {
        assert_eq!(found.unwrap_err().code, "account_changed");
        assert!(!caller.monitoring(), "terminal discovery must release this ticket");
        assert!(c.binding_recovery().is_none());
        assert_eq!(memory.read("fixture-a").unwrap().unwrap(), first);
        assert!(c.reread_after_proof(caller, generation, receipt).await.is_err());
        return false;
    }
    found.unwrap();
    let save = c.binding_recovery().unwrap();
    memory.refuse_one_save();
    assert_eq!(
        c.recover_binding(save).await.unwrap_err().code,
        "credentials_unavailable"
    );
    assert_eq!(memory.read("fixture-a").unwrap().unwrap(), first);
    assert!(c.reread_after_proof(caller, generation, receipt).await.is_err());
    memory.allow_one_save();
    c.recover_binding(save).await.unwrap();
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
    assert_eq!(hosted.factors.load(Ordering::SeqCst), 1);
    assert_eq!(hosted.mails.load(Ordering::SeqCst), 1);
    assert_eq!(hosted.grants.load(Ordering::SeqCst), 1);
    if matches!(cut, "factor_close" | "factor_pk_close" | "factor_enroll_close") {
        assert!(c.reread_after_proof(caller, generation, receipt).await.is_err());
        return false;
    }
    true
}
