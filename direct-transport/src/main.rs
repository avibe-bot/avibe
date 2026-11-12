mod admission;
mod handshake_limit;

use admission::Admissions;
use handshake_limit::{HandshakeLimit, HandshakePermit};
use std::{net::SocketAddr, sync::Arc, time::Duration};

use iroh::{
    Endpoint, EndpointAddr, RelayMode, Watcher,
    endpoint::{Incoming, IncomingAddr, PortmapperConfig, QuicTransportConfig, VarInt, presets},
};
use serde::{Deserialize, Serialize};
use tokio::{
    io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt, BufReader},
    net::TcpStream,
    sync::{Mutex, Semaphore, watch},
    task::JoinSet,
    time::{self, Instant},
};

const ALPN: &str = "avibe-direct/1";
const MAX_CONTROL_LINE: usize = 4096;
const HANDSHAKE_TIMEOUT: Duration = Duration::from_secs(5);
const NO_STREAM_TIMEOUT: Duration = Duration::from_secs(5 * 60);
const TRANSPORT_IDLE_TIMEOUT_MS: u32 = 6_000;
const TRANSPORT_KEEP_ALIVE: Duration = Duration::from_millis(1_500);
const IO_TIMEOUT: Duration = Duration::from_secs(2);

fn close_code() -> VarInt {
    VarInt::from_u32(1)
}

#[derive(Clone)]
struct StopSignal(watch::Sender<bool>);

impl StopSignal {
    fn new() -> Self {
        Self(watch::channel(false).0)
    }
    fn stop(&self) {
        self.0.send_replace(true);
    }
    async fn wait(&self) {
        let mut receiver = self.0.subscribe();
        let _ = receiver.wait_for(|stopped| *stopped).await;
    }
}

