//! On-demand measurements never enter the proxy race, ownership census or handover ledger.
use super::{
    clock::Clock, connect::Authority, credentials::Grant, public, transport::Stream, AddressFamily, Handover,
    SelectedPath, WindowRegistration,
};
use http_body_util::{BodyExt, Empty, Limited};
use hyper::{body::Bytes, client::conn::http1, Request};
use hyper_util::rt::TokioIo;
use iroh::TransportAddr;
use serde::Serialize;
use std::{
    future::Future,
    sync::{Arc, OnceLock},
    time::{Duration, SystemTime, UNIX_EPOCH},
};
use tokio::sync::Semaphore;
use tokio_rustls::{
    rustls::{self, pki_types::ServerName},
    TlsConnector,
};

#[cfg(test)]
#[path = "../../tests/direct/measure.rs"]
mod tests;

const COUNT: usize = 5;
const PER_SAMPLE: Duration = Duration::from_secs(5);
const TOTAL: Duration = Duration::from_secs(30);
static SLOT: Semaphore = Semaphore::const_new(1);

#[derive(Debug)]
pub struct MeasureBusy;
#[derive(Debug, Clone, Copy, Serialize)]
#[serde(rename_all = "lowercase")]
enum Error {
    Failed,
    Timeout,
}
#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct MeasureResult {
    measured_at: u64,
    basis: &'static str,
    samples: usize,
    current: Option<Leg>,
    tunnel: Option<Leg>,
}
#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct Leg {
    kind: &'static str,
    scope: Option<&'static str>,
    relay_region: Option<&'static str>,
    ok: usize,
    median_ms: Option<f64>,
    max_ms: Option<f64>,
    error: Option<Error>,
}
impl Leg {
    fn new(kind: &'static str, scope: Option<&'static str>, relay_region: Option<&'static str>) -> Self {
        Self {
            kind,
            scope,
            relay_region,
            ok: 0,
            median_ms: None,
            max_ms: None,
            error: None,
        }
    }
    fn finish(&mut self, times: &mut [Duration]) {
        times.sort_unstable();
        self.ok = times.len();
        let ms = |d: Duration| d.as_secs_f64() * 1000.;
        self.max_ms = times.last().copied().map(ms);
        if self.ok > 0 {
            self.median_ms = Some((ms(times[(self.ok - 1) / 2]) + ms(times[self.ok / 2])) / 2.);
        }
    }
}

fn tls_config(roots: rustls::RootCertStore) -> Result<TlsConnector, Error> {
    let mut config = rustls::ClientConfig::builder_with_provider(Arc::new(rustls::crypto::ring::default_provider()))
        .with_safe_default_protocol_versions()
        .map_err(|_| Error::Failed)?
        .with_root_certificates(roots)
        .with_no_client_auth();
    config.alpn_protocols = vec![b"http/1.1".to_vec()];
    Ok(TlsConnector::from(Arc::new(config)))
}

fn tls() -> Result<TlsConnector, Error> {
    static CONFIG: OnceLock<Result<TlsConnector, Error>> = OnceLock::new();
    CONFIG
        .get_or_init(|| {
            let roots = rustls::RootCertStore::from_iter(webpki_roots::TLS_SERVER_ROOTS.iter().cloned());
            tls_config(roots)
        })
        .clone()
}

// Tokio's awake-time timer alone cannot enforce a deadline on resume. Check
// before and after an operation and wake even if the peer becomes silent.
async fn bounded<T>(clock: &Clock, end: Duration, f: impl Future<Output = Result<T, Error>>) -> Result<T, Error> {
    let expiry = async {
        loop {
            let left = end.saturating_sub(clock.now());
            if left.is_zero() {
                break;
            }
            tokio::time::sleep(left.min(Duration::from_millis(250))).await;
        }
    };
    tokio::select! {biased; _=expiry=>Err(Error::Timeout), result=f=>if clock.now() >= end {Err(Error::Timeout)} else {result}}
}

async fn requests<S: tokio::io::AsyncRead + tokio::io::AsyncWrite + Unpin + Send>(
    socket: S,
    host: &Authority,
    clock: &Clock,
    end: Duration,
    times: &mut Vec<Duration>,
) -> Result<(), Error> {
    let (mut sender, driver) = http1::Builder::new()
        .max_buf_size(16 * 1024)
        .handshake(TokioIo::new(socket))
        .await
        .map_err(|_| Error::Failed)?;
    let work = async {
        for index in 0..=COUNT {
            let req = Request::get("/health")
                .header("Host", host.host())
                .header("Cache-Control", "no-cache, no-store")
                .body(Empty::<Bytes>::new())
                .map_err(|_| Error::Failed)?;
            let start = clock.now();
            let elapsed = bounded(clock, end.min(start.saturating_add(PER_SAMPLE)), async {
                let response = sender.send_request(req).await.map_err(|_| Error::Failed)?;
                if response.status() != 200 {
                    return Err(Error::Failed);
                }
                let bytes = Limited::new(response.into_body(), 8192)
                    .collect()
                    .await
                    .map_err(|_| Error::Failed)?
                    .to_bytes();
                let elapsed = clock.now().saturating_sub(start);
                let json: serde_json::Value = serde_json::from_slice(&bytes).map_err(|_| Error::Failed)?;
                if json.get("status").and_then(|v| v.as_str()) != Some("ok") {
                    return Err(Error::Failed);
                }
                Ok(elapsed)
            })
            .await?;
            if index > 0 {
                times.push(elapsed);
            }
        }
        Ok(())
    };
    // Drive Hyper inline. Never detach a socket driver past cancellation,
    // grant loss or the process-wide measurement slot. A clean EOF can race
    // with the last body: let the sender consume its buffered outcome.
    tokio::pin!(work);
    tokio::select! { biased; result=&mut work=>result, _=driver=>work.await }
}

