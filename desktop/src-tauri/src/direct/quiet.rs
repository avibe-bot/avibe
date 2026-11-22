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
    last: Instant,
    buffered: usize,
    stop: Option<oneshot::Sender<()>>,
}
impl Activity {
    fn quiet(&self, now: Instant) -> bool {
        self.buffered == 0 && now.saturating_duration_since(self.last) >= Duration::from_secs(1)
    }
}
impl PublicLegs {
    pub(super) async fn copy<A, B>(&self, a: A, b: B)
    where
        A: AsyncRead + AsyncWrite + Unpin,
        B: AsyncRead + AsyncWrite + Unpin,
    {
        let (stop, mut stopped) = oneshot::channel();
        let leg = Arc::new(Leg(Mutex::new(Activity {
            last: Instant::now(),
            buffered: 0,
            stop: Some(stop),
        })));
        {
            let mut entries = self.0.lock().unwrap();
            entries.retain(|e| e.strong_count() != 0);
            entries.push(Arc::downgrade(&leg));
        }
        let (mut a, mut b) = (Metered(a, &leg), Metered(b, &leg));
        tokio::select! {biased; _=&mut stopped=>{}, _=tokio::io::copy_bidirectional(&mut a,&mut b)=>{} }
    }
    /// None means there are still busy connections and we have time to wait.
    pub(super) fn claim(&self, deadline: Instant) -> Option<(usize, usize)> {
        let entries = self.0.lock().unwrap();
        let live: Vec<_> = entries.iter().filter_map(Weak::upgrade).collect();
        let mut states: Vec<_> = live.iter().map(|l| l.0.lock().unwrap()).collect();
        let now = Instant::now();
        let kept = states.iter().filter(|s| s.stop.is_some() && !s.quiet(now)).count();
        if kept > 0 && now < deadline {
            return None;
        }
        let mut closed = 0;
        for state in states.iter_mut().filter(|s| s.quiet(now)) {
            if let Some(stop) = state.stop.take() {
                closed += usize::from(stop.send(()).is_ok());
            }
        }
        Some((closed, kept))
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
