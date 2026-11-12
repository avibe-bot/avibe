//! Instance-owned admissions. All deadlines are local and monotonic; no credentials.
use std::{
    collections::HashMap,
    sync::{Arc, Mutex},
    time::Duration,
};

use iroh::endpoint::{AfterHandshakeOutcome, Connection, EndpointHooks, WeakConnectionHandle};
use tokio::{
    sync::watch,
    time::{self, Instant},
};

use crate::{StopSignal, close_code};

const CAPACITY: usize = 256;
const MAX_TTL: u64 = 300;

#[derive(Debug)]
struct Entry {
    deadline: Instant,
    order: u64,
    live: HashMap<usize, WeakConnectionHandle>,
}

impl Entry {
    fn close(self) {
        for weak in self.live.into_values() {
            if let Some(connection) = weak.upgrade() {
                connection.close(close_code(), b"admission ended");
            }
        }
    }
}

#[derive(Debug, Default)]
struct State {
    entries: HashMap<String, Entry>,
    next_order: u64,
}

impl State {
    fn expire(&mut self, now: Instant) {
        let expired: Vec<_> = self
            .entries
            .iter()
            .filter(|(_, entry)| entry.deadline <= now)
            .map(|(id, _)| id.clone())
            .collect();
        for id in expired {
            self.entries.remove(&id).expect("entry exists").close();
        }
    }

    fn update(&mut self, id: String, ttl: u64, now: Instant) {
        self.expire(now);
        if ttl == 0 {
            if let Some(entry) = self.entries.remove(&id) {
                entry.close();
            }
            return;
        }
        let deadline = now + Duration::from_secs(ttl.min(MAX_TTL));
        if let Some(entry) = self.entries.get_mut(&id) {
            // A refresh extends this admission, without reopening revoked or
            // expired connections and without changing insertion order.
            entry.deadline = deadline;
            return;
        }
        if self.entries.len() == CAPACITY {
            let oldest = self
                .entries
                .iter()
                .min_by_key(|(_, entry)| entry.order)
                .map(|(id, _)| id.clone())
                .expect("full set");
            self.entries.remove(&oldest).expect("entry exists").close();
        }
        let order = self.next_order;
        self.next_order = self
            .next_order
            .checked_add(1)
            .expect("admission sequence exhausted");
        self.entries.insert(
            id,
            Entry {
                deadline,
                order,
                live: HashMap::new(),
            },
        );
    }
}

#[derive(Clone, Debug)]
pub struct Admissions {
    state: Arc<Mutex<State>>,
    changed: watch::Sender<()>,
}

impl Admissions {
    pub fn new() -> Self {
        let (changed, _) = watch::channel(());
        Self {
            state: Arc::new(Mutex::new(State::default())),
            changed,
        }
    }

    pub fn update(&self, id: String, ttl: u64) {
        self.state
            .lock()
            .expect("admissions poisoned")
            .update(id, ttl, Instant::now());
        self.changed.send_replace(());
    }

    // This check also rejects a connection from an ended admission if the same
    // endpoint ID was immediately admitted again. A new admission needs a dial.
    pub fn allows_connection(&self, connection: &Connection) -> bool {
        let mut state = self.state.lock().expect("admissions poisoned");
        state.expire(Instant::now());
        state
            .entries
            .get(&connection.remote_id().to_string())
            .is_some_and(|entry| entry.live.contains_key(&connection.stable_id()))
    }

    pub async fn run_expiration(&self, stop: StopSignal) {
        let mut changed = self.changed.subscribe();
        loop {
            // Subscribe before taking the snapshot: changes during sleep setup
            // remain observable even if the changed branch is not yet polled.
            let deadline = {
                let mut state = self.state.lock().expect("admissions poisoned");
                state.expire(Instant::now());
                state.entries.values().map(|entry| entry.deadline).min()
            };
            tokio::select! {
                _ = stop.wait() => return,
                _ = changed.changed() => {},
                _ = async {
                    match deadline {
                        Some(deadline) => time::sleep_until(deadline).await,
                        None => std::future::pending::<()>().await,
                    }
                } => {},
            }
        }
    }
}

impl EndpointHooks for Admissions {
    async fn after_handshake(&self, connection: &Connection) -> AfterHandshakeOutcome {
        let mut state = self.state.lock().expect("admissions poisoned");
        state.expire(Instant::now());
        let Some(entry) = state.entries.get_mut(&connection.remote_id().to_string()) else {
            return AfterHandshakeOutcome::Reject {
                error_code: close_code(),
                reason: b"admission refused".to_vec(),
            };
        };
        // Weak handles cannot keep a transport alive; prune completed ones.
        entry.live.retain(|_, weak| weak.upgrade().is_some());
        entry
            .live
            .insert(connection.stable_id(), connection.weak_handle());
        AfterHandshakeOutcome::Accept
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn authoritative_empty_ttl_clamp_refresh_revoke_and_expiry() {
        let mut state = State::default();
        let now = Instant::now();
        assert!(state.entries.is_empty());
        state.update("peer".into(), u64::MAX, now);
        assert_eq!(
            state.entries["peer"].deadline,
            now + Duration::from_secs(300)
        );
        state.update("peer".into(), 2, now + Duration::from_secs(1));
        state.expire(now + Duration::from_secs(2));
        assert!(state.entries.contains_key("peer"));
        state.expire(now + Duration::from_secs(3));
        assert!(state.entries.is_empty());
        state.update("peer".into(), 5, now);
        state.update("peer".into(), 0, now);
        assert!(state.entries.is_empty());
    }

    #[test]
    fn capacity_evicts_expired_first_then_oldest_not_refreshed_first() {
        let mut state = State::default();
        let now = Instant::now();
        for index in 0..CAPACITY {
            state.update(index.to_string(), if index == 1 { 1 } else { 300 }, now);
        }
        state.update("new".into(), 300, now + Duration::from_secs(2));
        assert!(state.entries.contains_key("0"));
        assert!(!state.entries.contains_key("1"));
        state.update("0".into(), 300, now + Duration::from_secs(3));
        state.update("newer".into(), 300, now + Duration::from_secs(3));
        assert!(!state.entries.contains_key("0"));
        assert_eq!(state.entries.len(), CAPACITY);
    }
}
