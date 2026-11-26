//! All jars here are synthetic. Inject faults at the credential boundary, not
//! in the coordinator's decision about whether to publish, retry, or roll back.
use super::*;
use std::collections::VecDeque;
use std::sync::mpsc;

enum Write {
    Normal,
    Refuse,
    ChangedThenError,
    UnexpectedValue,
    Hold(oneshot::Sender<()>, mpsc::Receiver<()>),
}
#[derive(Default)]
pub(super) struct Memory {
    jar: Mutex<Option<Vec<u8>>>,
    writes: Mutex<VecDeque<Write>>,
    replace_after_read: Mutex<Option<Vec<u8>>>,
    attempts: AtomicU64,
    pub(super) changed: AtomicU64,
    boot: bool,
}
impl Memory {
    pub(super) fn refuse_one_save(&self) {
        self.writes.lock().unwrap().push_back(Write::Refuse);
    }
    pub(super) fn allow_one_save(&self) {
        self.writes.lock().unwrap().push_back(Write::Normal);
    }
    pub(super) fn seed(&self, bytes: Vec<u8>) {
        *self.jar.lock().unwrap() = Some(bytes);
    }
}
impl Credentials for Memory {
    fn read(&self, id: &str) -> Result<Option<Vec<u8>>> {
        assert_eq!(id, "fixture-a");
        let mut jar = self.jar.lock().unwrap();
        let current = jar.clone();
        if let Some(replacement) = self.replace_after_read.lock().unwrap().take() {
            *jar = Some(replacement);
        }
        Ok(current)
    }
    fn write(&self, id: &str, bytes: &[u8]) -> Result<()> {
        assert_eq!(id, "fixture-a");
        self.attempts.fetch_add(1, Ordering::SeqCst);
        let fault = self
            .writes
            .lock()
            .unwrap()
            .pop_front()
            .expect("unexpected credential write");
        if matches!(&fault, Write::Refuse) {
            return Err(ClientError::new("credentials_unavailable"));
        }
        self.seed(bytes.to_vec());
        match fault {
            Write::ChangedThenError => Err(ClientError::new("credentials_unavailable")),
            Write::UnexpectedValue => {
                self.seed(b"unattributed fixture".to_vec());
                Ok(())
            }
            Write::Hold(entered, release) => {
                entered.send(()).unwrap();
                release.recv_timeout(Duration::from_secs(4)).unwrap();
                Ok(())
            }
            _ => Ok(()),
        }
    }
    fn erase(&self, _: &str) -> Result<()> {
        panic!("a same-key adoption cannot erase")
    }
}
#[async_trait::async_trait]
impl ClientView for Memory {
    fn changed(&self, s: &ClientState) {
        match s.account.status {
            AccountStatus::SecondFactorRequired => {
                assert!(s.account.identity.is_none());
                assert_eq!(s.account.email.as_deref(), Some("fixture@example.test"));
            }
            AccountStatus::SignedIn => assert_eq!(s.account.identity.as_ref().unwrap().id, "fixture-a"),
            _ => panic!("unexpected account publication"),
        }
        self.changed.fetch_add(1, Ordering::SeqCst);
    }
    fn invalidate(&self, _: u64) {
        assert!(self.boot, "ordinary jar publication is not a navigation");
    }
    async fn commit(&self, _: Epoch, _: ClientTarget, _: Vec<cookie::Cookie<'static>>) -> Result<()> {
        panic!("unexpected view commit")
    }
    async fn restore(
        &self,
        _: Epoch,
        _: ClientTarget,
        _: crate::instance_client::protocol::RestoreDestination,
    ) -> Result<()> {
        panic!("unexpected view restore")
    }
    async fn signed_out(&self, _: Epoch, _: ClientTarget, _: bool) -> Result<()> {
        panic!("unexpected sign-out")
    }
}

