//! A submitted answerable notification is the success notice for its turn.
//! This evidence belongs to one adopted Runtime connection, never to a title
//! or a nearby timestamp. Pending OS submissions settle before a matching
//! success is considered; unknown or failed submissions do not suppress it.
use std::collections::VecDeque;
use std::sync::{Arc, Mutex, Weak};
use std::time::Duration;

use tokio::sync::watch;
use tokio::task::JoinSet;
use tokio::time::Instant;

use super::{NotificationReceipt, RETAINED_KEYS, RETENTION};

/// An OS callback that never returns must not swallow a completion. This is
/// a wait bound, not a timestamp-based correlation rule. The final native
/// check still catches a question accepted after this wait expires.
const SUBMISSION_WAIT: Duration = Duration::from_secs(30);

#[derive(Clone, Debug, PartialEq, Eq)]
pub(super) struct Turn {
    generation: u64,
    session_id: String,
    turn_id: String,
}

impl Turn {
    pub(super) fn new(generation: u64, session_id: Option<&str>, turn_id: Option<&str>) -> Option<Self> {
        let session_id = session_id.filter(|id| !id.trim().is_empty())?;
        let turn_id = turn_id.filter(|id| !id.trim().is_empty())?;
        Some(Self {
            generation,
            session_id: session_id.to_owned(),
            turn_id: turn_id.to_owned(),
        })
    }
}

struct Attempt {
    turn: Turn,
    submitted: watch::Receiver<Option<bool>>,
    at: Instant,
}

#[derive(Default)]
pub(super) struct AnswerPosts(VecDeque<Attempt>);

/// A final check for a success post queued in the native/main-thread boundary.
/// It also sees a question submitted after the run detail finished reading.
#[derive(Debug)]
pub(super) struct Completion {
    answers: Weak<Mutex<AnswerPosts>>,
    turn: Turn,
}

impl Completion {
    pub(super) fn new(answers: &Arc<Mutex<AnswerPosts>>, turn: Turn) -> Self {
        Self {
            answers: Arc::downgrade(answers),
            turn,
        }
    }

    pub(super) fn superseded(&self) -> bool {
        self.answers.upgrade().is_some_and(|answers| {
            answers
                .lock()
                .expect("answer posts")
                .submissions(&self.turn)
                .iter()
                .any(|submitted| *submitted.borrow() == Some(true))
        })
    }
}

impl AnswerPosts {
    fn prune(&mut self) {
        let now = Instant::now();
        self.0.retain(|attempt| now.duration_since(attempt.at) < RETENTION);
    }

    pub(super) fn observe(&mut self, turn: Turn, receipt: &NotificationReceipt) {
        self.prune();
        self.0.push_back(Attempt {
            turn,
            submitted: receipt.submission(),
            at: Instant::now(),
        });
        while self.0.len() > RETAINED_KEYS {
            self.0.pop_front();
        }
    }

    pub(super) fn submissions(&mut self, turn: &Turn) -> Vec<watch::Receiver<Option<bool>>> {
        self.prune();
        self.0
            .iter()
            .filter(|attempt| &attempt.turn == turn)
            .map(|attempt| attempt.submitted.clone())
            .collect()
    }
}

/// Wait outside every registry/preference lock and detail-read semaphore.
/// Dropping an unfinished sender is failure, never success. A submitted
/// question stays evidence even after its notification is read or dismissed.
pub(super) async fn any_submitted(submissions: Vec<watch::Receiver<Option<bool>>>) -> bool {
    tokio::time::timeout(SUBMISSION_WAIT, wait_for_submission(submissions))
        .await
        .unwrap_or(false)
}

async fn wait_for_submission(submissions: Vec<watch::Receiver<Option<bool>>>) -> bool {
    let mut pending = JoinSet::new();
    for mut submitted in submissions {
        let known = *submitted.borrow();
        match known {
            Some(true) => return true,
            Some(false) => {}
            None => {
                pending.spawn(async move {
                    loop {
                        let known = *submitted.borrow_and_update();
                        if let Some(posted) = known {
                            return posted;
                        }
                        if submitted.changed().await.is_err() {
                            return false;
                        }
                    }
                });
            }
        }
    }
    while let Some(result) = pending.join_next().await {
        if matches!(result, Ok(true)) {
            return true;
        }
    }
    false
}
