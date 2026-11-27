//! The native half of the live diagnostic. The caller owns the signed-in,
//! origin-scoped rendezvous adapter; this module never handles its session.
use avibe_desktop_lib::direct::{
    rendezvous::Reply, Client, Fault, FaultCode, RefreshOutcome, SelectedPath, Stage, WindowRegistration,
};
use base64::{engine::general_purpose::STANDARD, Engine};
use http_body_util::{BodyExt, Empty, Limited};
use hyper::{body::Bytes, client::conn::http1, Request};
use hyper_util::rt::TokioIo;
use std::{
    future::Future,
    sync::Arc,
    time::{Duration, Instant},
};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::TcpStream,
    sync::broadcast,
    time::timeout,
};
use tokio_rustls::{
    rustls::{self, pki_types::ServerName},
    TlsConnector,
};

pub struct Live {
    pub client: Client,
    pub registration: WindowRegistration,
    pub retirements: broadcast::Receiver<Fault>,
}

fn report(start: Instant, boundary: &'static str, path: Option<SelectedPath>) {
    let path = match path {
        Some(SelectedPath::Relay) => "relay",
        Some(SelectedPath::Punched(_)) => "punched",
        Some(SelectedPath::Lan(_)) => "lan",
        None => "none",
    };
    println!(
        "{}",
        serde_json::json!({"elapsed_ms":start.elapsed().as_millis(), "boundary":boundary,"path":path})
    );
}
fn failure(start: Instant, kind: &'static str, fault: Fault) {
    println!(
        "{}",
        serde_json::json!({"elapsed_ms":start.elapsed().as_millis(),"boundary":kind,"stage":fault.0,"code":fault.1})
    );
}

pub async fn qualify<F, Fut>(host: &str, instance: String, fetch: F) -> Result<Live, Fault>
where
    F: FnOnce(String) -> Fut,
    Fut: Future<Output = Reply>,
{
    let start = Instant::now();
    let mut client = Client::new(host, instance)
        .await
        .ok_or(Fault(Stage::Handover, FaultCode::ListenerClosed))?;
    let mut retirements = client.retirements();
    let mut steps = client.qualification_steps();
    report(start, "rendezvous_start", None);
    let (outcome, reason) = client
        .refresh_reply_detailed(|asked| async move {
            // The diagnostic's request carries the endpoint only: the
            // client's report of faults and path use is not sent by it.
            let answer = fetch(asked.endpoint).await;
            report(start, "rendezvous_returned", None);
            answer
        })
        .await;
    while let Ok(step) = steps.try_recv() {
        println!(
            "{}",
            serde_json::json!({"elapsed_ms":step.at.saturating_duration_since(start).as_millis(),"boundary":step.kind})
        );
    }
    let result = async {
        if outcome != RefreshOutcome::Renewed {
            return Err(reason.unwrap_or(Fault(Stage::Rendezvous, FaultCode::Other)));
        }
        let handover = client.handover();
        handover.set_direct_only(true);
        let registration = handover
            .window_credential("live-diagnostic")
            .ok_or(Fault(Stage::Handover, FaultCode::ListenerClosed))?;
        health(&client, &registration, host, start).await?;
        Ok(registration)
    }
    .await;
    match result {
        Ok(registration) => Ok(Live {
            client,
            registration,
            retirements,
        }),
        Err(fault) => {
            failure(start, "qualification_failed", fault);
            if let Ok(reason) = retirements.try_recv() {
                failure(start, "retired", reason);
            }
            Err(fault)
        }
    }
}

