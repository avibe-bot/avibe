use iroh::{
    Endpoint, EndpointAddr, RelayMode,
    endpoint::{Connection, PortmapperConfig, presets},
};
use serde_json::{Value, json};
use std::{
    net::{IpAddr, Ipv4Addr, Ipv6Addr, SocketAddr},
    process::Stdio,
    time::Duration,
};
use tokio::{
    io::{AsyncBufReadExt, AsyncReadExt, AsyncWriteExt, BufReader},
    net::{TcpListener, TcpStream},
    process::{Child, ChildStdin, ChildStdout, Command},
    time::{sleep, timeout},
};

const ALPN: &[u8] = b"avibe-direct/1";
const TOKEN: &str = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";
const WAIT: Duration = Duration::from_secs(8);

fn child_command() -> Command {
    // Test-only seams: an independent, prebuilt stock client can exercise an
    // experimental server or place only the sidecar in a capture namespace.
    let binary = std::env::var_os("AVDT_TEST_BINARY")
        .unwrap_or_else(|| env!("CARGO_BIN_EXE_avibe-direct-transport").into());
    let mut command = if let Some(wrapper) = std::env::var_os("AVDT_SIDECAR_WRAPPER") {
        let mut command = Command::new(wrapper);
        command.arg(binary);
        command
    } else {
        Command::new(binary)
    };
    command
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::inherit())
        .kill_on_drop(true);
    command
}

struct Sidecar {
    child: Child,
    stdin: Option<ChildStdin>,
    stdout: BufReader<ChildStdout>,
    ready: Value,
}

impl Sidecar {
    async fn start(stream_addr: SocketAddr) -> Self {
        Self::start_limits(stream_addr, 64, 64).await
    }

    async fn start_limits(stream_addr: SocketAddr, connections: u32, streams: u32) -> Self {
        let mut child = child_command().spawn().expect("spawn sidecar");
        let stdin = child.stdin.take().unwrap();
        let stdout = BufReader::new(child.stdout.take().unwrap());
        let mut this = Self {
            child,
            stdin: Some(stdin),
            stdout,
            ready: Value::Null,
        };
        this.send(
            json!({"type":"start", "protocol":1, "alpn":"avibe-direct/1",
            "stream_addr":stream_addr.to_string(), "token":TOKEN,
            "max_connections":connections, "max_streams_per_connection":streams}),
        )
        .await;
        this.ready = this.message("ready").await;
        assert_eq!(this.ready["protocol"], 1);
        let id = this.ready["endpoint_id"].as_str().unwrap();
        assert_eq!(id.len(), 64);
        assert!(
            id.bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        );
        this
    }

    async fn send(&mut self, value: Value) {
        let mut bytes = serde_json::to_vec(&value).unwrap();
        bytes.push(b'\n');
        timeout(WAIT, self.stdin.as_mut().unwrap().write_all(&bytes))
            .await
            .unwrap()
            .unwrap();
    }

    async fn admit(&mut self, endpoint: &Endpoint, ttl: u64) {
        self.send(
            json!({"type":"admission", "endpoint_id":endpoint.id().to_string(), "ttl_seconds":ttl}),
        )
        .await;
    }

    async fn message(&mut self, expected: &str) -> Value {
        timeout(WAIT, async {
            loop {
                let mut line = String::new();
                let count = self.stdout.read_line(&mut line).await.unwrap();
                assert!(count > 0 && count <= 4096, "invalid sidecar output length");
                let value: Value = serde_json::from_str(&line).unwrap();
                assert_ne!(value["type"], "admit", "obsolete admission request");
                assert_ne!(value["type"], "fatal", "{value}");
                if value["type"] == expected {
                    return value;
                }
            }
        })
        .await
        .expect("control message timeout")
    }

    async fn stop(mut self) {
        self.send(json!({"type":"stop"})).await;
        // Deliberately leave stdin open: stop must not wait for the pipe's EOF.
        assert!(
            timeout(WAIT, self.child.wait())
                .await
                .unwrap()
                .unwrap()
                .success()
        );
    }
}

fn ready_sockets(ready: &Value) -> Vec<SocketAddr> {
    ready["addresses"]
        .as_array()
        .unwrap()
        .iter()
        .map(|s| s.as_str().unwrap().parse().unwrap())
        .collect()
}

