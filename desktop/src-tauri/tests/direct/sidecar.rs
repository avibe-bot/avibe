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
    // The offer below deliberately replaces the advertised interface with loopback.
    // A wildcard client lets iroh migrate to that real interface mid-refresh;
    // the production monitor then correctly retires the unoffered path (#4149).
    let ep = transport::endpoint_builder(client.key.clone(), None)
        .clear_ip_transports()
        .bind_addr_with_opts(
            (Ipv4Addr::LOCALHOST, 0),
            iroh::endpoint::BindOpts::default()
                .set_prefix_len(32)
                .set_is_default_route(false),
        )
        .unwrap()
        .bind()
        .await
        .unwrap();
    let sockets = ep.bound_sockets();
    assert_eq!(sockets.len(), 1, "fixture must not have a wildcard or IPv6 socket");
    assert_eq!(sockets[0].ip(), Ipv4Addr::LOCALHOST);
    assert!(ep.id() == who);
    let admit = json!({"type":"admission","endpoint_id":who.to_string(),"ttl_seconds":30});
    control.write_all(format!("{admit}\n").as_bytes()).await.unwrap();
    let host = Authority::managed("desktop-test-app.avibe.bot").unwrap();
    let bytes = host.direct_head();
    let ingress_task = tokio::spawn(async move {
        for leg in 0..3 {
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
                let mut byte = [0];
                r.read_exact(&mut byte).await.unwrap();
                assert_eq!(&byte, b"w");
                r.get_mut().write_all(&byte).await.unwrap();
                assert_retired_read(r.read(&mut byte).await);
            }
            if leg == 2 {
                let mut data = [0; 6];
                r.read_exact(&mut data).await.unwrap();
                assert_eq!(&data, b"opaque");
                r.get_mut().write_all(b"answer").await.unwrap();
                // Hold the ingress end open: only client retirement may end the stream.
                let mut end = [0];
                assert_retired_read(r.read(&mut end).await);
            }
        }
    });
    let peer = ready["endpoint_id"].as_str().unwrap().parse().unwrap();
    let link = loop {
        let offer = Offer {
            generation: "ci".into(),
            id: peer,
            relay: None,
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
        relay: None,
        candidates: vec![(Ipv4Addr::LOCALHOST, port).into()],
        until: renewed_until,
    };
    let mut deadlines = link.deadlines();
    assert!(link.renew(&renewal));
    deadlines.changed().await.unwrap();
    assert_eq!(*deadlines.borrow_and_update(), renewed_until);
    renewal.relay = super::path::TrialRelay::offered("public-trial-v1", "https://aps1-1.relay.n0.iroh.link./");
    assert!(renewal.relay.is_some());
    assert!(
        !link.renew(&renewal),
        "changing relay authority cannot extend an old endpoint"
    );
    renewal.relay = None;
    renewal.generation = "replacement".into();
    assert!(!link.renew(&renewal));
    assert!(!deadlines.has_changed().unwrap());
    let public = if !changed_selection {
        let mut other = Client::new("other-app.avibe.bot", "other".into()).await.unwrap();
        let a = super::handover_tests::public_browser(&mut client).await;
        let b = super::handover_tests::public_browser(&mut other).await;
        Some((a, b, other))
    } else {
        None
    };
    let handover = client.handover();
    let view = handover.window_credential("instance-view").unwrap();
    let retired = handover.window_credential("second").unwrap();
    let delayed = handover.close_quiet_public_for_window(host.host(), &retired);
    handover.revoke_window(&retired);
    let pooled = if let Some((a, b, other)) = public {
        let first = handover.window_credential("first").unwrap();
        let second = handover.window_credential("second").unwrap();
        let mut one = super::credential_tests::connect_as(&client, &first, 200).await;
        let mut two = super::credential_tests::connect_as(&client, &second, 200).await;
        super::credential_tests::echo(&mut one).await;
        super::credential_tests::echo(&mut two).await;
        Some((a, b, other, first, second, one, two))
    } else {
        None
    };
    let mut changes_status = client.path_changes();
    assert_eq!(*changes_status.borrow_and_update(), super::PathStatus::default());
    client.publish(link.clone(), super::EndpointDrain::new(ep));
    assert_eq!(changes_status.borrow_and_update().sequence, 1);
    assert_eq!(
        handover
            .close_quiet_public_for_window("other-app.avibe.bot", &view)
            .await,
        super::HandoverResult::NotUsable
    );
    if pooled.is_none() {
        assert_eq!(
            handover.close_quiet_public_for_window(host.host(), &view).await,
            super::HandoverResult::Done { closed: 0, kept: 0 }
        );
    }
    assert_eq!(
        client.selected_path(),
        None,
        "the CONNECT probe is not a browser stream"
    );
    use base64::{engine::general_purpose::STANDARD, Engine};
    handover.set_direct_only(true);
    let credential = handover.window_credential("to-close").unwrap();
    let mut closing = super::credential_tests::connect_as(&client, &credential, 200).await;
    super::credential_tests::echo(&mut closing).await;
    handover.revoke_window(&credential);
    assert_retired_read(closing.read(&mut [0]).await);
    drop(super::credential_tests::connect_as(&client, &credential, 407).await);
    let active = handover.window_credential("second").unwrap();
    let (user, pass) = active.native_login();
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
    assert_eq!(
        client.selected_path(),
        Some(super::SelectedPath::Lan(super::AddressFamily::Ipv4))
    );
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
    if let Some((mut mine, mut foreign, _other, first, second, mut one, mut two)) = pooled {
        assert_eq!(
            handover.window_census(&first),
            Some(super::PublicCensus { quiet: 1, busy: 0 })
        );
        assert_eq!(
            handover.window_census(&second),
            Some(super::PublicCensus { quiet: 1, busy: 0 })
        );
        // First poll is after label reuse, with a replacement leg eligible to close.
        assert_eq!(delayed.await, super::HandoverResult::NotUsable);
        assert_eq!(handover.window_census(&retired), None);
        handover.revoke_window(&retired);
        assert_eq!(
            handover.window_census(&second),
            Some(super::PublicCensus { quiet: 1, busy: 0 })
        );
        assert_eq!(
            handover.close_quiet_public_for_window(host.host(), &first).await,
            super::HandoverResult::Done { closed: 1, kept: 0 }
        );
        assert_retired_read(one.read(&mut [0]).await);
        super::credential_tests::echo(&mut two).await;
        assert_eq!(
            handover.close_quiet_public_for_window(host.host(), &view).await,
            super::HandoverResult::Done { closed: 1, kept: 0 }
        );
        assert_retired_read(mine.read(&mut [0]).await);
        // Only the remaining public window is cut off. The committed direct
        // stream `r` and the other session still exchange payload below.
        assert_eq!(handover.close_public(), 1);
        assert_retired_read(two.read(&mut [0]).await);
        foreign.write_all(b"z").await.unwrap();
        assert_eq!(foreign.read_u8().await.unwrap(), b'z');
        assert!(
            !changes_status.has_changed().unwrap(),
            "intentional quiet handover is not an involuntary closure"
        );
    }
    if changed_selection {
        // A terminal accept error ends eligibility for *new* connections.
        // The already-committed stream must survive, including a transient refresh.
        client.listener.accept_fault.notify_one();
        timeout(
            Duration::from_secs(2),
            changes_status.wait_for(|s| s.preferred == super::PreferredPath::Tunnel),
        )
        .await
        .unwrap()
        .unwrap();
        assert_eq!(changes_status.borrow().closure, 0);
        assert!(client.route().is_none());
        assert_eq!(client.refresh_reply(|_| async { Reply::Failed }).await, retained);
    }
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
            relay: None,
            candidates: link.offer.candidates.clone()
        }));
    }
    timeout(Duration::from_secs(1), changes.wait_for(Option::is_none))
        .await
        .unwrap()
        .unwrap();
    // Why the link was withdrawn is on record for the instance's log, once,
    // under the kind of path it was on, and counted in its minute; the
    // CONNECTs the link carried are counted there as well.
    let withdrawn: Vec<_> = client
        .faults
        .take()
        .iter()
        .map(super::faults::LegFault::json)
        .filter(|fault| fault["stage"] == "handover")
        .collect();
    let last = withdrawn.last().expect("the withdrawal is on record");
    assert_eq!(
        last["code"],
        if changed_selection { "superseded" } else { "expired" },
        "{withdrawn:?}"
    );
    assert_eq!(last["path"], "lan", "{withdrawn:?}");
    let counted = client.listener.access.usage.counted();
    let on_lan = counted
        .iter()
        .find(|entry| entry["path"] == "lan")
        .expect("a count on the LAN path");
    assert!(on_lan["retired"].as_u64().unwrap() >= 1, "{on_lan}");
    assert!(on_lan["direct"].as_u64().unwrap() >= 1, "{on_lan}");
    assert_eq!(
        client.refresh_reply(|_| async { Reply::Failed }).await,
        RefreshOutcome::Retired
    );
    let mut end = [0];
    assert_retired_read(r.read(&mut end).await);
    assert_eq!(client.selected_path(), None);
    assert_eq!(
        *changes_status.borrow_and_update(),
        super::PathStatus {
            preferred: super::PreferredPath::Tunnel,
            sequence: 2,
            closure: u64::from(!changed_selection),
            strict_failures: 0,
        }
    );
    assert_eq!(
        handover.close_quiet_public_for_window(host.host(), &view).await,
        super::HandoverResult::NotUsable
    );
    ingress_task.await.unwrap();
    drop(client);
    control.write_all(b"{\"type\":\"stop\"}\n").await.unwrap();
    assert!(child.wait().await.unwrap().success());
}

fn assert_retired_read(read: std::io::Result<usize>) {
    // Only after the complete payload exchange and explicit retirement: a closed
    // TCP leg may surface as EOF or a reset/abort (including Winsock 10054).
    use std::io::ErrorKind::{ConnectionAborted, ConnectionReset};
    match read {
        Ok(0) => (),
        Err(e) if matches!(e.kind(), ConnectionReset | ConnectionAborted) => (),
        other => panic!("expected EOF or connection shutdown after retirement, got {other:?}"),
    }
}
