use super::*;
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};

#[tokio::test]
async fn bridge_future_is_send_busy_is_immediate_and_nonmanaged_hosts_make_no_attempt() {
    fn assert_send<T: Send>(value: T) -> T {
        value
    }
    let own = SLOT.try_acquire().unwrap();
    assert!(assert_send(measure("unit-app.avibe.bot", None)).await.is_err());
    drop(own);
    let result = assert_send(measure("https://not-a-host.invalid/path", None))
        .await
        .unwrap();
    let shape = serde_json::to_value(result).unwrap();
    assert_eq!(shape["current"], serde_json::Value::Null);
    assert_eq!(shape["tunnel"], serde_json::Value::Null);
    assert_eq!(shape["basis"], "warm-https-health");
    assert_eq!(shape["samples"], 5);
    assert!(shape["measuredAt"].as_u64().unwrap() > 0);
}

#[tokio::test(start_paused = true)]
async fn six_gets_use_one_socket_and_measure_the_end_of_a_chunked_body() {
    let host = Authority::managed("unit-app.avibe.bot").unwrap();
    let (client, server) = tokio::io::duplex(8192);
    let epoch = tokio::time::Instant::now();
    let clock = Clock(Arc::new(move || epoch.elapsed()));
    let mut times = Vec::new();
    let (result, ()) = tokio::join!(requests(client, &host, &clock, TOTAL, &mut times), async {
        let mut server = BufReader::new(server);
        for delay in [900, 10, 40, 20, 50, 30] {
            let mut line = String::new();
            server.read_line(&mut line).await.unwrap();
            assert_eq!(line, "GET /health HTTP/1.1\r\n");
            let mut headers = String::new();
            loop {
                line.clear();
                assert!(server.read_line(&mut line).await.unwrap() > 0);
                if line == "\r\n" {
                    break;
                }
                headers.push_str(&line.to_ascii_lowercase());
            }
            assert!(headers.contains("host: unit-app.avibe.bot\r\n"));
            assert!(!headers.contains("authorization:"));
            assert!(!headers.contains("cookie:"));
            server
                .write_all(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\nf\r\n{\"status\":\"ok\"}\r\n")
                .await
                .unwrap();
            tokio::time::sleep(Duration::from_millis(delay)).await;
            server.write_all(b"0\r\n\r\n").await.unwrap();
        }
    });
    result.unwrap();
    let mut leg = Leg::new("tunnel", None, None);
    leg.finish(&mut times);
    assert_eq!(leg.ok, 5);
    assert_eq!(leg.median_ms, Some(30.));
    assert_eq!(leg.max_ms, Some(50.));
}

#[tokio::test(start_paused = true)]
async fn stalled_body_times_out_without_reopening_or_counting_warmup() {
    let host = Authority::managed("unit-app.avibe.bot").unwrap();
    let (client, server) = tokio::io::duplex(8192);
    let epoch = tokio::time::Instant::now();
    let clock = Clock(Arc::new(move || epoch.elapsed()));
    let mut times = Vec::new();
    let (result, ()) = tokio::join!(requests(client, &host, &clock, TOTAL, &mut times), async {
        let mut server = BufReader::new(server);
        for n in 0..3 {
            let mut line = String::new();
            loop {
                line.clear();
                assert!(server.read_line(&mut line).await.unwrap() > 0);
                if line == "\r\n" {
                    break;
                }
            }
            server
                .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 15\r\n\r\n")
                .await
                .unwrap();
            if n != 2 {
                server.write_all(b"{\"status\":\"ok\"}").await.unwrap();
            }
        }
        let mut line = String::new();
        assert_eq!(server.read_line(&mut line).await.unwrap(), 0);
    });
    assert!(matches!(result, Err(Error::Timeout)));
    assert_eq!(times.len(), 1);
    assert_eq!(epoch.elapsed(), PER_SAMPLE);
}

/// Local integration with an ephemeral test CA (never added to the OS trust store).
/// Files: ca.der, leaf.der for unit-app.avibe.bot, and its PKCS#8 key.der.
#[tokio::test]
#[ignore = "requires disposable AVIBE_TEST_MEASURE_TLS DER fixtures"]
async fn tls_validates_the_instance_name_and_chain_before_sending_health() {
    use rustls::pki_types::{CertificateDer, PrivatePkcs8KeyDer};
    let dir = std::path::PathBuf::from(std::env::var_os("AVIBE_TEST_MEASURE_TLS").expect("missing TLS fixture"));
    let read = |name| std::fs::read(dir.join(name)).unwrap();
    for (name, trust, succeeds) in [
        ("unit-app.avibe.bot", true, true),
        ("other-app.avibe.bot", true, false),
        ("unit-app.avibe.bot", false, false),
    ] {
        let host = Authority::managed(name).unwrap();
        let mut roots = rustls::RootCertStore::empty();
        if trust {
            roots.add(CertificateDer::from(read("ca.der"))).unwrap()
        }
        let server_config =
            rustls::ServerConfig::builder_with_provider(Arc::new(rustls::crypto::ring::default_provider()))
                .with_safe_default_protocol_versions()
                .unwrap()
                .with_no_client_auth()
                .with_single_cert(
                    vec![CertificateDer::from(read("leaf.der"))],
                    PrivatePkcs8KeyDer::from(read("key.der")).into(),
                )
                .unwrap();
        let (client, server) = tokio::io::duplex(8192);
        let clock = Clock::default();
        let end = clock.now() + TOTAL;
        let mut times = Vec::new();
        let connector = if trust { tls_config(roots) } else { tls() };
        let (result, seen) = tokio::join!(
            bounded(
                &clock,
                end,
                probe(
                    &host,
                    connector,
                    async { Some(Box::new(client) as Stream) },
                    &clock,
                    end,
                    &mut times
                )
            ),
            async {
                let Ok(stream) = tokio_rustls::TlsAcceptor::from(Arc::new(server_config))
                    .accept(server)
                    .await
                else {
                    return 0;
                };
                let mut stream = BufReader::new(stream);
                let mut seen = 0;
                for _ in 0..=COUNT {
                    let mut line = String::new();
                    if stream.read_line(&mut line).await.unwrap_or(0) == 0 {
                        break;
                    }
                    assert_eq!(line, "GET /health HTTP/1.1\r\n");
                    loop {
                        line.clear();
                        assert!(stream.read_line(&mut line).await.unwrap() > 0);
                        if line == "\r\n" {
                            break;
                        }
                    }
                    seen += 1;
                    stream
                        .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 15\r\n\r\n{\"status\":\"ok\"}")
                        .await
                        .unwrap();
                }
                seen
            }
        );
        assert_eq!(result.is_ok(), succeeds);
        assert_eq!(seen, if succeeds { COUNT + 1 } else { 0 });
        assert_eq!(times.len(), if succeeds { COUNT } else { 0 });
    }
}
