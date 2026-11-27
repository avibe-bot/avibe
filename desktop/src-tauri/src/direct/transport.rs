//! QUIC authenticates the endpoint named in the offer; the WebView still
//! negotiates its own TLS with the instance and supplies its own HTTP auth.
use super::{
    clock::Clock,
    connect::Authority,
    offer::{Offer, Verdict, ALPN},
    path::{AddressClass, SelectedPath, TrialRelay},
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

/// Bounded diagnostic data only; never carry protocol text or endpoint identities.
#[derive(Clone, Copy, Debug, Eq, PartialEq, serde::Serialize)]
#[serde(rename_all = "snake_case")]
pub(super) enum Stage {
    Rendezvous,
    Dial,
    Open,
    Connect,
    Handover,
}
#[derive(Clone, Copy, Debug, Eq, PartialEq, serde::Serialize)]
#[serde(rename_all = "snake_case")]
pub(super) enum FaultCode {
    Timeout,
    Refused,
    Denied,
    Status,
    ClosedByPeer,
    Expired,
    Other,
    InvalidOffer,
    ListenerClosed,
    Superseded,
    NotReady,
    ConnectionEnded,
    InvalidHead,
    // The instance answered that it has nothing to dial ("unavailable"), by
    // the family of the reason it gave. Not a malformed offer.
    NotReadyCertificate,
    NotReadyOwnerRequired,
    NotReadyNotRunning,
    NotReadyPermission,
    NotReadyTransport,
    NotReadyDisabled,
    NotReadyNoAddress,
    NotReadyOther,
    // No permitted path is selected: the connection has no selected path
    // at all; or its selected path's address is one the grant does not
    // permit, by the class of that address (`AddressClass`). A class, never
    // an address.
    PathUnselected,
    PathRefusedRelay,
    PathRefusedPrivate,
    PathRefusedCgnat,
    PathRefusedUla,
    PathRefusedLinklocal,
    PathRefusedLoopback,
    PathRefusedPublic,
    PathRefusedGlobal,
    PathRefusedNat64,
    PathRefusedMapped,
    PathRefusedOther,
}
impl FaultCode {
    /// The selected path's address is one the grant does not permit.
    pub(super) fn refused(class: AddressClass) -> Self {
        match class {
            AddressClass::Relay => Self::PathRefusedRelay,
            AddressClass::Private => Self::PathRefusedPrivate,
            AddressClass::Cgnat => Self::PathRefusedCgnat,
            AddressClass::Ula => Self::PathRefusedUla,
            AddressClass::Linklocal => Self::PathRefusedLinklocal,
            AddressClass::Loopback => Self::PathRefusedLoopback,
            AddressClass::Public => Self::PathRefusedPublic,
            AddressClass::Global => Self::PathRefusedGlobal,
            AddressClass::Nat64 => Self::PathRefusedNat64,
            AddressClass::Mapped => Self::PathRefusedMapped,
            AddressClass::Other => Self::PathRefusedOther,
        }
    }
}
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(super) struct Fault(pub(super) Stage, pub(super) FaultCode);

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
        self.judged(paths).ok()
    }
    /// The selected path the grant permits; or why there is none, in the
    /// word the reports use: nothing is selected, or the selected path's
    /// address is of a class the grant refuses.
    pub(super) fn judged(&self, paths: iroh::endpoint::PathList<'_>) -> Result<SelectedPath, FaultCode> {
        let paths = paths.iter().map(|p| (p.is_selected(), p.remote_addr().clone()));
        match self.offer.judge(self.conn.remote_id(), paths) {
            Verdict::Accepted(path) => Ok(path),
            Verdict::Unselected => Err(FaultCode::PathUnselected),
            Verdict::Refused(class) => Err(FaultCode::refused(class)),
        }
    }
    pub(super) fn selected_path(&self) -> Option<SelectedPath> {
        if self.remaining().is_zero() || self.conn.close_reason().is_some() {
            return None;
        }
        self.selected_in(self.conn.paths())
    }
    pub(super) fn check_path(&self, stage: Stage) -> Result<(), Fault> {
        if self.remaining().is_zero() {
            return Err(Fault(stage, FaultCode::Expired));
        }
        if self.conn.close_reason().is_some() {
            return Err(Fault(stage, FaultCode::ConnectionEnded));
        }
        self.judged(self.conn.paths())
            .map(|_| ())
            .map_err(|code| Fault(stage, code))
    }
    #[cfg(test)]
    pub(super) async fn dial(ep: &Endpoint, offer: Offer, host: &Authority, clock: Clock) -> Option<Arc<Self>> {
        Self::dial_detailed(ep, offer, host, clock).await.ok()
    }
    pub(super) async fn dial_detailed(
        ep: &Endpoint,
        offer: Offer,
        host: &Authority,
        clock: Clock,
    ) -> Result<Arc<Self>, Fault> {
        let remaining = offer.until.saturating_sub(clock.now());
        if remaining.is_zero() {
            return Err(Fault(Stage::Dial, FaultCode::Expired));
        }
        let addr = offer.bootstrap();
        let until = Instant::now() + remaining.min(Duration::from_secs(5));
        let conn = timeout_at(until, ep.connect(addr, ALPN))
            .await
            .map_err(|_| Fault(Stage::Dial, FaultCode::Timeout))?
            .map_err(|_| Fault(Stage::Dial, FaultCode::Other))?;
        let link = Arc::new(Self {
            clock,
            deadline: tokio::sync::watch::channel(offer.until).0,
            conn,
            offer,
            plan: Selection::default(),
            commits: Mutex::default(),
        });
        let mut stage = Stage::Open;
        let _probe = timeout_at(until, link.open_stage(host, &mut stage))
            .await
            .map_err(|_| Fault(stage, FaultCode::Timeout))??;
        Ok(link)
    }
    pub(super) async fn open(&self, host: &Authority) -> Option<Stream> {
        self.open_detailed(host).await.ok()
    }
    pub(super) async fn open_detailed(&self, host: &Authority) -> Result<Stream, Fault> {
        let mut stage = Stage::Open;
        self.open_stage(host, &mut stage).await
    }
    async fn open_stage(&self, host: &Authority, stage: &mut Stage) -> Result<Stream, Fault> {
        self.check_path(*stage)?;
        let (mut send, mut recv) = self
            .conn
            .open_bi()
            .await
            .map_err(|_| Fault(*stage, FaultCode::ConnectionEnded))?;
        self.check_path(*stage)?;
        *stage = Stage::Connect;
        // AVDT1 is private to sidecar→ingress. Never include browser proxy
        // headers, a local admission token or any application-layer credentials.
        send.write_all(&host.direct_head())
            .await
            .map_err(|_| Fault(*stage, FaultCode::Other))?;
        let mut head = Vec::with_capacity(128);
        loop {
            let mut buf = [0; 512];
            let n = recv
                .read(&mut buf)
                .await
                .map_err(|_| Fault(*stage, FaultCode::Other))?
                .ok_or(Fault(*stage, FaultCode::ClosedByPeer))?;
            if n == 0 {
                return Err(Fault(*stage, FaultCode::ClosedByPeer));
            }
            head.extend_from_slice(&buf[..n]);
            if let Some(end) = head.windows(4).position(|s| s == b"\r\n\r\n") {
                // The browser has not sent ClientHello yet; unsolicited bytes
                // after the answer are not a valid TLS server response.
                if end > 4096 || end + 4 != head.len() {
                    return Err(Fault(*stage, FaultCode::InvalidHead));
                }
                self.check_path(*stage)?;
                let line = head.split(|b| *b == b'\r').next().unwrap_or_default();
                if line != b"HTTP/1.1 200" && !line.starts_with(b"HTTP/1.1 200 ") {
                    return Err(Fault(*stage, FaultCode::Status));
                }
                return Ok(Box::new(AdmittedStream {
                    inner: tokio::io::join(recv, send),
                    clock: self.clock.clone(),
                    deadline: self.deadlines(),
                }));
            }
            if head.len() > 4099 {
                return Err(Fault(*stage, FaultCode::InvalidHead));
            }
        }
    }
}
