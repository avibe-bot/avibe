use super::{
    clock::Clock,
    faults::{LegFault, PathWord},
    listener::Listener,
    offer::{Offer, ALPN},
    rendezvous::Reply,
    transport::{self, Fault, FaultCode, Stage, Timed},
    Client, PreferredPath,
};
use iroh::{
    endpoint::{presets, BindOpts, Builder, ConnectionError},
    Endpoint, RelayMode,
};
use std::{
    net::Ipv4Addr,
    sync::{
        atomic::{AtomicU64, Ordering},
        Arc,
    },
    time::Duration,
};
use tokio::time::timeout;

async fn confined(builder: Builder) -> Endpoint {
    let ep = builder
        .clear_ip_transports()
        .bind_addr_with_opts(
            (Ipv4Addr::LOCALHOST, 0),
            BindOpts::default().set_prefix_len(32).set_is_default_route(false),
        )
        .unwrap()
        .bind()
        .await
        .unwrap();
    let sockets = ep.bound_sockets();
    assert_eq!(sockets.len(), 1);
    assert_eq!(sockets[0].ip(), Ipv4Addr::LOCALHOST);
    ep
}

#[tokio::test]
async fn peer_observes_quic_close_on_probe_refusal_and_withdrawal() {
    for accept in [false, true] {
        timeout(Duration::from_secs(12), async {
            let mut client = Client::new("test-app.avibe.bot", "fixture".into()).await.unwrap();
            let server = confined(
                Endpoint::builder(presets::Minimal)
                    .relay_mode(RelayMode::Disabled)
                    .alpns(vec![ALPN.to_vec()]),
            )
            .await;
            let ep = confined(transport::endpoint_builder(client.key.clone(), None)).await;
            let target = Offer {
                generation: "fixture".into(),
                id: server.id(),
                candidates: server.bound_sockets(),
                relay: None,
                until: client.clock.now() + Duration::from_secs(30),
            };
            let head = client.host.direct_head();
            // Observe from the peer, not by checking the client's lease/watcher.
            let peer = tokio::spawn(async move {
                let conn = server.accept().await.unwrap().await.unwrap();
                let (mut send, mut recv) = conn.accept_bi().await.unwrap();
                let mut req = vec![0; head.len()];
                recv.read_exact(&mut req).await.unwrap();
                assert_eq!(req, head);
                send.write_all(if accept {
                    b"HTTP/1.1 200 OK\r\n\r\n"
                } else {
                    b"HTTP/1.1 403 Forbidden\r\n\r\n"
                })
                .await
                .unwrap();
                // A delivered application close, not idle timeout / local endpoint drop.
                let reason = timeout(Duration::from_secs(3), conn.closed())
                    .await
                    .expect("peer did not receive close");
                assert!(matches!(reason, ConnectionError::ApplicationClosed(_)), "{reason:?}");
                server.close().await;
            });
            // Keep a handle: dropping the last endpoint cannot satisfy this check.
            assert_eq!(client.probe(ep.clone(), target).await, accept);
            if accept {
                assert!(!client.refresh(|_| async { None }).await);
            }
            timeout(Duration::from_secs(1), ep.closed())
                .await
                .expect("endpoint never closed");
            peer.await.unwrap();
        })
        .await
        .expect("confined close fixture timed out");
    }
}

/// A client on a clock the test moves, in milliseconds, and a peer for it.
async fn fixture() -> (Client, Arc<AtomicU64>, Endpoint, Endpoint, Offer) {
    let mut client = Client::new("test-app.avibe.bot", "fixture".into()).await.unwrap();
    let millis = Arc::new(AtomicU64::new(10_000));
    let now = millis.clone();
    client.clock = Clock(Arc::new(move || Duration::from_millis(now.load(Ordering::SeqCst))));
    let server = confined(
        Endpoint::builder(presets::Minimal)
            .relay_mode(RelayMode::Disabled)
            .alpns(vec![ALPN.to_vec()]),
    )
    .await;
    let ep = confined(transport::endpoint_builder(client.key.clone(), None)).await;
    let target = Offer {
        generation: "fixture".into(),
        id: server.id(),
        candidates: server.bound_sockets(),
        relay: None,
        until: client.clock.now() + Duration::from_secs(30),
    };
    (client, millis, server, ep, target)
}

