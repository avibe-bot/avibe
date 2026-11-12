//! CI-only idle/keepalive qualification: an unmodified stock client (the parent
//! module's `client()`, default transport config) against whichever sidecar
//! `AVDT_TEST_BINARY` names. Nothing here sets or assumes a transport timeout.
//!
//! Every test is ignored and refuses to start unless it is root on a GitHub
//! Actions Linux runner with `AVDT_ISOLATED_IDLE=1`; the runner gives each test
//! its own disposable network namespace. Faults are applied by the runner's
//! `AVDT_IDLE_FAULT_HOOK` to every UDP port either endpoint has bound, in both
//! address families, so no candidate path can bypass them:
//!
//!   <hook> <mode> <client-port-v4> <client-port-v6> <server-port-v4> <server-port-v6>
//!
//! with mode `clear`, `both`, `client-to-server`, `server-to-client` or `loss5`,
//! and port 0 for an unbound family. `clear` is idempotent and ends its stdout
//! with one JSON object carrying `client_dropped_packets` and
//! `server_dropped_packets`: drops since the rules were installed, by owner of
//! the source port.
//!
//! Each trial or window writes one `AVDT_IDLE_RESULT {json}` line straight to
//! stdout, bypassing libtest's capture, before any assertion on it is made.
//!
//! Three timers must not be confused in the evidence:
//! - the QUIC idle timeout and keep-alive, which is what is being qualified;
//! - the sidecar's 300 s no-open-stream policy, an application close with
//!   reason `idle_timeout` (`quiet_no_stream` asserts exactly that);
//! - the admission TTL, refreshed over the control pipe every window. A refresh
//!   changes sidecar-local state only and puts nothing on the network.
use super::*;
use std::{ffi::OsString, time::Instant};

type ConnectionError = iroh::endpoint::ConnectionError;

/// Upper bound on waiting for a blackholed connection to close.
const HARD_WAIT: Duration = Duration::from_secs(45);
const HOOK_WAIT: Duration = Duration::from_secs(10);
const WINDOW: Duration = Duration::from_secs(60);
/// Quiet time between the warmup roundtrip and the fault, one per trial, so the
/// three trials start the fault at different phases of both keep-alive timers.
const SETTLE_MS: [u64; 3] = [500, 1700, 2900];
/// The sidecar's no-open-stream policy (`NO_STREAM_TIMEOUT` in `src/main.rs`).
const NO_STREAM: Duration = Duration::from_secs(300);
/// Time for the NAT traversal rounds that follow a new connection to finish,
/// so that they do not move the keep-alive phase an outage trial is aimed at.
const QNT_SETTLE: Duration = Duration::from_secs(10);
/// How long a connection is watched after it has recovered from an outage.
const HEALTHY: Duration = Duration::from_secs(5);

const COUNTERS: [&str; 14] = [
    "udp_tx_datagrams",
    "udp_tx_bytes",
    "udp_rx_datagrams",
    "udp_rx_bytes",
    "quic_packets_sent",
    "quic_packets_lost",
    "ping_tx",
    "ping_rx",
    "path_challenge_tx",
    "path_challenge_rx",
    "path_response_tx",
    "path_response_rx",
    "reach_out_tx",
    "add_address_rx",
];
/// What `client_counters` measures; attached to every result line.
const COUNTER_NOTE: &str = "delta of the client's Connection::stats() over duration_ms. \
     udp_*_bytes are UDP payload bytes (QUIC datagrams), without UDP/IP headers. \
     udp_tx counts datagrams the client's QUIC stack produced, including ones a fault \
     then dropped and including off-path NAT traversal probes (path_challenge/path_response); \
     udp_rx counts datagrams delivered to this connection.";

fn require_isolated_runner() {
    let is = |name: &str, value: &str| std::env::var(name).is_ok_and(|found| found == value);
    assert!(
        cfg!(target_os = "linux") && is("GITHUB_ACTIONS", "true") && is("AVDT_ISOLATED_IDLE", "1"),
        "idle qualification runs only in a disposable network namespace on a Linux GitHub \
         Actions runner (GITHUB_ACTIONS=true and AVDT_ISOLATED_IDLE=1)"
    );
    let status = std::fs::read_to_string("/proc/self/status").expect("read /proc/self/status");
    let effective_uid = status
        .lines()
        .find_map(|line| line.strip_prefix("Uid:"))
        .and_then(|ids| ids.split_whitespace().nth(1));
    assert_eq!(effective_uid, Some("0"), "idle qualification runs as root");
}

