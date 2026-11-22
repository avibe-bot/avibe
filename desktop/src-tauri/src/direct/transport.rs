//! QUIC authenticates the endpoint named in the offer; the WebView still
//! negotiates its own TLS with the instance and supplies its own HTTP auth.
use super::{
    clock::Clock,
    connect::Authority,
    offer::{Offer, ALPN},
    path::{SelectedPath, TrialRelay},
    selection::Selection,
};
use iroh::{
    endpoint::{presets, Connection, PortmapperConfig, QuicTransportConfig, VarInt},
    Endpoint, NetReportConfig,
};
use std::{
    io,
    pin::Pin,
    sync::{Arc, Mutex},
    task::{Context, Poll},
    time::Duration,
};
use tokio::{
    io::{AsyncRead, AsyncWrite, ReadBuf},
    time::{timeout_at, Instant},
};

pub(super) trait Duplex: AsyncRead + AsyncWrite + Unpin + Send {}
impl<T: AsyncRead + AsyncWrite + Unpin + Send> Duplex for T {}
pub(super) type Stream = Box<dyn Duplex>;

// The idle monitor may not yet have run after resume. Enforce admission
// before polling an existing direct stream, not just before opening a new one.
struct AdmittedStream<S> {
    inner: S,
    clock: Clock,
    deadline: tokio::sync::watch::Receiver<Duration>,
}
impl<S> AdmittedStream<S> {
    fn check(&self) -> io::Result<()> {
        if self.clock.now() >= *self.deadline.borrow() {
            Err(io::ErrorKind::TimedOut.into())
        } else {
            Ok(())
        }
    }
}
impl<S: AsyncRead + Unpin> AsyncRead for AdmittedStream<S> {
    fn poll_read(mut self: Pin<&mut Self>, cx: &mut Context<'_>, buf: &mut ReadBuf<'_>) -> Poll<io::Result<()>> {
        self.check()?;
        Pin::new(&mut self.inner).poll_read(cx, buf)
    }
}
impl<S: AsyncWrite + Unpin> AsyncWrite for AdmittedStream<S> {
    fn poll_write(mut self: Pin<&mut Self>, cx: &mut Context<'_>, buf: &[u8]) -> Poll<io::Result<usize>> {
        self.check()?;
        Pin::new(&mut self.inner).poll_write(cx, buf)
    }
    fn poll_flush(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        self.check()?;
        Pin::new(&mut self.inner).poll_flush(cx)
    }
    fn poll_shutdown(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        Pin::new(&mut self.inner).poll_shutdown(cx)
    }
}

pub(super) fn endpoint_builder(key: iroh::SecretKey, relay: Option<&TrialRelay>) -> iroh::endpoint::Builder {
    let keep_alive = Duration::from_millis(1500);
    let config = QuicTransportConfig::builder()
        .max_concurrent_bidi_streams(VarInt::from_u32(0))
        .max_concurrent_uni_streams(VarInt::from_u32(0))
        .stream_receive_window(VarInt::from_u32(65536))
        .receive_window(VarInt::from_u32(4 << 20))
        .send_window(4 << 20)
        .datagram_receive_buffer_size(None)
        .max_idle_timeout(Some(VarInt::from_u32(6000).into()))
        .keep_alive_interval(keep_alive)
        .default_path_keep_alive_interval(keep_alive)
        .send_observed_address_reports(false)
        .max_remote_nat_traversal_addresses(8)
        .receive_observed_address_reports(false)
        .build();
    let mut report = NetReportConfig::minimal();
    report.https_probes = relay.is_some();
    Endpoint::builder(presets::Minimal)
        .secret_key(key)
        .relay_mode(TrialRelay::mode(relay))
        .clear_address_lookup()
        .portmapper_config(PortmapperConfig::Disabled)
        .net_report_config(report)
        .transport_config(config)
}

#[derive(Default)]
struct Commits {
    live: usize,
    lost: bool,
    retired: bool,
}
pub(super) struct Committed(Arc<Link>);
impl Drop for Committed {
    fn drop(&mut self) {
        let mut c = self.0.commits.lock().unwrap();
        c.live -= 1;
        // I/O can observe loss before the path monitor gets scheduled.
        c.lost |= !self.0.usable();
    }
}

