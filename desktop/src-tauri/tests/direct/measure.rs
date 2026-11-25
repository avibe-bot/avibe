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
    let mut times = Samples::new(|| true);
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
    leg.finish(&mut times.times);
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
    let mut times = Samples::new(|| true);
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
    assert_eq!(times.times.len(), 1);
    assert_eq!(epoch.elapsed(), PER_SAMPLE);
}

async fn read_health_get<S: tokio::io::AsyncRead + Unpin>(reader: &mut BufReader<S>) {
    let mut line = String::new();
    reader.read_line(&mut line).await.unwrap();
    assert_eq!(line, "GET /health HTTP/1.1\r\n");
    loop {
        line.clear();
        assert!(reader.read_line(&mut line).await.unwrap() > 0);
        if line == "\r\n" {
            break;
        }
    }
}

#[tokio::test]
async fn intervening_selection_edges_discard_only_the_interrupted_sample() {
    let host = Authority::managed("unit-app.avibe.bot").unwrap();
    let clock = Clock::default();
    let (client, server) = tokio::io::duplex(8192);
    let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel();
    let snapshot = std::sync::atomic::AtomicBool::new(true); // true = original relay
    let mut events = futures_util::stream::poll_fn(|cx| rx.poll_recv(cx)).map(|_selection: bool| true);
    let mut samples = Samples::new(|| snapshot.load(std::sync::atomic::Ordering::SeqCst) && unchanged(&mut events));
    let (result, ()) = tokio::join!(
        requests(client, &host, &clock, clock.now() + TOTAL, &mut samples),
        async {
            let mut server = BufReader::new(server);
            for n in 0..3 {
                read_health_get(&mut server).await;
                if n == 2 {
                    // Direct then relay, without yielding to the pending GET.
                    for relay in [false, true] {
                        snapshot.store(relay, std::sync::atomic::Ordering::SeqCst);
                        tx.send(relay).unwrap();
                    }
                }
                server
                    .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 15\r\n\r\n{\"status\":\"ok\"}")
                    .await
                    .unwrap();
            }
        }
    );
    assert!(matches!(result, Err(Error::Failed)));
    assert_eq!(samples.times.len(), 1);
}

#[tokio::test]
async fn fully_read_unhealthy_response_preserves_first_error_and_uses_remaining_samples() {
    for (status, body) in [("503 Unavailable", "retry"), ("200 OK", "not health")] {
        let host = Authority::managed("unit-app.avibe.bot").unwrap();
        let clock = Clock::default();
        let mut samples = Samples::new(|| true);
        let (client, server) = tokio::io::duplex(8192);
        let (result, ()) = tokio::join!(
            requests(client, &host, &clock, clock.now() + TOTAL, &mut samples),
            async {
                let mut server = BufReader::new(server);
                for n in 0..=COUNT {
                    read_health_get(&mut server).await;
                    let (status, body) = if n == 1 {
                        (status, body)
                    } else {
                        ("200 OK", "{\"status\":\"ok\"}")
                    };
                    server
                        .write_all(
                            format!("HTTP/1.1 {status}\r\nContent-Length: {}\r\n\r\n{body}", body.len()).as_bytes(),
                        )
                        .await
                        .unwrap();
                }
            }
        );
        let leg = samples.finish(Leg::new("tunnel", None, None), result);
        assert_eq!(leg.ok, 4);
        assert!(matches!(leg.error, Some(Error::Failed)));
    }
}

/// All fixture material lives in a temporary directory. No production trust
/// store, network or credential provider is involved. The normal native CI
/// Rust suites run this regression on both macOS and Windows.
#[tokio::test]
async fn tls_validates_the_instance_name_and_chain_before_sending_health() {
    use rustls::pki_types::{CertificateDer, PrivatePkcs8KeyDer};
    let temp = tempfile::tempdir().unwrap();
    let dir = temp.path();
    let conf = "[req]\nprompt=no\ndistinguished_name=dn\n[dn]\nCN=test-only\n[ca]\nbasicConstraints=critical,CA:TRUE\nkeyUsage=critical,keyCertSign\n[leaf]\nbasicConstraints=critical,CA:FALSE\nsubjectAltName=DNS:unit-app.avibe.bot\nextendedKeyUsage=serverAuth\n";
    std::fs::write(dir.join("fixture.cnf"), conf).unwrap();
    let openssl = if cfg!(windows) && std::path::Path::new(r"C:\Program Files\Git\usr\bin\openssl.exe").is_file() {
        r"C:\Program Files\Git\usr\bin\openssl.exe"
    } else {
        "openssl"
    };
    for args in [
        "req -config fixture.cnf -x509 -extensions ca -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 1 -keyout ca.key -out ca.pem",
        "req -config fixture.cnf -subj /CN=unit-app.avibe.bot -new -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -keyout leaf.key -out leaf.csr",
        "x509 -req -in leaf.csr -CA ca.pem -CAkey ca.key -set_serial 2 -days 1 -extfile fixture.cnf -extensions leaf -outform DER -out leaf.der",
        "x509 -in ca.pem -outform DER -out ca.der",
        "pkcs8 -topk8 -nocrypt -in leaf.key -outform DER -out key.der",
    ] {
        let output=std::process::Command::new(openssl).env("OPENSSL_CONF",dir.join("fixture.cnf")).current_dir(dir).args(args.split_ascii_whitespace()).output().expect("test fixture needs openssl");
        assert!(output.status.success(),"OpenSSL fixture command failed: {}",String::from_utf8_lossy(&output.stderr));
    }
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
        let mut times = Samples::new(|| true);
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
        assert_eq!(times.times.len(), if succeeds { COUNT } else { 0 });
    }
}