fn dial_address(ready: &Value) -> EndpointAddr {
    let sockets = ready_sockets(ready);
    let ips = std::env::var("AVDT_TEST_DIAL").unwrap_or_else(|_| "127.0.0.1".into());
    let mut address = EndpointAddr::new(ready["endpoint_id"].as_str().unwrap().parse().unwrap());
    for ip in ips.split(',').map(|s| s.parse::<IpAddr>().unwrap()) {
        let socket = sockets
            .iter()
            .find(|s| s.is_ipv4() == ip.is_ipv4())
            .expect("dial family present");
        address = address.with_ip_addr(SocketAddr::new(ip, socket.port()));
    }
    address
}

async fn client() -> Endpoint {
    Endpoint::builder(presets::Minimal)
        .relay_mode(RelayMode::Disabled)
        .portmapper_config(PortmapperConfig::Disabled)
        .bind()
        .await
        .unwrap()
}

async fn connect(sidecar: &mut Sidecar, endpoint: &Endpoint, ttl: u64) -> Connection {
    sidecar.admit(endpoint, ttl).await;
    // There is intentionally no acknowledgement/barrier in the protocol. Give
    // the pipe reader a scheduling turn; production clients retry a raced dial.
    sleep(Duration::from_millis(30)).await;
    timeout(WAIT, endpoint.connect(dial_address(&sidecar.ready), ALPN))
        .await
        .unwrap()
        .unwrap()
}

async fn listener() -> TcpListener {
    TcpListener::bind("127.0.0.1:0").await.unwrap()
}

async fn accept_header(listener: &TcpListener, endpoint: &Endpoint) -> TcpStream {
    let (mut tcp, _) = timeout(WAIT, listener.accept()).await.unwrap().unwrap();
    let mut header = Vec::new();
    timeout(WAIT, async {
        for _ in 0..160 {
            let byte = tcp.read_u8().await.unwrap();
            header.push(byte);
            if byte == b'\n' {
                return;
            }
        }
        panic!("header too large");
    })
    .await
    .unwrap();
    assert_eq!(
        header,
        format!("AVDT1 {TOKEN} {}\n", endpoint.id()).as_bytes()
    );
    tcp
}

async fn open(connection: &Connection) -> (iroh::endpoint::SendStream, iroh::endpoint::RecvStream) {
    timeout(WAIT, connection.open_bi()).await.unwrap().unwrap()
}

#[tokio::test]
async fn admitted_stream_forwards_bytes_and_half_closes() {
    let listener = listener().await;
    let mut sidecar = Sidecar::start(listener.local_addr().unwrap()).await;
    let client = client().await;
    let mut changed_addresses = Value::Null;
    if let Some(hook) = std::env::var_os("AVDT_TEST_PRE_DIAL_HOOK") {
        assert!(
            timeout(WAIT, Command::new(hook).kill_on_drop(true).status())
                .await
                .unwrap()
                .unwrap()
                .success()
        );
        let dial_ips: Vec<IpAddr> = std::env::var("AVDT_TEST_DIAL")
            .unwrap()
            .split(',')
            .map(|s| s.parse().unwrap())
            .collect();
        timeout(Duration::from_secs(10), async {
            loop {
                let message = sidecar.message("addresses").await;
                if ready_sockets(&message)
                    .iter()
                    .any(|s| dial_ips.contains(&s.ip()))
                {
                    sidecar.ready["addresses"] = message["addresses"].clone();
                    changed_addresses = message;
                    break;
                }
            }
        })
        .await
        .unwrap();
    }
    if let Some(path) = std::env::var_os("AVDT_TEST_CONTEXT") {
        let path = std::path::PathBuf::from(path);
        let temporary = path.with_extension("tmp");
        std::fs::write(&temporary, serde_json::to_vec(&json!({"ready":sidecar.ready,
            "addresses":changed_addresses, "client_endpoint_id":client.id().to_string(), "token":TOKEN,
            "dial":dial_address(&sidecar.ready).ip_addrs().map(ToString::to_string).collect::<Vec<_>>(),
            "client_bound_sockets":client.bound_sockets().iter().map(ToString::to_string).collect::<Vec<_>>(),
            "stream_addr":listener.local_addr().unwrap().to_string()})).unwrap()).unwrap();
        std::fs::rename(temporary, path).unwrap();
    }
    let connection = connect(&mut sidecar, &client, 300).await;
    let (mut send, mut recv) = open(&connection).await;
    send.write_all(b"from-app").await.unwrap(); // open_bi alone does not notify the peer.
    let mut tcp = accept_header(&listener, &client).await;
    send.finish().unwrap();
    let mut body = Vec::new();
    timeout(WAIT, tcp.read_to_end(&mut body))
        .await
        .unwrap()
        .unwrap();
    assert_eq!(body, b"from-app");
    tcp.write_all(b"from-instance").await.unwrap();
    tcp.shutdown().await.unwrap();
    assert_eq!(
        timeout(WAIT, recv.read_to_end(1024))
            .await
            .unwrap()
            .unwrap(),
        b"from-instance"
    );
    // A prebuilt stock client observes decrypted frame counters, not encrypted
    // packet guesses. This assertion is enabled only in the non-shipping job.
    if std::env::var_os("AVDT_EXPECT_NAT_DISABLED").is_some() {
        sleep(Duration::from_millis(150)).await;
        assert_eq!(connection.stats().frame_rx.add_address, 0);
        assert_eq!(connection.stats().frame_tx.reach_out, 0);
        eprintln!("NAT experiment: received ADD_ADDRESS=0, sent REACH_OUT=0");
    }
    sidecar.stop().await;
    client.close().await;
}

