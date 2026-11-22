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
    pub fn credential(&self, window: &str) -> Option<Arc<Credential>> {
        if window.is_empty() || window.len() > 256 {
            return None;
        }
        let mut windows = self.windows.lock().unwrap();
        if let Some(entry) = windows.get(window) {
            return Some(entry.credential.clone());
        }
        if windows.len() >= 256 {
            return None;
        }
        let entry = Entry::new()?;
        let credential = entry.credential.clone();
        windows.insert(window.to_owned(), entry);
        Some(credential)
    }
    pub fn revoke(&self, window: &str) {
        let mut windows = self.windows.lock().unwrap();
        if let Some(entry) = windows.remove(window) {
            entry.valid.send_replace(false);
        }
    }
    pub fn window(&self, window: &str) -> Option<Grant> {
        self.windows.lock().unwrap().get(window).map(Entry::grant)
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
