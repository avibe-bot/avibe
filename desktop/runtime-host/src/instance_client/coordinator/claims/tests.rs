mod decision;
use super::*;
use serde_json::json;

fn account() -> Account {
    Account {
        id: "fixture-account".into(),
        email: "x@example.test".into(),
    }
}
fn document(owner: &Arc<Mutex<Claims>>) -> ClaimDocument {
    let mut o = owner.lock().unwrap();
    o.retire();
    ClaimDocument {
        owner: owner.clone(),
        epoch: o.epoch(),
    }
}

#[test]
fn first_read_consumes_only_its_receipt_and_cancel_wakes_the_outstanding_read_and_child() {
    for close_document in [false, true] {
        let owner = Arc::new(Mutex::new(Claims::default()));
        let doc = document(&owner);
        let old = doc.caller().unwrap();
        let receipt = owner.lock().unwrap().reserve(account()).unwrap();
        let (wake, mut http) = oneshot::channel();
        let mut pipe = owner.lock().unwrap().begin(receipt, &account(), wake).unwrap();
        let late_work = Borrow {
            owner: &owner,
            receipt,
            returned: false,
        };
        // A duplicate transition cannot replace either cancellation sender.
        assert!(owner
            .lock()
            .unwrap()
            .begin(receipt, &account(), oneshot::channel().0)
            .is_err());
        assert!(matches!(pipe.try_recv(), Err(oneshot::error::TryRecvError::Empty)));
        assert!(matches!(http.try_recv(), Err(oneshot::error::TryRecvError::Empty)));
        if close_document {
            drop(doc);
        } else {
            owner.lock().unwrap().cancel(receipt).unwrap();
            drop(doc);
        }
        assert!(old.check().is_err());
        assert!(matches!(pipe.try_recv(), Err(oneshot::error::TryRecvError::Closed)));
        assert!(matches!(http.try_recv(), Err(oneshot::error::TryRecvError::Closed)));
        let new_doc = document(&owner);
        let new = new_doc.caller().unwrap();
        let successor = owner.lock().unwrap().reserve(account()).unwrap();
        drop(late_work);
        old.retire();
        assert!(owner.lock().unwrap().cancel(receipt).is_err());
        assert!(new.monitoring());
        let (wake, _http) = oneshot::channel();
        let mut pipe = owner.lock().unwrap().begin(successor, &account(), wake).unwrap();
        assert!(matches!(pipe.try_recv(), Err(oneshot::error::TryRecvError::Empty)));
    }
}

#[tokio::test(start_paused = true)]
async fn an_unconsumed_receipt_expires_even_if_no_visibility_tick_or_reply_ran() {
    for deliver_first in [false, true] {
        let owner = Arc::new(Mutex::new(Claims::default()));
        let doc = document(&owner);
        let caller = doc.caller().unwrap();
        let receipt = owner.lock().unwrap().reserve(account()).unwrap();
        let reply = ClaimReply {
            account: Epoch::new(Arc::new(AtomicU64::new(1)), 1),
            caller: caller.clone(),
            body: ReplyBody::Reserved(receipt),
        };
        let mut other = account();
        other.id = "different".into();
        assert!(owner
            .lock()
            .unwrap()
            .begin(receipt, &other, oneshot::channel().0)
            .is_err());
        tokio::time::advance(RECEIPT_TTL).await;
        if deliver_first {
            assert!(reply.deliver().is_err());
        } else {
            assert!(owner
                .lock()
                .unwrap()
                .begin(receipt, &account(), oneshot::channel().0)
                .is_err());
            assert!(reply.deliver().is_err());
        }
        assert!(!caller.monitoring());
        assert!(owner.lock().unwrap().active.is_none());
    }
}
#[test]
fn the_direct_review_command_has_separate_receipt_and_decision_inputs() {
    let body = |action| json!({"expected_generation":1,"action":action});
    for action in [
        json!({"type":"open"}),
        json!({"type":"observe_current_binding"}),
        json!({"type":"read","receipt":"opaque"}),
        json!({"type":"cancel","receipt":"opaque"}),
        json!({"type":"check","revision":"opaque","prefix":"fixture"}),
        json!({"type":"confirm","receipt":"x","revision":"y"}),
        json!({"type":"send_proof_code","receipt":"x","offer":"y"}),
    ] {
        assert!(serde_json::from_value::<ClaimRequest>(body(action)).is_ok());
    }
    for input in [
        body(json!({"type":"open","user_code":"private"})),
        body(json!({"type":"open","receipt":"chosen"})),
        body(json!({"type":"observe_current_binding","receipt":"old"})),
        body(json!({"type":"observe_current_binding","instance_id":"other"})),
        body(json!({"type":"observe_current_binding","enable":true})),
        body(json!({"type":"read","revision":"x"})),
        body(json!({"type":"check","receipt":"x","prefix":"fixture"})),
        body(json!({"type":"check","revision":"x","prefix":"fixture","existing_instance_id":"other"})),
        body(json!({"type":"grant"})),
        body(json!({"type":"confirm","receipt":"x","revision":"y","hostname":"another"})),
        body(json!({"type":"send_proof_code","receipt":"x","offer":"y","email":"other@example.test"})),
        body(json!({"type":"send_proof_code","receipt":"x","revision":"y"})),
        body(json!({"type":"send_claim_code","revision":"y"})),
        body(json!({"type":"submit_proof_code","receipt":"r","delivery":"d","code":"000001","account":"chosen"})),
        body(json!({"type":"review_after_proof","receipt":"r","binding_id":"chosen"})),
        body(json!({"type":"submit_factor_code","receipt":"r","submission":"s","method":"passkey","code":"000001"})),
        body(json!({"type":"prepare_totp_setup","receipt":"r","submission":"s","url":"https://example.test"})),
        body(json!({"type":"finish_totp_setup","receipt":"r","submission":"s","code":"000001"})),
        body(json!({"type":"cancel","revision":"x"})),
        body(json!({"type":"cancel","receipt":"x","account":"other"})),
        json!({"expected_generation":1,"action":{"type":"open"},"url":"https://example.test"}),
    ] {
        assert!(serde_json::from_value::<ClaimRequest>(input).is_err());
    }
}