/// What a failed probe says about time is its own stage's: not the dial
/// before it. (The drain after it does not move this test's clock; that it
/// is left out is by where the duration is read.)
#[tokio::test]
async fn a_refused_probe_says_how_long_its_own_stage_ran() {
    timeout(Duration::from_secs(12), async {
        let (mut client, millis, server, ep, target) = fixture().await;
        let head = client.host.direct_head();
        let peer = tokio::spawn(async move {
            let incoming = server.accept().await.unwrap();
            // Three seconds pass in the dial, before the connection exists.
            millis.fetch_add(3_000, Ordering::SeqCst);
            let conn = incoming.await.unwrap();
            let (mut send, mut recv) = conn.accept_bi().await.unwrap();
            let mut req = vec![0; head.len()];
            recv.read_exact(&mut req).await.unwrap();
            // And 700 ms in the CONNECT, between its head and its answer.
            millis.fetch_add(700, Ordering::SeqCst);
            send.write_all(b"HTTP/1.1 403 Forbidden\r\n\r\n").await.unwrap();
            let _ = timeout(Duration::from_secs(3), conn.closed()).await;
            server.close().await;
        });
        assert_eq!(
            client.probe_detailed(ep.clone(), target).await,
            Err(Timed(
                Fault(Stage::Connect, FaultCode::Status),
                Duration::from_millis(700)
            ))
        );
        peer.await.unwrap();
    })
    .await
    .expect("confined probe fixture timed out");
}

/// A CONNECT answered `502` is a failed direct leg in the count when a
/// direct open was started for it: beside the tunnel on a link that is up,
/// then alone in direct-only. (With no usable link none is started: the
/// listener's own test, and the direct-only one in `strict.rs`.)
#[tokio::test]
async fn a_connect_that_no_leg_carried_counts_as_a_failed_direct_leg_where_one_was_tried() {
    timeout(Duration::from_secs(25), async {
        let (mut client, _millis, server, ep, target) = fixture().await;
        // The tunnel's leg is unreachable, and needs no network.
        client.listener = Listener::bind_via(
            client.host.clone(),
            client.links.subscribe(),
            client.status.clone(),
            |_| std::future::ready(None),
        )
        .await
        .unwrap();
        let head = client.host.direct_head();
        let peer = tokio::spawn(async move {
            let conn = server.accept().await.unwrap().await.unwrap();
            // The probe's stream is accepted; the window's two are refused.
            let refused = "HTTP/1.1 403 Forbidden\r\n\r\n";
            for answer in ["HTTP/1.1 200 OK\r\n\r\n", refused, refused] {
                let (mut send, mut recv) = conn.accept_bi().await.unwrap();
                let mut req = vec![0; head.len()];
                recv.read_exact(&mut req).await.unwrap();
                send.write_all(answer.as_bytes()).await.unwrap();
            }
            let _ = timeout(Duration::from_secs(20), conn.closed()).await;
            server.close().await;
        });
        assert_eq!(client.probe_detailed(ep.clone(), target).await, Ok(()));
        let handover = client.handover();
        let window = handover.window_credential("instance-view").unwrap();
        drop(super::credential_tests::connect_as(&client, &window, 502).await);
        let usage = &client.listener.access.usage;
        let total = |key| usage.total(None, key);
        assert_eq!(usage.total(Some(PathWord::Lan), "direct_failed"), 1);
        assert_eq!(
            (
                total("direct_failed"),
                total("direct"),
                total("tunnel"),
                total("strict")
            ),
            (1, 0, 0, 0)
        );
        // In direct-only the refused open is not tried again on the same
        // link: the CONNECT is answered when its budget is over.
        handover.set_direct_only(true);
        drop(super::credential_tests::connect_as(&client, &window, 502).await);
        assert_eq!(usage.total(Some(PathWord::Lan), "direct_failed"), 2);
        assert_eq!((total("direct_failed"), total("strict")), (2, 1));
        assert_eq!((total("direct"), total("tunnel")), (0, 0));
        drop(client);
        peer.await.unwrap();
    })
    .await
    .expect("confined connect fixture timed out");
}

/// The withdrawals on record: code, kind of path, and how long the link lived.
fn withdrawn(client: &Client) -> Vec<(String, String, u64)> {
    client
        .faults
        .take()
        .iter()
        .map(LegFault::json)
        .filter(|fault| fault["stage"] == "handover")
        .map(|fault| {
            (
                fault["code"].as_str().unwrap().to_owned(),
                fault["path"].as_str().unwrap().to_owned(),
                fault["ms"].as_u64().unwrap(),
            )
        })
        .collect()
}