#[tokio::test]
async fn instance_half_close_does_not_cancel_app_upload() {
    let listener = listener().await;
    let mut sidecar = Sidecar::start(listener.local_addr().unwrap()).await;
    let client = client().await;
    let connection = connect(&mut sidecar, &client, 300).await;
    let (mut send, mut recv) = open(&connection).await;
    send.write_all(b"first").await.unwrap();
    let mut tcp = accept_header(&listener, &client).await;
    tcp.write_all(b"response").await.unwrap();
    tcp.shutdown().await.unwrap();
    assert_eq!(
        timeout(WAIT, recv.read_to_end(1024))
            .await
            .unwrap()
            .unwrap(),
        b"response"
    );
    send.write_all(b"second").await.unwrap();
    send.finish().unwrap();
    let mut body = Vec::new();
    timeout(WAIT, tcp.read_to_end(&mut body))
        .await
        .unwrap()
        .unwrap();
    assert_eq!(body, b"firstsecond");
    sidecar.stop().await;
    client.close().await;
}

#[tokio::test]
async fn unknown_peer_is_closed_without_request_or_listener_connection() {
    let listener = listener().await;
    let mut sidecar = Sidecar::start(listener.local_addr().unwrap()).await;
    let client = client().await;
    if let Ok(connection) = timeout(WAIT, client.connect(dial_address(&sidecar.ready), ALPN))
        .await
        .unwrap()
    {
        timeout(Duration::from_secs(2), connection.closed())
            .await
            .expect("unknown peer closed promptly");
    }
    assert!(
        timeout(Duration::from_millis(250), listener.accept())
            .await
            .is_err()
    );
    // A stranger does not require, or produce, an admission round trip.
    let mut output = String::new();
    assert!(
        timeout(
            Duration::from_millis(100),
            sidecar.stdout.read_line(&mut output)
        )
        .await
        .is_err(),
        "unexpected output: {output}"
    );
    sidecar.stop().await;
    client.close().await;
}

#[tokio::test]
async fn live_admission_expires_and_closes_the_stream() {
    let listener = listener().await;
    let mut sidecar = Sidecar::start(listener.local_addr().unwrap()).await;
    let client = client().await;
    let connection = connect(&mut sidecar, &client, 2).await;
    let (mut send, _recv) = open(&connection).await;
    send.write_all(b"x").await.unwrap();
    let mut tcp = accept_header(&listener, &client).await;
    timeout(Duration::from_secs(4), connection.closed())
        .await
        .unwrap();
    let mut body = Vec::new();
    let _ = timeout(WAIT, tcp.read_to_end(&mut body)).await.unwrap();
    sidecar.stop().await;
    client.close().await;
}

