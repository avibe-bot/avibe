//! Authenticated, one-authority loopback CONNECT listener. The browser's TLS
//! bytes stay buffered until a candidate is committed; there is no retry on
//! any error after the 200. Drop/abort owns and cancels all accepted sockets.
use super::{
    connect::{parse_connect, Authority, Head},
    credentials::Windows,
    faults::PathWord,
    public,
    quiet::{PublicTicket, SessionPolicy},
    strict,
    transport::{Committed, Link, Stream},
    usage::Usage,
    PathStatus, PreferredPath,
};
use std::{
    future::Future,
    net::Ipv4Addr,
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    },
    time::Duration,
};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::{TcpListener, TcpStream},
    sync::watch,
    task::{JoinHandle, JoinSet},
    time::timeout,
};

pub(super) struct Listener {
    #[cfg(test)]
    pub(super) accept_fault: Arc<tokio::sync::Notify>,
    pub access: Arc<ListenerAccess>,
    pub port: u16,
    // Retain the bound port until the native owner detaches, even if the
    // accept worker dies. Another process must not inherit a published port.
    _reservation: Arc<TcpListener>,
}
// A handle operation can retain these references, never the port reservation or
// the owner's decision to retire. Only Listener owns shutdown.
pub(super) struct ListenerAccess {
    pub windows: Arc<Windows>,
    pub(super) policy: SessionPolicy,
    /// Which leg carried the windows' connections, for the instance's log.
    pub(super) usage: Arc<Usage>,
    retired: AtomicBool,
    health: watch::Receiver<()>,
    task: JoinHandle<()>,
}
impl ListenerAccess {
    pub(super) fn owned(&self) -> bool {
        !self.retired.load(Ordering::Acquire)
    }
    pub(super) fn is_alive(&self) -> bool {
        self.owned() && self.health.has_changed().is_ok() && !self.task.is_finished()
    }
    pub(super) fn window_credential(&self, window: &str) -> Option<super::WindowRegistration> {
        self.is_alive().then(|| self.windows.credential(window)).flatten()
    }
}
impl Drop for Listener {
    fn drop(&mut self) {
        self.retire();
    }
}
impl Listener {
    pub(super) fn retire(&self) {
        if !self.access.retired.swap(true, Ordering::AcqRel) {
            self.access.task.abort();
        }
    }
    pub(super) async fn bind(
        host: Authority,
        links: watch::Receiver<Option<Arc<Link>>>,
        status: watch::Sender<PathStatus>,
    ) -> Option<Self> {
        Self::bind_with_port(
            host,
            links,
            status,
            |h, port| async move { public::open(&h, port).await },
        )
        .await
    }
    pub(super) fn is_alive(&self) -> bool {
        self.access.is_alive()
    }
    pub(super) fn health(&self) -> watch::Receiver<()> {
        self.access.health.clone()
    }
    #[cfg(test)]
    pub(super) async fn bind_via<F, Fut>(
        host: Authority,
        links: watch::Receiver<Option<Arc<Link>>>,
        status: watch::Sender<PathStatus>,
        open_public: F,
    ) -> Option<Self>
    where
        F: Fn(Authority) -> Fut + Send + Sync + Clone + 'static,
        Fut: Future<Output = Option<TcpStream>> + Send + 'static,
    {
        Self::bind_with_port(host, links, status, move |h, _| {
            let f = open_public(h);
            async move { f.await.ok_or(public::OpenError::Unreachable) }
        })
        .await
    }
    async fn bind_with_port<F, Fut>(
        host: Authority,
        links: watch::Receiver<Option<Arc<Link>>>,
        status: watch::Sender<PathStatus>,
        open_public: F,
    ) -> Option<Self>
    where
        F: Fn(Authority, u16) -> Fut + Send + Sync + Clone + 'static,
        Fut: Future<Output = Result<TcpStream, public::OpenError>> + Send + 'static,
    {
        let windows = Arc::new(Windows::default());
        let socket = Arc::new(TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).await.ok()?);
        let reservation = socket.clone();
        let (live, health) = watch::channel(());
        let port = socket.local_addr().ok()?.port();
        let keys = windows.clone();
        #[cfg(test)]
        let accept_fault = Arc::new(tokio::sync::Notify::new());
        #[cfg(test)]
        let fault = accept_fault.clone();
        let policy = SessionPolicy::default();
        let accept_policy = policy.clone();
        let timing = strict::Timing::default();
        let boot_clock = super::clock::Clock::default();
        let usage = Arc::new(Usage::new(boot_clock.clone()));
        let counted = usage.clone();
        let task = tokio::spawn(async move {
            let _live = live;
            let mut clients = JoinSet::new();
            let mut refusals = JoinSet::new();
            while let Ok((socket, peer)) = accept_with_retry(|| async {
                #[cfg(test)]
                { tokio::select! { biased; _=fault.notified()=>Err(std::io::ErrorKind::InvalidInput.into()), incoming=socket.accept()=>incoming } }
                #[cfg(not(test))]
                { socket.accept().await }
            }).await {
                while clients.try_join_next().is_some() {}
                while refusals.try_join_next().is_some() {}
                if !peer.ip().is_loopback() {
                    continue;
                }
                if clients.len() >= SERVED {
                    // Told so, where it was dropped unanswered: a window
                    // whose route allows it then goes to the host by itself.
                    // Past as many refusals at once, dropped as before.
                    if refusals.len() < SERVED {
                        refusals.spawn(refuse(socket));
                    }
                    continue;
                }
                let h = host.clone();
                let keys=keys.clone();
                let links = links.clone();
                let dial = open_public.clone();
                let policy = accept_policy.clone();
                let status = status.clone();
                let budget = timing.strict_open;
                let clock = boot_clock.clone();
                let usage = counted.clone();
                let paths = status.clone();
                clients.spawn(async move {
                    let mut failed = false;
                    // For the minute's count: whether this CONNECT was
                    // decided in direct-only, and whether a direct open was
                    // started for it at all. Waiting in direct-only for a
                    // link that does not come starts none. Set where the
                    // open runs, read where the CONNECT is counted.
                    let (only, tried) = (AtomicBool::new(false), AtomicBool::new(false));
                    // Counted under the kind of direct path new connections
                    // prefer when the CONNECT is decided.
                    let decided = |what: Decided| {
                        let path = PathWord::of(match paths.borrow().preferred {
                            PreferredPath::Direct(path) => Some(path),
                            PreferredPath::Tunnel => None,
                        });
                        let only = only.load(Ordering::Relaxed);
                        match what {
                            Decided::Direct => usage.direct(path, only),
                            Decided::Public => usage.tunnel(path),
                            Decided::Failed => usage.unanswered(path, only, tried.load(Ordering::Relaxed)),
                        }
                    };
                    serve(socket, &h, &keys, decided, || async {
                        let Some(ticket) = policy.sample() else {
                            only.store(true, Ordering::Relaxed);
                            let (host, tried) = (&h, &tried);
                            let opened = strict::open(links.clone(), &clock, budget, |l| async move {
                                tried.store(true, Ordering::Relaxed);
                                let stream = l.open(host).await?;
                                Some(Backend::Direct(stream, l.commit_stream()?))
                            }).await;
                            failed = opened.is_none();
                            return opened;
                        };
                        let link = links.borrow().clone();
                        let fallback = async { dial(h.clone(), port).await.map(|s| Backend::Public(s, ticket)) };
                        match link {
                            Some(l) if l.usable() => {
                                l.plan
                                    .choose(
                                        async {
                                            tried.store(true, Ordering::Relaxed);
                                            l.open(&h).await.map(|s| Backend::Direct(s, l.clone()))
                                        },
                                        fallback,
                                    )
                                    .await
                            }
                            _ => timeout(Duration::from_secs(8), fallback).await.ok()?.ok(),
                        }?.commit()
                    })
                    .await;
                    // Never publish under the registration validity read lock.
                    if failed { status.send_modify(|s| s.strict_failures = s.strict_failures.saturating_add(1)); }
                });
            }
            // No new accepts. Notify route owners before draining live streams.
            drop(_live);
            drop(refusals);
            while clients.join_next().await.is_some() {}
        });
        Some(Self {
            #[cfg(test)]
            accept_fault,
            access: Arc::new(ListenerAccess {
                windows,
                task,
                health,
                policy,
                usage,
                retired: AtomicBool::new(false),
            }),
            port,
            _reservation: reservation,
        })
    }
}
/// How many connections one listener serves at a time: one session's
/// instance view and the app windows opened from it.
const SERVED: usize = 64;
const BAD_GATEWAY: &[u8] = b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n";

