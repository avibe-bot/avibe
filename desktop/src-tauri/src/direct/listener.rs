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
    task: JoinHandle<()>,
}
impl Drop for Listener {
    fn drop(&mut self) {
        self.task.abort()
    }
}
impl Listener {
    pub(super) async fn bind(host: Authority, links: watch::Receiver<Option<Arc<Link>>>) -> Option<Self> {
        let credential = Arc::new(Credential::generate()?);
        let socket = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).await.ok()?;
        let port = socket.local_addr().ok()?.port();
        let key = credential.clone();
        let task = tokio::spawn(async move {
            let mut clients = JoinSet::new();
            loop {
                tokio::select! {
                    _=clients.join_next(), if !clients.is_empty()=>{},
                    incoming=socket.accept()=>{let Ok((socket,peer))=incoming else{break}; if !peer.ip().is_loopback() || clients.len()>=64 {continue}
                        let h=host.clone();let key=key.clone();let links=links.clone();
                        clients.spawn(async move {serve(socket,&h,&key.header,|| async {
                            let link=links.borrow().clone();
                            let fallback=async {public::open(&h).await.map(|s|Box::new(s) as Stream)};
                            match link {
                                Some(l) if l.usable()=>l.plan.choose(l.open(&h),fallback).await,
                                _=>timeout(Duration::from_secs(8),fallback).await.ok().flatten(),
                            }
                        }).await;});
                    }
                }
            }
        });
        Some(Self { port, credential, task })
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
