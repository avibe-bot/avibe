//! Which leg carried the connections the windows opened through this
//! listener, per minute and per kind of direct path present in it, for the
//! instance's log: `path_use` on the rendezvous request
//! (docs/plans/remote-direct-rendezvous.md section 2).
//!
//! Counted where the listener decides each authenticated CONNECT (direct,
//! the tunnel, or a direct leg that was tried and gave nothing), and where a
//! link is withdrawn. Counts and fixed
//! words only: no address, host, identifier or wall time. A minute is taken
//! for a request once it is over and given back if the instance did not
//! answer; the oldest are dropped past `KEPT`.
//!
//! What this desktop does not send yet is zero or `none` in the entry: the
//! race's counts and timings, the hand-overs, and why a connection was not
//! raced, except `strict`. A
//! window that is not routed through the listener is not seen here at all;
//! the instance's own count of requests by arrival path is the number that
//! sees it.
use super::{clock::Clock, faults::PathWord, transport::FaultCode};
use std::{collections::BTreeMap, sync::Mutex};

/// The most entries one request carries; the instance ignores a longer list
/// whole.
pub(super) const LIMIT: usize = 10;
/// The most minute-and-path entries kept while no request is answered.
const KEPT: usize = 30;
/// The instance accepts no greater age, in minutes.
const MAX_AGE: u64 = 1440;
const MAX: u64 = i32::MAX as u64;

#[derive(Clone, Debug, Default, PartialEq, Eq)]
struct Counts {
    direct: u64,
    tunnel: u64,
    /// Authenticated CONNECTs answered `502` after a direct leg was tried
    /// for them: alone, or beside the tunnel on a usable link.
    failed: u64,
    /// Those decided with only the direct path and the relay allowed,
    /// carried or not.
    strict: u64,
    retired: u64,
    /// Why links were withdrawn in this minute, in the order first seen.
    retired_for: Vec<(FaultCode, u64)>,
}

/// One finished minute of one kind of path, taken for a request.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(super) struct Entry {
    minute: u64,
    path: PathWord,
    counts: Counts,
}
impl Entry {
    /// Exactly the 22 keys the instance requires, within its bounds. `now`
    /// is the minute the request is made in: an age, never a wall time.
    pub(super) fn json(&self, now: u64) -> serde_json::Value {
        let most = self
            .counts
            .retired_for
            .iter()
            .fold(None::<(FaultCode, u64)>, |best, &(code, count)| match best {
                Some((_, most)) if most >= count => best,
                _ => Some((code, count)),
            });
        let retired_rule = most.map_or(serde_json::json!("none"), |(code, _)| serde_json::json!(code));
        let bounded = |count: u64| count.min(MAX);
        serde_json::json!({
            "client": "desktop",
            "age_min": now.saturating_sub(self.minute).min(MAX_AGE),
            "path": self.path.word(),
            "direct": bounded(self.counts.direct),
            "tunnel": bounded(self.counts.tunnel),
            "raced": 0, "direct_won": 0, "tunnel_won": 0,
            "no_direct": 0, "sticky": 0, "strict": bounded(self.counts.strict), "tunnel_only": 0,
            "direct_failed": bounded(self.counts.failed),
            "direct_timed_out": 0,
            "direct_ms": 0, "tunnel_ms": 0,
            "handover_asked": 0, "handover_done": 0, "handover_refused": 0,
            "handover_rule": "none",
            "retired": bounded(self.counts.retired),
            "retired_rule": retired_rule,
        })
    }
}

