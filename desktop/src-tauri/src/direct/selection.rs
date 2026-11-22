//! Candidates return an already-ready backend (direct CONNECT 200 or public
//! TCP). No candidate receives the WebView's TLS bytes during this race.
use super::public::OpenError;
use std::future::Future;
use std::sync::Mutex;
use std::time::Duration;
use tokio::time::{sleep, timeout};

#[derive(Default)]
pub(super) struct Selection(Mutex<State>);
#[derive(Default)]
struct State {
    sticky: bool,
    misses: u64,
}

impl Selection {
    // One Selection per admitted link: replacing it fences old races. Inside
    // one link an older winner must not restore stickiness after a later miss.
    pub(super) async fn choose<T>(
        &self,
        direct: impl Future<Output = Option<T>>,
        public: impl Future<Output = Result<T, OpenError>>,
    ) -> Option<T> {
        let (sticky, serial) = {
            let s = self.0.lock().unwrap();
            (s.sticky, s.misses)
        };
        let mark = |success| {
            let mut s = self.0.lock().unwrap();
            if success {
                if s.misses == serial {
                    s.sticky = true;
                }
            } else {
                s.sticky = false;
                s.misses = s.misses.wrapping_add(1);
            }
        };
        // Deadlines include opening the stream and reading its CONNECT answer.
        let direct = timeout(Duration::from_millis(if sticky { 1500 } else { 5000 }), direct);
        let public = async {
            timeout(Duration::from_secs(8), public)
                .await
                .unwrap_or(Err(OpenError::Unreachable))
        };
        tokio::pin!(direct, public);
        if sticky {
            let result = direct.await.ok().flatten();
            mark(result.is_some());
            return if result.is_some() { result } else { public.await.ok() };
        }
        tokio::select! {
            biased;
            d = &mut direct => {
                let result = d.ok().flatten(); mark(result.is_some());
                return if result.is_some() {result} else {public.await.ok()};
            }
            _ = sleep(Duration::from_millis(500)) => {}
        }
        tokio::select! {
            d = &mut direct => {let result=d.ok().flatten(); mark(result.is_some()); if result.is_some() {result} else {public.await.ok()} }
            p = &mut public => match p {
                Ok(stream) => Some(stream),
                Err(OpenError::Terminal) => None,
                Err(OpenError::Unreachable) => { let result=direct.await.ok().flatten(); mark(result.is_some()); result }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::future::{pending, ready};
    use tokio::time::Instant;
    #[tokio::test(start_paused = true)]
    async fn sticky_miss_drops_to_a_half_second_hedge() {
        let s = Selection::default();
        assert_eq!(
            s.choose(ready(Some(1)), async { panic!("no public dial") }).await,
            Some(1)
        );
        for ms in [1500, 500] {
            let start = Instant::now();
            assert_eq!(s.choose(pending(), ready(Ok(2))).await, Some(2));
            assert_eq!(start.elapsed(), Duration::from_millis(ms));
        }
    }
    #[tokio::test(start_paused = true)]
    async fn public_failure_does_not_discard_pending_direct() {
        let s = Selection::default();
        assert_eq!(
            s.choose(
                async {
                    sleep(Duration::from_secs(1)).await;
                    Some(1)
                },
                ready(Err(OpenError::Unreachable))
            )
            .await,
            Some(1)
        );
        let start = Instant::now();
        assert_eq!(s.choose(ready(None), ready(Ok(2))).await, Some(2));
        assert_eq!(start.elapsed(), Duration::ZERO);
    }
}
