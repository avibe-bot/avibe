use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, AtomicI32, Ordering};
use std::sync::{Arc, OnceLock};
use std::time::Duration;
use tokio::sync::watch;
use tokio::time::Instant;

use super::answers::Completion;
/// Native submit APIs can return before their delivered/history entry exists.
/// Absence is authoritative only once this bounded monotonic grace has passed.
const NATIVE_DELIVERY_GRACE: Duration = Duration::from_secs(30);

/// One delivery, not a replaceable Session-wide identifier. The receipt is
/// reserved before detail reads, and follows queued native work too.
#[derive(Debug)]
pub struct NotificationReceipt {
    id: i32,
    key: String,
    session_id: Option<String>,
    retired: AtomicBool,
    readable: AtomicBool,
    read_reconciliation_eligible: AtomicBool,
    observed_unread: AtomicBool,
    /// A delivery about one new unread message (an Agent's question).
    unread_message: AtomicBool,
    /// Set when a snapshot taken after `unread_message` was confirmed includes
    /// this receipt: the unread read paired with that snapshot was issued after
    /// the message was committed, so its zero is a read, not a stale sample.
    sampled_since_confirmed: AtomicBool,
    posted_at: OnceLock<Instant>,
    /// The OS submission result, independent of later read/dismissal cleanup.
    submission: watch::Sender<Option<bool>>,
    completion: OnceLock<Completion>,
    opens: OnceLock<String>,
}

impl NotificationReceipt {
    pub fn id(&self) -> i32 {
        self.id
    }

    pub fn key(&self) -> &str {
        &self.key
    }

    pub fn session_id(&self) -> Option<&str> {
        self.session_id.as_deref()
    }

    /// Name what a click on this notification opens, as an `avibe://` link.
    /// Only a link the deep-link parser accepts is kept, and only the first.
    pub fn set_opens(&self, link: String) {
        if crate::deep_link::parse_deep_link(&link).is_some() {
            let _ = self.opens.set(link);
        }
    }

    /// The `avibe://` link a click opens: the one named for it, else its
    /// Session's conversation. `None` when neither is a valid deep link.
    pub fn opens(&self) -> Option<String> {
        if let Some(link) = self.opens.get() {
            return Some(link.clone());
        }
        let link = format!("avibe://session/{}", self.session_id.as_deref()?);
        crate::deep_link::parse_deep_link(&link).map(|_| link)
    }

    pub fn confirm_workbench_session(&self, read_reconciliation_eligible: bool) {
        self.readable.store(true, Ordering::SeqCst);
        self.read_reconciliation_eligible
            .store(read_reconciliation_eligible, Ordering::SeqCst);
    }

    /// A delivery about one new unread message in a Workbench Session (an
    /// Agent's question, a `result` the unread count includes). Unlike a run, a
    /// zero count from a read issued after this confirmation retires it even
    /// when no earlier sample saw the Session unread: the question was read on
    /// another device before the shell's next refresh.
    pub fn confirm_unread_message(&self) {
        self.confirm_workbench_session(true);
        self.unread_message.store(true, Ordering::SeqCst);
    }

    pub fn readable(&self) -> bool {
        self.readable.load(Ordering::SeqCst)
    }

    pub fn retired(&self) -> bool {
        self.readable()
            && self.read_reconciliation_eligible.load(Ordering::SeqCst)
            && self.retired.load(Ordering::SeqCst)
    }

    pub fn posted(&self) {
        let _ = self.posted_at.set(Instant::now());
        self.submission.send_replace(Some(true));
    }

    pub(super) fn submission(&self) -> watch::Receiver<Option<bool>> {
        self.submission.subscribe()
    }

    pub(super) fn follow_answer(&self, completion: Completion) {
        let _ = self.completion.set(completion);
    }

    /// Rechecked by the native sink immediately before submission, because
    /// permission and main-thread work may finish after the detail lookup.
    pub fn superseded(&self) -> bool {
        self.completion.get().is_some_and(Completion::superseded)
    }

    fn not_posted(&self) {
        self.submission.send_if_modified(|result| {
            if result.is_none() {
                *result = Some(false);
                true
            } else {
                false
            }
        });
    }