#[derive(Debug, Clone, Copy)]
struct SidecarError(&'static str);
type Result<T> = std::result::Result<T, SidecarError>;

#[derive(Deserialize)]
#[serde(tag = "type", deny_unknown_fields)]
enum Command {
    #[serde(rename = "start")]
    Start {
        protocol: u32,
        alpn: String,
        stream_addr: String,
        token: String,
        max_connections: u32,
        max_streams_per_connection: u32,
    },
    #[serde(rename = "admission")]
    Admission {
        endpoint_id: String,
        ttl_seconds: u64,
    },
    #[serde(rename = "stop")]
    Stop,
}

struct StartConfig {
    stream_addr: SocketAddr,
    token: String,
    max_connections: u32,
    max_streams_per_connection: u32,
}

#[derive(Serialize)]
#[serde(tag = "type")]
enum Message<'a> {
    #[serde(rename = "ready")]
    Ready {
        protocol: u32,
        endpoint_id: String,
        addresses: Vec<String>,
    },
    #[serde(rename = "addresses")]
    Addresses { addresses: Vec<String> },
    #[serde(rename = "peer_closed")]
    PeerClosed {
        endpoint_id: &'a str,
        reason: &'static str,
    },
    #[serde(rename = "fatal")]
    Fatal { error: &'static str },
}

#[derive(Clone)]
struct Control {
    stdout: Arc<Mutex<tokio::io::Stdout>>,
    admissions: Admissions,
    stop: StopSignal,
    failure: Arc<Mutex<Option<SidecarError>>>,
}

impl Control {
    fn new() -> Self {
        Self {
            stdout: Arc::new(Mutex::new(tokio::io::stdout())),
            admissions: Admissions::new(),
            stop: StopSignal::new(),
            failure: Arc::new(Mutex::new(None)),
        }
    }

    async fn emit(&self, value: &Message<'_>) -> Result<()> {
        let mut line =
            serde_json::to_vec(value).map_err(|_| SidecarError("output_encode_failed"))?;
        line.push(b'\n');
        if line.len() > MAX_CONTROL_LINE {
            return Err(SidecarError("output_line_too_large"));
        }
        time::timeout(IO_TIMEOUT, async {
            let mut stdout = self.stdout.lock().await;
            stdout
                .write_all(&line)
                .await
                .map_err(|_| SidecarError("stdout_failed"))?;
            stdout
                .flush()
                .await
                .map_err(|_| SidecarError("stdout_failed"))
        })
        .await
        .map_err(|_| SidecarError("stdout_timeout"))?
    }

    async fn fail(&self, error: SidecarError) {
        self.failure.lock().await.get_or_insert(error);
        self.stop.stop();
    }

    async fn fatal(&self, error: SidecarError) {
        // End networking even if the instance has stopped reading stdout.
        self.fail(error).await;
        let _ = self.emit(&Message::Fatal { error: error.0 }).await;
    }

    async fn peer_closed(&self, endpoint_id: &str, reason: &'static str) {
        if let Err(error) = self
            .emit(&Message::PeerClosed {
                endpoint_id,
                reason,
            })
            .await
        {
            self.fail(error).await;
        }
    }
}

fn valid_endpoint_id(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

fn parse_start(command: Command) -> Result<StartConfig> {
    let Command::Start {
        protocol,
        alpn,
        stream_addr,
        token,
        max_connections,
        max_streams_per_connection,
    } = command
    else {
        return Err(SidecarError("start_required"));
    };
    if protocol != 1 || alpn != ALPN {
        return Err(SidecarError("invalid_start"));
    }
    let stream_addr = stream_addr
        .parse::<SocketAddr>()
        .map_err(|_| SidecarError("invalid_stream_addr"))?;
    if !stream_addr.ip().is_loopback() || stream_addr.port() == 0 {
        return Err(SidecarError("invalid_stream_addr"));
    }
    if token.len() != 64 || !token.bytes().all(|b| b.is_ascii_hexdigit()) {
        return Err(SidecarError("invalid_token"));
    }
    if !(1..=64).contains(&max_connections) || !(1..=64).contains(&max_streams_per_connection) {
        return Err(SidecarError("invalid_limits"));
    }
    Ok(StartConfig {
        stream_addr,
        token: token.to_ascii_lowercase(),
        max_connections,
        max_streams_per_connection,
    })
}

async fn read_command<R: AsyncRead + Unpin>(reader: &mut R) -> Result<Option<Command>> {
    // The cap includes LF. Never use unbounded read_line/lines on this boundary.
    let mut line = Vec::with_capacity(MAX_CONTROL_LINE);
    for _ in 0..MAX_CONTROL_LINE {
        let byte = match reader.read_u8().await {
            Ok(byte) => byte,
            Err(error) if error.kind() == std::io::ErrorKind::UnexpectedEof => {
                return if line.is_empty() {
                    Ok(None)
                } else {
                    Err(SidecarError("control_unterminated"))
                };
            }
            Err(_) => return Err(SidecarError("control_read_failed")),
        };
        if byte == b'\n' {
            return serde_json::from_slice(&line)
                .map(Some)
                .map_err(|_| SidecarError("control_invalid_json"));
        }
        line.push(byte);
    }
    Err(SidecarError("control_line_too_large"))
}

async fn run_control_reader<R: AsyncRead + Unpin>(mut reader: R, control: Control) {
    loop {
        match read_command(&mut reader).await {
            Ok(Some(Command::Admission {
                endpoint_id,
                ttl_seconds,
            })) => {
                if !valid_endpoint_id(&endpoint_id) {
                    control.fail(SidecarError("invalid_endpoint_id")).await;
                    return;
                }
                control.admissions.update(endpoint_id, ttl_seconds);
            }
            Ok(None) | Ok(Some(Command::Stop)) => {
                control.stop.stop();
                return;
            }
            Ok(Some(Command::Start { .. })) => {
                control.fail(SidecarError("start_must_be_first")).await;
                return;
            }
            Err(error) => {
                control.fail(error).await;
                return;
            }
        }
    }
}

fn endpoint_addresses(address: &EndpointAddr) -> Vec<String> {
    address.ip_addrs().map(ToString::to_string).collect()
}

async fn watch_addresses<W: Watcher<Value = EndpointAddr> + Send + 'static>(
    mut watcher: W,
    mut previous: EndpointAddr,
    control: Control,
) {
    loop {
        tokio::select! {
            _ = control.stop.wait() => return,
            updated = watcher.updated() => {
                let Ok(address) = updated else {
                    control.fail(SidecarError("address_watcher_closed")).await;
                    return;
                };
                if address == previous { continue; }
                previous = address.clone();
                if let Err(error) = control.emit(&Message::Addresses { addresses: endpoint_addresses(&address) }).await {
                    control.fail(error).await;
                    return;
                }
            }
        }
    }
}

async fn copy_half<R: AsyncRead + Unpin, W: AsyncWrite + Unpin>(
    reader: &mut R,
    writer: &mut W,
) -> Result<()> {
    // Tokio copy has a fixed 8 KiB buffer and awaits the destination's capacity.
    tokio::io::copy(reader, writer)
        .await
        .map_err(|_| SidecarError("stream_copy_failed"))?;
    writer
        .shutdown()
        .await
        .map_err(|_| SidecarError("stream_copy_failed"))
}

fn reset_stream(send: &mut iroh::endpoint::SendStream, recv: &mut iroh::endpoint::RecvStream) {
    let _ = send.reset(close_code());
    let _ = recv.stop(close_code());
}

async fn forward_stream(
    mut send: iroh::endpoint::SendStream,
    mut recv: iroh::endpoint::RecvStream,
    config: Arc<StartConfig>,
    endpoint_id: String,
    _permit: tokio::sync::OwnedSemaphorePermit,
) -> Result<()> {
    let mut tcp = match time::timeout(IO_TIMEOUT, TcpStream::connect(config.stream_addr)).await {
        Ok(Ok(tcp)) => tcp,
        _ => {
            reset_stream(&mut send, &mut recv);
            return Err(SidecarError("stream_connect_failed"));
        }
    };
    let header = format!("AVDT1 {} {endpoint_id}\n", config.token);
    if !matches!(
        time::timeout(IO_TIMEOUT, tcp.write_all(header.as_bytes())).await,
        Ok(Ok(()))
    ) {
        reset_stream(&mut send, &mut recv);
        let _ = tcp.set_zero_linger();
        return Err(SidecarError("stream_write_failed"));
    }
    let result = {
        let (mut tcp_reader, mut tcp_writer) = tcp.split();
        tokio::try_join!(
            copy_half(&mut recv, &mut tcp_writer),
            copy_half(&mut tcp_reader, &mut send)
        )
        .map(|_| ())
    };
    if result.is_err() {
        // SendStream's Drop implicitly finishes; preserve errors as resets instead.
        reset_stream(&mut send, &mut recv);
        let _ = tcp.set_zero_linger();
    }
    result
}

async fn handle_connection(
    incoming: Incoming,
    handshake_permit: HandshakePermit,
    control: Control,
    _connection_permit: tokio::sync::OwnedSemaphorePermit,
    config: Arc<StartConfig>,
) {
    let connection = match time::timeout(HANDSHAKE_TIMEOUT, incoming).await {
        Ok(Ok(connection)) => connection,
        _ => return,
    };
    drop(handshake_permit);
    let endpoint_id = connection.remote_id().to_string();
    if !control.admissions.allows_connection(&connection) {
        connection.close(close_code(), b"admission ended");
        control.peer_closed(&endpoint_id, "admission_ended").await;
        return;
    }
    connection.set_max_concurrent_bi_streams(VarInt::from_u32(config.max_streams_per_connection));
    let stream_permits = Arc::new(Semaphore::new(config.max_streams_per_connection as usize));
    let mut stream_tasks = JoinSet::new();
    let no_stream = time::sleep(NO_STREAM_TIMEOUT);
    tokio::pin!(no_stream);
    let mut reason = "connection_closed";
    loop {
        tokio::select! {
            _ = control.stop.wait() => { reason = "stopped"; break; }
            _ = &mut no_stream, if stream_tasks.is_empty() => { reason = "idle_timeout"; break; }
            incoming_stream = connection.accept_bi() => {
                let Ok((mut send, mut recv)) = incoming_stream else { break; };
                if !control.admissions.allows_connection(&connection) {
                    reset_stream(&mut send, &mut recv);
                    reason = "admission_ended";
                    break;
                }
                let Ok(permit) = stream_permits.clone().try_acquire_owned() else {
                    reset_stream(&mut send, &mut recv);
                    continue;
                };
                let config = Arc::clone(&config);
                let peer = endpoint_id.clone();
                stream_tasks.spawn(async move { let _ = forward_stream(send, recv, config, peer, permit).await; });
            }
            _ = stream_tasks.join_next(), if !stream_tasks.is_empty() => {
                if stream_tasks.is_empty() { no_stream.as_mut().reset(Instant::now() + NO_STREAM_TIMEOUT); }
            }
        }
    }
    if reason == "connection_closed" && !control.admissions.allows_connection(&connection) {
        reason = "admission_ended";
    }
    connection.close(close_code(), reason.as_bytes());
    stream_tasks.abort_all();
    while stream_tasks.join_next().await.is_some() {}
    control.peer_closed(&endpoint_id, reason).await;
}

async fn run(control: &Control) -> Result<()> {
    let mut stdin = BufReader::new(tokio::io::stdin());
    let first = read_command(&mut stdin)
        .await?
        .ok_or(SidecarError("start_required"))?;
    let config = Arc::new(parse_start(first)?);
    let transport_config = QuicTransportConfig::builder()
        .max_concurrent_bidi_streams(VarInt::from_u32(0))
        .max_concurrent_uni_streams(VarInt::from_u32(0))
        .stream_receive_window(VarInt::from_u32(64 * 1024))
        .receive_window(VarInt::from_u32(4 * 1024 * 1024))
        .send_window(4 * 1024 * 1024)
        .datagram_receive_buffer_size(None)
        // Negotiate the idle timeout with an unmodified client. These are
        // failure-detection/tolerance settings, not the no-open-stream policy.
        .max_idle_timeout(Some(VarInt::from_u32(TRANSPORT_IDLE_TIMEOUT_MS).into()))
        .keep_alive_interval(TRANSPORT_KEEP_ALIVE)
        .default_path_keep_alive_interval(TRANSPORT_KEEP_ALIVE)
        // Stock iroh ignores values below eight: zero does NOT disable it.
        .max_remote_nat_traversal_addresses(8)
        .build();
    let endpoint = Endpoint::builder(presets::Minimal)
        .relay_mode(RelayMode::Disabled)
        .portmapper_config(PortmapperConfig::Disabled)
        .transport_config(transport_config)
        .hooks(control.admissions.clone())
        .alpns(vec![ALPN.as_bytes().to_vec()])
        .bind()
        .await
        .map_err(|_| SidecarError("endpoint_bind_failed"))?;

    let mut endpoint_watcher = endpoint.watch_addr();
    let ready_address = endpoint_watcher.get();
    if let Err(error) = control
        .emit(&Message::Ready {
            protocol: 1,
            endpoint_id: endpoint.id().to_string(),
            addresses: endpoint_addresses(&ready_address),
        })
        .await
    {
        let _ = time::timeout(IO_TIMEOUT, endpoint.close()).await;
        return Err(error);
    }
    let address_task = tokio::spawn(watch_addresses(
        endpoint_watcher,
        ready_address,
        control.clone(),
    ));
    let admissions = control.admissions.clone();
    let expiry_stop = control.stop.clone();
    let expiry_task = tokio::spawn(async move { admissions.run_expiration(expiry_stop).await });
    let control_task = tokio::spawn(run_control_reader(stdin, control.clone()));
    let handshake_limit = HandshakeLimit::new();
    let connection_limit = Arc::new(Semaphore::new(config.max_connections as usize));
    let mut connection_tasks = JoinSet::new();
    loop {
        tokio::select! {
            biased;
            _ = control.stop.wait() => break,
            // Drain finished tasks before more arrivals, so an inbound flood
            // cannot retain an unbounded backlog of completed JoinSet entries.
            _ = connection_tasks.join_next(), if !connection_tasks.is_empty() => {},
            incoming = endpoint.accept() => {
                let Some(incoming) = incoming else {
                    control.fail(SidecarError("endpoint_closed")).await;
                    break;
                };
                // Stateless Retry verifies the source before it can consume a
                // source bucket or hold a pending-handshake slot with a spoofed IP.
                if !incoming.remote_addr_validated() {
                    let _ = incoming.retry();
                    continue;
                }
                let Ok(permit) = connection_limit.clone().try_acquire_owned() else { incoming.ignore(); continue; };
                let IncomingAddr::Ip(source) = incoming.remote_addr() else { incoming.ignore(); continue; };
                let Some(handshake_permit) = handshake_limit.acquire(source.ip()) else { incoming.ignore(); continue; };
                connection_tasks.spawn(handle_connection(incoming, handshake_permit, control.clone(), permit, Arc::clone(&config)));
            }
        }
    }
    control.stop.stop();
    // Let each handler close QUIC before dropping its streams; only unresponsive
    // handlers are aborted, rather than turning truncated streams into FINs.
    let _ = time::timeout(IO_TIMEOUT, async {
        while connection_tasks.join_next().await.is_some() {}
    })
    .await;
    connection_tasks.abort_all();
    while connection_tasks.join_next().await.is_some() {}
    let _ = time::timeout(IO_TIMEOUT, endpoint.close()).await;
    address_task.abort();
    expiry_task.abort();
    control_task.abort();
    let _ = address_task.await;
    let _ = expiry_task.await;
    let _ = control_task.await;
    match *control.failure.lock().await {
        Some(error) => Err(error),
        None => Ok(()),
    }
}

#[tokio::main(flavor = "multi_thread", worker_threads = 2)]
async fn main() {
    let control = Control::new();
    let status = match run(&control).await {
        Ok(()) => 0,
        Err(error) => {
            control.fatal(error).await;
            eprintln!("fatal: {}", error.0);
            1
        }
    };
    // Tokio stdio uses uncancellable blocking I/O. Explicit exit after bounded
    // endpoint cleanup prevents still-open stdin/stdout from hanging shutdown.
    std::process::exit(status);
}