// Rebuild from the two durable inputs alone. No old Coordinator, slot, proof or
// document is supplied. This models the two crash cuts, not platform atomicity.
struct SessionGet(&'static str);
#[async_trait::async_trait]
impl Transport for SessionGet {
    async fn send(&self, req: Request) -> Result<Response> {
        assert_eq!(req.method, reqwest::Method::GET);
        assert_eq!(req.url.as_str(), "https://avibe.bot/api/mobile/session");
        assert_eq!(req.headers[reqwest::header::COOKIE], self.0);
        Ok(Response {
            status: 200,
            headers: Default::default(),
            body: serde_json::to_vec(
                &json!({"account":{"id":"fixture-a","email":"fixture@example.test"},"instances":[]}),
            )
            .unwrap(),
        })
    }
}
async fn cold_restore(
    record: &crate::instance_client::storage::ConnectionRecord,
    bytes: Vec<u8>,
    cookie: &'static str,
) {
    let root = tempfile::tempdir().unwrap();
    let store = ConnectionStore::new(root.path().join("connection.json"));
    store.save(record).unwrap();
    let saved = store.load("unused").unwrap();
    let mem = Arc::new(Memory {
        boot: true,
        ..Default::default()
    });
    mem.seed(bytes);
    mem.writes.lock().unwrap().push_back(Write::Normal);
    let boot = Coordinator::new(
        ClientState::new("fixture computer".into(), "0.1.0".into(), json!({})),
        saved,
        store,
        Arc::new(SessionGet(cookie)),
        mem.clone(),
        Arc::new(NoPlatform),
        mem.clone(),
    );
    boot.restore_account().await.unwrap();
    assert_eq!(boot.state().account.identity.unwrap().id, "fixture-a");
    assert_eq!(mem.attempts.load(Ordering::SeqCst), 1);
    assert_eq!(recovery::inspect(&Arc::new(boot)).await, json!({"state":"unavailable"}));
}

pub(super) async fn exercise(c: &Arc<Coordinator>, mem: &Arc<Memory>, cut: &str) {
    let old = mem.read("fixture-a").unwrap().unwrap();
    let account_is_old = || {
        c.inner
            .lock()
            .unwrap()
            .account
            .session
            .cookie_value("session")
            .as_deref()
            == Some("synthetic")
    };
    assert!(account_is_old());
    let save = recovery::offer(c).await;
    if matches!(cut, "proof_start_save_absent_prior" | "proof_start_save_invalid_prior") {
        let prior = (cut == "proof_start_save_invalid_prior").then(|| b"not-a-jar".to_vec());
        *mem.jar.lock().unwrap() = prior.clone();
        assert_eq!(recovery::resume(c, &save).await.unwrap()["state"], "available");
        assert!(mem.read("fixture-a").unwrap() == prior && account_is_old());
        assert_eq!(mem.attempts.load(Ordering::SeqCst), 0);
        assert_eq!(mem.changed.load(Ordering::SeqCst), 0);
        return;
    }
    if cut == "proof_start_save_mismatch" {
        mem.writes.lock().unwrap().push_back(Write::UnexpectedValue);
        assert_eq!(
            recovery::resume(c, &save).await.unwrap(),
            json!({"state":"storage_blocked"})
        );
        assert!(c.credential_io.lock().await.cleanup_error.is_some());
        assert!(account_is_old());
        assert!(mem.read("fixture-a").unwrap().as_deref() == Some(&b"unattributed fixture"[..]));
        assert_eq!(recovery::inspect(c).await, json!({"state":"storage_blocked"}));
        assert!(recovery::resume(c, &save).await.is_err());
        assert_eq!(
            mem.attempts.load(Ordering::SeqCst),
            1,
            "never write over an unattributed value"
        );
        assert_eq!(mem.changed.load(Ordering::SeqCst), 0);
        return;
    }
    let record = c.store.load("unused").unwrap();
    let mut post_key = None;
    if cut == "proof_start_bound" {
        mem.writes
            .lock()
            .unwrap()
            .extend([Write::Refuse, Write::ChangedThenError]);
        let refused = recovery::resume(c, &save).await.unwrap();
        assert_eq!(refused["state"], "available");
        assert!(account_is_old() && mem.read("fixture-a").unwrap().unwrap() == old);
        assert_eq!(mem.changed.load(Ordering::SeqCst), 0);
        assert_eq!(
            recovery::resume(c, refused["receipt"].as_str().unwrap()).await.unwrap(),
            json!({"state":"saved"})
        );
    } else {
        let (entered, started) = oneshot::channel();
        let (release, held) = mpsc::channel();
        mem.writes.lock().unwrap().push_back(Write::Hold(entered, held));
        let worker = c.clone();
        let receipt = save.clone();
        let waiter = tokio::spawn(async move { recovery::resume(&worker, &receipt).await });
        started.await.unwrap();
        assert!(c.credential_io.try_lock().is_err());
        assert!(c.operation.try_lock().is_ok() && c.claims.try_lock().is_ok());
        assert!(account_is_old());
        let superseded = cut != "proof_start_save_drop";
        if !superseded {
            // Key has changed, but readback and RAM publication cannot yet have run.
            post_key = Some((c.store.load("unused").unwrap(), mem.read("fixture-a").unwrap().unwrap()));
            c.desktop_update(json!({"fixture":"during-write"}));
        }
        if superseded {
            // The actual ordinary request must evict the slot at admission, even
            // if its caller disappears while queued behind the credential writer.
            use crate::instance_client::protocol::{ClientAction, ClientRequest};
            let queued = c.request(ClientRequest {
                version: 1,
                request_id: "fixture-refresh".into(),
                expected_generation: c.state().generation,
                action: ClientAction::RefreshInstances,
            });
            tokio::select! { biased;
                _ = queued => panic!("credential owner was held"),
                _ = std::future::ready(()) => (),
            }
            assert_eq!(recovery::inspect(c).await, json!({"state":"unavailable"}));
            if cut == "proof_start_save_changed_before_undo" {
                // Replaced after the writer's first positive read, before its fenced
                // RAM commit can undo. Do not overwrite it based on the earlier read.
                *mem.replace_after_read.lock().unwrap() = Some(b"unrelated replacement".to_vec());
            } else {
                mem.writes.lock().unwrap().push_back(match cut {
                    "proof_start_save_rollback_error" => Write::Refuse,
                    "proof_start_save_rollback_late_error" => Write::ChangedThenError,
                    _ => Write::Normal,
                });
            }
        }
        waiter.abort();
        assert!(waiter.await.unwrap_err().is_cancelled());
        release.send(()).unwrap();
        let owner = c.credential_io.lock().await;
        let blocked = matches!(
            cut,
            "proof_start_save_rollback_error" | "proof_start_save_changed_before_undo"
        );
        assert_eq!(owner.cleanup_error.is_some(), blocked);
        drop(owner);
        if superseded {
            assert!(account_is_old());
            assert_eq!(recovery::inspect(c).await, json!({"state":"unavailable"}));
            if cut == "proof_start_save_changed_before_undo" {
                assert_eq!(mem.attempts.load(Ordering::SeqCst), 1);
                assert!(mem.read("fixture-a").unwrap().as_deref() == Some(&b"unrelated replacement"[..]));
            } else {
                assert_eq!(mem.attempts.load(Ordering::SeqCst), 2);
                assert_eq!(mem.read("fixture-a").unwrap().unwrap() == old, !blocked);
            }
            assert_eq!(mem.changed.load(Ordering::SeqCst), 0);
            return;
        }
    }
    assert_eq!(recovery::inspect(c).await, json!({"state":"unavailable"}));
    assert!(recovery::resume(c, &save).await.is_err());
    assert!(mem.writes.lock().unwrap().is_empty());
    let disk = mem.read("fixture-a").unwrap().unwrap();
    let restored = NativeSession::restore_account(c.transport.clone(), &disk).unwrap();
    assert_eq!(restored.cookie_value("ordinary").as_deref(), Some("new"));
    assert!(restored.cookie_value("session").is_none());
    assert!(!String::from_utf8_lossy(&disk).contains("__Host-avibe-sign-in-"));
    let record_after = c.store.load("unused").unwrap();
    assert_eq!(
        serde_json::to_value(&record_after).unwrap(),
        serde_json::to_value(&record).unwrap()
    );
    if let Some((at_key, bytes)) = post_key {
        assert_eq!(c.state().desktop.update, json!({"fixture":"during-write"}));
        cold_restore(&record, old, "session=synthetic").await;
        cold_restore(&at_key, bytes, "ordinary=new").await;
    }
    let ordinary = c.inner.lock().unwrap().account.session.clone();
    assert!(ordinary.account_attempt(Some(&ordinary)).is_ok()); // not fenced to the retired slot
    assert_eq!(ordinary.cookie_value("ordinary").as_deref(), Some("new"));
}
