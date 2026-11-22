use super::{
    offer::{Offer, ALPN},
    transport, Client,
};
use iroh::{
    endpoint::{presets, BindOpts, Builder, ConnectionError},
    Endpoint, RelayMode,
};
use std::{net::Ipv4Addr, time::Duration};
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
