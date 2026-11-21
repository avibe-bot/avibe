use super::*;
use tokio::net::TcpListener;

#[test]
fn system_answers_include_fake_ips_but_never_an_origin_on_loopback() {
    for ip in [
        "198.18.0.1:443",
        "198.19.255.254:443",
        "[::ffff:198.18.0.1]:443",
        "10.0.0.1:443",
    ] {
        assert!(allowed(ip.parse().unwrap(), false, 8080));
    }
    for ip in [
        "0.0.0.0:443",
        "127.0.0.2:443",
        "[::]:443",
        "[::1]:443",
        "[::ffff:127.0.0.1]:443",
        "[::ffff:0.0.0.0]:443",
    ] {
        assert!(!allowed(ip.parse().unwrap(), false, 8080));
    }
    assert!(allowed("127.0.0.1:8081".parse().unwrap(), true, 8080));
    assert!(!allowed("[::ffff:127.0.0.1]:8080".parse().unwrap(), true, 8080));
}
#[test]
fn automatic_and_explicit_windows_settings_are_not_no_proxy() {
    let h = Some("desk-app.avibe.bot");
    for (auto, pac) in [(true, ""), (false, "https://pac.test/rules")] {
        assert!(matches!(windows_policy(h, auto, pac, "", "*"), Some(Policy::Automatic)));
    }
    let proxy = "http=work.test:80;https=127.0.0.1:8080";
    assert!(matches!(
        windows_policy(h, false, "", proxy, "<local>;*.example.test"),
        Some(Policy::Http(_, 8080))
    ));
    assert!(matches!(
        windows_policy(h, false, "", proxy, "*.avibe.bot"),
        Some(Policy::Direct)
    ));
    assert!(!matches!(
        windows_policy(None, false, "", proxy, "*"),
        Some(Policy::Direct)
    ));
    for v in [
        "https=https://work.test:443",
        "socks=127.0.0.1:1000",
        "http://user:pw@work.test:80",
    ] {
        assert!(windows_policy(h, false, "", v, "").is_none());
    }
}
#[cfg(target_os = "macos")]
#[test]
fn mac_incomplete_proxy_is_not_a_bypass() {
    use objc2::runtime::AnyObject;
    use objc2_foundation::{NSArray, NSMutableDictionary, NSNumber, NSString};
    let host = "desk-app.avibe.bot";
    let h = Some(host);
    let keys = ["HTTPSEnable", "HTTPSProxy", "HTTPSPort", "ExceptionsList"].map(NSString::from_str);
    let on = NSNumber::new_i32(1);
    let port = NSNumber::new_i32(8080);
    let name = NSString::from_str("proxy.test");
    let bypass = NSArray::from_slice(&[&*NSString::from_str(host)]);
    let settings = NSMutableDictionary::<NSString, AnyObject>::new();
    let set = |key: &NSString, value: &AnyObject| {
        // Fixture owns the mutable dictionary; NSString implements NSCopying.
        unsafe {
            let _: () = objc2::msg_send![&*settings, setObject: value, forKey: key];
        }
    };
    assert!(matches!(mac_policy(h, &settings), Some(Policy::Direct)));
    set(&keys[0], &on);
    assert!(mac_policy(h, &settings).is_none());
    set(&keys[1], &name);
    set(&keys[2], &port);
    assert!(matches!(mac_policy(h, &settings), Some(Policy::Http(_, 8080))));
    set(&keys[3], &bypass);
    assert!(matches!(mac_policy(h, &settings), Some(Policy::Direct)));
}

#[tokio::test]
async fn explicit_connect_has_no_credentials_and_preserves_opaque_bytes() {
    let a = Authority::managed("desk-app.avibe.bot").unwrap();
    for status in [200, 403, 407] {
        let l = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = l.local_addr().unwrap().port();
        let head = a.direct_head();
        let server = tokio::spawn(async move {
            let (mut s, _) = l.accept().await.unwrap();
            let mut got = vec![0; head.len()];
            s.read_exact(&mut got).await.unwrap();
            assert_eq!(got, head);
            s.write_all(format!("HTTP/1.1 {status} fixture\r\n\r\n").as_bytes())
                .await
                .unwrap();
            if status == 200 {
                s.write_all(b"\xff\x01").await.unwrap();
                assert_eq!(s.read_u8().await.unwrap(), 234);
            }
        });
        let result = open_via(Policy::Http("127.0.0.1".into(), port), &a, 0).await;
        if status == 200 {
            let mut s = result.unwrap();
            assert_eq!(s.read_u16().await.unwrap(), 0xff01);
            s.write_u8(234).await.unwrap()
        } else {
            assert!(matches!(result, Err(OpenError::Terminal)))
        }
        server.await.unwrap();
    }
}
