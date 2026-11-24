//! Strict CONNECTs wait only for the session's admitted transport. This path
//! deliberately has no public dialer and does not use the automatic hedge.
use super::clock::Clock;
use std::{future::Future, time::Duration};
use tokio::sync::watch;

pub(super) struct Timing {
    pub strict_open: Duration,
}
impl Default for Timing {
    fn default() -> Self {
        Self {
            strict_open: Duration::from_secs(10),
        }
    }
}

pub(super) async fn open<L: Clone, T, F: Future<Output = Option<T>>>(
    mut links: watch::Receiver<Option<L>>,
    clock: &Clock,
    budget: Duration,
    dial: impl Fn(L) -> F,
) -> Option<T> {
    let deadline = clock.now().checked_add(budget)?;
    // Tokio timers may stop while suspended. Bound the awake delay before
    // noticing an overdue CONNECT even when neither the peer nor watch wakes.
    let expiry = async {
        loop {
            let left = deadline.saturating_sub(clock.now());
            if left.is_zero() {
                break;
            }
            tokio::time::sleep(left.min(Duration::from_millis(250))).await;
        }
    };
    tokio::pin!(expiry);
    loop {
        if clock.now() >= deadline {
            return None;
        }
        let current = links.borrow_and_update().clone();
        if let Some(link) = current {
            tokio::select! { biased;
                _ = &mut expiry => return None,
                changed = links.changed() => { changed.ok()?; continue; }
                opened = dial(link) => if let Some(stream) = opened {
                    return (clock.now() < deadline).then_some(stream);
                }
            }
        }
        // Failed or absent link. Do not spin or re-dial a refusing peer; only
        // a new publication can authorize another attempt under this deadline.
        tokio::select! { biased;
            _ = &mut expiry => return None,
            changed = links.changed() => changed.ok()?,
        }
    }
}