fn emit(mut result: Value) {
    use std::io::Write as _;
    result["counter_note"] = json!(COUNTER_NOTE);
    let line = format!("AVDT_IDLE_RESULT {result}\n");
    // One write of the whole line: tests in other processes or threads cannot
    // interleave with it, and libtest's capture does not hide it.
    let mut stdout = std::io::stdout().lock();
    stdout
        .write_all(line.as_bytes())
        .and_then(|()| stdout.flush())
        .expect("write result line");
}

fn millis(duration: Duration) -> u64 {
    duration.as_millis() as u64
}

/// The first of `names` the runner has set to a non-empty value, in milliseconds.
fn env_ms(names: &[&str]) -> Option<u64> {
    names
        .iter()
        .find_map(|name| std::env::var(name).ok().filter(|value| !value.is_empty()))
        .map(|value| {
            value
                .parse::<u64>()
                .unwrap_or_else(|_| panic!("{names:?} takes milliseconds, not {value:?}"))
        })
}

fn counters(connection: &Connection) -> [u64; 14] {
    let stats = connection.stats();
    [
        stats.udp_tx.datagrams,
        stats.udp_tx.bytes,
        stats.udp_rx.datagrams,
        stats.udp_rx.bytes,
        stats.sent_packets,
        stats.lost_packets,
        stats.frame_tx.ping,
        stats.frame_rx.ping,
        stats.frame_tx.path_challenge,
        stats.frame_rx.path_challenge,
        stats.frame_tx.path_response,
        stats.frame_rx.path_response,
        stats.frame_tx.reach_out,
        stats.frame_rx.add_address,
    ]
}

fn counters_since(before: &[u64; 14], connection: &Connection) -> Value {
    let after = counters(connection);
    COUNTERS
        .iter()
        .zip(before.iter().zip(after))
        .map(|(name, (before, after))| (name.to_string(), json!(after as i64 - *before as i64)))
        .collect::<serde_json::Map<String, Value>>()
        .into()
}

fn paths(connection: &Connection) -> Value {
    let paths = connection.paths();
    paths
        .iter()
        .map(|path| {
            json!({"remote": format!("{:?}", path.remote_addr()), "selected": path.is_selected()})
        })
        .collect::<Vec<Value>>()
        .into()
}

fn close_reason(error: &ConnectionError) -> Value {
    match error {
        ConnectionError::TimedOut => json!({"kind": "timed_out"}),
        ConnectionError::ApplicationClosed(close) => json!({
            "kind": "application_closed",
            "code": close.error_code.into_inner(),
            "reason": String::from_utf8_lossy(&close.reason),
        }),
        ConnectionError::ConnectionClosed(close) => {
            json!({"kind": "transport_closed", "detail": close.to_string()})
        }
        ConnectionError::Reset => json!({"kind": "reset"}),
        ConnectionError::LocallyClosed => json!({"kind": "locally_closed"}),
        other => json!({"kind": "other", "detail": other.to_string()}),
    }
}

fn close_reason_now(connection: &Connection) -> Value {
    connection
        .close_reason()
        .map_or(Value::Null, |error| close_reason(&error))
}

/// The sidecar's own no-open-stream close, as the client sees it.
fn is_application_idle_close(error: &ConnectionError) -> bool {
    matches!(error, ConnectionError::ApplicationClosed(close)
        if close.error_code.into_inner() == 1 && &close.reason[..] == b"idle_timeout".as_slice())
}

/// The sidecar's control output, read with a length bound and without losing
/// bytes when a read is cancelled by a timeout.
#[derive(Default)]
struct ControlLines {
    partial: Vec<u8>,
}

impl ControlLines {
    /// The next message, or `None` once the sidecar has closed its output.
    async fn next(&mut self, sidecar: &mut Sidecar) -> Option<Value> {
        loop {
            let (used, complete) = {
                let available = sidecar.stdout.fill_buf().await.ok()?;
                if available.is_empty() {
                    return None;
                }
                let (used, complete) = match available.iter().position(|byte| *byte == b'\n') {
                    Some(index) => (index + 1, true),
                    None => (available.len(), false),
                };
                self.partial.extend_from_slice(&available[..used]);
                (used, complete)
            };
            sidecar.stdout.consume(used);
            assert!(self.partial.len() <= 4096, "sidecar output line too long");
            if complete {
                let line = std::mem::take(&mut self.partial);
                return Some(serde_json::from_slice(&line).expect("sidecar output is JSON"));
            }
        }
    }

    /// The reason of the next `peer_closed` message.
    async fn peer_closed(&mut self, sidecar: &mut Sidecar) -> Option<String> {
        loop {
            let message = self.next(sidecar).await?;
            if message["type"] == "peer_closed" {
                return Some(message["reason"].as_str().unwrap_or_default().to_owned());
            }
        }
    }
}

