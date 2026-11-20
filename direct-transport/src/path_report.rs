//! V2 includes the authenticated peer key only on the negotiated private pipe.
//! Never serialize a Path, socket address or dependency Debug representation.
use futures_core::Stream;
use iroh::{
    TransportAddr,
    endpoint::{Connection, PathEvent, PathEventStream, PathStats},
};
use serde::Serialize;
use std::{
    future::poll_fn,
    pin::Pin,
    sync::{Arc, Mutex},
};
use tokio::sync::mpsc;

#[derive(Clone, Copy, Serialize, Default)]
pub(super) struct Counts {
    open: u64,
    rx_bytes: u64,
    tx_bytes: u64,
    challenge_tx: u64,
    challenge_rx: u64,
    response_tx: u64,
    response_rx: u64,
}
impl Counts {
    fn add(&mut self, s: &PathStats) {
        self.rx_bytes = self.rx_bytes.saturating_add(s.udp_rx.bytes);
        self.tx_bytes = self.tx_bytes.saturating_add(s.udp_tx.bytes);
        self.challenge_tx = self.challenge_tx.saturating_add(s.frame_tx.path_challenge);
        self.challenge_rx = self.challenge_rx.saturating_add(s.frame_rx.path_challenge);
        self.response_tx = self.response_tx.saturating_add(s.frame_tx.path_response);
        self.response_rx = self.response_rx.saturating_add(s.frame_rx.path_response);
    }
}
fn family(addr: &TransportAddr) -> (usize, &'static str) {
    match addr {
        TransportAddr::Relay(_) => (0, "relay"),
        TransportAddr::Ip(ip) if ip.is_ipv4() => (1, "ipv4"),
        TransportAddr::Ip(_) => (2, "ipv6"),
        _ => (3, "unknown"),
    }
}
fn scope(addr: &TransportAddr) -> Option<&'static str> {
    use std::net::IpAddr;
    let TransportAddr::Ip(sock) = addr else {
        return None;
    };
    let local = match sock.ip().to_canonical() {
        IpAddr::V4(ip) => {
            ip.is_private()
                || ip.is_link_local()
                || (ip.octets()[0] == 100 && ip.octets()[1] >> 6 == 1)
        }
        IpAddr::V6(ip) => ip.is_unique_local() || ip.is_unicast_link_local(),
    };
    Some(if local { "local" } else { "global" })
}

#[derive(Clone, Serialize)]
struct DeviceSample {
    peer: String,
    epoch: u64,
    scope: Option<&'static str>,
    rtt_ms: Option<u64>,
}

#[derive(Serialize)]
pub(super) struct Report {
    #[serde(flatten)]
    device: Option<DeviceSample>,
    ordinal: u64,
    phase: &'static str,
    selected: &'static str,
    partial: bool,
    // Fixed order, including paths closed earlier in this connection.
    families: [Counts; 4],
}
// At most one queued measurement and one reserved close per connection. A
// slow sink never blocks QUIC or accumulates a history of obsolete RTTs.
type Latest = Arc<Mutex<Option<Report>>>;
pub(super) enum Queued {
    Sample(Latest),
    Close(Report),
}
impl Queued {
    pub(super) fn take(self) -> Report {
        match self {
            Self::Sample(slot) => slot.lock().unwrap().take().expect("queued measurement"),
            Self::Close(report) => report,
        }
    }
}
pub(super) fn queue(limit: usize) -> (mpsc::Sender<Queued>, mpsc::Receiver<Queued>) {
    mpsc::channel(limit * 2)
}
#[derive(Default)]
pub(super) struct Output {
    pending: Latest,
    close: Option<mpsc::OwnedPermit<Queued>>,
    opened: bool,
}
impl Output {
    fn sample(&mut self, tx: &mpsc::Sender<Queued>, mut report: Report) -> bool {
        if self.close.is_none() {
            self.close = tx.clone().try_reserve_owned().ok();
            if self.close.is_none() {
                return false;
            }
        }
        let mut pending = self.pending.lock().unwrap();
        if let Some(previous) = pending.as_ref() {
            // The consumer must see an open before a change for this ordinal.
            report.phase = previous.phase;
            *pending = Some(report);
            return true;
        }
        let Ok(permit) = tx.try_reserve() else {
            return false;
        };
        report.phase = if self.opened { "change" } else { "open" };
        *pending = Some(report);
        permit.send(Queued::Sample(self.pending.clone()));
        self.opened = true;
        true
    }
    pub(super) fn finish(self, report: Report) {
        if self.opened {
            self.close
                .expect("reserved close")
                .send(Queued::Close(report));
        }
    }
}

