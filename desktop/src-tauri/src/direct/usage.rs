//! Which leg carried the connections the windows opened through this
//! listener, per minute and per kind of direct path present in it, for the
//! instance's log: `path_use` on the rendezvous request
//! (docs/plans/remote-direct-rendezvous.md section 2).
//!
//! Counted where the listener decides each authenticated CONNECT (direct,
//! the tunnel, or a direct open that was started and gave nothing), and
//! where a link is withdrawn. Counts and fixed
//! words only: no address, host, identifier or wall time. A minute is taken
//! for a request once it is over and given back if the instance did not
//! answer; the oldest are dropped past `KEPT`. Best effort: a minute can be
//! dropped unsent, and can arrive more than once when an answer is lost.
//!
//! Each entry also names this app's own build (`app`), when the shell gave
//! one the instance accepts. The key is optional to an instance that knows
//! it. One from before it ignores the whole `path_use` list of a request
//! whose entries carry it, so nothing of this desktop's use is in that
//! instance's log until the instance is updated.
//!
//! What this desktop does not send yet is zero or `none` in the entry: the
//! race's counts and timings, the hand-overs, and why a connection was not
//! raced, except `strict`. A
//! window that is not routed through the listener is not seen here at all;
//! the instance's own count of requests by arrival path is the number that
//! sees it.
use super::{clock::Clock, faults::PathWord, transport::FaultCode};
use std::{
    collections::BTreeMap,
    sync::{Mutex, OnceLock},
};

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
    /// Authenticated CONNECTs answered `502` after a direct open was
    /// started for them: alone, or beside the tunnel on a usable link.
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
    /// Exactly the 22 keys the instance requires, within its bounds, and
    /// `app` with them when this app's build is known. `now` is the minute
    /// the request is made in: an age, never a wall time.
    fn json(&self, now: u64, app: Option<&str>) -> serde_json::Value {
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
        let mut entry = serde_json::json!({
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
        });
        if let Some(app) = app {
            entry["app"] = serde_json::json!(app);
        }
        entry
    }
}

/// A build as an entry may name it: what the instance accepts for `app`, 1
/// to 32 bytes of printable ASCII, exactly as given. `0.0.0` is what a
/// package that was given no version is stamped with: no build to name.
fn build_word(build: &str) -> Option<&str> {
    let printable = build.bytes().all(|byte| (0x20..=0x7e).contains(&byte));
    (printable && (1..=32).contains(&build.len()) && build != "0.0.0").then_some(build)
}

pub(super) struct Usage {
    clock: Clock,
    minutes: Mutex<BTreeMap<(u64, PathWord), Counts>>,
    /// This app's own build, once the shell has said it and only if it is
    /// one an entry may name (`build_word`).
    app: OnceLock<String>,
}
impl Usage {
    pub(super) fn new(clock: Clock) -> Self {
        Self {
            clock,
            minutes: Mutex::default(),
            app: OnceLock::new(),
        }
    }
    /// The shell says which build this app is. One that an entry may not
    /// name is no build: the entries then go without `app`.
    pub(super) fn named(&self, build: &str) {
        if let Some(build) = build_word(build) {
            let _ = self.app.set(build.to_owned());
        }
    }
    /// `taken` as the request carries it, at the minute it is made in.
    pub(super) fn report(&self, taken: &[Entry]) -> Vec<serde_json::Value> {
        let (now, app) = (self.minute(), self.app.get().map(String::as_str));
        taken.iter().map(|entry| entry.json(now, app)).collect()
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
    /// A CONNECT answered `502`: no leg gave a backend. A failed direct leg
    /// where a direct open was started for it (`tried`). One that waited
    /// in direct-only for a link that did not come is `strict` and no
    /// failed leg; one for which only the tunnel was tried is in no count.
    pub(super) fn unanswered(&self, path: PathWord, strict: bool, tried: bool) {
        if !(strict || tried) {
            return;
        }
        self.count(path, |counts| {
            counts.failed += u64::from(tried);
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
                entry.json(now, None)[key].as_u64().expect("a count")
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
        usage.unanswered(PathWord::Relay, true, true);
        // Waited in direct-only for a link that did not come: no failed leg.
        usage.unanswered(PathWord::Relay, true, false);
        // Only the tunnel was tried: in no count, and it makes no entry.
        usage.unanswered(PathWord::Punched, false, false);
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
        let entry = usage.report(&taken).remove(1);
        let keys = serde_json::json!({
                "client": "desktop", "age_min": 1, "path": "relay",
                "direct": 2, "tunnel": 2,
                "raced": 0, "direct_won": 0, "tunnel_won": 0,
                "no_direct": 0, "sticky": 0, "strict": 3, "tunnel_only": 0,
                "direct_failed": 1, "direct_timed_out": 0,
                "direct_ms": 0, "tunnel_ms": 0,
                "handover_asked": 0, "handover_done": 0, "handover_refused": 0,
                "handover_rule": "none",
                "retired": 3, "retired_rule": "path_refused_cgnat",
        });
        assert_eq!(entry, keys);
        assert_eq!(entry.as_object().unwrap().len(), 22);
        assert_eq!(usage.report(&taken)[0]["retired_rule"], "none");
        // Once the shell has said which build this app is, every entry
        // names it, beside the same 22 keys. It is said once.
        usage.named("3.2.9-rc.4");
        usage.named("3.2.10");
        let mut named = keys;
        named["app"] = serde_json::json!("3.2.9-rc.4");
        assert_eq!(usage.report(&taken)[1], named);
        assert_eq!(usage.report(&taken)[0]["app"], "3.2.9-rc.4");
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
        assert_eq!(usage.report(&again)[1]["age_min"], 3);
    }

    /// A build the instance would not accept for `app` is not sent: one
    /// refused entry would cost the whole list. Nor is the version of a
    /// package that was given none.
    #[test]
    fn only_a_build_the_instance_accepts_is_named() {
        for build in ["3.2.9", "3.2.9-rc.4", "0.1.0", "57.1 (4)", "x", &"9".repeat(32)] {
            assert_eq!(build_word(build), Some(build), "{build:?}");
        }
        for build in [
            "",
            "0.0.0",
            &"9".repeat(33),
            "3.2.9\n",
            "3.2.9\t",
            "3.2.9\u{7f}",
            "版本 3",
        ] {
            assert_eq!(build_word(build), None, "{build:?}");
        }
        let (usage, seconds) = usage();
        usage.tunnel(PathWord::None);
        seconds.fetch_add(60, Ordering::SeqCst);
        let taken = usage.take();
        // Not a build: the entry goes as it always did, and a build that
        // is said later is still taken.
        usage.named("0.0.0");
        let entry = &usage.report(&taken)[0];
        assert!(entry.get("app").is_none());
        assert_eq!(entry.as_object().unwrap().len(), 22);
        usage.named("3.2.9");
        assert_eq!(usage.report(&taken)[0]["app"], "3.2.9");
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
        assert_eq!(usage.report(&first)[0]["age_min"], 30);
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
