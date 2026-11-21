//! Authenticated, one-authority loopback CONNECT listener. The browser's TLS
//! bytes stay buffered until a candidate is committed; there is no retry on
//! any error after the 200. Drop/abort owns and cancels all accepted sockets.
use super::{
    connect::{parse_connect, Authority, Head},
    public,
    transport::{Link, Stream},
};
use base64::{engine::general_purpose::STANDARD, Engine};
use std::{future::Future, net::Ipv4Addr, sync::Arc, time::Duration};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::{TcpListener, TcpStream},
    sync::watch,
    task::{JoinHandle, JoinSet},
    time::timeout,
};

pub(super) struct Credential {
    password: String,
    header: String,
}
impl Credential {
    pub fn generate() -> Option<Self> {
        let mut bytes = [0u8; 32];
        getrandom::fill(&mut bytes).ok()?;
        let password = base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(bytes);
        let header = format!("Basic {}", STANDARD.encode(format!("avibe:{password}")));
        Some(Self { password, header })
    }
    // Native proxy configuration only. No Debug, telemetry or serde impl.
    pub fn native_login(&self) -> (&str, &str) {
        ("avibe", &self.password)
    }
}
pub(super) struct Listener {
    pub port: u16,
    pub credential: Arc<Credential>,
    // Retain the bound port until the native owner detaches, even if the
    // accept worker dies. Another process must not inherit a published port.
    _reservation: Arc<TcpListener>,
    health: watch::Receiver<()>,
    task: JoinHandle<()>,
}
impl Drop for Listener {
    fn drop(&mut self) {
        self.task.abort()
    }
}
impl Listener {
    pub(super) async fn bind(host: Authority, links: watch::Receiver<Option<Arc<Link>>>) -> Option<Self> {
        Self::bind_with_port(host, links, |h, port| async move {
            public::open(&h, port).await.map(|s| Box::new(s) as Stream)
        })
        .await
    }
    pub(super) fn is_alive(&self) -> bool {
        self.health.has_changed().is_ok() && !self.task.is_finished()
    }
    pub(super) fn health(&self) -> watch::Receiver<()> {
        self.health.clone()
    }
    #[cfg(test)]
    pub(super) async fn bind_via<F, Fut>(
        host: Authority,
        links: watch::Receiver<Option<Arc<Link>>>,
        open_public: F,
    ) -> Option<Self>
    where
        F: Fn(Authority) -> Fut + Send + Sync + Clone + 'static,
        Fut: Future<Output = Option<Stream>> + Send + 'static,
    {
        Self::bind_with_port(host, links, move |h, _| open_public(h)).await
    }
    async fn bind_with_port<F, Fut>(
        host: Authority,
        links: watch::Receiver<Option<Arc<Link>>>,
        open_public: F,
    ) -> Option<Self>
    where
        F: Fn(Authority, u16) -> Fut + Send + Sync + Clone + 'static,
        Fut: Future<Output = Option<Stream>> + Send + 'static,
    {
        let credential = Arc::new(Credential::generate()?);
        let socket = Arc::new(TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).await.ok()?);
        let reservation = socket.clone();
        let (live, health) = watch::channel(());
        let port = socket.local_addr().ok()?.port();
        let key = credential.clone();
        let task = tokio::spawn(async move {
            let _live = live;
            let mut clients = JoinSet::new();
            while let Ok((socket, peer)) = accept_with_retry(|| socket.accept()).await {
                while clients.try_join_next().is_some() {}
                if !peer.ip().is_loopback() || clients.len() >= 64 {
                    continue;
                }
                let h = host.clone();
                let key = key.clone();
                let links = links.clone();
                let dial = open_public.clone();
                clients.spawn(async move {
                    serve(socket, &h, &key.header, || async {
                        let link = links.borrow().clone();
                        let fallback = dial(h.clone(), port);
                        match link {
                            Some(l) if l.usable() => l.plan.choose(l.open(&h), fallback).await,
                            _ => timeout(Duration::from_secs(8), fallback).await.ok().flatten(),
                        }
                    })
                    .await;
                });
            }
            // No new accepts. Notify route owners before draining live streams.
            drop(_live);
            while clients.join_next().await.is_some() {}
        });
        Some(Self {
            port,
            credential,
            task,
            health,
            _reservation: reservation,
        })
    }
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
async fn serve<F: Future<Output = Option<Stream>>>(
    mut browser: TcpStream,
    host: &Authority,
    key: &str,
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
            match parse_connect(&data, host, Some(key)) {
                Head::More => {}
                other => return Some(other),
            }
        }
    })
    .await;
    let Ok(Some(parsed)) = parsed else { return };
    let end = match parsed {
        Head::Accept(n) => n,
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
    let Some(mut stream) = open().await else {
        let _ = browser
            .write_all(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
            .await;
        return;
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
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{
        future::ready,
        io::{Error, ErrorKind},
    };
    #[tokio::test]
    async fn authenticated_connect_without_a_backend_returns_502_then_closes() {
        let host = Authority::managed("desk-app.avibe.bot").unwrap();
        let (_tx, rx) = watch::channel(None);
        let l = Listener::bind_via(host.clone(), rx, |_| ready(None)).await.unwrap();
        let mut browser = TcpStream::connect((Ipv4Addr::LOCALHOST, l.port)).await.unwrap();
        let request = format!(
            "CONNECT {}:443 HTTP/1.1\r\nProxy-Authorization: {}\r\n\r\n",
            host.host(),
            l.credential.header
        );
        browser.write_all(request.as_bytes()).await.unwrap();
        let mut response = Vec::new();
        timeout(Duration::from_secs(2), browser.read_to_end(&mut response))
            .await
            .unwrap()
            .unwrap();
        assert_eq!(response, b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n");
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
        c.listener.task.abort();
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
