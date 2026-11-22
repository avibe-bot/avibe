//! The proxy never inspects TLS. The page holds new requests and waits for
//! existing requests; here quiet means a second without bytes in either
//! direction and no bytes still buffered by our copy. One registry per listener.
use std::{
    io,
    pin::Pin,
    sync::{Arc, Mutex, Weak},
    task::{Context, Poll},
    time::Duration,
};
use tokio::{
    io::{AsyncRead, AsyncWrite, ReadBuf},
    sync::oneshot,
    time::Instant,
};

#[derive(Clone, Default)]
pub(super) struct PublicLegs(Arc<Mutex<Vec<Weak<Leg>>>>);
struct Leg(Mutex<Activity>);
struct Activity {
    setup: bool,
    inputs: Option<[std::net::TcpStream; 2]>,
    last: Instant,
    buffered: usize,
    stop: Option<oneshot::Sender<()>>,
}
impl Activity {
    fn quiet(&self, now: Instant) -> bool {
        !self.setup
            && self.buffered == 0
            && now.saturating_duration_since(self.last) >= Duration::from_secs(1)
            && self.inputs.as_ref().is_some_and(|inputs| {
                inputs
                    .iter()
                    .all(|s| matches!(s.peek(&mut [0]), Err(e) if e.kind() == io::ErrorKind::WouldBlock))
            })
    }
}
// Peek in the kernel, not in Tokio's (possibly not yet updated) readiness cache.
// Duplicates refer to the same receive queues. If duplication or a peek fails,
// don't impair the public connection: just exclude it from discretionary closure.
fn probe(s: &tokio::net::TcpStream) -> io::Result<std::net::TcpStream> {
    #[cfg(unix)]
    let copy = {
        use std::os::fd::AsFd;
        s.as_fd().try_clone_to_owned()?
    };
    #[cfg(windows)]
    let copy = {
        use std::os::windows::io::AsSocket;
        s.as_socket().try_clone_to_owned()?
    };
    let stream = std::net::TcpStream::from(copy);
    stream.set_nonblocking(true)?;
    Ok(stream)
}
pub(super) struct PublicCopy {
    leg: Arc<Leg>,
    stopped: oneshot::Receiver<()>,
}
#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct PublicCensus {
    pub quiet: usize,
    pub busy: usize,
}
impl PublicLegs {
    pub(super) fn census(&self) -> PublicCensus {
        let mut c = PublicCensus::default();
        let now = Instant::now();
        for leg in self.0.lock().unwrap().iter().filter_map(Weak::upgrade) {
            let s = leg.0.lock().unwrap();
            if s.stop.is_some() {
                if s.quiet(now) {
                    c.quiet += 1;
                } else {
                    c.busy += 1;
                }
            }
        }
        c
    }
    pub(super) fn commit(&self, browser: &tokio::net::TcpStream, peer: &tokio::net::TcpStream) -> PublicCopy {
        let (stop, stopped) = oneshot::channel();
        let leg = Arc::new(Leg(Mutex::new(Activity {
            setup: true,
            inputs: probe(browser).ok().zip(probe(peer).ok()).map(|(a, b)| [a, b]),
            last: Instant::now(),
            buffered: 0,
            stop: Some(stop),
        })));
        {
            let mut entries = self.0.lock().unwrap();
            entries.retain(|e| e.strong_count() != 0);
            entries.push(Arc::downgrade(&leg));
        }
        PublicCopy { leg, stopped }
    }
    /// None means there are still busy connections and we have time to wait.
    pub(super) fn claim(&self, deadline: Instant) -> Option<(usize, usize)> {
        let entries = self.0.lock().unwrap();
        let live: Vec<_> = entries.iter().filter_map(Weak::upgrade).collect();
        let mut states: Vec<_> = live.iter().map(|l| l.0.lock().unwrap()).collect();
        let now = Instant::now();
        if now < deadline && states.iter().any(|s| s.stop.is_some() && !s.quiet(now)) {
            return None;
        }
        let (mut closed, mut kept) = (0, 0);
        for state in &mut states {
            if state.stop.is_none() {
                continue;
            }
            if !state.quiet(now) {
                kept += 1;
                continue;
            }
            if let Some(stop) = state.stop.take() {
                closed += usize::from(stop.send(()).is_ok());
            }
        }
        Some((closed, kept))
    }
}
impl PublicCopy {
    pub(super) async fn copy<A: AsyncRead + AsyncWrite + Unpin, B: AsyncRead + AsyncWrite + Unpin>(
        mut self,
        a: A,
        b: B,
    ) {
        {
            let mut activity = self.leg.0.lock().unwrap();
            activity.setup = false;
            activity.last = Instant::now();
        }
        let (mut a, mut b) = (Metered(a, &self.leg), Metered(b, &self.leg));
        tokio::select! {biased; _=&mut self.stopped=>{}, _=tokio::io::copy_bidirectional(&mut a,&mut b)=>{} }
    }
}
// Mark positive reads/writes in the same critical section as the close claim.
// Never await with a guard held. Closing one leg never stops another copy.
struct Metered<'a, T>(T, &'a Leg);
impl<T: AsyncRead + Unpin> AsyncRead for Metered<'_, T> {
    fn poll_read(self: Pin<&mut Self>, cx: &mut Context<'_>, buf: &mut ReadBuf<'_>) -> Poll<io::Result<()>> {
        let this = self.get_mut();
        let mut s = this.1 .0.lock().unwrap();
        if s.stop.is_none() {
            return Poll::Ready(Err(io::ErrorKind::ConnectionAborted.into()));
        }
        let before = buf.filled().len();
        let result = Pin::new(&mut this.0).poll_read(cx, buf);
        let n = buf.filled().len() - before;
        if n > 0 {
            s.buffered += n;
            s.last = Instant::now();
        }
        result
    }
}
impl<T: AsyncWrite + Unpin> AsyncWrite for Metered<'_, T> {
    fn poll_write(self: Pin<&mut Self>, cx: &mut Context<'_>, buf: &[u8]) -> Poll<io::Result<usize>> {
        let this = self.get_mut();
        let mut s = this.1 .0.lock().unwrap();
        if s.stop.is_none() {
            return Poll::Ready(Err(io::ErrorKind::ConnectionAborted.into()));
        }
        let result = Pin::new(&mut this.0).poll_write(cx, buf);
        if let Poll::Ready(Ok(n)) = result {
            if n > 0 {
                s.buffered -= n;
                s.last = Instant::now();
            }
        }
        result
    }
    fn poll_flush(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        Pin::new(&mut self.0).poll_flush(cx)
    }
    fn poll_shutdown(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        Pin::new(&mut self.0).poll_shutdown(cx)
    }
}