    pub fn awaiting_native_delivery(&self) -> bool {
        self.posted_at
            .get()
            .is_some_and(|at| at.elapsed() < NATIVE_DELIVERY_GRACE)
    }
}

pub type NotificationSnapshot = Vec<Arc<NotificationReceipt>>;

/// Shared by reconnects. All posting and reconciliation at the native boundary
/// is serialized on the main thread; a receipt's retirement also cancels slow
/// detail reads and deliveries already queued there.
#[derive(Default)]
pub struct NotificationRegistry {
    receipts: NotificationSnapshot,
}

impl NotificationRegistry {
    pub fn reserve(&mut self, session_id: Option<String>) -> Option<Arc<NotificationReceipt>> {
        // Never reuse a Linux plugin id, even if the connection is replaced.
        static NEXT_ID: AtomicI32 = AtomicI32::new(1);
        let id = {
            let mut current = NEXT_ID.load(Ordering::SeqCst);
            loop {
                let next = current.checked_add(1)?;
                match NEXT_ID.compare_exchange(current, next, Ordering::SeqCst, Ordering::SeqCst) {
                    Ok(_) => break current,
                    Err(observed) => current = observed,
                }
            }
        };
        let receipt = Arc::new(NotificationReceipt {
            id,
            key: uuid::Uuid::new_v4().simple().to_string(),
            session_id: session_id.filter(|id| !id.is_empty()),
            retired: AtomicBool::new(false),
            readable: AtomicBool::new(false),
            read_reconciliation_eligible: AtomicBool::new(false),
            observed_unread: AtomicBool::new(false),
            unread_message: AtomicBool::new(false),
            sampled_since_confirmed: AtomicBool::new(false),
            posted_at: OnceLock::new(),
            submission: watch::channel(None).0,
            completion: OnceLock::new(),
            opens: OnceLock::new(),
        });
        if receipt.session_id.is_some() {
            self.prune_abandoned();
            self.receipts.push(receipt.clone());
        }
        Some(receipt)
    }

    /// Must be taken BEFORE the unread HTTP request, not when it completes.
    /// Taken immediately before an unread read; that read is paired with it.
    pub fn snapshot(&mut self) -> NotificationSnapshot {
        self.prune_abandoned();
        for receipt in &self.receipts {
            if receipt.unread_message.load(Ordering::SeqCst) {
                receipt.sampled_since_confirmed.store(true, Ordering::SeqCst);
            }
        }
        self.receipts.clone()
    }

    /// Native history can also confirm that unread posts were dismissed, so
    /// lifecycle cleanup must inspect all submitted keys, not only read ones.
    pub fn posted_snapshot(&self) -> NotificationSnapshot {
        self.receipts
            .iter()
            .filter(|receipt| receipt.posted_at.get().is_some())
            .cloned()
            .collect()
    }

    pub fn forget(&mut self, id: i32) {
        self.receipts.retain(|receipt| {
            if receipt.id == id {
                receipt.not_posted();
                false
            } else {
                true
            }
        });
    }

    /// A complete raw unread map, never the preference-filtered badge total.
    /// Return exact posted keys, including retired posts awaiting OS delivery:
    /// e.g. Linux's plugin may not have assigned its D-Bus id on the first pass.
    /// `unread` is the complete map of one read (the server lists every Session
    /// with unread > 0 and the total is checked against its sum), so a Session
    /// absent from it has zero. A read that fails, or a map that is partial,
    /// never reaches here.
    pub fn reconcile(&mut self, snapshot: NotificationSnapshot, unread: &HashMap<String, u64>) -> NotificationSnapshot {
        for receipt in snapshot {
            if let Some(session) = receipt.session_id() {
                if unread.get(session).copied().unwrap_or(0) > 0 {
                    receipt.observed_unread.store(true, Ordering::SeqCst);
                } else if receipt.observed_unread.load(Ordering::SeqCst)
                    || receipt.sampled_since_confirmed.load(Ordering::SeqCst)
                {
                    // Even successful Runs can settle without a result. Initial
                    // absence is not a read; require an observed unread -> read
                    // transition after this delivery was reserved.
                    receipt.retired.store(true, Ordering::SeqCst);
                }
            }
        }
        self.receipts.retain(|receipt| {
            if receipt.retired() && receipt.posted_at.get().is_none() {
                receipt.not_posted();
                false
            } else {
                true
            }
        });
        self.receipts
            .iter()
            .filter(|receipt| receipt.retired())
            .cloned()
            .collect()
    }