/// An admitted connection with one forwarded stream, both ends retained.
struct Live {
    connection: Connection,
    send: iroh::endpoint::SendStream,
    recv: iroh::endpoint::RecvStream,
    tcp: TcpStream,
}

impl Live {
    /// Connects, opens a stream and completes one byte in each direction, so
    /// the transport is established and both directions are acknowledged
    /// before anything is measured.
    async fn establish(sidecar: &mut Sidecar, client: &Endpoint, listener: &TcpListener) -> Self {
        let connection = connect(sidecar, client, 300).await;
        let (mut send, recv) = open(&connection).await;
        // open_bi alone does not notify the peer: the first byte opens the stream.
        timeout(WAIT, send.write_all(&[0x57]))
            .await
            .expect("warmup write timeout")
            .expect("warmup write");
        let tcp = accept_header(listener, client).await;
        let mut live = Self {
            connection,
            send,
            recv,
            tcp,
        };
        live.complete_roundtrip(0x57)
            .await
            .expect("warmup roundtrip");
        live
    }

    async fn app_sends(&mut self, byte: u8) -> Result<(), String> {
        match timeout(WAIT, self.send.write_all(&[byte])).await {
            Ok(Ok(())) => Ok(()),
            Ok(Err(error)) => Err(format!("app write: {error}")),
            Err(_) => Err("app write timeout".into()),
        }
    }

    async fn instance_receives(&mut self, byte: u8) -> Result<(), String> {
        let mut found = [0u8; 1];
        match timeout(WAIT, self.tcp.read_exact(&mut found)).await {
            Ok(Ok(_)) if found[0] == byte => Ok(()),
            Ok(Ok(_)) => Err(format!("instance read {:#04x}, sent {byte:#04x}", found[0])),
            Ok(Err(error)) => Err(format!("instance read: {error}")),
            Err(_) => Err("instance read timeout".into()),
        }
    }

    async fn instance_sends(&mut self, byte: u8) -> Result<(), String> {
        match timeout(WAIT, self.tcp.write_all(&[byte])).await {
            Ok(Ok(())) => Ok(()),
            Ok(Err(error)) => Err(format!("instance write: {error}")),
            Err(_) => Err("instance write timeout".into()),
        }
    }

    async fn app_receives(&mut self, byte: u8) -> Result<(), String> {
        let mut found = [0u8; 1];
        match timeout(WAIT, self.recv.read_exact(&mut found)).await {
            Ok(Ok(())) if found[0] == byte => Ok(()),
            Ok(Ok(())) => Err(format!("app read {:#04x}, sent {byte:#04x}", found[0])),
            Ok(Err(error)) => Err(format!("app read: {error}")),
            Err(_) => Err("app read timeout".into()),
        }
    }

    /// The rest of a roundtrip whose first byte the app has already sent.
    async fn complete_roundtrip(&mut self, byte: u8) -> Result<(), String> {
        self.instance_receives(byte).await?;
        self.instance_sends(!byte).await?;
        self.app_receives(!byte).await
    }

    /// One byte from the app to the instance and a different one back.
    async fn roundtrip(&mut self, byte: u8) -> Result<(), String> {
        self.app_sends(byte).await?;
        self.complete_roundtrip(byte).await
    }
}

/// The runner's fault script, bound to the UDP ports of one client and sidecar.
struct Fault {
    hook: OsString,
    /// Client IPv4, client IPv6, server IPv4, server IPv6; 0 where unbound.
    ports: [u16; 4],
}

impl Fault {
    fn new(client: &Endpoint, sidecar: &Sidecar) -> Self {
        fn port(sockets: &[SocketAddr], ipv4: bool) -> u16 {
            let mut ports: Vec<u16> = sockets
                .iter()
                .filter(|socket| socket.is_ipv4() == ipv4)
                .map(SocketAddr::port)
                .collect();
            ports.sort_unstable();
            ports.dedup();
            assert!(
                ports.len() <= 1,
                "several UDP ports in one family: {sockets:?}"
            );
            ports.first().copied().unwrap_or(0)
        }
        let hook = std::env::var_os("AVDT_IDLE_FAULT_HOOK")
            .expect("AVDT_IDLE_FAULT_HOOK names the runner's fault script");
        let (client, server) = (client.bound_sockets(), ready_sockets(&sidecar.ready));
        let ports = [
            port(&client, true),
            port(&client, false),
            port(&server, true),
            port(&server, false),
        ];
        assert!(
            (ports[0] != 0 || ports[1] != 0) && (ports[2] != 0 || ports[3] != 0),
            "client {client:?} or sidecar {server:?} reports no UDP port"
        );
        Self { hook, ports }
    }

