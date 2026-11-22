//! A new credential is not a second confirmation. Keep the same private
//! peer alive to distinguish no grant from an accidentally re-opened ticket.
use super::*;
use crate::instance_client::{
    machine::tests::Peer,
    protocol::{ClientAction, ClientRequest},
};

pub(super) async fn exercise(
    c: &Arc<Coordinator>,
    memory: &save::Memory,
    hosted: &Hosted,
    mut peer: Peer,
    caller: &ClaimCaller,
    handles: (Uuid, Uuid),
    cut: &str,
) {
    let (receipt, refused) = handles;
    let generation = c.state().generation;
    let again = || c.reread_after_proof(caller, generation, receipt);
    let confirm = |r| c.confirm_claim(caller, generation, receipt, r);
    let save = recovery::offer(c).await;
    assert!(again().await.is_err()); // discovery alone is not key publication
    if cut == "unsaved" {
        {
            let _inner = c.inner.lock().unwrap();
            c.supersede_binding();
        }
        assert!(recovery::resume(c, &save).await.is_err() && again().await.is_err());
        assert_eq!(hosted.reads.load(Ordering::SeqCst), 2);
        assert_eq!(hosted.ticket_reads.load(Ordering::SeqCst), 1);
        peer.assert_unarmed();
        return;
    }
    memory.allow_one_save();
    assert_eq!(recovery::resume(c, &save).await.unwrap(), json!({"state":"saved"}));
    let factor = cut.starts_with("factor_");
    if factor && !super::factor::challenge(c, memory, hosted, caller, receipt, cut).await {
        assert_eq!(hosted.grants.load(Ordering::SeqCst), 1);
        return;
    }
    let stored = memory.read("fixture-a").unwrap().unwrap();
    assert!(c.reread_after_proof(caller, generation, Uuid::new_v4()).await.is_err());
    assert!(confirm(refused).await.is_err());
    peer.assert_unarmed();
    let read = again().await;
    assert_eq!(hosted.reads.load(Ordering::SeqCst), if factor { 5 } else { 3 });
    assert_eq!(
        hosted.ticket_reads.load(Ordering::SeqCst),
        if matches!(cut, "subject" | "mfa") { 1 } else { 2 }
    );
    assert_eq!(hosted.grants.load(Ordering::SeqCst), 1);
    if matches!(cut, "subject" | "target" | "mfa") {
        assert_eq!(
            read.err().unwrap().code,
            if cut == "mfa" {
                "two_factor_required"
            } else {
                "decision_changed"
            }
        );
        assert!(peer.arm().await.is_err());
    } else {
        let reply = read.unwrap();
        let ReplyBody::Review(fresh, _) = &reply.body else {
            panic!("expected a fresh review")
        };
        let fresh = *fresh;
        assert_ne!(fresh, refused);
        assert!(confirm(fresh).await.is_err()); // private reply not yet delivered
        assert!(confirm(refused).await.is_err());
        if cut == "close" {
            caller.retire();
        }
        if cut == "replaced" {
            let _held = c.credential_io.lock().await;
            let ordinary = c.request(ClientRequest {
                version: 1,
                request_id: "fixture-refresh".into(),
                expected_generation: generation,
                action: ClientAction::RefreshInstances,
            });
            tokio::select! { biased; _ = ordinary => panic!("credential writer held"), _ = std::future::ready(()) => () }
        }
        if matches!(cut, "close" | "replaced") {
            assert!(reply.deliver().is_err() && confirm(fresh).await.is_err());
            assert!(peer.arm().await.is_err());
        } else {
            let projection = reply.deliver().unwrap().unwrap();
            assert_eq!(projection["screen"]["prefix"], "desk");
            peer.assert_unarmed();
            assert_eq!(hosted.grants.load(Ordering::SeqCst), 1);
            if cut == "refused" {
                let DecisionOutcome::Terminal(reply) = confirm(fresh).await.unwrap() else {
                    panic!("unexpected second offer")
                };
                assert_eq!(reply.deliver(c).unwrap_err().code, "reauthentication_failed");
                assert!(peer.arm().await.is_err());
            } else {
                let second = confirm(fresh);
                tokio::pin!(second);
                let arm = tokio::select! { _ = &mut second => panic!("outcome held"), r = peer.arm() => r.unwrap() };
                assert_eq!(arm["hostname"], "desk-app.avibe.bot");
                let (_adoption, nonce, ack) = peer.published();
                let challenge = tokio::select! { _ = &mut second => panic!("recheck held"), n = nonce => n.unwrap() };
                assert_eq!(challenge.len(), 32);
                ack.send(Ok(())).unwrap();
                let DecisionOutcome::Terminal(reply) = second.await.unwrap() else {
                    panic!("original owner unverified")
                };
                assert!(reply.deliver(c).unwrap());
            }
            assert!(confirm(fresh).await.is_err());
            assert_eq!(hosted.grants.load(Ordering::SeqCst), 2);
        }
    }
    assert_eq!(hosted.mails.load(Ordering::SeqCst), 1);
    assert_eq!(memory.read("fixture-a").unwrap().unwrap(), stored);
    assert_eq!(
        c.inner
            .lock()
            .unwrap()
            .account
            .session
            .cookie_value("ordinary")
            .as_deref(),
        Some(if factor { "after-factor" } else { "new" })
    );
}