#[tokio::test]
async fn revoke_then_immediate_readmission_does_not_save_old_connection() {
    let listener = listener().await;
    let mut sidecar = Sidecar::start(listener.local_addr().unwrap()).await;
    let client = client().await;
    let connection = connect(&mut sidecar, &client, 300).await;
    let (mut send, _recv) = open(&connection).await;
    send.write_all(b"x").await.unwrap();
    let _tcp = accept_header(&listener, &client).await;
    sidecar.admit(&client, 0).await;
    sidecar.admit(&client, 300).await;
    timeout(Duration::from_secs(2), connection.closed())
        .await
        .unwrap();
    sidecar.stop().await;
    client.close().await;
}

#[tokio::test]
async fn refreshing_admission_extends_a_live_connection() {
    let listener = listener().await;
    let mut sidecar = Sidecar::start(listener.local_addr().unwrap()).await;
    let client = client().await;
    let connection = connect(&mut sidecar, &client, 2).await;
    sleep(Duration::from_secs(1)).await;
    sidecar.admit(&client, 3).await;
    assert!(
        timeout(Duration::from_millis(1500), connection.closed())
            .await
            .is_err()
    );
    let (mut send, _recv) = open(&connection).await;
    send.write_all(b"after original expiry").await.unwrap();
    let _tcp = accept_header(&listener, &client).await;
    timeout(Duration::from_secs(3), connection.closed())
        .await
        .unwrap();
    sidecar.stop().await;
    client.close().await;
}

#[tokio::test]
async fn stream_credit_is_bounded_and_listener_failure_is_reset() {
    let listener = listener().await;
    let addr = listener.local_addr().unwrap();
    let mut sidecar = Sidecar::start_limits(addr, 1, 1).await;
    let client = client().await;
    let connection = connect(&mut sidecar, &client, 300).await;
    let (mut send, _recv) = open(&connection).await;
    send.write_all(b"x").await.unwrap();
    let tcp = accept_header(&listener, &client).await;
    assert!(
        timeout(Duration::from_millis(150), connection.open_bi())
            .await
            .is_err()
    );
    drop(tcp);
    drop(listener);
    // A separate admitted endpoint reaches a deliberately closed TCP listener.
    sidecar.stop().await;
    client.close().await;
    let mut sidecar = Sidecar::start(addr).await;
    let client2 = crate::client().await;
    let connection = connect(&mut sidecar, &client2, 300).await;
    let (mut send, mut recv) = open(&connection).await;
    send.write_all(b"unreachable listener").await.unwrap();
    assert!(
        timeout(WAIT, recv.read_to_end(1024))
            .await
            .unwrap()
            .is_err()
    );
    sidecar.stop().await;
    client2.close().await;
}

#[tokio::test]
async fn wrong_application_protocol_is_refused() {
    let listener = listener().await;
    let mut sidecar = Sidecar::start(listener.local_addr().unwrap()).await;
    let client = client().await;
    sidecar.admit(&client, 300).await;
    assert!(
        timeout(
            WAIT,
            client.connect(dial_address(&sidecar.ready), b"wrong/protocol")
        )
        .await
        .unwrap()
        .is_err()
    );
    assert!(
        timeout(Duration::from_millis(100), listener.accept())
            .await
            .is_err()
    );
    sidecar.stop().await;
    client.close().await;
}

#[tokio::test]
async fn malformed_unknown_and_oversized_control_exit_with_error() {
    for input in [
        b"{not-json}\n".to_vec(),
        vec![b'a'; 4097],
        b"{\"type\":\"unknown\"}\n".to_vec(),
        vec![0xff, b'\n'],
    ] {
        let mut child = child_command().spawn().unwrap();
        let mut stdin = child.stdin.take().unwrap();
        let _ = stdin.write_all(&input).await;
        drop(stdin);
        assert!(
            !timeout(WAIT, child.wait())
                .await
                .unwrap()
                .unwrap()
                .success()
        );
    }
    for message in [
        json!({"type":"admission","endpoint_id":"A".repeat(64),"ttl_seconds":1}),
        json!({"type":"admission","endpoint_id":"0".repeat(64),"ttl_seconds":-1}),
        json!({"type":"admission","request":1,"allow":true}),
        json!({"type":"start"}),
    ] {
        let listener = listener().await;
        let mut sidecar = Sidecar::start(listener.local_addr().unwrap()).await;
        sidecar.send(message).await;
        assert!(
            !timeout(WAIT, sidecar.child.wait())
                .await
                .unwrap()
                .unwrap()
                .success()
        );
    }
}