    /// UDP ports bound in this network namespace that the fault rules would
    /// not match. Anything listed here could carry a path around the fault.
    fn uncovered_ports(&self) -> Vec<u16> {
        let mut uncovered = Vec::new();
        for (table, ours) in [
            ("/proc/net/udp", [self.ports[0], self.ports[2]]),
            ("/proc/net/udp6", [self.ports[1], self.ports[3]]),
        ] {
            let sockets = match std::fs::read_to_string(table) {
                Ok(sockets) => sockets,
                // No IPv6 in this namespace, and neither endpoint claims a socket in it.
                Err(_) if table.ends_with('6') && ours == [0, 0] => continue,
                Err(error) => panic!("cannot read {table}: {error}"),
            };
            for line in sockets.lines().skip(1) {
                // "<slot>: <local address>:<port> <remote address>:<port> ...", in hexadecimal.
                let port = line
                    .split_whitespace()
                    .nth(1)
                    .and_then(|local| local.rsplit(':').next())
                    .and_then(|port| u16::from_str_radix(port, 16).ok())
                    .unwrap_or_else(|| panic!("unreadable {table} line: {line}"));
                if !ours.contains(&port) {
                    uncovered.push(port);
                }
            }
        }
        uncovered
    }

    /// Runs the hook. Returns only once it has exited successfully, which the
    /// contract defines as the rules being in effect (or gone).
    async fn run(&self, mode: &str) -> Vec<u8> {
        let child = Command::new(&self.hook)
            .arg(mode)
            .args(self.ports.map(|port| port.to_string()))
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit())
            .kill_on_drop(true)
            .spawn()
            .expect("spawn fault hook");
        let output = timeout(HOOK_WAIT, child.wait_with_output())
            .await
            .unwrap_or_else(|_| panic!("fault hook {mode} did not exit"))
            .expect("wait for fault hook");
        assert!(
            output.status.success(),
            "fault hook {mode} failed: {}",
            output.status
        );
        output.stdout
    }

    async fn apply(&self, mode: &str) {
        self.run(mode).await;
    }

    /// Removes the rules. Returns the hook's counters object and, from it, the
    /// packets dropped by source port owner: (client, server).
    async fn clear(&self) -> (Value, u64, u64) {
        let stdout = self.run("clear").await;
        let text = String::from_utf8_lossy(&stdout);
        let line = text
            .lines()
            .rev()
            .find(|line| !line.trim().is_empty())
            .expect("fault hook clear printed its counters");
        let counters: Value = serde_json::from_str(line).expect("fault hook counters are JSON");
        let dropped = |side: &str| {
            counters[format!("{side}_dropped_packets")]
                .as_u64()
                .unwrap_or_else(|| panic!("fault hook counters lack {side}_dropped_packets"))
        };
        let (client, server) = (dropped("client"), dropped("server"));
        (counters, client, server)
    }
}

/// Stops the sidecar. The final write can meet a closed pipe if the sidecar has
/// already exited; its exit status, not that write, says whether it ended well.
async fn stop_sidecar(mut sidecar: Sidecar) {
    if let Some(stdin) = sidecar.stdin.as_mut() {
        let _ = timeout(WAIT, stdin.write_all(b"{\"type\":\"stop\"}\n")).await;
    }
    let status = timeout(WAIT, sidecar.child.wait())
        .await
        .expect("sidecar did not exit after stop")
        .expect("wait for sidecar");
    assert!(status.success(), "sidecar exited with {status}");
}

async fn close_client(client: Endpoint) {
    timeout(WAIT, client.close())
        .await
        .expect("client endpoint did not close");
}

/// Leaves `connection` without application data until `total` has passed since
/// `started`, in windows of at most 60 s. Writes one result line per window and
/// refreshes the admission after each. Returns the close, if one happened.
async fn quiet(
    kind: &str,
    sidecar: &mut Sidecar,
    client: &Endpoint,
    connection: &Connection,
    started: Instant,
    total: Duration,
) -> Option<ConnectionError> {
    for window in 0.. {
        let offset = started.elapsed();
        if offset >= total {
            break;
        }
        let before = counters(connection);
        let window_started = Instant::now();
        let closed = timeout(WINDOW.min(total - offset), connection.closed())
            .await
            .ok();
        emit(json!({
            "kind": kind,
            "phase": "quiet_window",
            "window": window,
            "offset_ms": millis(offset),
            "duration_ms": millis(window_started.elapsed()),
            "closed": closed.is_some(),
            "close_reason": closed.as_ref().map(close_reason),
            "client_counters": counters_since(&before, connection),
            "paths": paths(connection),
        }));
        if closed.is_some() {
            return closed;
        }
        sidecar.admit(client, 300).await;
    }
    None
}