pub(super) struct Usage {
    clock: Clock,
    minutes: Mutex<BTreeMap<(u64, PathWord), Counts>>,
}
impl Usage {
    pub(super) fn new(clock: Clock) -> Self {
        Self {
            clock,
            minutes: Mutex::default(),
        }
    }
    /// The minute it is now, on the clock that keeps counting during sleep.
    pub(super) fn minute(&self) -> u64 {
        self.clock.now().as_secs() / 60
    }
    fn count(&self, path: PathWord, change: impl FnOnce(&mut Counts)) {
        let now = self.minute();
        let mut minutes = self.minutes.lock().unwrap();
        change(minutes.entry((now, path)).or_default());
        Self::drop_old(&mut minutes, now);
    }
    /// `strict`: decided with only the direct path and the relay allowed.
    pub(super) fn direct(&self, path: PathWord, strict: bool) {
        self.count(path, |counts| {
            counts.direct += 1;
            counts.strict += u64::from(strict);
        });
    }
    pub(super) fn tunnel(&self, path: PathWord) {
        self.count(path, |counts| counts.tunnel += 1);
    }
    /// A direct leg was tried and the CONNECT was answered `502`.
    pub(super) fn direct_failed(&self, path: PathWord, strict: bool) {
        self.count(path, |counts| {
            counts.failed += 1;
            counts.strict += u64::from(strict);
        });
    }
    pub(super) fn retired(&self, path: PathWord, code: FaultCode) {
        self.count(path, |counts| {
            counts.retired += 1;
            match counts.retired_for.iter_mut().find(|(seen, _)| *seen == code) {
                Some((_, count)) => *count += 1,
                None => counts.retired_for.push((code, 1)),
            }
        });
    }
    /// The minutes that are over, oldest first, at most `LIMIT`, for one
    /// request. Nothing of them is kept meanwhile: what the instance does
    /// not answer comes back by `restore`.
    pub(super) fn take(&self) -> Vec<Entry> {
        let now = self.minute();
        let mut minutes = self.minutes.lock().unwrap();
        Self::drop_old(&mut minutes, now);
        let over: Vec<_> = minutes
            .keys()
            .filter(|(minute, _)| *minute < now)
            .take(LIMIT)
            .copied()
            .collect();
        over.into_iter()
            .filter_map(|key| {
                minutes.remove(&key).map(|counts| Entry {
                    minute: key.0,
                    path: key.1,
                    counts,
                })
            })
            .collect()
    }
    /// The request that carried `taken` was not answered: kept again. A
    /// minute is over when it is taken, so nothing was counted in it since.
    pub(super) fn restore(&self, taken: Vec<Entry>) {
        let now = self.minute();
        let mut minutes = self.minutes.lock().unwrap();
        for entry in taken {
            minutes.entry((entry.minute, entry.path)).or_insert(entry.counts);
        }
        Self::drop_old(&mut minutes, now);
    }
    /// One count as it would be sent, summed over every minute counted so
    /// far, the one still counting included: on one kind of path, or on
    /// all. A test's connections may fall either side of a minute's end.
    /// Nothing is taken.
    #[cfg(test)]
    pub(super) fn total(&self, path: Option<PathWord>, key: &str) -> u64 {
        let now = self.minute();
        let minutes = self.minutes.lock().unwrap();
        minutes
            .iter()
            .filter(|((_, on), _)| path.is_none_or(|path| path == *on))
            .map(|(&(minute, path), counts)| {
                let entry = Entry {
                    minute,
                    path,
                    counts: counts.clone(),
                };
                entry.json(now)[key].as_u64().expect("a count")
            })
            .sum()
    }
    /// Minutes too old to send, then the oldest past `KEPT`.
    fn drop_old(minutes: &mut BTreeMap<(u64, PathWord), Counts>, now: u64) {
        minutes.retain(|(minute, _), _| now.saturating_sub(*minute) <= MAX_AGE);
        while minutes.len() > KEPT {
            minutes.pop_first();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{
        sync::{
            atomic::{AtomicU64, Ordering},
            Arc,
        },
        time::Duration,
    };

    fn usage() -> (Usage, Arc<AtomicU64>) {
        let seconds = Arc::new(AtomicU64::new(600));
        let now = seconds.clone();
        let clock = Clock(Arc::new(move || Duration::from_secs(now.load(Ordering::SeqCst))));
        (Usage::new(clock), seconds)
    }

    #[test]
    fn a_minute_is_taken_once_it_is_over_with_exactly_the_instances_keys() {
        let (usage, seconds) = usage();
        usage.direct(PathWord::Relay, false);
        usage.direct(PathWord::Relay, true);
        usage.tunnel(PathWord::Relay);
        usage.tunnel(PathWord::Relay);
        usage.direct_failed(PathWord::Relay, true);
        usage.retired(PathWord::Relay, FaultCode::Superseded);
        usage.retired(PathWord::Relay, FaultCode::PathRefusedCgnat);
        usage.retired(PathWord::Relay, FaultCode::PathRefusedCgnat);
        usage.tunnel(PathWord::None);
        // Nothing while the minute is still counting.
        assert!(usage.take().is_empty());
        seconds.fetch_add(60, Ordering::SeqCst);
        usage.direct(PathWord::Lan, false);
        // Summed over the minutes, the one still counting included.
        assert_eq!(usage.total(None, "direct"), 3);
        assert_eq!(usage.total(Some(PathWord::Relay), "direct"), 2);
        let taken = usage.take();
        assert_eq!(
            taken.iter().map(|entry| entry.path).collect::<Vec<_>>(),
            [PathWord::None, PathWord::Relay]
        );
        let entry = taken[1].json(usage.minute());
        assert_eq!(
            entry,
            serde_json::json!({
                "client": "desktop", "age_min": 1, "path": "relay",
                "direct": 2, "tunnel": 2,
                "raced": 0, "direct_won": 0, "tunnel_won": 0,
                "no_direct": 0, "sticky": 0, "strict": 2, "tunnel_only": 0,
                "direct_failed": 1, "direct_timed_out": 0,
                "direct_ms": 0, "tunnel_ms": 0,
                "handover_asked": 0, "handover_done": 0, "handover_refused": 0,
                "handover_rule": "none",
                "retired": 3, "retired_rule": "path_refused_cgnat",
            })
        );
        assert_eq!(entry.as_object().unwrap().len(), 22);
        assert_eq!(taken[0].json(usage.minute())["retired_rule"], "none");
        // Taken once; the minute still counting stays.
        assert!(usage.take().is_empty());
        // Not answered: kept again, and older by the time it is sent.
        usage.restore(taken);
        seconds.fetch_add(120, Ordering::SeqCst);
        let again = usage.take();
        assert_eq!(
            again.iter().map(|entry| entry.path).collect::<Vec<_>>(),
            [PathWord::None, PathWord::Relay, PathWord::Lan]
        );
        assert_eq!(again[1].json(usage.minute())["age_min"], 3);
    }

    #[test]
    fn what_is_kept_and_sent_is_bounded() {
        let (usage, seconds) = usage();
        for _ in 0..40 {
            seconds.fetch_add(60, Ordering::SeqCst);
            usage.tunnel(PathWord::None);
        }
        seconds.fetch_add(60, Ordering::SeqCst);
        let first = usage.take();
        assert_eq!(first.len(), LIMIT);
        // The oldest of the thirty kept.
        assert_eq!(first[0].json(usage.minute())["age_min"], 30);
        let mut total = first.len();
        loop {
            let next = usage.take();
            if next.is_empty() {
                break;
            }
            total += next.len();
        }
        assert_eq!(total, KEPT);
        // A minute older than the instance accepts is dropped unsent.
        usage.direct(PathWord::Relay, false);
        seconds.fetch_add(60 * (MAX_AGE + 2), Ordering::SeqCst);
        assert!(usage.take().is_empty());
    }
}
