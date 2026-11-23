use super::{Client, WindowRegistration};
use base64::{engine::general_purpose::STANDARD, Engine};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::TcpStream,
    time::{timeout, Duration},
};

pub(super) async fn connect_as(c: &Client, credential: &WindowRegistration, expected: u16) -> TcpStream {
    let route = c.route().unwrap();
    connect_at(route.proxy, route.host, credential, expected).await
}
async fn connect_at(
    proxy: std::net::SocketAddrV4,
    host: &str,
    credential: &WindowRegistration,
    expected: u16,
) -> TcpStream {
    let (user, pass) = credential.native_login();
    let auth = STANDARD.encode(format!("{user}:{pass}"));
    let mut stream = TcpStream::connect(proxy).await.unwrap();
    stream
        .write_all(
            format!(
                "CONNECT {}:443 HTTP/1.1\r\nProxy-Authorization: Basic {auth}\r\n\r\n",
                host
            )
            .as_bytes(),
        )
        .await
        .unwrap();
    let mut response = Vec::new();
    while !response.ends_with(b"\r\n\r\n") {
        response.push(stream.read_u8().await.unwrap());
        assert!(response.len() < 1024);
    }
    assert!(
        response.starts_with(format!("HTTP/1.1 {expected} ").as_bytes()),
        "unexpected proxy status"
    );
    stream
}
pub(super) async fn echo(s: &mut TcpStream) {
    s.write_all(b"w").await.unwrap();
    assert_eq!(s.read_u8().await.unwrap(), b'w');
}
#[tokio::test]
async fn handle_issues_and_revokes_while_client_refresh_is_in_flight() {
    timeout(Duration::from_secs(4), async {
        let host = "one-app.avibe.bot";
        let mut c = Client::new(host, "one".into()).await.unwrap();
        let mut legacy = super::handover_tests::public_browser(&mut c).await;
        let proxy = c.route().unwrap().proxy;
        let h = c.handover();
        let fault = c.listener.accept_fault.clone();
        let mut health = c.listener_lifetime();
        let (entered, started) = tokio::sync::oneshot::channel();
        let (release, wait) = tokio::sync::oneshot::channel();
        let refresh = c.refresh_reply(|_| async move {
            entered.send(()).unwrap();
            wait.await.unwrap();
            super::rendezvous::Reply::NotReady
        });
        let windows = async {
            started.await.unwrap();
            let a = h.window_credential("a").unwrap();
            let mut leg = connect_at(proxy, host, &a, 200).await;
            echo(&mut leg).await;
            h.revoke_window(&a);
            assert!(matches!(leg.read(&mut [0]).await, Ok(0) | Err(_)));
            drop(connect_at(proxy, host, &a, 407).await);
            let fresh = h.window_credential("a").unwrap();
            let mut replacement = connect_at(proxy, host, &fresh, 200).await;
            h.revoke_window(&a);
            echo(&mut replacement).await;
            fault.notify_one();
            assert!(health.changed().await.is_err());
            assert!(h.window_credential("a").is_none());
            h.revoke_window(&fresh); // existing streams can still be retired during drain
            assert!(matches!(replacement.read(&mut [0]).await, Ok(0) | Err(_)));
            echo(&mut legacy).await;
            release.send(()).unwrap();
        };
        let (outcome, ()) = tokio::join!(refresh, windows);
        assert_eq!(outcome, super::RefreshOutcome::Retired);
        drop(c);
        assert!(h.window_credential("a").is_none());
    })
    .await
    .unwrap();
}

#[test]
fn every_credential_byte_is_required_by_the_registry() {
    let keys = super::credentials::Windows::new().unwrap();
    let c = keys.credential("one").unwrap();
    let (u, p) = c.native_login();
    let header = format!("Basic {}", STANDARD.encode(format!("{u}:{p}")));
    assert!(keys.authenticate(&header).is_some());
    for i in 0..header.len() {
        let mut bad = header.as_bytes().to_vec();
        bad[i] ^= 1;
        assert!(keys.authenticate(std::str::from_utf8(&bad).unwrap()).is_none());
    }
}

#[tokio::test]
async fn revoked_window_cannot_open_or_keep_a_connection_or_cross_instances() {
    timeout(Duration::from_secs(4), async {
        let mut c = Client::new("one-app.avibe.bot", "one".into()).await.unwrap();
        let mut legacy = super::handover_tests::public_browser(&mut c).await;
        let other = Client::new("two-app.avibe.bot", "two".into()).await.unwrap();
        let a = c.window_credential("a").unwrap();
        let b = c.window_credential("b").unwrap();
        let mut sa = connect_as(&c, &a, 200).await;
        let mut sb = connect_as(&c, &b, 200).await;
        echo(&mut sa).await;
        echo(&mut sb).await;
        drop(connect_as(&other, &a, 407).await);
        let foreign = other.window_credential("a").unwrap();
        c.revoke_window(&foreign);
        assert_eq!(c.handover().window_census(&foreign), None);
        echo(&mut sa).await;
        c.revoke_window(&a);
        let mut byte = [0];
        assert!(matches!(sa.read(&mut byte).await, Ok(0) | Err(_)));
        let fresh = c.window_credential("a").unwrap();
        drop(connect_as(&c, &a, 407).await);
        c.revoke_window(&a); // late teardown, after same-label registration
        let mut reopened = connect_as(&c, &fresh, 200).await;
        echo(&mut reopened).await;
        echo(&mut sb).await;
        echo(&mut legacy).await;
    })
    .await
    .unwrap();
}