/// Three fresh trials of one blackhole mode: how long the stock client takes to
/// notice, and why its connection ended. A measurement; the only gate is the
/// runner's optional bound for this direction, `AVDT_IDLE_MAX_CLOSE_BOTH_MS`,
/// `_C2S_MS` or `_S2C_MS`, or else the older `AVDT_IDLE_MAX_CLOSE_MS`.
async fn blackhole(mode: &str) {
    require_isolated_runner();
    let direction_bound = match mode {
        "both" => "AVDT_IDLE_MAX_CLOSE_BOTH_MS",
        "client-to-server" => "AVDT_IDLE_MAX_CLOSE_C2S_MS",
        _ => "AVDT_IDLE_MAX_CLOSE_S2C_MS",
    };
    let max_close_ms = env_ms(&[direction_bound, "AVDT_IDLE_MAX_CLOSE_MS"]);
    let mut failures = Vec::new();
    for (trial, settle_ms) in SETTLE_MS.into_iter().enumerate() {
        let listener = listener().await;
        let mut sidecar = Sidecar::start(listener.local_addr().unwrap()).await;
        let client = client().await;
        let fault = Fault::new(&client, &sidecar);
        fault.clear().await;
        let live = Live::establish(&mut sidecar, &client, &listener).await;
        let uncovered = fault.uncovered_ports();
        sleep(Duration::from_millis(settle_ms)).await;
        assert!(
            live.connection.close_reason().is_none(),
            "connection closed before the fault"
        );
        let paths = paths(&live.connection);
        let before = counters(&live.connection);
        let hook_started = Instant::now();
        fault.apply(mode).await;
        let applied = Instant::now();
        let mut lines = ControlLines::default();
        let ((closed, elapsed), (sidecar_reason, sidecar_elapsed)) = tokio::join!(
            async {
                let closed = timeout(HARD_WAIT, live.connection.closed()).await.ok();
                (closed, applied.elapsed())
            },
            async {
                let reason = timeout(HARD_WAIT, lines.peer_closed(&mut sidecar)).await;
                (reason.ok().flatten(), applied.elapsed())
            },
        );
        let client_counters = counters_since(&before, &live.connection);
        let sidecar_elapsed_ms = sidecar_reason.as_ref().map(|_| millis(sidecar_elapsed));
        let (hook, client_dropped, server_dropped) = fault.clear().await;
        // No handle of this trial survives into the next one.
        drop(live);
        stop_sidecar(sidecar).await;
        close_client(client).await;
        emit(json!({
            "kind": "blackhole",
            "mode": mode,
            "trial": trial,
            "settle_ms": settle_ms,
            "closed": closed.is_some(),
            // From the hook's return, when every rule is in effect, to the close.
            "elapsed_ms": millis(elapsed),
            "duration_ms": millis(elapsed),
            "hook_apply_ms": millis(applied - hook_started),
            "max_close_ms": max_close_ms,
            "close_reason": closed.as_ref().map(close_reason),
            "sidecar_peer_closed": sidecar_reason,
            "sidecar_elapsed_ms": sidecar_elapsed_ms,
            "client_counters": client_counters,
            "paths_before_fault": paths,
            "ports": fault.ports,
            "uncovered_udp_ports": uncovered,
            "hook": hook,
        }));
        if closed.is_none() {
            failures.push(format!("trial {trial}: still open after {HARD_WAIT:?}"));
        }
        if max_close_ms.is_some_and(|max| millis(elapsed) > max) {
            failures.push(format!("trial {trial}: closed after {elapsed:?}"));
        }
        // No application byte follows the fault, so a client whose own
        // keep-alive is slower than the idle timeout may send nothing at all
        // before it gives up: a both-way blackhole can drop zero client packets.
        let dropped_as_named = match mode {
            "client-to-server" => client_dropped > 0 && server_dropped == 0,
            "server-to-client" => client_dropped == 0 && server_dropped > 0,
            _ => server_dropped > 0,
        };
        if !dropped_as_named {
            failures.push(format!(
                "trial {trial}: the fault dropped {client_dropped} client and {server_dropped} server packets"
            ));
        }
        if !uncovered.is_empty() {
            failures.push(format!(
                "trial {trial}: UDP ports {uncovered:?} bypass the fault"
            ));
        }
    }
    assert!(failures.is_empty(), "{mode}: {failures:?}");
}