async fn probe(
    host: &Authority,
    connector: Result<TlsConnector, Error>,
    open: impl Future<Output = Option<Stream>>,
    clock: &Clock,
    end: Duration,
    times: &mut Vec<Duration>,
) -> Result<(), Error> {
    let raw = open.await.ok_or(Error::Failed)?;
    let name = ServerName::try_from(host.host().to_owned()).map_err(|_| Error::Failed)?;
    let socket = connector?.connect(name, raw).await.map_err(|_| Error::Failed)?;
    requests(socket, host, clock, end, times).await
}

fn selected(link: &super::Link) -> Option<(TransportAddr, Leg)> {
    if link.remaining().is_zero() || link.conn.close_reason().is_some() {
        return None;
    }
    let paths = link.conn.paths();
    let path = paths.iter().find(|p| p.is_selected())?;
    let addr = path.remote_addr();
    let selected = link.offer.selected(link.conn.remote_id(), addr)?;
    let leg = match selected {
        SelectedPath::Lan(f) | SelectedPath::Punched(f) => Leg::new(
            match f {
                AddressFamily::Ipv4 => "ipv4",
                AddressFamily::Ipv6 => "ipv6",
            },
            Some(if matches!(selected, SelectedPath::Lan(_)) {
                "local"
            } else {
                "global"
            }),
            None,
        ),
        SelectedPath::Relay => {
            let TransportAddr::Relay(url) = addr else { return None };
            let region = match url.as_str() {
                "https://use1-1.relay.n0.iroh.link./" => Some("use1"),
                "https://usw1-1.relay.n0.iroh.link./" => Some("usw1"),
                "https://euc1-1.relay.n0.iroh.link./" => Some("euc1"),
                "https://aps1-1.relay.n0.iroh.link./" => Some("aps1"),
                _ => None,
            };
            Leg::new("relay", None, region)
        }
    };
    Some((addr.clone(), leg))
}

async fn current_leg(
    host: &Authority,
    current: Option<(&Handover, &WindowRegistration)>,
    clock: &Clock,
    end: Duration,
) -> Option<Leg> {
    let (h, r) = current?;
    if h.host.host() != host.host() {
        return None;
    }
    let owner = h.listener.upgrade()?;
    if !owner.is_alive() {
        return None;
    }
    let grant: Grant = owner.windows.window(r)?;
    if !*grant.valid.borrow() {
        return None;
    }
    let link = h.links.borrow().clone()?;
    let (addr, mut leg) = selected(&link)?;
    let valid = || {
        owner.is_alive()
            && h.links.borrow().as_ref().is_some_and(|l| Arc::ptr_eq(l, &link))
            && selected(&link).is_some_and(|(a, _)| a == addr)
    };
    let mut times = Vec::with_capacity(COUNT);
    let mut result = Err(Error::Failed);
    grant
        .until_revoked(async {
            let operation = probe(host, tls(), link.open(host), clock, end, &mut times);
            tokio::pin!(operation);
            let guarded = std::future::poll_fn(|cx| {
                if valid() {
                    operation.as_mut().poll(cx)
                } else {
                    std::task::Poll::Ready(Err(Error::Failed))
                }
            });
            result = bounded(clock, end, guarded).await;
        })
        .await;
    leg.error = result.err();
    leg.finish(&mut times);
    Some(leg)
}

/// Fixed target, native-selected host only. `None` skips the current leg,
/// never the deliberate tunnel diagnostic. No application credentials,
/// redirects, reconnects, proxy-listener traffic or selection mutations.
pub async fn measure(
    host: &str,
    current: Option<(&Handover, &WindowRegistration)>,
) -> Result<MeasureResult, MeasureBusy> {
    let _slot = SLOT.try_acquire().map_err(|_| MeasureBusy)?;
    let clock = Clock::default();
    let end = clock.now().saturating_add(TOTAL);
    let (current, tunnel) = if let Some(host) = Authority::managed(host) {
        let port = current.map_or(0, |(h, _)| h.port);
        let tunnel = async {
            let mut times = Vec::with_capacity(COUNT);
            let mut leg = Leg::new("tunnel", None, None);
            let dial = async { public::open(&host, port).await.ok().map(|s| Box::new(s) as Stream) };
            leg.error = bounded(&clock, end, probe(&host, tls(), dial, &clock, end, &mut times))
                .await
                .err();
            leg.finish(&mut times);
            Some(leg)
        };
        tokio::join!(current_leg(&host, current, &clock, end), tunnel)
    } else {
        (None, None)
    };
    let measured_at = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as u64;
    Ok(MeasureResult {
        measured_at,
        basis: "warm-https-health",
        samples: COUNT,
        current,
        tunnel,
    })
}
