//! An address on this computer that carries nothing, for a route that must
//! lead nowhere: a window of an instance whose choice is "the direct path
//! and the relay only" that has no session to join. With no failover on its
//! route, such a window's requests fail; without a route they would take
//! the tunnel.
//!
//! It is a listener of this app's own that answers every connection with
//! `502` and closes it, which is what a session's listener answers when it
//! has no path: the probe (`examples/direct_webview_probe.rs`) holds both
//! WebViews to failing on that answer when the route allows no failover.
//! Its port stays taken for as long as the app runs, so no listener that
//! starts later is given it by chance and answers in its place. (A socket
//! that is bound and does not listen would do with less, but a connection
//! to one is not refused on macOS: it waits out its timeout, and so would
//! the window.)
use std::{
    io::{Read, Write},
    net::{Ipv4Addr, Shutdown, SocketAddr, SocketAddrV4, TcpListener},
    time::Duration,
};

pub struct DeadEnd {
    address: SocketAddrV4,
}

/// How long a connection may take to say what it wants before it is
/// answered anyway. One connection is answered at a time.
const PATIENCE: Duration = Duration::from_millis(250);
const ANSWER: &[u8] = b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\nContent-Length: 0\r\n\r\n";

impl DeadEnd {
    /// Where no listener can be made at all, the address is a port nothing
    /// is expected to serve and nothing holds: the route still names no
    /// listener of a session, and still allows no failover.
    const UNHELD: SocketAddrV4 = SocketAddrV4::new(Ipv4Addr::LOCALHOST, 1);

    /// Binds the listener and starts the thread that answers it, for as
    /// long as the app runs. Called when the first route to nothing is
    /// needed, not before.
    pub fn hold() -> Self {
        let held = TcpListener::bind(SocketAddr::from((Ipv4Addr::LOCALHOST, 0)))
            .ok()
            .and_then(|listener| match listener.local_addr().ok()? {
                SocketAddr::V4(address) if address.port() != 0 => Some((listener, address)),
                _ => None,
            })
            .and_then(|(listener, address)| {
                std::thread::Builder::new()
                    .name("direct-dead-end".into())
                    .spawn(move || answer(&listener))
                    .ok()
                    .map(|_| address)
            });
        Self {
            address: held.unwrap_or(Self::UNHELD),
        }
    }

    pub fn address(&self) -> SocketAddrV4 {
        self.address
    }
}

fn answer(listener: &TcpListener) {
    for connection in listener.incoming() {
        let Ok(mut connection) = connection else { continue };
        let _ = connection.set_read_timeout(Some(PATIENCE));
        let _ = connection.set_write_timeout(Some(PATIENCE));
        // What it asks for is read, before the answer and after it, so
        // that closing does not reset the answer away. It is not looked at.
        let mut asked = [0u8; 2048];
        let _ = connection.read(&mut asked);
        let _ = connection.write_all(ANSWER);
        let _ = connection.shutdown(Shutdown::Write);
        let _ = connection.read(&mut asked);
    }
}