/// How a published link comes to be withdrawn in one case below.
enum Ending {
    Reply(Reply),
    /// The lease runs out, and a refresh fails before the monitor has run.
    ExpiredThenFailedRefresh,
    /// The peer ends the connection, and a refresh fails after it.
    PeerClosedThenFailedRefresh,
    /// New connections leave the path (the listener ends), then the lease
    /// runs out.
    ListenerEndedThenExpired,
}

#[tokio::test]
async fn a_withdrawal_says_why_and_on_which_path_the_link_was() {
    let unavailable = r#"{"version":1,"instance_id":"fixture","hostname":"test-app.avibe.bot","status":"unavailable","reason":"permission_expired","retry_after_seconds":60}"#;
    for (ending, code, lived) in [
        // A refused refresh is not a replacement.
        (Ending::Reply(Reply::Refused(403)), "denied", 2_000),
        (Ending::Reply(Reply::Refused(404)), "refused", 2_000),
        (Ending::Reply(Reply::Controlled), "denied", 2_000),
        (
            Ending::Reply(Reply::Ready(unavailable.into())),
            "not_ready_permission",
            2_000,
        ),
        (Ending::Reply(Reply::Ready("{".into())), "invalid_offer", 2_000),
        // A changed selection is.
        (Ending::Reply(Reply::SelectionChanged), "superseded", 2_000),
        // A link with nothing left says the loss that was seen, whichever
        // of the failed refresh and the monitor removes it.
        (Ending::ExpiredThenFailedRefresh, "expired", 31_000),
        (Ending::PeerClosedThenFailedRefresh, "connection_ended", 500),
        (Ending::ListenerEndedThenExpired, "expired", 31_000),
    ] {
        timeout(Duration::from_secs(12), async {
            let (mut client, millis, server, ep, target) = fixture().await;
            let head = client.host.direct_head();
            let end = Arc::new(tokio::sync::Notify::new());
            let ended = end.clone();
            let peer = tokio::spawn(async move {
                let conn = server.accept().await.unwrap().await.unwrap();
                let (mut send, mut recv) = conn.accept_bi().await.unwrap();
                let mut req = vec![0; head.len()];
                recv.read_exact(&mut req).await.unwrap();
                send.write_all(b"HTTP/1.1 200 OK\r\n\r\n").await.unwrap();
                tokio::select! {
                    _ = ended.notified() => conn.close(0u32.into(), b"ended"),
                    _ = timeout(Duration::from_secs(5), conn.closed()) => {}
                }
                server.close().await;
            });
            assert_eq!(client.probe_detailed(ep.clone(), target).await, Ok(()));
            let mut links = client.links.subscribe();
            let link = client.links.borrow().clone().expect("a published link");
            match ending {
                Ending::Reply(reply) => {
                    millis.fetch_add(2_000, Ordering::SeqCst);
                    client.refresh_reply(|_| async move { reply }).await;
                }
                Ending::ExpiredThenFailedRefresh => {
                    millis.fetch_add(31_000, Ordering::SeqCst);
                    client.refresh_reply(|_| async { Reply::Failed }).await;
                }
                Ending::PeerClosedThenFailedRefresh => {
                    millis.fetch_add(500, Ordering::SeqCst);
                    end.notify_one();
                    link.conn.closed().await;
                    client.refresh_reply(|_| async { Reply::Failed }).await;
                }
                Ending::ListenerEndedThenExpired => {
                    let mut status = client.path_changes();
                    client.listener.accept_fault.notify_one();
                    status
                        .wait_for(|status| status.preferred == PreferredPath::Tunnel)
                        .await
                        .unwrap();
                    millis.fetch_add(31_000, Ordering::SeqCst);
                }
            }
            drop(link);
            links.wait_for(Option::is_none).await.unwrap();
            // Once, with its own reason, under the path the link was on.
            assert_eq!(withdrawn(&client), [(code.to_owned(), "lan".to_owned(), lived)]);
            let usage = &client.listener.access.usage;
            assert_eq!(
                (
                    usage.total(Some(PathWord::Lan), "retired"),
                    usage.total(None, "retired")
                ),
                (1, 1),
                "{code}"
            );
            drop(client);
            peer.await.unwrap();
        })
        .await
        .unwrap_or_else(|_| panic!("confined withdrawal fixture timed out: {code}"));
    }
}