#[tokio::test]
#[ignore = "isolated idle qualification"]
async fn blackhole_both() {
    blackhole("both").await;
}

#[tokio::test]
#[ignore = "isolated idle qualification"]
async fn blackhole_client_to_server() {
    blackhole("client-to-server").await;
}

#[tokio::test]
#[ignore = "isolated idle qualification"]
async fn blackhole_server_to_client() {
    blackhole("server-to-client").await;
}

/// A connection that never opens a stream stays up on keep-alives alone, and is
/// then closed by the sidecar's 300 s application policy, not by QUIC.
#[tokio::test]
#[ignore = "isolated idle qualification"]
async fn quiet_no_stream() {
    require_isolated_runner();
    let kind = "quiet_no_stream";
    let listener = listener().await;
    let mut sidecar = Sidecar::start(listener.local_addr().unwrap()).await;
    let client = client().await;
    // The sidecar starts its timer when the handshake completes, which is
    // between these two instants.
    let dial_started = Instant::now();
    let connection = connect(&mut sidecar, &client, 300).await;
    let established = Instant::now();
    let early = quiet(
        kind,
        &mut sidecar,
        &client,
        &connection,
        established,
        NO_STREAM - Duration::from_secs(5),
    )
    .await;
    let before = counters(&connection);
    let window_started = Instant::now();
    let closed = match early.clone() {
        Some(error) => Some(error),
        None => {
            let deadline = NO_STREAM + Duration::from_secs(10);
            let remaining = deadline.saturating_sub(established.elapsed());
            timeout(remaining, connection.closed()).await.ok()
        }
    };
    let since_established = established.elapsed();
    let since_dial = dial_started.elapsed();
    let mut lines = ControlLines::default();
    let sidecar_reason = timeout(Duration::from_secs(2), lines.peer_closed(&mut sidecar))
        .await
        .ok()
        .flatten();
    emit(json!({
        "kind": kind,
        "phase": "application_idle_close",
        "closed": closed.is_some(),
        // From the client's connect returning to the close.
        "elapsed_ms": millis(since_established),
        "since_dial_started_ms": millis(since_dial),
        "duration_ms": millis(window_started.elapsed()),
        "close_reason": closed.as_ref().map(close_reason),
        "sidecar_peer_closed": sidecar_reason,
        "client_counters": counters_since(&before, &connection),
    }));
    drop(connection);
    stop_sidecar(sidecar).await;
    close_client(client).await;
    assert!(
        early.is_none(),
        "closed before the no-stream policy: {early:?}"
    );
    let closed = closed.expect("still open 10 s after the no-stream policy was due");
    assert!(
        is_application_idle_close(&closed),
        "closed by {closed:?}, not by the application idle policy"
    );
    assert_eq!(sidecar_reason.as_deref(), Some("idle_timeout"));
    assert!(
        since_dial >= NO_STREAM - Duration::from_millis(100),
        "the 300 s policy fired after {since_dial:?}"
    );
}

/// With a stream open the application policy is not armed, so this is the
/// transport alone: no application byte for 305 s, no close, and the stream
/// still carries data afterwards.
#[tokio::test]
#[ignore = "isolated idle qualification"]
async fn quiet_open_stream() {
    require_isolated_runner();
    let kind = "quiet_open_stream";
    let listener = listener().await;
    let mut sidecar = Sidecar::start(listener.local_addr().unwrap()).await;
    let client = client().await;
    let mut live = Live::establish(&mut sidecar, &client, &listener).await;
    sleep(Duration::from_millis(SETTLE_MS[0])).await;
    let closed = quiet(
        kind,
        &mut sidecar,
        &client,
        &live.connection,
        Instant::now(),
        NO_STREAM + Duration::from_secs(5),
    )
    .await;
    let before = counters(&live.connection);
    let roundtrip_started = Instant::now();
    let roundtrip = match &closed {
        Some(_) => Err("connection closed".to_owned()),
        None => live.roundtrip(0x31).await,
    };
    emit(json!({
        "kind": kind,
        "phase": "liveness_roundtrip",
        "duration_ms": millis(roundtrip_started.elapsed()),
        "roundtrip_error": roundtrip.as_ref().err(),
        "closed": live.connection.close_reason().is_some(),
        "close_reason": close_reason_now(&live.connection),
        "client_counters": counters_since(&before, &live.connection),
    }));
    drop(live);
    stop_sidecar(sidecar).await;
    close_client(client).await;
    assert!(closed.is_none(), "quiet transport closed: {closed:?}");
    roundtrip.expect("roundtrip after the quiet period");
}