async fn health(client: &Client, registration: &WindowRegistration, host: &str, start: Instant) -> Result<(), Fault> {
    let route = client
        .route()
        .ok_or(Fault(Stage::Handover, FaultCode::ListenerClosed))?;
    let (user, pass) = registration.native_login();
    let auth = STANDARD.encode(format!("{user}:{pass}"));
    // Never send the session cookie or the proxy credential to the inner origin.
    let socket = timeout(Duration::from_secs(12), async {
        let mut socket = TcpStream::connect(route.proxy)
            .await
            .map_err(|_| Fault(Stage::Handover, FaultCode::Unreachable))?;
        socket
            .write_all(format!("CONNECT {host}:443 HTTP/1.1\r\nProxy-Authorization: Basic {auth}\r\n\r\n").as_bytes())
            .await
            .map_err(|_| Fault(Stage::Connect, FaultCode::Other))?;
        let mut head = Vec::new();
        while head.len() < 4096 && !head.ends_with(b"\r\n\r\n") {
            head.push(
                socket
                    .read_u8()
                    .await
                    .map_err(|_| Fault(Stage::Connect, FaultCode::ConnectionEnded))?,
            );
        }
        if !head.ends_with(b"\r\n\r\n") {
            return Err(Fault(Stage::Connect, FaultCode::InvalidHead));
        }
        if head.starts_with(b"HTTP/1.1 407 ") {
            return Err(Fault(Stage::Connect, FaultCode::ProxyAuth));
        }
        if !head.starts_with(b"HTTP/1.1 200 ") {
            return Err(Fault(Stage::Connect, FaultCode::Status));
        }
        Ok(socket)
    })
    .await
    .map_err(|_| Fault(Stage::Connect, FaultCode::Timeout))??;
    let path = client
        .selected_path()
        .ok_or(Fault(Stage::Handover, FaultCode::NoPermittedPath))?;
    report(start, "strict_listener_committed", Some(path));
    let roots = rustls::RootCertStore::from_iter(webpki_roots::TLS_SERVER_ROOTS.iter().cloned());
    let mut config = rustls::ClientConfig::builder_with_provider(Arc::new(rustls::crypto::ring::default_provider()))
        .with_safe_default_protocol_versions()
        .map_err(|_| Fault(Stage::Tls, FaultCode::Other))?
        .with_root_certificates(roots)
        .with_no_client_auth();
    config.alpn_protocols = vec![b"http/1.1".to_vec()];
    let name = ServerName::try_from(host.to_owned()).map_err(|_| Fault(Stage::Tls, FaultCode::Denied))?;
    let tls = timeout(
        Duration::from_secs(10),
        TlsConnector::from(Arc::new(config)).connect(name, socket),
    )
    .await
    .map_err(|_| Fault(Stage::Tls, FaultCode::Timeout))?
    .map_err(|_| Fault(Stage::Tls, FaultCode::Other))?;
    report(start, "inner_tls_verified", client.selected_path());
    timeout(Duration::from_secs(10), async {
        let (mut sender, driver) = http1::Builder::new()
            .max_buf_size(16 * 1024)
            .handshake(TokioIo::new(tls))
            .await
            .map_err(|_| Fault(Stage::Health, FaultCode::Other))?;
        let work = async {
            let request = Request::get("/health")
                .header("Host", host)
                .header("Cache-Control", "no-store")
                .body(Empty::<Bytes>::new())
                .map_err(|_| Fault(Stage::Health, FaultCode::Other))?;
            let response = sender
                .send_request(request)
                .await
                .map_err(|_| Fault(Stage::Health, FaultCode::ConnectionEnded))?;
            if response.status() != 200 {
                return Err(Fault(Stage::Health, FaultCode::Status));
            }
            let body = Limited::new(response.into_body(), 8192)
                .collect()
                .await
                .map_err(|_| Fault(Stage::Health, FaultCode::Other))?
                .to_bytes();
            if !serde_json::from_slice::<serde_json::Value>(&body).is_ok_and(|v| v["status"] == "ok") {
                return Err(Fault(Stage::Health, FaultCode::Other));
            }
            Ok(())
        };
        tokio::pin!(work);
        tokio::select! {biased; result=&mut work=>result, _=driver=>work.await}
    })
    .await
    .map_err(|_| Fault(Stage::Health, FaultCode::Timeout))??;
    report(start, "health_body_completed", client.selected_path());
    Ok(())
}
