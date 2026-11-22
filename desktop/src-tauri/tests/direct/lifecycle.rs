use super::{listener::Listener, transport::Stream, Client, Route};
use base64::{engine::general_purpose::STANDARD, Engine};
use serde_json::json;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpStream;
use tokio::time::{timeout, Duration};

#[tokio::test]
async fn no_transport_for_unavailable_missing_malformed_or_ineligible_offer() {
    let mut client = Client::new("desk-app.avibe.bot", "one".into()).await.unwrap();
    let id = client.key.public().to_string();
    let peer = iroh::SecretKey::generate().public();
    let unavailable = json!({"status":"unavailable","reason":"not_running"});
    let no_lan = json!({"version":1,"status":"ready","hostname":"desk-app.avibe.bot","instance_id":"one","generation":"a","issued_at":10,"expires_at":300,"alpn":"avibe-direct/1","endpoint":{"id":peer.to_string(),"addresses":[{"kind":"ipv6","address":"[2001:db8::1]:443"}]},"client":{"endpoint_id":id,"admitted_until":300}});
    let port = client.route().unwrap().proxy;
    assert!(client.monitor.is_none());
    for answer in [
        None,
        Some("{".into()),
        Some(unavailable.to_string()),
        Some(no_lan.to_string()),
    ] {
        assert!(
            !client
                .refresh(|key| {
                    assert!(key == id);
                    async move { answer }
                })
                .await
        );
        assert!(client.monitor.is_none());
        assert!(client.links.borrow().is_none());
        assert_eq!(client.route().unwrap().proxy, port);
    }
}
pub(super) async fn connect(route: Route<'_>) -> TcpStream {
    let auth = STANDARD.encode(format!("{}:{}", route.username, route.password));
    let head = format!(
        "CONNECT {}:443 HTTP/1.1\r\nProxy-Authorization: Basic {auth}\r\n\r\n",
        route.host
    );
    let mut s = TcpStream::connect(route.proxy).await.unwrap();
    s.write_all(head.as_bytes()).await.unwrap();
    let ok = b"HTTP/1.1 200 Connection Established\r\n\r\n";
    let mut buf = vec![0; ok.len()];
    s.read_exact(&mut buf).await.unwrap();
    assert_eq!(buf, ok);
    s
}
#[tokio::test]
async fn failed_refresh_keeps_existing_and_new_public_connects_until_detach() {
    timeout(Duration::from_secs(4), async {
        let mut c = Client::new("desk-app.avibe.bot", "one".into()).await.unwrap();
        // Substitute only the public dial target before publishing the route.
        // Use the production accept, authorization, selection and TCP relay.
        c.listener = Listener::bind_via(c.host.clone(), c.links.subscribe(), |_| async {
            let (stream, mut echo) = tokio::io::duplex(1024);
            tokio::spawn(async move {
                let (mut r, mut w) = tokio::io::split(&mut echo);
                let _ = tokio::io::copy(&mut r, &mut w).await;
            });
            Some(Box::new(stream) as Stream)
        })
        .await
        .unwrap();
        let port = c.route().unwrap().proxy;
        let mut a = connect(c.route().unwrap()).await;
        let mut life = c.listener_lifetime();
        assert!(!c.refresh(|_| async { None }).await);
        assert!(life.has_changed().is_ok());
        assert_eq!(port, c.route().unwrap().proxy);
        let mut b = connect(c.route().unwrap()).await;
        for (i, s) in [&mut a, &mut b].into_iter().enumerate() {
            let x = [i as u8 + 1];
            s.write_all(&x).await.unwrap();
            let mut read = [0];
            s.read_exact(&mut read).await.unwrap();
            assert_eq!(x, read);
        }
        drop(c);
        assert!(life.changed().await.is_err());
        let mut byte = [0];
        assert!(matches!(a.read(&mut byte).await, Ok(0) | Err(_)));
    })
    .await
    .unwrap();
}
