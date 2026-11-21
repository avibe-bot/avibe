//! QUIC authenticates the endpoint named in the offer; the WebView still
//! negotiates its own TLS with the instance and supplies its own HTTP auth.
use super::{
    connect::Authority,
    offer::{Offer, ALPN},
    selection::Selection,
};
use iroh::{
    endpoint::{presets, Connection, PortmapperConfig, QuicTransportConfig, VarInt},
    Endpoint, EndpointAddr, NetReportConfig, RelayMode, TransportAddr,
};
use std::{sync::Arc, time::Duration};
use tokio::{
    io::{AsyncRead, AsyncWrite},
    time::{timeout_at, Instant},
};

pub(super) trait Duplex: AsyncRead + AsyncWrite + Unpin + Send {}
impl<T: AsyncRead + AsyncWrite + Unpin + Send> Duplex for T {}
pub(super) type Stream = Box<dyn Duplex>;

pub(super) async fn bind(key: iroh::SecretKey) -> Option<Endpoint> {
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
        .receive_observed_address_reports(false)
        .build();
    Endpoint::builder(presets::Minimal)
        .secret_key(key)
        .relay_mode(RelayMode::Disabled)
        .clear_address_lookup()
        .portmapper_config(PortmapperConfig::Disabled)
        .net_report_config(NetReportConfig::minimal())
        .transport_config(config)
        .bind()
        .await
        .ok()
}

pub(super) struct Link {
    pub conn: Connection,
    pub offer: Offer,
    pub plan: Selection,
    deadline: tokio::sync::watch::Sender<Instant>,
}
impl Drop for Link {
    fn drop(&mut self) {
        self.retire()
    }
}
impl Link {
    pub(super) fn retire(&self) {
        self.conn.close(0u32.into(), b"retired")
    }
    pub(super) fn deadlines(&self) -> tokio::sync::watch::Receiver<Instant> {
        self.deadline.subscribe()
    }
    pub(super) fn renew(&self, next: &Offer) -> bool {
        if self.offer.id != next.id
            || self.offer.generation != next.generation
            || self.offer.candidates != next.candidates
            || !self.usable()
        {
            return false;
        }
        self.deadline.send_replace(next.until);
        true
    }
    pub(super) fn usable(&self) -> bool {
        Instant::now() < *self.deadline.borrow()
            && self.conn.close_reason().is_none()
            && self.conn.paths().iter().any(|p| {
                p.is_selected() && matches!(p.remote_addr(),TransportAddr::Ip(a) if self.offer.candidates.contains(a))
            })
    }
    pub(super) async fn dial(ep: &Endpoint, offer: Offer, host: &Authority) -> Option<Arc<Self>> {
        let until = offer.until.min(Instant::now() + Duration::from_secs(5));
        if Instant::now() >= until {
            return None;
        }
        let addr = offer
            .candidates
            .iter()
            .fold(EndpointAddr::new(offer.id), |a, s| a.with_ip_addr(*s));
        let conn = timeout_at(until, ep.connect(addr, ALPN)).await.ok()?.ok()?;
        let link = Arc::new(Self {
            deadline: tokio::sync::watch::channel(offer.until).0,
            conn,
            offer,
            plan: Selection::default(),
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
                return Some(Box::new(tokio::io::join(recv, send)));
            }
            if head.len() > 4099 {
                return None;
            }
        }
    }
}