/// Answers a connection the listener has no room for, without reading what
/// it asked: `502`, as when no leg gives a backend, which a route that
/// allows failover takes as leave to go to the host itself. The answer is
/// sent and its end marked before the request is read out, so the close
/// that follows does not reset the connection ahead of the answer. Nothing
/// is authenticated, counted or opened for it.
async fn refuse(mut browser: TcpStream) {
    let _ = timeout(Duration::from_secs(2), async {
        browser.write_all(BAD_GATEWAY).await.ok()?;
        browser.shutdown().await.ok()?;
        let mut rest = [0; 1024];
        while browser.read(&mut rest).await.ok()? != 0 {}
        Some(())
    })
    .await;
}

async fn accept_with_retry<T, F: Future<Output = std::io::Result<T>>>(
    mut accept: impl FnMut() -> F,
) -> std::io::Result<T> {
    let mut backoff = Duration::from_millis(25);
    loop {
        match accept().await {
            Ok(value) => return Ok(value),
            Err(e)
                if matches!(
                    e.kind(),
                    std::io::ErrorKind::Interrupted
                        | std::io::ErrorKind::WouldBlock
                        | std::io::ErrorKind::ConnectionAborted
                        | std::io::ErrorKind::ConnectionReset
                ) || matches!(e.raw_os_error(), Some(23 | 24 | 10024 | 10055)) =>
            {
                tokio::time::sleep(backoff).await;
                backoff = (backoff * 2).min(Duration::from_secs(1));
            }
            Err(e) => return Err(e),
        }
    }
}
enum Backend<G = Committed> {
    Direct(Stream, G),
    Public(TcpStream, PublicTicket),
}
impl Backend<Arc<Link>> {
    fn commit(self) -> Option<Backend> {
        match self {
            Self::Direct(s, link) => Some(Backend::Direct(s, link.commit_stream()?)),
            Self::Public(s, ticket) => Some(Backend::Public(s, ticket)),
        }
    }
}
/// What became of one authenticated CONNECT, for the minute's count. One
/// whose tunnel leg was ready and then fenced is answered `502` as well and
/// is not counted: no leg failed.
enum Decided {
    Direct,
    Public,
    /// Answered `502`: no leg gave a backend.
    Failed,
}
async fn serve<F: Future<Output = Option<Backend>>>(
    mut browser: TcpStream,
    host: &Authority,
    keys: &Windows,
    decided: impl Fn(Decided),
    open: impl FnOnce() -> F,
) {
    let mut data = Vec::with_capacity(1024);
    let parsed = timeout(Duration::from_secs(5), async {
        loop {
            let mut buf = [0; 1024];
            let n = browser.read(&mut buf).await.ok()?;
            if n == 0 {
                return None;
            }
            data.extend_from_slice(&buf[..n]);
            match parse_connect(&data, host, |v| keys.authenticate(v?)) {
                Head::More => {}
                other => return Some(other),
            }
        }
    })
    .await;
    let Ok(Some(parsed)) = parsed else { return };
    let (end, grant) = match parsed {
        Head::Accept(n, grant) => (n, grant),
        other => {
            let answer:&[u8]=match other {
        Head::Refuse(407)=>b"HTTP/1.1 407 Proxy Authentication Required\r\nProxy-Authenticate: Basic realm=\"Avibe Direct\"\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",
        Head::Refuse(403)=>b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n",
        Head::Refuse(405)=>b"HTTP/1.1 405 Method Not Allowed\r\nConnection: close\r\n\r\n",
        _=>b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n"};
            let _ = browser.write_all(answer).await;
            return;
        }
    };
    grant
        .until_revoked(async {
            let Some(backend) = open().await else {
                decided(Decided::Failed);
                let _ = browser.write_all(BAD_GATEWAY).await;
                return;
            };
            let (mut stream, _direct) = match backend {
                Backend::Direct(s, guard) => {
                    decided(Decided::Direct);
                    (s, guard)
                }
                Backend::Public(s, ticket) => {
                    if let Some(copy) = ticket.commit(&grant.legs, &browser, &s) {
                        decided(Decided::Public);
                        copy.serve(browser, s, &data[end..]).await;
                    } else {
                        // Fenced by a cut-over after it was ready: in no
                        // count, since no leg failed.
                        let _ = browser.write_all(BAD_GATEWAY).await;
                    }
                    return;
                }
            };
            if browser
                .write_all(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                .await
                .is_err()
                || stream.write_all(&data[end..]).await.is_err()
            {
                return;
            }
            let _ = tokio::io::copy_bidirectional(&mut browser, &mut stream).await;
        })
        .await;
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{
        future::ready,
        io::{Error, ErrorKind},
    };
    fn basic(credential: &super::super::WindowRegistration) -> String {
        use base64::{engine::general_purpose::STANDARD, Engine};
        let (u, p) = credential.native_login();
        format!("Basic {}", STANDARD.encode(format!("{u}:{p}")))
    }
    #[tokio::test]
    async fn authenticated_connect_without_a_backend_returns_502_then_closes() {
        let host = Authority::managed("desk-app.avibe.bot").unwrap();
        let (_tx, rx) = watch::channel(None);
        let l = Listener::bind_via(host.clone(), rx, watch::channel(PathStatus::default()).0, |_| {
            ready(None)
        })
        .await
        .unwrap();
        let mut browser = TcpStream::connect((Ipv4Addr::LOCALHOST, l.port)).await.unwrap();
        let request = format!(
            "CONNECT {}:443 HTTP/1.1\r\nProxy-Authorization: {}\r\n\r\n",
            host.host(),
            basic(&l.access.window_credential("test-window").unwrap())
        );
        browser.write_all(request.as_bytes()).await.unwrap();
        let mut response = Vec::new();
        timeout(Duration::from_secs(2), browser.read_to_end(&mut response))
            .await
            .unwrap()
            .unwrap();
        assert_eq!(response, b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n");
        // Not a failed direct leg in the instance's log: there was no usable
        // link, so no direct open was started, and nothing carried it.
        let usage = &l.access.usage;
        for key in ["direct", "tunnel", "direct_failed", "strict"] {
            assert_eq!(usage.total(None, key), 0, "{key}");
        }
    }
    /// A listener with no room answers `502` and closes, where it used to
    /// close without a word; and it has room again once connections end.
    #[tokio::test]
    async fn a_connection_beyond_what_the_listener_serves_is_answered_502() {
        let host = Authority::managed("desk-app.avibe.bot").unwrap();
        let (_tx, rx) = watch::channel(None);
        let l = Listener::bind_via(host.clone(), rx, watch::channel(PathStatus::default()).0, |_| {
            ready(None)
        })
        .await
        .unwrap();
        // As many as it serves, each still to send its request.
        let mut held = Vec::new();
        for _ in 0..SERVED {
            held.push(TcpStream::connect((Ipv4Addr::LOCALHOST, l.port)).await.unwrap());
        }
        // One more, with a request it has already sent in full.
        let unauthenticated = format!("CONNECT {}:443 HTTP/1.1\r\n\r\n", host.host());
        let mut beyond = TcpStream::connect((Ipv4Addr::LOCALHOST, l.port)).await.unwrap();
        beyond.write_all(unauthenticated.as_bytes()).await.unwrap();
        let mut answer = Vec::new();
        timeout(Duration::from_secs(2), beyond.read_to_end(&mut answer))
            .await
            .unwrap()
            .unwrap();
        assert_eq!(answer, BAD_GATEWAY);
        // It was not read as a request: nothing is counted for it.
        for key in ["direct", "tunnel", "direct_failed", "strict"] {
            assert_eq!(l.access.usage.total(None, key), 0, "{key}");
        }
        // The held ones end: the listener serves again, and asks the next
        // one for its credential as it asks any.
        drop(held);
        timeout(Duration::from_secs(2), async {
            loop {
                let mut next = TcpStream::connect((Ipv4Addr::LOCALHOST, l.port)).await.unwrap();
                next.write_all(unauthenticated.as_bytes()).await.unwrap();
                let mut answer = Vec::new();
                next.read_to_end(&mut answer).await.unwrap();
                if answer.starts_with(b"HTTP/1.1 407 ") {
                    break;
                }
                assert_eq!(answer, BAD_GATEWAY);
                tokio::time::sleep(Duration::from_millis(20)).await;
            }
        })
        .await
        .unwrap();
    }
    #[tokio::test]
    async fn observed_denial_fails_authenticated_connect_despite_pending_lan_success() {
        use crate::direct::selection::Selection;
        let host = Authority::managed("desk-app.avibe.bot").unwrap();
        let socket = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).await.unwrap();
        let mut browser = TcpStream::connect(socket.local_addr().unwrap()).await.unwrap();
        let (accepted, _) = socket.accept().await.unwrap();
        let (release, waiting) = tokio::sync::oneshot::channel();
        let keys = Windows::default();
        let authorization = basic(&keys.credential("test-window").unwrap());
        let worker = tokio::spawn(async move {
            serve(
                accepted,
                &host,
                &keys,
                |_| {},
                || async {
                    Selection::default()
                        .choose(
                            async {
                                waiting.await.unwrap();
                                panic!("a late LAN success must not be polled after refusal")
                            },
                            async {
                                // A LAN success is available if selection polls it again.
                                release.send(()).unwrap();
                                Err(public::OpenError::Terminal)
                            },
                        )
                        .await
                },
            )
            .await;
        });
        browser
            .write_all(
                format!("CONNECT desk-app.avibe.bot:443 HTTP/1.1\r\nProxy-Authorization: {authorization}\r\n\r\n")
                    .as_bytes(),
            )
            .await
            .unwrap();
        let mut reply = Vec::new();
        timeout(Duration::from_secs(3), browser.read_to_end(&mut reply))
            .await
            .unwrap()
            .unwrap();
        worker.await.unwrap();
        assert_eq!(reply, b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n");
    }
    /// A tunnel leg that was ready and then fenced by a cut-over is answered
    /// `502` like a failure, and is none: it is in no count.
    #[tokio::test]
    async fn a_fenced_public_leg_is_answered_502_and_counted_as_nothing() {
        use std::sync::atomic::AtomicUsize;
        let host = Authority::managed("desk-app.avibe.bot").unwrap();
        let socket = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).await.unwrap();
        let mut browser = TcpStream::connect(socket.local_addr().unwrap()).await.unwrap();
        let (accepted, _) = socket.accept().await.unwrap();
        // The tunnel's leg, ready; then the cut-over, before it is committed.
        let public = TcpStream::connect(socket.local_addr().unwrap()).await.unwrap();
        let policy = SessionPolicy::default();
        let ticket = policy.sample().unwrap();
        assert_eq!(policy.close_public(), 0);
        let keys = Windows::default();
        let authorization = basic(&keys.credential("test-window").unwrap());
        let decisions = Arc::new(AtomicUsize::new(0));
        let counted = decisions.clone();
        let worker = tokio::spawn(async move {
            serve(
                accepted,
                &host,
                &keys,
                |_| {
                    counted.fetch_add(1, Ordering::SeqCst);
                },
                || async { Some(Backend::Public(public, ticket)) },
            )
            .await;
        });
        browser
            .write_all(
                format!("CONNECT desk-app.avibe.bot:443 HTTP/1.1\r\nProxy-Authorization: {authorization}\r\n\r\n")
                    .as_bytes(),
            )
            .await
            .unwrap();
        let mut reply = Vec::new();
        timeout(Duration::from_secs(3), browser.read_to_end(&mut reply))
            .await
            .unwrap()
            .unwrap();
        worker.await.unwrap();
        assert_eq!(reply, b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n");
        assert_eq!(decisions.load(Ordering::SeqCst), 0);
    }
    #[tokio::test(start_paused = true)]
    async fn temporary_accept_errors_back_off_and_terminal_errors_are_returned() {
        let start = tokio::time::Instant::now();
        let mut n = 0;
        let actual = accept_with_retry(|| {
            n += 1;
            ready(if n <= 2 {
                Err(Error::from(ErrorKind::ConnectionAborted))
            } else {
                Ok(7)
            })
        })
        .await
        .unwrap();
        assert_eq!(actual, 7);
        assert_eq!(n, 3);
        assert_eq!(start.elapsed(), Duration::from_millis(75));
        let end = accept_with_retry(|| ready(Err::<(), _>(Error::from(ErrorKind::InvalidInput))))
            .await
            .unwrap_err();
        assert_eq!(end.kind(), ErrorKind::InvalidInput);
    }
    #[tokio::test]
    async fn a_terminated_listener_withdraws_route_and_keeps_its_port_reserved_until_detach() {
        let c = crate::direct::Client::new("desk-app.avibe.bot", "one".into())
            .await
            .unwrap();
        let addr = c.route().unwrap().proxy;
        let mut life = c.listener_lifetime();
        c.listener.access.task.abort();
        assert!(life.changed().await.is_err());
        assert!(c.route().is_none());
        assert!(TcpListener::bind(addr).await.is_err());
        let reservation = Arc::downgrade(&c.listener._reservation);
        drop(c);
        // The worker's cancellation is asynchronous. Test our own socket's
        // lifetime, not whether the OS immediately reuses an ephemeral port.
        timeout(Duration::from_secs(2), async {
            while reservation.upgrade().is_some() {
                tokio::task::yield_now().await;
            }
        })
        .await
        .unwrap();
    }
}