#[cfg(test)]
mod output_tests {
    use super::*;

    fn measurement(ordinal: u64, phase: &'static str, tick: u64) -> Report {
        let mut families = [Counts::default(); 4];
        families[0].rx_bytes = tick;
        Report {
            device: None,
            ordinal,
            phase,
            selected: "relay",
            partial: false,
            families,
        }
    }

    #[test]
    fn every_live_connection_has_sample_room_at_the_64_connection_limit() {
        let (tx, mut rx) = queue(64);
        let mut outputs: Vec<_> = (0..64).map(|_| Output::default()).collect();
        // Deterministic cadence ticks. The real connection test independently
        // exercises the timer. Stall the writer for the opening burst so all
        // terminal reservations and all sample slots are occupied together.
        for tick in [0, 5, 10, 15] {
            for (id, output) in outputs.iter_mut().enumerate() {
                assert!(output.sample(&tx, measurement(id as u64, "change", tick)));
            }
            assert_eq!(tx.capacity(), 0);
            // A noisy connection can only replace its own pending measurement.
            for _ in 0..3 {
                assert!(outputs[0].sample(&tx, measurement(0, "change", tick + 1)));
            }
            for id in 0..64 {
                let r = rx.try_recv().expect("refresh missing at this tick").take();
                assert_eq!(r.ordinal, id);
                assert_eq!(r.phase, if tick == 0 { "open" } else { "change" });
                assert_eq!(r.families[0].rx_bytes, tick + u64::from(id == 0));
            }
            assert!(rx.try_recv().is_err());
        }
        for (id, output) in outputs.into_iter().enumerate() {
            output.finish(measurement(id as u64, "close", 16));
        }
        for id in 0..64 {
            let r = rx.try_recv().unwrap().take();
            assert_eq!((r.ordinal, r.phase), (id, "close"));
        }
    }

    #[test]
    fn first_deliverable_measurement_opens_after_queue_pressure() {
        let (tx, mut rx) = queue(1);
        let mut old = Output::default();
        assert!(old.sample(&tx, measurement(1, "open", 0)));
        old.finish(measurement(1, "close", 1));
        let mut new = Output::default();
        assert!(!new.sample(&tx, measurement(2, "open", 2))); // Both slots full.
        assert_eq!(rx.try_recv().unwrap().take().phase, "open");
        assert!(!new.sample(&tx, measurement(2, "change", 3))); // Only close fits.
        assert_eq!(rx.try_recv().unwrap().take().phase, "close");
        assert!(new.sample(&tx, measurement(2, "change", 4)));
        assert!(new.sample(&tx, measurement(2, "change", 5)));
        new.finish(measurement(2, "close", 6));
        let r = rx.try_recv().unwrap().take();
        assert_eq!((r.ordinal, r.phase, r.families[0].rx_bytes), (2, "open", 5));
        assert_eq!(rx.try_recv().unwrap().take().phase, "close");
    }
}

pub(super) struct Observer {
    device: Option<DeviceSample>,
    ordinal: u64,
    events: PathEventStream,
    closed: [Counts; 4],
    partial: bool,
    selected: &'static str,
}
// A missing selection is an observation, not a change of the remembered path.
fn selection(
    last: &mut &'static str,
    current: Option<&'static str>,
    closing: bool,
) -> &'static str {
    if let Some(name) = current {
        *last = name;
    }
    current.unwrap_or(if closing { *last } else { "unknown" })
}

