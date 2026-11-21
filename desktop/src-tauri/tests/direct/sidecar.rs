//! Use only the immutable CI fixture verified by direct_sidecar_fixture.py.
//! Replace the sidecar's advertised interface with loopback in this test's
//! private offer. The production answer parser never admits loopback.
use super::{
    clock::Clock,
    connect::Authority,
    offer::Offer,
    rendezvous::Reply,
    transport::{self, Link},
    Client, RefreshOutcome,
};
use serde_json::{json, Value};
use std::{
    net::{Ipv4Addr, SocketAddr},
    process::Stdio,
    sync::{
        atomic::{AtomicU64, Ordering},
        Arc,
    },
    time::Duration,
};
use tokio::{
    io::{AsyncBufReadExt, AsyncReadExt, AsyncWriteExt, BufReader},
    net::{TcpListener, TcpStream},
    process::Command,
    time::timeout,
};

#[tokio::test]
#[ignore = "requires verified pinned release fixture; required in desktop-shell CI"]
async fn desktop_direct_real_sidecar() {
    for changed_selection in [false, true] {
        timeout(Duration::from_secs(30), run(changed_selection))
            .await
            .expect("loopback interop timed out");
    }
}
async fn run(changed_selection: bool) {
    let exe = std::env::var_os("AVIBE_TEST_DIRECT_SIDECAR").expect("missing verified fixture");
    let ingress = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).await.unwrap();
    let mut child = Command::new(exe)
        .kill_on_drop(true)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .expect("start fixture");
    let mut control = child.stdin.take().unwrap();
    let mut events = BufReader::new(child.stdout.take().unwrap()).lines();
    let token = "ef".repeat(32);
    let start = json!({"type":"start","protocol":1,"alpn":"avibe-direct/1","token":token,"stream_addr":ingress.local_addr().unwrap().to_string(),"max_connections":8,"max_streams_per_connection":8});
    control.write_all(format!("{start}\n").as_bytes()).await.unwrap();
    let ready: Value = serde_json::from_str(&events.next_line().await.unwrap().unwrap()).unwrap();
    assert_eq!(ready["type"], "ready");
    let port = ready["addresses"]
        .as_array()
        .unwrap()
        .iter()
        .filter_map(|v| v.as_str()?.parse::<SocketAddr>().ok())
        .find(|s| s.is_ipv4())
        .expect("IPv4 fixture")
        .port();
    let mut client = Client::new("desktop-test-app.avibe.bot", "fixture".into())
        .await
        .unwrap();
    let elapsed = Arc::new(AtomicU64::new(0));
    let ticks = elapsed.clone();
    client.clock = Clock(Arc::new(move || Duration::from_secs(ticks.load(Ordering::SeqCst))));
    let who = client.key.public();
    let ep = transport::bind(client.key.clone()).await.unwrap();
    assert!(ep.id() == who);
    let admit = json!({"type":"admission","endpoint_id":who.to_string(),"ttl_seconds":30});
    control.write_all(format!("{admit}\n").as_bytes()).await.unwrap();
    let host = Authority::managed("desktop-test-app.avibe.bot").unwrap();
    let bytes = host.direct_head();
    let ingress_task = tokio::spawn(async move {
        for leg in 0..2 {
            let (socket, _) = ingress.accept().await.unwrap();
            let mut r = BufReader::new(socket);
            let mut preface = String::new();
            r.read_line(&mut preface).await.unwrap();
            assert!(preface == format!("AVDT1 {token} {who}\n"));
            let mut req = vec![0; bytes.len()];
            r.read_exact(&mut req).await.unwrap();
            assert!(req == bytes);
            r.get_mut().write_all(b"HTTP/1.1 200 OK\r\n\r\n").await.unwrap();
            if leg == 1 {
                let mut data = [0; 6];
                r.read_exact(&mut data).await.unwrap();
                assert_eq!(&data, b"opaque");
                r.get_mut().write_all(b"answer").await.unwrap();
                // Hold the ingress end open: only client retirement may end the stream.
                let mut end = [0];
                assert!(matches!(r.read(&mut end).await, Ok(0) | Err(_)));
            }
        }
    });
    let peer = ready["endpoint_id"].as_str().unwrap().parse().unwrap();
    let link = loop {
        let offer = Offer {
            generation: "ci".into(),
            id: peer,
            candidates: vec![(Ipv4Addr::LOCALHOST, port).into()],
            until: client.clock.now() + Duration::from_secs(20),
        };
        if let Some(l) = Link::dial(&ep, offer, &host, client.clock.clone()).await {
            break l;
        }
        tokio::time::sleep(Duration::from_millis(30)).await;
    };
    // A fresh grant for this exact endpoint, generation and candidates keeps
    // the existing authenticated connection. A changed generation must redial.
    let renewed_until = client.clock.now() + Duration::from_secs(25);
    let mut renewal = Offer {
        generation: "ci".into(),
        id: peer,
        candidates: vec![(Ipv4Addr::LOCALHOST, port).into()],
        until: renewed_until,
    };
    let mut deadlines = link.deadlines();
    assert!(link.renew(&renewal));
    deadlines.changed().await.unwrap();
    assert_eq!(*deadlines.borrow_and_update(), renewed_until);
    renewal.generation = "replacement".into();
    assert!(!link.renew(&renewal));
    assert!(!deadlines.has_changed().unwrap());
    client.endpoint = Some(ep);
    client.publish(link.clone());
    use base64::{engine::general_purpose::STANDARD, Engine};
    let (user, pass) = client.listener.credential.native_login();
    let auth = STANDARD.encode(format!("{user}:{pass}"));
    let mut socket = TcpStream::connect((Ipv4Addr::LOCALHOST, client.listener.port))
        .await
        .unwrap();
    socket
        .write_all(
            format!("CONNECT desktop-test-app.avibe.bot:443 HTTP/1.1\r\nProxy-Authorization: Basic {auth}\r\n\r\n")
                .as_bytes(),
        )
        .await
        .unwrap();
    let mut r = BufReader::new(socket);
    let mut line = String::new();
    r.read_line(&mut line).await.unwrap();
    assert!(line.starts_with("HTTP/1.1 200 "));
    line.clear();
    r.read_line(&mut line).await.unwrap();
    assert_eq!(line, "\r\n");
    elapsed.store(23, Ordering::SeqCst);
    let retained = RefreshOutcome::Retained {
        retry_before: Duration::from_secs(1),
    };
    for reply in [Reply::NotReady, Reply::Failed, Reply::Refused(503)] {
        assert_eq!(client.refresh_reply(|_| async { reply }).await, retained);
        assert!(!deadlines.has_changed().unwrap());
    }
    if !changed_selection {
        assert_eq!(client.refresh_reply(|_| std::future::pending()).await, retained);
    }
    assert_eq!(*deadlines.borrow(), renewed_until);
    // Same established stream, after the failed renewals; no CONNECT replay.
    r.get_mut().write_all(b"opaque").await.unwrap();
    let mut answer = [0; 6];
    r.read_exact(&mut answer).await.unwrap();
    assert_eq!(&answer, b"answer");
    let mut changes = client.links.subscribe();
    if changed_selection {
        // Same live grant, still before expiry. A known lost binding is final.
        assert_eq!(
            client.refresh_reply(|_| async { Reply::SelectionChanged }).await,
            RefreshOutcome::Retired
        );
    } else {
        // Simulate suspend while the Tokio timer has not elapsed. No refresh
        // or path event is required to retire the idle connection on resume.
        elapsed.store(30, Ordering::SeqCst);
        assert!(!link.usable());
        assert!(!link.renew(&Offer {
            until: Duration::from_secs(60),
            generation: "ci".into(),
            id: peer,
            candidates: link.offer.candidates.clone()
        }));
    }
    timeout(Duration::from_secs(1), changes.wait_for(Option::is_none))
        .await
        .unwrap()
        .unwrap();
    assert_eq!(
        client.refresh_reply(|_| async { Reply::Failed }).await,
        RefreshOutcome::Retired
    );
    let mut end = [0];
    assert!(matches!(r.read(&mut end).await, Ok(0) | Err(_)));
    ingress_task.await.unwrap();
    drop(client);
    control.write_all(b"{\"type\":\"stop\"}\n").await.unwrap();
    assert!(child.wait().await.unwrap().success());
}