#[tokio::test]
async fn eof_exits_and_releases_udp_sockets_and_live_tcp() {
    let listener = listener().await;
    let mut sidecar = Sidecar::start(listener.local_addr().unwrap()).await;
    let sockets = ready_sockets(&sidecar.ready);
    let client = client().await;
    let connection = connect(&mut sidecar, &client, 300).await;
    let (mut send, _recv) = open(&connection).await;
    send.write_all(b"x").await.unwrap();
    let mut tcp = accept_header(&listener, &client).await;
    sidecar.stdin.take();
    assert!(
        timeout(WAIT, sidecar.child.wait())
            .await
            .unwrap()
            .unwrap()
            .success()
    );
    assert!(sidecar.child.try_wait().unwrap().is_some());
    let mut body = Vec::new();
    let _ = timeout(WAIT, tcp.read_to_end(&mut body)).await.unwrap();
    for ipv4 in [true, false] {
        if let Some(socket) = sockets.iter().find(|s| s.is_ipv4() == ipv4) {
            let ip = if ipv4 {
                IpAddr::V4(Ipv4Addr::UNSPECIFIED)
            } else {
                IpAddr::V6(Ipv6Addr::UNSPECIFIED)
            };
            let _rebound = std::net::UdpSocket::bind(SocketAddr::new(ip, socket.port()))
                .expect("sidecar left UDP socket open");
        }
    }
    client.close().await;
}

// Explicit opt-in because this experiment needs a runner with IPv6 enabled.
#[tokio::test]
#[ignore = "run in isolated NAT-disabled experiment"]
async fn multiple_candidates_with_one_dead() {
    for dead_first in [true, false] {
        let listener = listener().await;
        let mut sidecar = Sidecar::start(listener.local_addr().unwrap()).await;
        let client = client().await;
        sidecar.admit(&client, 300).await;
        sleep(Duration::from_millis(30)).await;
        let sockets = ready_sockets(&sidecar.ready);
        // EndpointAddr is a BTreeSet: varying insertion order is not a test of
        // candidate preference. IPv4 sorts first, so vary which family is dead.
        let live_port = sockets
            .iter()
            .find(|s| s.is_ipv6() == dead_first)
            .expect("both address families enabled on experiment runner")
            .port();
        let live_ip = if dead_first {
            IpAddr::V6(Ipv6Addr::LOCALHOST)
        } else {
            IpAddr::V4(Ipv4Addr::LOCALHOST)
        };
        let live = SocketAddr::new(live_ip, live_port);
        // A bound socket with no receiver consumes packets without ICMP refusal.
        let blackhole =
            std::net::UdpSocket::bind(if dead_first { "127.0.0.1:0" } else { "[::1]:0" }).unwrap();
        let dead = blackhole.local_addr().unwrap();
        let candidates = if dead_first {
            [dead, live]
        } else {
            [live, dead]
        };
        let mut address = EndpointAddr::new(
            sidecar.ready["endpoint_id"]
                .as_str()
                .unwrap()
                .parse()
                .unwrap(),
        );
        for candidate in candidates {
            address = address.with_ip_addr(candidate);
        }
        let started = std::time::Instant::now();
        let connection = timeout(Duration::from_secs(15), client.connect(address, ALPN))
            .await
            .expect("candidate failover timeout")
            .unwrap();
        eprintln!(
            "candidate experiment dead_first={dead_first} connected_ms={}",
            started.elapsed().as_millis()
        );
        let (mut send, mut recv) = open(&connection).await;
        send.write_all(b"candidate test").await.unwrap();
        send.finish().unwrap();
        let mut tcp = accept_header(&listener, &client).await;
        tcp.write_all(b"ok").await.unwrap();
        tcp.shutdown().await.unwrap();
        assert_eq!(
            timeout(WAIT, recv.read_to_end(1024))
                .await
                .unwrap()
                .unwrap(),
            b"ok"
        );
        assert_eq!(connection.stats().frame_rx.add_address, 0);
        sidecar.stop().await;
        client.close().await;
    }
}
