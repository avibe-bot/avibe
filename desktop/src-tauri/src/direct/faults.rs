//! The direct legs that did not end in a working stream, and why a link was
//! withdrawn, for the instance's log: `leg_faults` on the rendezvous request
//! (docs/plans/remote-direct-rendezvous.md section 2). A desktop that fails
//! every direct connection on some network leaves nothing on either side to
//! say why; this is that record.
//!
//! The transport's own words only (`Stage`, `FaultCode`), the kind of path, a
//! duration and a count: no address, host, identifier or error text. The
//! last few are kept, equal ones in a row as one with a count. They are
//! taken for a request and given back if the instance did not answer it.
use super::{
    transport::{Fault, FaultCode, Stage},
    SelectedPath,
};
use std::{collections::VecDeque, sync::Mutex, time::Duration};

/// The most entries one request carries; the instance ignores a longer list
/// whole.
pub(super) const LIMIT: usize = 5;
const MAX: u64 = i32::MAX as u64;

/// The kind of a direct path, in the instance's words. Never an address.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub(super) enum PathWord {
    None,
    Lan,
    Punched,
    Relay,
}
impl PathWord {
    pub(super) fn of(path: Option<SelectedPath>) -> Self {
        match path {
            None => Self::None,
            Some(SelectedPath::Lan(_)) => Self::Lan,
            Some(SelectedPath::Punched(_)) => Self::Punched,
            Some(SelectedPath::Relay) => Self::Relay,
        }
    }
    pub(super) fn word(self) -> &'static str {
        match self {
            Self::None => "none",
            Self::Lan => "lan",
            Self::Punched => "punched",
            Self::Relay => "relay",
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub(super) struct LegFault {
    stage: Stage,
    code: FaultCode,
    path: PathWord,
    /// The leg ran with only the direct path and the relay allowed.
    strict: bool,
    /// How long that stage ran; for a withdrawn link, how long it lived.
    ms: u64,
    /// How many equal entries in a row this one stands for.
    count: u64,
}
impl LegFault {
    /// Exactly the six keys the instance requires, within its bounds.
    pub(super) fn json(&self) -> serde_json::Value {
        serde_json::json!({
            "stage": self.stage,
            "code": self.code,
            "path": self.path.word(),
            "strict": self.strict,
            "ms": self.ms.min(MAX),
            "count": self.count.clamp(1, MAX),
        })
    }
    fn same(&self, other: &Self) -> bool {
        (self.stage, self.code, self.path, self.strict) == (other.stage, other.code, other.path, other.strict)
    }
}

#[derive(Default)]
pub(super) struct Faults(Mutex<VecDeque<LegFault>>);
impl Faults {
    pub(super) fn record(&self, fault: Fault, path: PathWord, strict: bool, ran: Duration) {
        let next = LegFault {
            stage: fault.0,
            code: fault.1,
            path,
            strict,
            ms: u64::try_from(ran.as_millis()).unwrap_or(u64::MAX),
            count: 1,
        };
        let mut kept = self.0.lock().unwrap();
        match kept.back_mut() {
            // One repeated failure does not fill the record: the latest
            // duration, and how many there were.
            Some(last) if last.same(&next) => {
                last.ms = next.ms;
                last.count = last.count.saturating_add(1);
            }
            _ => {
                kept.push_back(next);
                while kept.len() > LIMIT {
                    kept.pop_front();
                }
            }
        }
    }
    /// Everything kept, oldest first, for one request. Nothing is kept
    /// meanwhile: what the instance does not answer comes back by `restore`.
    pub(super) fn take(&self) -> Vec<LegFault> {
        self.0.lock().unwrap().drain(..).collect()
    }
    /// The request that carried `taken` was not answered: they are kept
    /// again, before what was recorded since, the oldest dropped past the
    /// limit.
    pub(super) fn restore(&self, taken: Vec<LegFault>) {
        let mut kept = self.0.lock().unwrap();
        for fault in taken.into_iter().rev() {
            kept.push_front(fault);
        }
        while kept.len() > LIMIT {
            kept.pop_front();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::direct::AddressFamily;

    fn fault(stage: Stage, code: FaultCode) -> Fault {
        Fault(stage, code)
    }

    #[test]
    fn an_entry_has_exactly_the_instances_six_keys_in_the_transports_words() {
        let faults = Faults::default();
        faults.record(
            fault(Stage::Handover, FaultCode::PathRefusedCgnat),
            PathWord::of(Some(SelectedPath::Punched(AddressFamily::Ipv6))),
            true,
            Duration::from_millis(650),
        );
        let taken = faults.take();
        assert_eq!(
            taken[0].json(),
            serde_json::json!({
                "stage": "handover", "code": "path_refused_cgnat", "path": "punched",
                "strict": true, "ms": 650, "count": 1,
            })
        );
        assert_eq!(taken[0].json().as_object().unwrap().len(), 6);
        // Taken once: nothing is sent twice.
        assert!(faults.take().is_empty());
        for (path, word) in [
            (None, "none"),
            (Some(SelectedPath::Relay), "relay"),
            (Some(SelectedPath::Lan(AddressFamily::Ipv4)), "lan"),
        ] {
            assert_eq!(PathWord::of(path).word(), word);
        }
    }

    /// The instance accepts a fixed list of code words and ignores a report
    /// that carries any other (`_LEG_FAULT_CODES` in
    /// `vibe/direct_rendezvous.py`): the words for a refused path are that
    /// list's, one for each class of address.
    #[test]
    fn a_refused_path_is_reported_by_the_class_of_its_address_in_the_instances_words() {
        use crate::direct::path::AddressClass::*;
        let word = |code: FaultCode| serde_json::json!(code).as_str().unwrap().to_owned();
        assert_eq!(word(FaultCode::PathUnselected), "path_unselected");
        for (class, expected) in [
            (Relay, "path_refused_relay"),
            (Private, "path_refused_private"),
            (Cgnat, "path_refused_cgnat"),
            (Ula, "path_refused_ula"),
            (Linklocal, "path_refused_linklocal"),
            (Loopback, "path_refused_loopback"),
            (Public, "path_refused_public"),
            (Global, "path_refused_global"),
            (Nat64, "path_refused_nat64"),
            (Mapped, "path_refused_mapped"),
            (Other, "path_refused_other"),
        ] {
            assert_eq!(word(FaultCode::refused(class)), expected);
        }
    }

    #[test]
    fn equal_faults_in_a_row_are_one_entry_and_the_last_five_are_kept() {
        let faults = Faults::default();
        for ms in [100, 200, 300] {
            faults.record(
                fault(Stage::Dial, FaultCode::Timeout),
                PathWord::None,
                false,
                Duration::from_millis(ms),
            );
        }
        // Another path, or strict, is another entry.
        faults.record(
            fault(Stage::Dial, FaultCode::Timeout),
            PathWord::Relay,
            false,
            Duration::ZERO,
        );
        faults.record(
            fault(Stage::Dial, FaultCode::Timeout),
            PathWord::Relay,
            true,
            Duration::ZERO,
        );
        let taken = faults.take();
        assert_eq!(taken.len(), 3);
        assert_eq!((taken[0].count, taken[0].ms), (3, 300));
        // More than the limit: the oldest go.
        for code in [
            FaultCode::Timeout,
            FaultCode::Refused,
            FaultCode::Status,
            FaultCode::Expired,
            FaultCode::Superseded,
            FaultCode::ConnectionEnded,
            FaultCode::ClosedByPeer,
        ] {
            faults.record(
                fault(Stage::Handover, code),
                PathWord::Relay,
                false,
                Duration::from_secs(u64::MAX / 1000),
            );
        }
        let taken = faults.take();
        assert_eq!(taken.len(), LIMIT);
        assert_eq!(taken[0].code, FaultCode::Status);
        assert_eq!(taken[4].code, FaultCode::ClosedByPeer);
        // A duration past the instance's bound is sent as the bound.
        assert_eq!(taken[0].json()["ms"], i32::MAX);
    }

    #[test]
    fn what_a_request_did_not_deliver_is_kept_again_before_what_came_since() {
        let faults = Faults::default();
        faults.record(
            fault(Stage::Dial, FaultCode::Timeout),
            PathWord::None,
            false,
            Duration::ZERO,
        );
        faults.record(
            fault(Stage::Open, FaultCode::Refused),
            PathWord::Relay,
            false,
            Duration::ZERO,
        );
        let taken = faults.take();
        faults.record(
            fault(Stage::Handover, FaultCode::Expired),
            PathWord::Relay,
            false,
            Duration::ZERO,
        );
        faults.restore(taken);
        let again = faults.take();
        assert_eq!(
            again.iter().map(|fault| fault.code).collect::<Vec<_>>(),
            [FaultCode::Timeout, FaultCode::Refused, FaultCode::Expired]
        );
    }
}
