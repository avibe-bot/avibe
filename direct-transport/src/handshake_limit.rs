//! Admission to expensive handshakes, before crypto or an application connection.
//! Source IPs are in-memory accounting keys only; never emit them in diagnostics.
use std::{
    collections::HashMap,
    net::IpAddr,
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};

const SOURCE_SLOTS: usize = 256;
const MAX_HANDSHAKES: usize = 16;
const MAX_HANDSHAKES_PER_SOURCE: usize = 4;
const SOURCE_BURST: f64 = 8.0;
const GLOBAL_BURST: f64 = 32.0;
const STALE_AFTER: Duration = Duration::from_secs(60);

struct Bucket {
    tokens: f64,
    updated: Instant,
}

impl Bucket {
    fn new(capacity: f64, now: Instant) -> Self {
        Self {
            tokens: capacity,
            updated: now,
        }
    }

    fn refill(&mut self, now: Instant, capacity: f64, per_second: f64) {
        self.tokens = (self.tokens + now.duration_since(self.updated).as_secs_f64() * per_second)
            .min(capacity);
        self.updated = now;
    }
}

struct Source {
    bucket: Bucket,
    active: usize,
    last_attempt: Instant,
}

struct State {
    global: Bucket,
    active: usize,
    sources: HashMap<IpAddr, Source>,
}

#[derive(Clone)]
pub struct HandshakeLimit(Arc<Mutex<State>>);

pub struct HandshakePermit {
    limit: HandshakeLimit,
    source: IpAddr,
}

impl HandshakeLimit {
    pub fn new() -> Self {
        Self(Arc::new(Mutex::new(State {
            global: Bucket::new(GLOBAL_BURST, Instant::now()),
            active: 0,
            sources: HashMap::new(),
        })))
    }

    pub fn acquire(&self, source: IpAddr) -> Option<HandshakePermit> {
        // One LAN prefix must not get a fresh budget for every temporary IPv6
        // address. IPv4 remains /32; IPv6 accounting is /64.
        let source = match source {
            IpAddr::V6(address) => {
                let mut bytes = address.octets();
                bytes[8..].fill(0);
                IpAddr::V6(bytes.into())
            }
            address => address,
        };
        let now = Instant::now();
        let mut state = self.0.lock().expect("handshake accounting poisoned");
        state.global.refill(now, GLOBAL_BURST, 8.0);
        if state.active >= MAX_HANDSHAKES || state.global.tokens < 1.0 {
            return None;
        }
        if !state.sources.contains_key(&source) {
            // Do not evict recent counters: changing source addresses must not
            // reset somebody else's budget. Saturation fails closed for new IPs.
            state.sources.retain(|_, entry| {
                entry.active != 0 || now.duration_since(entry.last_attempt) < STALE_AFTER
            });
            if state.sources.len() >= SOURCE_SLOTS {
                return None;
            }
        }
        let entry = state.sources.entry(source).or_insert_with(|| Source {
            bucket: Bucket::new(SOURCE_BURST, now),
            active: 0,
            last_attempt: now,
        });
        entry.last_attempt = now;
        entry.bucket.refill(now, SOURCE_BURST, 1.0);
        if entry.active >= MAX_HANDSHAKES_PER_SOURCE || entry.bucket.tokens < 1.0 {
            return None;
        }
        entry.bucket.tokens -= 1.0;
        entry.active += 1;
        state.global.tokens -= 1.0;
        state.active += 1;
        Some(HandshakePermit {
            limit: self.clone(),
            source,
        })
    }
}

impl Drop for HandshakePermit {
    fn drop(&mut self) {
        let mut state = self.limit.0.lock().expect("handshake accounting poisoned");
        state.active -= 1;
        if let Some(source) = state.sources.get_mut(&self.source) {
            source.active -= 1;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn handshake_concurrency_is_bounded_and_recovers_when_a_task_ends() {
        let limit = HandshakeLimit::new();
        let source = "127.0.0.1".parse().unwrap();
        let mut permits = Vec::new();
        for _ in 0..4 {
            permits.push(limit.acquire(source).expect("below source cap"));
        }
        assert!(limit.acquire(source).is_none());
        permits.pop();
        permits.push(limit.acquire(source).expect("released source slot"));
        for address in ["127.0.0.2", "127.0.0.3", "127.0.0.4"] {
            for _ in 0..4 {
                permits.push(
                    limit
                        .acquire(address.parse().unwrap())
                        .expect("below global cap"),
                );
            }
        }
        assert!(limit.acquire("127.0.0.5".parse().unwrap()).is_none());
        drop(permits);
        assert!(limit.acquire("127.0.0.5".parse().unwrap()).is_some());
    }

    #[test]
    fn temporary_ipv6_addresses_share_the_prefix_budget() {
        let limit = HandshakeLimit::new();
        for index in 1..=8 {
            drop(
                limit
                    .acquire(format!("2001:db8:1:2::{index}").parse().unwrap())
                    .expect("source burst"),
            );
        }
        assert!(
            limit
                .acquire("2001:db8:1:2::ffff".parse().unwrap())
                .is_none()
        );
        assert!(limit.acquire("2001:db8:1:3::1".parse().unwrap()).is_some());
    }

    #[test]
    fn closing_connections_does_not_reset_the_source_rate_budget() {
        let limit = HandshakeLimit::new();
        let source = "127.0.0.1".parse().unwrap();
        for _ in 0..8 {
            drop(limit.acquire(source).expect("within source burst"));
        }
        assert!(limit.acquire(source).is_none());
        assert!(limit.acquire("127.0.0.2".parse().unwrap()).is_some());
    }
}