pub(super) struct Link {
    pub conn: Connection,
    pub offer: Offer,
    pub plan: Selection,
    commits: Mutex<Commits>,
    pub(super) clock: Clock,
    deadline: tokio::sync::watch::Sender<Duration>,
}
impl Drop for Link {
    fn drop(&mut self) {
        self.retire()
    }
}
impl Link {
    pub(super) fn retire(&self) {
        self.deadline.send_replace(Duration::ZERO);
        self.conn.close(0u32.into(), b"retired")
    }
    pub(super) fn remaining(&self) -> Duration {
        self.deadline.borrow().saturating_sub(self.clock.now())
    }
    pub(super) fn deadlines(&self) -> tokio::sync::watch::Receiver<Duration> {
        self.deadline.subscribe()
    }
    pub(super) fn renew(&self, next: &Offer) -> bool {
        if self.offer.id != next.id
            || self.offer.generation != next.generation
            || self.offer.relay != next.relay
            || self.offer.candidates != next.candidates
            || self.clock.now() >= next.until
            || !self.usable()
        {
            return false;
        }
        self.deadline.send_replace(next.until);
        true
    }
    pub(super) fn commit_stream(self: &Arc<Self>) -> Option<Committed> {
        let mut c = self.commits.lock().unwrap();
        if c.retired || !self.usable() {
            return None;
        }
        c.live += 1;
        Some(Committed(self.clone()))
    }
    pub(super) fn claim_loss(&self) -> bool {
        let mut c = self.commits.lock().unwrap();
        let closed = !c.retired && (c.live > 0 || c.lost);
        c.retired = true;
        closed
    }
    pub(super) fn has_stream(&self) -> bool {
        self.commits.lock().unwrap().live > 0
    }
    pub(super) fn usable(&self) -> bool {
        self.selected_path().is_some()
    }
    pub(super) fn selected_in(&self, paths: iroh::endpoint::PathList<'_>) -> Option<SelectedPath> {
        paths
            .iter()
            .find(|p| p.is_selected())
            .and_then(|p| self.offer.selected(self.conn.remote_id(), p.remote_addr()))
    }
    pub(super) fn selected_path(&self) -> Option<SelectedPath> {
        if self.remaining().is_zero() || self.conn.close_reason().is_some() {
            return None;
        }
        self.selected_in(self.conn.paths())
    }
    pub(super) async fn dial(ep: &Endpoint, offer: Offer, host: &Authority, clock: Clock) -> Option<Arc<Self>> {
        let remaining = offer.until.saturating_sub(clock.now());
        if remaining.is_zero() {
            return None;
        }
        let addr = offer.bootstrap();
        let until = Instant::now() + remaining.min(Duration::from_secs(5));
        let conn = timeout_at(until, ep.connect(addr, ALPN)).await.ok()?.ok()?;
        let link = Arc::new(Self {
            clock,
            deadline: tokio::sync::watch::channel(offer.until).0,
            conn,
            offer,
            plan: Selection::default(),
            commits: Mutex::default(),
        });
        let _probe = timeout_at(until, link.open(host)).await.ok()??;
        Some(link)
    }
    pub(super) async fn open(&self, host: &Authority) -> Option<Stream> {
        if !self.usable() {
            return None;
        }
        let (mut send, mut recv) = self.conn.open_bi().await.ok()?;
        if !self.usable() {
            return None;
        }
        // AVDT1 is private to sidecar→ingress. Never include browser proxy
        // headers, a local admission token or any application-layer credentials.
        send.write_all(&host.direct_head()).await.ok()?;
        let mut head = Vec::with_capacity(128);
        loop {
            let mut buf = [0; 512];
            let n = recv.read(&mut buf).await.ok()??;
            if n == 0 {
                return None;
            }
            head.extend_from_slice(&buf[..n]);
            if let Some(end) = head.windows(4).position(|s| s == b"\r\n\r\n") {
                // The browser has not sent ClientHello yet; unsolicited bytes
                // after the answer are not a valid TLS server response.
                if end > 4096 || end + 4 != head.len() || !self.usable() {
                    return None;
                }
                let line = head.split(|b| *b == b'\r').next()?;
                if line != b"HTTP/1.1 200" && !line.starts_with(b"HTTP/1.1 200 ") {
                    return None;
                }
                return Some(Box::new(AdmittedStream {
                    inner: tokio::io::join(recv, send),
                    clock: self.clock.clone(),
                    deadline: self.deadlines(),
                }));
            }
            if head.len() > 4099 {
                return None;
            }
        }
    }
}