/// Two minutes with every twentieth packet from each bound port dropped. That
/// is periodic 5 % loss, not random loss.
#[tokio::test]
#[ignore = "isolated idle qualification"]
async fn moderate_loss() {
    require_isolated_runner();
    let kind = "moderate_loss";
    let listener = listener().await;
    let mut sidecar = Sidecar::start(listener.local_addr().unwrap()).await;
    let client = client().await;
    let fault = Fault::new(&client, &sidecar);
    fault.clear().await;
    let mut live = Live::establish(&mut sidecar, &client, &listener).await;
    let uncovered = fault.uncovered_ports();
    sleep(Duration::from_millis(SETTLE_MS[0])).await;
    let before = counters(&live.connection);
    let hook_started = Instant::now();
    fault.apply("loss5").await;
    let closed = quiet(
        kind,
        &mut sidecar,
        &client,
        &live.connection,
        Instant::now(),
        Duration::from_secs(120),
    )
    .await;
    let (hook, client_dropped, server_dropped) = fault.clear().await;
    let loss_duration = hook_started.elapsed();
    let client_counters = counters_since(&before, &live.connection);
    let roundtrip = match &closed {
        Some(_) => Err("connection closed".to_owned()),
        None => live.roundtrip(0x32).await,
    };
    emit(json!({
        "kind": kind,
        "phase": "loss_then_roundtrip",
        "mode": "loss5",
        "loss": "periodic_1_in_20_per_rule",
        "duration_ms": millis(loss_duration),
        "roundtrip_error": roundtrip.as_ref().err(),
        "closed": live.connection.close_reason().is_some(),
        "close_reason": close_reason_now(&live.connection),
        "client_counters": client_counters,
        "ports": fault.ports,
        "uncovered_udp_ports": uncovered,
        "hook": hook,
    }));
    drop(live);
    stop_sidecar(sidecar).await;
    close_client(client).await;
    assert!(closed.is_none(), "closed under 5 % loss: {closed:?}");
    roundtrip.expect("roundtrip after the loss period");
    assert!(
        client_dropped > 0 && server_dropped > 0,
        "the fault dropped {client_dropped} client and {server_dropped} server packets"
    );
    assert!(
        uncovered.is_empty(),
        "UDP ports {uncovered:?} bypass the fault"
    );
}

