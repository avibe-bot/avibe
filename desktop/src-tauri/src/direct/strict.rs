//! Strict CONNECTs wait only for the session's admitted transport. This path
//! deliberately has no public dialer and does not use the automatic hedge.
use std::{future::Future, time::Duration};
use tokio::{sync::watch, time::Instant};

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
    budget: Duration,
    dial: impl Fn(L) -> F,
) -> Option<T> {
    let deadline = Instant::now() + budget;
    let expiry = tokio::time::sleep_until(deadline);
    tokio::pin!(expiry);
    loop {
        if Instant::now() >= deadline {
            return None;
        }
        let current = links.borrow_and_update().clone();
        if let Some(link) = current {
            tokio::select! { biased;
                _ = &mut expiry => return None,
                changed = links.changed() => { changed.ok()?; continue; }
                opened = dial(link) => if opened.is_some() { return opened; }
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
