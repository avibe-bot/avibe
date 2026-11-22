use super::{quiet::PublicLegs, AddressFamily, PathStatus, PreferredPath, SelectedPath};
use tokio::{
    io::{duplex, AsyncReadExt, AsyncWriteExt},
    sync::watch,
    time::{Duration, Instant},
};

pub(super) async fn public_browser(client: &mut super::Client) -> tokio::net::TcpStream {
    use super::{listener::Listener, transport::Stream};
    client.listener = Listener::bind_via(client.host.clone(), client.links.subscribe(), |_| async {
        let (stream, mut echo) = duplex(64);
        tokio::spawn(async move {
            let (mut r, mut w) = tokio::io::split(&mut echo);
            let _ = tokio::io::copy(&mut r, &mut w).await;
        });
        Some(Box::new(stream) as Stream)
    })
    .await
    .unwrap();
    let mut s = super::lifecycle_tests::connect(client.route().unwrap()).await;
    s.write_all(b"x").await.unwrap();
    assert_eq!(s.read_u8().await.unwrap(), b'x');
    s
}

#[tokio::test(start_paused = true)]
async fn close_touches_only_quiet_committed_public_legs_of_one_listener() {
    async fn leg(legs: PublicLegs) -> (tokio::io::DuplexStream, tokio::io::DuplexStream) {
        let (mut client, a) = duplex(64);
        let (b, mut peer) = duplex(1);
        tokio::spawn(async move {
            legs.copy(a, b).await;
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
