//! Test-only client instrumentation for the isolated security experiment.
//! The iroh methods used here are added only to a disposable vendored copy.
//! Targets are fixed, test-owned UDP listeners in an isolated CI namespace.
use iroh::{
    Endpoint, EndpointAddr,
    endpoint::{AfterHandshakeOutcome, Connection, EndpointHooks, presets},
};
use serde_json::{Value, json};
use std::{
    process::Stdio,
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};
use tokio::{
    io::{AsyncBufReadExt, AsyncWriteExt, BufReader},
    net::{TcpListener, UdpSocket},
    process::Command,
    time::{sleep, timeout},
};

#[derive(Debug, Default)]
struct Observation {
    hook_ran: bool,
    extension_available: bool,
    learned_addresses: usize,
    received_add_address: u64,
    sent_reach_out: u64,
    close_observed: bool,
}

#[derive(Clone, Debug)]
struct ProbeHook {
    target: std::net::SocketAddr,
    observed: Arc<Mutex<Observation>>,
}

impl EndpointHooks for ProbeHook {
    async fn after_handshake(&self, connection: &Connection) -> AfterHandshakeOutcome {
        let extension = connection.avdt_experiment_request_probe(self.target);
        {
            let mut state = self.observed.lock().unwrap();
            state.hook_ran = true;
            state.extension_available = extension;
            state.learned_addresses = connection.avdt_experiment_remote_address_count();
            state.received_add_address = connection.stats().frame_rx.add_address;
            state.sent_reach_out = connection.stats().frame_tx.reach_out;
        }
        let weak = connection.weak_handle();
        let closed = weak.closed();
        let observed = self.observed.clone();
        // Weak reference only. Capture final counters even when the remote close
        // wins the race with Endpoint::connect returning to the caller.
        tokio::spawn(async move {
            if let Some(closed) = closed.await {
                let mut state = observed.lock().unwrap();
                state.received_add_address = closed.stats.frame_rx.add_address;
                state.sent_reach_out = closed.stats.frame_tx.reach_out;
                state.close_observed = true;
            }
        });
        AfterHandshakeOutcome::Accept
    }
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|byte| format!("{byte:02x}")).collect()
}