#[cfg(test)]
#[test]
fn scope_matches_device_address_classes() {
    for (host, expected) in [
        ("10.1.2.3:9", "local"),
        ("172.16.0.1:9", "local"),
        ("192.168.1.1:9", "local"),
        ("169.254.2.1:9", "local"),
        ("100.64.0.1:9", "local"),
        ("100.127.255.255:9", "local"),
        ("100.128.0.1:9", "global"),
        ("172.32.0.1:9", "global"),
        ("127.0.0.1:9", "global"),
        ("[fd00::1]:9", "local"),
        ("[fe80::1]:9", "local"),
        ("[::ffff:192.168.1.2]:9", "local"),
        ("[2001:db8::1]:9", "global"),
    ] {
        assert_eq!(
            scope(&TransportAddr::Ip(host.parse().unwrap())),
            Some(expected)
        );
    }
    assert_eq!(
        scope(&TransportAddr::Relay(
            "https://relay.invalid/".parse().unwrap()
        )),
        None
    );
}

#[cfg(test)]
#[test]
fn empty_change_does_not_erase_close_fallback() {
    let mut last = "unknown";
    assert_eq!(selection(&mut last, None, true), "unknown");
    assert_eq!(selection(&mut last, Some("relay"), false), "relay");
    assert_eq!(selection(&mut last, None, false), "unknown");
    assert_eq!(selection(&mut last, None, true), "relay");
    assert_eq!(selection(&mut last, Some("ipv6"), true), "ipv6");
}

impl Observer {
    pub(super) fn new(conn: &Connection, epoch: Option<u64>) -> Self {
        static NEXT: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(1);
        Self {
            device: epoch.map(|epoch| DeviceSample {
                peer: conn.remote_id().to_string(),
                epoch,
                scope: None,
                rtt_ms: None,
            }),
            ordinal: NEXT.fetch_add(1, std::sync::atomic::Ordering::Relaxed),
            events: conn.path_events(),
            closed: Default::default(),
            partial: false,
            selected: "unknown",
        }
    }
    pub(super) async fn next(&mut self) -> Option<PathEvent> {
        poll_fn(|cx| Pin::new(&mut self.events).poll_next(cx)).await
    }
    pub(super) async fn finish(mut self) -> Report {
        // Closing the connection causes iroh to emit Closed.last_stats for
        // each remaining path before it drops the event sender. Its retained
        // list is *not* an open-path snapshot at this point.
        if tokio::time::timeout(std::time::Duration::from_millis(750), async {
            while let Some(event) = self.next().await {
                self.update(event);
            }
        })
        .await
        .is_err()
        {
            self.partial = true;
        }
        Report {
            device: self.device,
            ordinal: self.ordinal,
            phase: "close",
            selected: self.selected,
            partial: self.partial,
            families: self.closed,
        }
    }
    pub(super) fn update(&mut self, event: PathEvent) {
        match event {
            PathEvent::Closed {
                remote_addr,
                last_stats,
                ..
            } => self.closed[family(&remote_addr).0].add(&last_stats),
            PathEvent::Selected { remote_addr, .. } => {
                self.selected = family(&remote_addr).1;
                if let Some(d) = &mut self.device {
                    d.scope = scope(&remote_addr);
                    d.rtt_ms = None; // A selection event alone has no RTT sample.
                }
            }
            PathEvent::Lagged { .. } => self.partial = true,
            _ => (),
        }
    }
    pub(super) fn report(
        &mut self,
        conn: &Connection,
        phase: &'static str,
        tx: &mpsc::Sender<Queued>,
        output: &mut Output,
    ) {
        let mut families = self.closed;
        let mut selected = None;
        for p in conn.paths().iter() {
            let (i, name) = family(p.remote_addr());
            families[i].open += 1;
            let stats = p.stats();
            families[i].add(&stats);
            if p.is_selected() {
                selected = Some(name);
                if let Some(d) = &mut self.device {
                    d.scope = scope(p.remote_addr());
                    d.rtt_ms = u64::try_from(stats.rtt.as_millis()).ok();
                }
            }
        }
        let selected = selection(&mut self.selected, selected, phase == "close");
        if !output.sample(
            tx,
            Report {
                device: self.device.clone().map(|mut d| {
                    if selected == "unknown" {
                        d.scope = None;
                        d.rtt_ms = None;
                    }
                    d
                }),
                ordinal: self.ordinal,
                phase,
                selected,
                partial: self.partial,
                families,
            },
        ) {
            self.partial = true;
        }
    }
}
