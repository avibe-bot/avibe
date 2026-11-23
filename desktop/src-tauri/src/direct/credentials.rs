//! Native-only authorization. Each registration owns a fresh census and a
//! revocation channel. Reusing a window label never reuses either one.
use super::quiet::PublicLegs;
use base64::{
    engine::general_purpose::{STANDARD, URL_SAFE_NO_PAD},
    Engine,
};
use std::{
    collections::HashMap,
    sync::{Arc, Mutex},
};
use subtle::ConstantTimeEq;
use tokio::sync::watch;

pub struct Credential {
    password: String,
    header: String,
}
impl Credential {
    fn generate() -> Option<Self> {
        let mut bytes = [0; 32];
        getrandom::fill(&mut bytes).ok()?;
        let password = URL_SAFE_NO_PAD.encode(bytes);
        let header = format!("Basic {}", STANDARD.encode(format!("avibe:{password}")));
        Some(Self { password, header })
    }
    pub fn native_login(&self) -> (&str, &str) {
        ("avibe", &self.password)
    }
    /// No early exit on matching prefixes. The encoded credential length is
    /// fixed and public, so rejecting other lengths is not secret-dependent.
    fn matches(&self, header: &str) -> bool {
        bool::from(self.header.as_bytes().ct_eq(header.as_bytes()))
    }
}
/// Native capability for one registration lifetime. Clones retain its identity,
/// not its authority: a revoked registration can never become current again.
#[derive(Clone)]
pub struct WindowRegistration {
    label: String,
    credential: Arc<Credential>,
}
impl WindowRegistration {
    pub fn native_login(&self) -> (&str, &str) {
        self.credential.native_login()
    }
    fn matches(&self, entry: &Entry) -> bool {
        Arc::ptr_eq(&self.credential, &entry.credential)
    }
}
struct Entry {
    credential: Arc<Credential>,
    valid: watch::Sender<bool>,
    legs: PublicLegs,
}
impl Entry {
    fn new() -> Option<Self> {
        Some(Self {
            credential: Arc::new(Credential::generate()?),
            valid: watch::channel(true).0,
            legs: PublicLegs::default(),
        })
    }
    fn grant(&self) -> Grant {
        Grant {
            valid: self.valid.subscribe(),
            legs: self.legs.clone(),
        }
    }
}
#[derive(Clone)]
pub(super) struct Grant {
    pub valid: watch::Receiver<bool>,
    pub legs: PublicLegs,
}
impl Grant {
    /// Hold the read side only for one synchronous poll. Revocation fences
    /// later polls, and wakes pending dials, setup writes and both copy paths.
    pub async fn until_revoked(&self, work: impl std::future::Future<Output = ()>) {
        let mut stop = self.valid.clone();
        tokio::pin!(work);
        let guarded = std::future::poll_fn(|cx| {
            let valid = self.valid.borrow();
            if *valid {
                work.as_mut().poll(cx)
            } else {
                std::task::Poll::Ready(())
            }
        });
        tokio::select! { biased; _ = stop.changed() => {}, _ = guarded => {} }
    }
}
pub(super) struct Windows {
    legacy: Entry,
    windows: Mutex<HashMap<String, Entry>>,
}
impl Windows {
    pub fn new() -> Option<Self> {
        Some(Self {
            legacy: Entry::new()?,
            windows: Mutex::default(),
        })
    }
    pub fn legacy(&self) -> &Credential {
        &self.legacy.credential
    }
    pub fn legacy_grant(&self) -> Grant {
        self.legacy.grant()
    }
    pub fn credential(&self, window: &str) -> Option<WindowRegistration> {
        if window.is_empty() || window.len() > 256 {
            return None;
        }
        let mut windows = self.windows.lock().unwrap();
        let entry = if let Some(entry) = windows.get(window) {
            entry
        } else {
            if windows.len() >= 256 {
                return None;
            }
            windows.entry(window.to_owned()).or_insert(Entry::new()?)
        };
        Some(WindowRegistration {
            label: window.to_owned(),
            credential: entry.credential.clone(),
        })
    }
    pub fn revoke(&self, registration: &WindowRegistration) {
        let mut windows = self.windows.lock().unwrap();
        if let std::collections::hash_map::Entry::Occupied(slot) = windows.entry(registration.label.clone()) {
            if registration.matches(slot.get()) {
                slot.remove().valid.send_replace(false);
            }
        }
    }
    pub fn window(&self, registration: &WindowRegistration) -> Option<Grant> {
        self.windows
            .lock()
            .unwrap()
            .get(&registration.label)
            .filter(|entry| registration.matches(entry))
            .map(Entry::grant)
    }
    pub fn authenticate(&self, header: &str) -> Option<Grant> {
        let windows = self.windows.lock().unwrap();
        let mut found = None;
        for entry in std::iter::once(&self.legacy).chain(windows.values()) {
            if entry.credential.matches(header) {
                found = Some(entry);
            }
        }
        found.map(Entry::grant)
    }
}

#[cfg(test)]
mod tests {
    use super::super::{handover_tests::public_browser, Client};
    use std::{
        sync::{mpsc, TryLockError},
        time::Duration,
    };
    use tokio::{io::AsyncReadExt, net::TcpStream, sync::oneshot, time::timeout};

    #[tokio::test]
    async fn healthy_owner_drop_stops_listener_before_an_inflight_revoke_finishes() {
        timeout(Duration::from_secs(4), async {
            let mut c = Client::new("one-app.avibe.bot", "one".into()).await.unwrap();
            let mut browser = public_browser(&mut c).await;
            let proxy = c.route().unwrap().proxy;
            let h = c.handover();
            let mut lifetime = c.listener_lifetime();
            let a = h.window_credential("a").unwrap();
            let grant = c.listener.access.windows.window(&a).unwrap();
            let (locked, ready) = oneshot::channel();
            let (release, wait) = mpsc::channel();
            let reader = tokio::task::spawn_blocking(move || {
                let _guard = grant.valid.borrow();
                locked.send(()).unwrap();
                wait.recv_timeout(Duration::from_secs(5)).unwrap();
            });
            ready.await.unwrap();
            let current = a.clone();
            let worker = h.clone();
            let revoke = tokio::task::spawn_blocking(move || worker.revoke_window(&a));
            // No other registry user: contention proves revoke is inside its
            // critical section, unable to finish until we release the grant read.
            while !matches!(
                c.listener.access.windows.windows.try_lock(),
                Err(TryLockError::WouldBlock)
            ) {
                tokio::task::yield_now().await;
            }
            drop(c); // Healthy accept loop; keep the overlapping revoke blocked.
            assert!(h.window_credential("late").is_none());
            h.revoke_window(&current);
            assert_eq!(h.window_census(&current), None);
            assert_eq!(
                h.close_quiet_public_for_window("one-app.avibe.bot", &current).await,
                super::super::HandoverResult::NotUsable
            );
            assert!(lifetime.changed().await.is_err());
            assert!(matches!(browser.read(&mut [0]).await, Ok(0) | Err(_)));
            assert!(TcpStream::connect(proxy).await.is_err());
            assert!(!revoke.is_finished());
            release.send(()).unwrap();
            reader.await.unwrap();
            revoke.await.unwrap();
        })
        .await
        .unwrap();
    }
}
