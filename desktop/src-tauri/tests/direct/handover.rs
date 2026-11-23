use super::{quiet::PublicLegs, AddressFamily, PathStatus, PreferredPath, SelectedPath};
use tokio::{
    io::{duplex, AsyncReadExt, AsyncWriteExt},
    sync::watch,
    time::{Duration, Instant},
};

pub(super) async fn pair() -> (tokio::net::TcpStream, tokio::net::TcpStream) {
    use tokio::net::{TcpListener, TcpStream};
    let l = TcpListener::bind((std::net::Ipv4Addr::LOCALHOST, 0)).await.unwrap();
    let (a, b) = tokio::join!(TcpStream::connect(l.local_addr().unwrap()), l.accept());
    (a.unwrap(), b.unwrap().0)
}
pub(super) async fn echo_socket() -> Option<tokio::net::TcpStream> {
    let (stream, mut echo) = pair().await;
    tokio::spawn(async move {
        let (mut r, mut w) = echo.split();
        let _ = tokio::io::copy(&mut r, &mut w).await;
    });
    Some(stream)
}
pub(super) async fn public_browser(client: &mut super::Client) -> tokio::net::TcpStream {
    use super::listener::Listener;
    client.listener = Listener::bind_via(client.host.clone(), client.links.subscribe(), |_| echo_socket())
        .await
        .unwrap();
    let window = client.handover().window_credential("instance-view").unwrap();
    let mut s = super::credential_tests::connect_as(client, &window, 200).await;
    s.write_all(b"x").await.unwrap();
    assert_eq!(s.read_u8().await.unwrap(), b'x');
    s
}

#[tokio::test(start_paused = true)]
async fn close_touches_only_quiet_committed_public_legs_of_one_listener() {
    async fn leg(legs: PublicLegs) -> (tokio::io::DuplexStream, tokio::io::DuplexStream) {
        let (mut client, a) = duplex(64);
        let (b, mut peer) = duplex(1);
        let (input1, hold1) = pair().await;
        let (input2, hold2) = pair().await;
        let copy = legs.commit(&input1, &input2);
        tokio::spawn(async move {
            // Controlled copy buffers for deterministic backpressure; keep the
            // observation sockets open and empty throughout this test.
            let _peers = (hold1, hold2);
            copy.copy(a, b).await;
        });
        client.write_all(b"a").await.unwrap();
        assert_eq!(peer.read_u8().await.unwrap(), b'a');
        (client, peer)
    }
    let host = PublicLegs::default();
    let other = PublicLegs::default();
    let (mut idle, _idle_peer) = leg(host.clone()).await;
    let (mut busy, mut slow_peer) = leg(host.clone()).await;
    let (mut foreign, mut foreign_peer) = leg(other).await;
    // The upstream buffer fits one byte. The second has left the browser but
    // has not reached the peer; silence alone must not classify this as idle.
    busy.write_all(b"xy").await.unwrap();
    tokio::task::yield_now().await;
    let end = Instant::now() + Duration::from_secs(3);
    assert_eq!(host.claim(end), None);
    tokio::time::advance(Duration::from_secs(2)).await;
    assert_eq!(host.claim(end), None);
    tokio::time::advance(Duration::from_secs(1)).await;
    assert_eq!(host.claim(end), Some((1, 1)));
    assert_eq!(idle.read(&mut [0]).await.unwrap(), 0);
    let mut payload = [0; 2];
    slow_peer.read_exact(&mut payload).await.unwrap();
    assert_eq!(&payload, b"xy");
    foreign_peer.write_all(b"z").await.unwrap();
    assert_eq!(foreign.read_u8().await.unwrap(), b'z');
}

#[tokio::test(start_paused = true)]
async fn bytes_queued_before_copy_is_polled_keep_the_committed_leg() {
    use std::{
        future::Future,
        pin::pin,
        task::{Context, Waker},
    };
    for from_browser in [true, false] {
        let legs = PublicLegs::default();
        let (mut browser, a) = pair().await;
        let (b, mut upstream) = pair().await;
        let a = a.into_std().unwrap();
        let b = b.into_std().unwrap();
        let observe = if from_browser { &a } else { &b }.try_clone().unwrap();
        let a = tokio::net::TcpStream::from_std(a).unwrap();
        let b = tokio::net::TcpStream::from_std(b).unwrap();
        let setup = legs.commit(&a, &b);
        tokio::time::advance(Duration::from_secs(4)).await;
        assert_eq!(
            legs.claim(Instant::now()),
            Some((0, 1)),
            "committed setup is busy even before copy"
        );
        let mut copy = pin!(setup.copy(a, b));
        assert!(copy.as_mut().poll(&mut Context::from_waker(Waker::noop())).is_pending());
        tokio::time::advance(Duration::from_millis(1050)).await;
        let (sender, receiver) = if from_browser {
            (&mut browser, &mut upstream)
        } else {
            (&mut upstream, &mut browser)
        };
        sender.write_all(b"q").await.unwrap();
        let mut byte = [0];
        let bound = std::time::Instant::now() + Duration::from_secs(2);
        loop {
            match observe.peek(&mut byte) {
                Ok(1) => break,
                Err(e) if e.kind() == std::io::ErrorKind::WouldBlock && std::time::Instant::now() < bound => {
                    tokio::task::yield_now().await
                }
                other => panic!("queued byte not observed: {other:?}"),
            }
        }
        assert_eq!(&byte, b"q");
        // No poll of `copy` between this confirmed kernel peek and the claim.
        assert_eq!(legs.claim(Instant::now()), Some((0, 1)));
        tokio::select! {byte=receiver.read_u8()=>assert_eq!(byte.unwrap(),b'q'),_=&mut copy=>panic!("queued data was dropped")}
    }
}

#[test]
fn relay_upgrade_updates_preference_once_without_claiming_a_closure() {
    let (tx, mut rx) = watch::channel(PathStatus::default());
    let lan = Some(SelectedPath::Lan(AddressFamily::Ipv4));
    for (path, seq) in [
        (None, 0),
        (Some(SelectedPath::Relay), 1),
        (Some(SelectedPath::Relay), 1),
        (lan, 2),
        (lan, 2),
        (None, 3),
    ] {
        let before = rx.borrow_and_update().sequence;
        PathStatus::publish(&tx, path, false);
        assert_eq!(rx.has_changed().unwrap(), before != seq);
        assert_eq!(
            *rx.borrow(),
            PathStatus {
                preferred: path.map(PreferredPath::Direct).unwrap_or_default(),
                sequence: seq,
                closure: 0
            }
        );
    }
}