#[tokio::main(flavor = "current_thread")]
async fn main() {
    assert_eq!(
        std::env::var("AVDT_ISOLATED_EXPERIMENT").as_deref(),
        Ok("1")
    );
    assert_eq!(std::env::var("GITHUB_ACTIONS").as_deref(), Ok("true"));
    assert_ne!(
        std::fs::read_link("/proc/self/ns/net")
            .unwrap()
            .to_string_lossy(),
        std::env::var("AVDT_EXPERIMENT_HOST_NETNS").unwrap()
    );
    let binary = std::env::args().nth(1).expect("server path");
    let label = std::env::args().nth(2).expect("variant label");
    let target_class = std::env::args().nth(3).expect("loopback or namespace");
    let target_ip = match target_class.as_str() {
        "loopback" => "127.0.0.1",
        "namespace" => "192.168.245.2",
        _ => panic!("only fixed test-owned destinations are allowed"),
    };
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let probe_listener = Arc::new(UdpSocket::bind((target_ip, 0)).await.unwrap());
    let target = probe_listener.local_addr().unwrap();
    let mut child = Command::new(binary)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::inherit())
        .kill_on_drop(true)
        .spawn()
        .unwrap();
    let mut input = child.stdin.take().unwrap();
    input
        .write_all(
            format!(
                "{}\n",
                json!({"type":"start","protocol":1,"alpn":"avibe-direct/1",
        "stream_addr":listener.local_addr().unwrap().to_string(),"token":"11".repeat(32),
        "max_connections":64,"max_streams_per_connection":64})
            )
            .as_bytes(),
        )
        .await
        .unwrap();
    let mut output = BufReader::new(child.stdout.take().unwrap());
    let mut line = String::new();
    timeout(Duration::from_secs(5), output.read_line(&mut line))
        .await
        .unwrap()
        .unwrap();
    let ready: Value = serde_json::from_str(&line).unwrap();
    assert_eq!(ready["type"], "ready");
    let server_socket = ready["addresses"]
        .as_array()
        .unwrap()
        .iter()
        .map(|s| s.as_str().unwrap().parse::<std::net::SocketAddr>().unwrap())
        .find(|s| s.is_ipv4())
        .unwrap();
    let destination = std::net::SocketAddr::new("127.0.0.1".parse().unwrap(), server_socket.port());
    let observations = Arc::new(Mutex::new(Observation::default()));
    let issued_cids = iroh::avdt_experiment_cids::enable();
    let client = Endpoint::builder(presets::Minimal)
        .hooks(ProbeHook {
            target,
            observed: observations.clone(),
        })
        .bind()
        .await
        .unwrap();
    let probe_socket = probe_listener.clone();
    let expected_server_port = server_socket.port();
    let captured_cids = issued_cids.clone();
    let counter = tokio::spawn(async move {
        let mut packets = Vec::new();
        let mut buffer = [0u8; 65536];
        let until = tokio::time::Instant::now() + Duration::from_millis(1600);
        while let Ok(Ok((size, source))) =
            tokio::time::timeout_at(until, probe_socket.recv_from(&mut buffer)).await
        {
            assert!(
                source.ip().is_loopback()
                    || matches!(
                        source.ip().to_string().as_str(),
                        "192.168.245.1" | "192.168.245.2"
                    )
            );
            assert_eq!(
                source.port(),
                expected_server_port,
                "unexpected sender to test listener"
            );
            assert!(size >= 1 + 20 + 1 + 16, "truncated short-header packet");
            assert_eq!(buffer[0] & 0x80, 0, "expected a QUIC short header");
            // The destination CID is outside QUIC header protection. Do not
            // pretend to decode the protected packet number or ciphertext.
            let dcid = &buffer[1..21];
            assert!(captured_cids.lock().unwrap().iter().any(|cid| cid == dcid));
            packets.push(json!({"size":size,"source":source.to_string(),
                "udp_payload_hex":hex(&buffer[..size]),"destination_cid_hex":hex(dcid),
                "destination_cid_matches_issued":true}));
        }
        packets
    });
    let started = Instant::now();
    let connected = timeout(
        Duration::from_secs(5),
        client.connect(
            EndpointAddr::new(ready["endpoint_id"].as_str().unwrap().parse().unwrap())
                .with_ip_addr(destination),
            b"avibe-direct/1",
        ),
    )
    .await
    .unwrap();
    let connection_returned = connected.is_ok();
    if let Ok(ref connection) = connected {
        let _ = timeout(Duration::from_secs(2), connection.closed()).await;
    }
    let rejected_after_ms = started.elapsed().as_millis();
    let packets = counter.await.unwrap();
    let sizes: Vec<usize> = packets
        .iter()
        .map(|packet| packet["size"].as_u64().unwrap() as usize)
        .collect();
    let issued: Vec<String> = issued_cids
        .lock()
        .unwrap()
        .iter()
        .map(|cid| hex(cid))
        .collect();
    sleep(Duration::from_millis(20)).await;
    assert!(
        timeout(Duration::from_millis(100), listener.accept())
            .await
            .is_err(),
        "unadmitted peer reached ingress"
    );
    let observed = observations.lock().unwrap();
    println!(
        "{}",
        json!({"variant":label,"target_class":target_class,"target":target.to_string(),
        "issued_cid_length":20,"issued_cids_hex":issued,"captured_packets":packets,
        "connection_returned":connection_returned,
        "client_hook_ran":observed.hook_ran,"extension_available":observed.extension_available,
        "learned_address_count_at_client_hook":observed.learned_addresses,
        "received_add_address":observed.received_add_address,"sent_reach_out":observed.sent_reach_out,
        "close_observed":observed.close_observed,"refusal_observed_ms":rejected_after_ms,
        "probe_packets":sizes.len(),"probe_udp_bytes":sizes.iter().sum::<usize>(),"probe_sizes":sizes})
    );
    drop(observed);
    drop(connected);
    input.write_all(b"{\"type\":\"stop\"}\n").await.unwrap();
    assert!(
        timeout(Duration::from_secs(8), child.wait())
            .await
            .unwrap()
            .unwrap()
            .success()
    );
    client.close().await;
}
