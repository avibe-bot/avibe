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
    handles: (Uuid, Uuid, Uuid),
    cut: &str,
) {
    let (receipt, refused, first) = handles;
    let generation = c.state().generation;
    let again = |r: Uuid, s: Uuid| {
        output::dispatch(
            c,
            caller,
            generation,
            json!({"type":"review_after_proof","receipt":r.to_string(),"submission":s.to_string()}),
        )
    };
    let confirm = |r| c.confirm_claim(caller, generation, receipt, r);
    let save = recovery::offer(c).await;
    assert!(again(receipt, first).await.is_err()); // discovery alone is not key publication
    if cut == "unsaved" {
        {
            let _inner = c.inner.lock().unwrap();
            c.supersede_binding();
        }
        assert!(recovery::resume(c, &save).await.is_err() && again(receipt, first).await.is_err());
        assert_eq!(hosted.reads.load(Ordering::SeqCst), 2);
        assert_eq!(hosted.ticket_reads.load(Ordering::SeqCst), 1);
        peer.assert_unarmed();
        return;
    }
    memory.allow_one_save();
    assert_eq!(recovery::resume(c, &save).await.unwrap(), json!({"state":"saved"}));
    let factor = cut.starts_with("factor_");
    let current = if factor {
        let Some(next) = super::factor::challenge(c, memory, hosted, caller, (receipt, first), cut).await else {
            assert_eq!(hosted.grants.load(Ordering::SeqCst), 1);
            return;
        };
        assert_ne!(next, first);
        assert!(again(receipt, first).await.is_err());
        next
    } else {
        first
    };
    let stored = memory.read("fixture-a").unwrap().unwrap();
    assert!(again(Uuid::new_v4(), current).await.is_err());
    assert!(confirm(refused).await.is_err());
    peer.assert_unarmed();
    if matches!(cut, "ok" | "refused") {
        let reread = again(receipt, current);
        tokio::pin!(reread);
        if cut == "refused" {
            assert_eq!(
                reread.await.unwrap().deliver().unwrap_err().code,
                "reauthentication_failed"
            );
            assert!(peer.arm().await.is_err());
        } else {
            let arm = tokio::select! { _ = &mut reread => panic!("publication held"), r = peer.arm() => r.unwrap() };
            assert_eq!(arm["hostname"], "desk-app.avibe.bot");
            assert!(
                again(receipt, current).await.is_err(),
                "only one review may admit a grant"
            );
            let (_adoption, nonce, ack) = peer.published();
            let challenge = tokio::select! { _ = &mut reread => panic!("recheck held"), n = nonce => n.unwrap() };
            assert_eq!(challenge.len(), 32);
            ack.send(Ok(())).unwrap();
            assert_eq!(reread.await.unwrap().deliver().unwrap(), Some(json!({"state":"bound"})));
        }
        assert_eq!(hosted.grants.load(Ordering::SeqCst), 2);
        assert_eq!(hosted.ticket_reads.load(Ordering::SeqCst), 2);
        assert_eq!(hosted.mails.load(Ordering::SeqCst), 1);
        assert_eq!(memory.read("fixture-a").unwrap().unwrap(), stored);
        return;
    }
    let read = again(receipt, current).await;
    assert_eq!(hosted.reads.load(Ordering::SeqCst), if factor { 5 } else { 3 });
    assert_eq!(
        hosted.ticket_reads.load(Ordering::SeqCst),
        if cut == "subject" || cut.starts_with("mfa") {
            1
        } else {
            2
        }
    );
    assert_eq!(hosted.grants.load(Ordering::SeqCst), 1);
    if cut.starts_with("mfa_late_") {
        let pending = read.unwrap();
        let successor = if cut == "mfa_late_successor" {
            caller.retire();
            let doc = c.claim_document(generation).unwrap();
            let other = doc.caller().unwrap();
            c.reserve_claim(&other, Epoch::new(c.generation.clone(), generation))
                .unwrap()
                .deliver()
                .unwrap();
            Some((doc, other))
        } else {
            // Keep subject, document, generation and ticket fixed. A different
            // ordinary credential cannot receive the hosted refusal either.
            c.inner.lock().unwrap().account.session = NativeSession::account(c.transport.clone());
            None
        };
        assert_eq!(
            serde_json::to_value(pending.deliver().unwrap_err()).unwrap(),
            json!({"code":"stale_generation"})
        );
        if let Some((_doc, other)) = successor {
            assert!(other.monitoring());
        }
        assert!(peer.arm().await.is_err());
        assert_eq!(memory.read("fixture-a").unwrap().unwrap(), stored);
        return;
    }
    if matches!(cut, "subject" | "target" | "mfa") {
        assert_eq!(
            read.unwrap().deliver().unwrap_err().code,
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
            assert_eq!(projection["screen"]["prefilled_prefix"], "changed-suggestion");
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