    fn prune_abandoned(&mut self) {
        self.receipts.retain(|receipt| {
            let keep = receipt.posted_at.get().is_some() || Arc::strong_count(receipt) > 1;
            if !keep {
                receipt.not_posted();
            }
            keep
        });
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn posted(registry: &mut NotificationRegistry, session: &str) -> Arc<NotificationReceipt> {
        let receipt = registry.reserve(Some(session.into())).unwrap();
        receipt.confirm_workbench_session(true);
        receipt.posted();
        registry.reconcile(vec![receipt.clone()], &HashMap::from([(session.into(), 1)]));
        receipt
    }

    #[test]
    fn read_retires_only_captured_deliveries_of_fully_read_sessions() {
        let mut registry = NotificationRegistry::default();
        let old = posted(&mut registry, "a");
        let other = posted(&mut registry, "b");
        let system = registry.reserve(None).unwrap();
        let snapshot = registry.snapshot();
        let newer = posted(&mut registry, "a");
        let removed = registry.reconcile(snapshot, &HashMap::from([("b".into(), 2)]));
        assert_eq!(removed.iter().map(|r| r.id()).collect::<Vec<_>>(), [old.id()]);
        assert!(old.retired());
        assert!(!other.retired());
        assert!(!newer.retired());
        assert!(!system.retired());
        assert_ne!(old.key(), newer.key());
        assert_ne!(old.id(), newer.id());
    }

    #[test]
    fn newer_unread_conservatively_keeps_old_posts_even_for_explicit_zero_badges() {
        let mut registry = NotificationRegistry::default();
        let old = posted(&mut registry, "a");
        let snapshot = registry.snapshot();
        assert!(registry
            .reconcile(snapshot, &HashMap::from([("a".into(), 1)]))
            .is_empty());
        assert!(!old.retired());
        let snapshot = registry.snapshot();
        assert_eq!(registry.reconcile(snapshot, &HashMap::from([("a".into(), 0)])).len(), 1);
        assert!(old.retired());
    }

    #[test]
    fn read_during_detail_lookup_cancels_only_a_confirmed_workbench_candidate() {
        let mut registry = NotificationRegistry::default();
        let pending = registry.reserve(Some("a".into())).unwrap();
        let im = registry.reserve(Some("im".into())).unwrap();
        registry.reconcile(vec![pending.clone()], &HashMap::from([("a".into(), 1)]));
        let snapshot = registry.snapshot();
        assert!(registry.reconcile(snapshot, &HashMap::new()).is_empty());
        assert!(!pending.retired()); // source platform still unknown
        pending.confirm_workbench_session(true);
        assert!(pending.retired()); // late detail/queued delivery cannot resurrect it
        assert!(!im.retired()); // the Avibe-only map says nothing about IM channels
        let newer = registry.reserve(Some("a".into())).unwrap();
        newer.confirm_workbench_session(true);
        assert!(!newer.retired());
    }

    #[test]
    fn asynchronous_os_posts_retry_exact_retired_keys_without_extending_the_cutoff() {
        let mut registry = NotificationRegistry::default();
        let old = posted(&mut registry, "a");
        let snapshot = registry.snapshot();
        assert_eq!(registry.reconcile(snapshot, &HashMap::new()).len(), 1);
        let newer = posted(&mut registry, "a");
        let snapshot = registry.snapshot();
        let retry = registry.reconcile(snapshot, &HashMap::from([("a".into(), 1)]));
        assert_eq!(retry.iter().map(|r| r.id()).collect::<Vec<_>>(), [old.id()]);
        assert!(!newer.retired());
        registry.forget(old.id());
        let snapshot = registry.snapshot();
        assert!(registry
            .reconcile(snapshot, &HashMap::from([("a".into(), 1)]))
            .is_empty());
    }

    #[tokio::test(start_paused = true)]
    async fn absent_native_posts_have_a_bounded_monotonic_grace() {
        let mut registry = NotificationRegistry::default();
        let pending = posted(&mut registry, "a");
        assert!(pending.awaiting_native_delivery());
        assert_eq!(registry.posted_snapshot().len(), 1);
        tokio::time::advance(NATIVE_DELIVERY_GRACE).await;
        assert!(!pending.awaiting_native_delivery());
        registry.forget(pending.id());
        assert!(registry.posted_snapshot().is_empty());
    }

    #[test]
    fn abandoned_detail_reads_do_not_leak_reservations_and_ids_survive_reconnects() {
        let mut registry = NotificationRegistry::default();
        let receipt = registry.reserve(Some("a".into())).unwrap();
        let old_id = receipt.id();
        drop(receipt);
        assert!(registry.snapshot().is_empty());
        let mut reconnected = NotificationRegistry::default();
        assert!(reconnected.reserve(Some("a".into())).unwrap().id() > old_id);
    }

    #[test]
    fn a_click_opens_the_named_link_or_the_session_and_never_an_unparsed_target() {
        let mut registry = NotificationRegistry::default();
        let session = registry.reserve(Some("ses_a".into())).unwrap();
        assert_eq!(session.opens().as_deref(), Some("avibe://session/ses_a"));

        let approval = registry.reserve(None).unwrap();
        assert_eq!(approval.opens(), None);
        approval.set_opens("avibe://vaults/request/req_1".into());
        assert_eq!(approval.opens().as_deref(), Some("avibe://vaults/request/req_1"));
        // The first accepted link stays; later ones do not replace it.
        approval.set_opens("avibe://vaults/request/req_2".into());
        assert_eq!(approval.opens().as_deref(), Some("avibe://vaults/request/req_1"));

        let refused = registry.reserve(None).unwrap();
        for link in [
            "https://avibe.bot/vaults",
            "avibe://vaults/request/a b",
            "avibe://session/../x",
        ] {
            refused.set_opens(link.into());
        }
        assert_eq!(refused.opens(), None);
        let odd_session = registry.reserve(Some("ses a".into())).unwrap();
        assert_eq!(odd_session.opens(), None);
    }

    #[test]
    fn a_question_read_before_any_unread_sample_is_retired_but_a_run_is_not() {
        let mut registry = NotificationRegistry::default();
        let question = registry.reserve(Some("asked".into())).unwrap();
        question.confirm_unread_message();
        question.posted();
        let run = registry.reserve(Some("ran".into())).unwrap();
        run.confirm_workbench_session(true);
        run.posted();
        let snapshot = registry.snapshot();
        let zero = HashMap::from([(String::from("asked"), 0), (String::from("ran"), 0)]);
        let retired = registry.reconcile(snapshot, &zero);
        assert_eq!(
            retired.iter().map(|receipt| receipt.id()).collect::<Vec<_>>(),
            [question.id()]
        );
        assert!(question.retired());
        assert!(!run.retired());
    }

    #[test]
    fn a_session_absent_from_a_complete_map_has_zero_unread() {
        // The server lists only Sessions with unread > 0; a read Session is absent.
        let mut registry = NotificationRegistry::default();
        let question = registry.reserve(Some("asked".into())).unwrap();
        question.confirm_unread_message();
        question.posted();
        let snapshot = registry.snapshot();
        registry.reconcile(snapshot, &HashMap::from([(String::from("other"), 2)]));
        assert!(question.retired());
    }

    #[test]
    fn a_zero_from_a_sample_taken_before_confirmation_does_not_retire_the_question() {
        let mut registry = NotificationRegistry::default();
        let question = registry.reserve(Some("asked".into())).unwrap();
        // This read was paired with a snapshot taken before the question was confirmed.
        let early = registry.snapshot();
        question.confirm_unread_message();
        question.posted();
        registry.reconcile(early, &HashMap::new());
        assert!(!question.retired());
        // A later read that still counts it unread keeps it, and its read retires it.
        let counted = registry.snapshot();
        registry.reconcile(counted, &HashMap::from([(String::from("asked"), 1)]));
        assert!(!question.retired());
        let read = registry.snapshot();
        registry.reconcile(read, &HashMap::new());
        assert!(question.retired());
    }
}
