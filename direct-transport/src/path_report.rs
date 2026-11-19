//! Aggregate only on the sidecar side of the control boundary. Never serialize
//! a Path, address, endpoint ID, or a dependency Debug representation.
use futures_core::Stream;
use iroh::{
    TransportAddr,
    endpoint::{Connection, PathEvent, PathEventStream, PathStats},
};
use serde::Serialize;
use std::{future::poll_fn, pin::Pin, task::Poll};
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
#[derive(Serialize)]
pub(super) struct Report {
    ordinal: u64,
    phase: &'static str,
    selected: &'static str,
    partial: bool,
    // Fixed order, including paths closed earlier in this connection.
    families: [Counts; 4],
}
pub(super) struct Observer {
    ordinal: u64,
    events: PathEventStream,
    closed: [Counts; 4],
    partial: bool,
    selected: &'static str,
}
impl Observer {
    pub(super) fn new(conn: &Connection) -> Self {
        static NEXT: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(1);
        Self {
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
    pub(super) async fn drain_ready(&mut self) {
        // A terminal accept and the last Closed events can become ready together.
        // Read buffered close-time stats without waiting for a future event.
        for _ in 0..256 {
            let item = poll_fn(|cx| Poll::Ready(Pin::new(&mut self.events).poll_next(cx))).await;
            match item {
                Poll::Ready(Some(e)) => self.update(e),
                _ => return,
            }
        }
        self.partial = true;
    }
    pub(super) fn update(&mut self, event: PathEvent) {
        match event {
            PathEvent::Closed {
                remote_addr,
                last_stats,
                ..
            } => self.closed[family(&remote_addr).0].add(&last_stats),
            PathEvent::Selected { remote_addr, .. } => self.selected = family(&remote_addr).1,
            PathEvent::Lagged { .. } => self.partial = true,
            _ => (),
        }
    }
    pub(super) fn report(
        &mut self,
        conn: &Connection,
        phase: &'static str,
        tx: &mpsc::Sender<Report>,
    ) {
        let mut families = self.closed;
        let mut selected = "unknown";
        for p in conn.paths().iter() {
            let (i, name) = family(p.remote_addr());
            families[i].open += 1;
            families[i].add(&p.stats());
            if p.is_selected() {
                selected = name;
            }
        }
        if phase != "close" || selected != "unknown" {
            self.selected = selected;
        }
        if tx
            .try_send(Report {
                ordinal: self.ordinal,
                phase,
                selected: self.selected,
                partial: self.partial,
                families,
            })
            .is_err()
        {
            self.partial = true;
        }
    }
}
