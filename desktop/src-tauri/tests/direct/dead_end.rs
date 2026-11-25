//! The address a route to nothing names (`dead_end.rs`): held by this app,
//! and answering `502` to whatever connects. What a WebView does with that
//! answer and no failover is measured by `examples/direct_webview_probe.rs`
//! (the listener that answers 502).
use super::dead_end::DeadEnd;
use std::{
    io::{Read, Write},
    net::{SocketAddr, TcpListener, TcpStream},
    time::{Duration, Instant},
};

fn asked(address: SocketAddr, request: &[u8]) -> String {
    let mut connection = TcpStream::connect_timeout(&address, Duration::from_secs(5)).expect("the dead end listens");
    connection
        .set_read_timeout(Some(Duration::from_secs(5)))
        .expect("a timeout");
    connection.write_all(request).expect("a request");
    let mut answer = String::new();
    // Read to the end: the connection is closed after its answer.
    connection.read_to_string(&mut answer).expect("an answer, then the end");
    answer
}

#[test]
fn a_dead_end_is_an_address_this_app_holds_that_carries_nothing() {
    // Made as the app makes it: outside any async runtime.
    let dead_end = DeadEnd::hold();
    let address = dead_end.address();
    assert!(address.ip().is_loopback());
    // Held: a real port, not the one nothing holds.
    assert!(address.port() > 1, "no listener could be made on this machine");
    let address = SocketAddr::V4(address);

    // Whatever is asked, the answer is 502 and the connection ends: a
    // tunnel through it, every time, and a plain request alike.
    for _ in 0..3 {
        let answer = asked(
            address,
            b"CONNECT one-app.avibe.bot:443 HTTP/1.1\r\nHost: one-app.avibe.bot:443\r\n\r\n",
        );
        assert!(answer.starts_with("HTTP/1.1 502 "), "{answer:?}");
        assert!(answer.contains("Connection: close\r\n"), "{answer:?}");
    }
    assert!(asked(address, b"GET / HTTP/1.1\r\nHost: one-app.avibe.bot\r\n\r\n").starts_with("HTTP/1.1 502 "));
    // One that says nothing is answered too, and soon: it holds up no one.
    let began = Instant::now();
    assert!(asked(address, b"").starts_with("HTTP/1.1 502 "));
    assert!(began.elapsed() < Duration::from_secs(3), "{:?}", began.elapsed());

    // No other socket can take the port while it is held.
    assert!(TcpListener::bind(address).is_err());
    // Two dead ends are two addresses.
    assert_ne!(SocketAddr::V4(DeadEnd::hold().address()), address);
}