/// Six fresh connections through one both-way outage of `outage_ms`: nothing
/// sent during it (trials 0-2) and one request byte sent just after the cut
/// (trials 3-5), each at 0.1, 0.5 and 0.9 of the sidecar's keep-alive interval
/// after the sidecar last heard from the client.
///
/// A measurement. A connection that closes, or a request that is lost, is
/// recorded as `survived: false` and the next trial runs. The test fails only
/// when setup fails, a UDP port is uncovered, the fault dropped nothing of a
/// request in flight, or `AVDT_IDLE_MIN_OUTAGE_MS` says this outage is short
/// enough that every trial must survive.
async fn outage(outage_ms: u64) {
    require_isolated_runner();
    let keep_alive_ms = env_ms(&["AVDT_IDLE_KEEP_ALIVE_MS"])
        .expect("AVDT_IDLE_KEEP_ALIVE_MS is the keep-alive interval of the sidecar under test");
    let must_survive =
        env_ms(&["AVDT_IDLE_MIN_OUTAGE_MS"]).is_some_and(|threshold| outage_ms <= threshold);
    let outage = Duration::from_millis(outage_ms);
    let mut failures = Vec::new();
    for trial in 0..6u8 {
        let in_flight = trial >= 3;
        let traffic = if in_flight {
            "one_byte_in_flight"
        } else {
            "quiet"
        };
        let phase = Duration::from_millis(keep_alive_ms * [1, 5, 9][usize::from(trial % 3)] / 10);
        let listener = listener().await;
        let mut sidecar = Sidecar::start(listener.local_addr().unwrap()).await;
        let client = client().await;
        let fault = Fault::new(&client, &sidecar);
        // The first hook run also says roughly how long the next one will take.
        let hook_cost_started = Instant::now();
        fault.clear().await;
        let hook_cost = hook_cost_started.elapsed();
        let mut live = Live::establish(&mut sidecar, &client, &listener).await;
        sleep(QNT_SETTLE).await;
        // A received packet restarts the sidecar's keep-alive timer, and the
        // last one it receives here is the client's acknowledgement of the
        // reply: the phase is counted from the end of this roundtrip.
        live.roundtrip(0x60 + trial)
            .await
            .expect("anchor roundtrip");
        let anchor = Instant::now();
        let uncovered = fault.uncovered_ports();
        assert!(
            live.connection.close_reason().is_none(),
            "connection closed before the fault"
        );
        let paths = paths(&live.connection);
        let before = counters(&live.connection);
        // Aim the hook's return, when the cut is in effect, at the phase.
        sleep(phase.saturating_sub(anchor.elapsed() + hook_cost)).await;
        let hook_started = Instant::now();
        fault.apply("both").await;
        let applied = Instant::now();
        let request_byte = 0x70 + trial;
        let sent = if in_flight {
            live.app_sends(request_byte).await
        } else {
            Ok(())
        };
        sleep(outage.saturating_sub(applied.elapsed())).await;
        let outage_at_least = applied.elapsed();
        let (hook, client_dropped, server_dropped) = fault.clear().await;
        let cleared = Instant::now();
        let request = match sent {
            Ok(()) if in_flight => live.complete_roundtrip(request_byte).await,
            sent => sent,
        };
        let request_done = cleared.elapsed();
        let roundtrip = match &request {
            Ok(()) => live.roundtrip(0x50 + trial).await,
            Err(_) => Err("not attempted: the request in flight failed".to_owned()),
        };
        let recovery = if in_flight {
            request_done
        } else {
            cleared.elapsed()
        };
        let recovered = request.is_ok() && roundtrip.is_ok();
        let closed = if recovered {
            timeout(HEALTHY, live.connection.closed()).await.ok()
        } else {
            live.connection.close_reason()
        };
        let survived = recovered && closed.is_none();
        let mut lines = ControlLines::default();
        let sidecar_reason = timeout(Duration::from_millis(250), lines.peer_closed(&mut sidecar))
            .await
            .ok()
            .flatten();
        emit(json!({
            "kind": "outage",
            "mode": "both",
            "trial": trial,
            "traffic": traffic,
            "outage_ms": outage_ms,
            // The cut was fully in effect for at least this long, and no rule
            // existed outside the longer span that includes both hook runs.
            "outage_at_least_ms": millis(outage_at_least),
            "outage_at_most_ms": millis(cleared - hook_started),
            "keep_alive_ms": keep_alive_ms,
            "phase_ms": millis(phase),
            // When the cut took effect, counted from the anchor roundtrip.
            "fault_phase_at_least_ms": millis(hook_started - anchor),
            "fault_phase_at_most_ms": millis(applied - anchor),
            "hook_apply_ms": millis(applied - hook_started),
            "survived": survived,
            "closed": closed.is_some(),
            "close_reason": closed.as_ref().map(close_reason),
            "sidecar_peer_closed": sidecar_reason,
            "request_error": request.as_ref().err(),
            "roundtrip_error": roundtrip.as_ref().err(),
            // From the restore to the request completing, or for a quiet trial
            // to the first roundtrip completing; meaningful when it succeeded.
            "recovery_ms": millis(recovery),
            "must_survive": must_survive,
            "duration_ms": millis(hook_started.elapsed()),
            "client_counters": counters_since(&before, &live.connection),
            "paths_before_fault": paths,
            "ports": fault.ports,
            "uncovered_udp_ports": uncovered,
            "hook": hook,
        }));
        drop(live);
        stop_sidecar(sidecar).await;
        close_client(client).await;
        if !uncovered.is_empty() {
            failures.push(format!(
                "trial {trial}: UDP ports {uncovered:?} bypass the fault"
            ));
        }
        if in_flight && client_dropped == 0 {
            failures.push(format!(
                "trial {trial}: the outage dropped no client packet ({server_dropped} server)"
            ));
        }
        if must_survive && !survived {
            failures.push(format!(
                "trial {trial}: did not survive a {outage_ms} ms outage"
            ));
        }
    }
    assert!(failures.is_empty(), "{outage_ms} ms outage: {failures:?}");
}

#[tokio::test]
#[ignore = "isolated idle qualification"]
async fn outage_1500() {
    outage(1500).await;
}

#[tokio::test]
#[ignore = "isolated idle qualification"]
async fn outage_2500() {
    outage(2500).await;
}

#[tokio::test]
#[ignore = "isolated idle qualification"]
async fn outage_3000() {
    outage(3000).await;
}

#[tokio::test]
#[ignore = "isolated idle qualification"]
async fn outage_3500() {
    outage(3500).await;
}

#[tokio::test]
#[ignore = "isolated idle qualification"]
async fn outage_5000() {
    outage(5000).await;
}
