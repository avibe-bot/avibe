//! Presence probing against the Avibe Web UI server.

use std::time::Duration;

use async_trait::async_trait;

use crate::origin::LoopbackOrigin;

const MAX_READINESS_BYTES: usize = 1024;

/// What one `/ready` response proves about the Runtime serving an origin,
/// judged against the desktop Runtime identity this shell runs.
///
/// The identity that decides is the Controller's: it owns the service lock, the
/// agents, and every process a scoped stop can find. The UI identity never makes
/// a Runtime this shell's.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Presence {
    /// The Controller carries the expected identity. `ready` is false for the
    /// exact identity mismatch: the Controller is this shell's but the UI is not.
    Mine { ready: bool },
    /// The Controller carries another valid desktop identity.
    Foreign { runtime_id: String, ready: bool },
    /// A ready Avibe Runtime whose Controller carries no desktop identity.
    Unmanaged,
    /// Nothing accepted the connection.
    Absent,
    /// Anything else: not ready, not Avibe, or not provable.
    Unknown,
}

/// Classifies one `/ready` response for a shell expecting `expected`, the
/// identity of the Runtime it runs, if any.
///
/// Transport errors and raw response bodies stay inside the probe so nothing
/// from the network reaches the bootstrap UI.
#[async_trait]
pub trait HealthProbe: Send + Sync {
    async fn presence(&self, origin: &LoopbackOrigin, expected: Option<&str>) -> Presence;
}

/// `GET <origin>/ready`, requiring UI, service ownership, and Controller IPC.
pub struct HttpHealthProbe {
    client: reqwest::Client,
}

impl HttpHealthProbe {
    pub fn new(timeout: Duration) -> Result<Self, reqwest::Error> {
        let client = reqwest::Client::builder()
            .timeout(timeout)
            // Readiness must be proved by this exact loopback listener. Following
            // a redirect would let an unrelated local service delegate trust to
            // arbitrary remote content.
            .redirect(reqwest::redirect::Policy::none())
            // A proxy configured for the wider machine must never sit between the
            // shell and a loopback Runtime.
            .no_proxy()
            .build()?;
        Ok(Self { client })
    }
}

#[async_trait]
impl HealthProbe for HttpHealthProbe {
    async fn presence(&self, origin: &LoopbackOrigin, expected: Option<&str>) -> Presence {
        let response = match self.client.get(origin.readiness_url()).send().await {
            Ok(response) => response,
            Err(error) if is_connection_refused(&error) => return Presence::Absent,
            Err(_) => return Presence::Unknown,
        };
        let status = response.status();
        let Some(body) = bounded_response_body(response).await else {
            return Presence::Unknown;
        };
        let controller = match status {
            reqwest::StatusCode::OK => parse_readiness_body(&body).map(|runtime_id| (runtime_id, true)),
            reqwest::StatusCode::SERVICE_UNAVAILABLE => {
                parse_runtime_identity_mismatch_body(&body).map(|runtime_id| (Some(runtime_id), false))
            }
            _ => None,
        };
        match controller {
            Some((runtime_id, ready)) => classify(runtime_id, ready, expected),
            None => Presence::Unknown,
        }
    }
}

/// Whether a request failed because the connection was refused, the only
/// connect failure that proves nothing listens. A timeout, a denied socket or
/// an unavailable local address may hide a listener.
fn is_connection_refused(error: &(dyn std::error::Error + 'static)) -> bool {
    std::iter::successors(Some(error), |error| error.source())
        .filter_map(|error| error.downcast_ref::<std::io::Error>())
        .any(|error| error.kind() == std::io::ErrorKind::ConnectionRefused)
}

fn classify(controller_runtime_id: Option<String>, ready: bool, expected: Option<&str>) -> Presence {
    match controller_runtime_id {
        Some(runtime_id) if Some(runtime_id.as_str()) == expected => Presence::Mine { ready },
        Some(runtime_id) => Presence::Foreign { runtime_id, ready },
        None => Presence::Unmanaged,
    }
}

async fn bounded_response_body(mut response: reqwest::Response) -> Option<String> {
    if response
        .content_length()
        .is_some_and(|length| length > MAX_READINESS_BYTES as u64)
    {
        return None;
    }
    let mut body = Vec::new();
    loop {
        let chunk = match response.chunk().await {
            Ok(Some(chunk)) => chunk,
            Ok(None) => break,
            Err(_) => return None,
        };
        let length = body.len().checked_add(chunk.len())?;
        if length > MAX_READINESS_BYTES {
            return None;
        }
        body.extend_from_slice(&chunk);
    }
    String::from_utf8(body).ok()
}

/// Parses the exact affirmative `/ready` payload into its Controller identity.
///
/// The Python endpoint performs the authoritative service-lock and internal IPC
/// checks. Rust accepts only its exact payload: `Some(None)` is a ready
/// Controller without a desktop identity. A UI identity alone names only where
/// the UI runs from, so it is validated and then set aside.
fn parse_readiness_body(body: &str) -> Option<Option<String>> {
    let Ok(payload) = serde_json::from_str::<serde_json::Value>(body) else {
        return None;
    };
    let object = payload.as_object()?;
    let controller_runtime_id = match object.get("desktop_runtime_id") {
        Some(value) => Some(value.as_str()?),
        None => None,
    };
    let ui_runtime_id = match object.get("desktop_ui_runtime_id") {
        Some(value) => Some(value.as_str()?),
        None => None,
    };
    let expected_len = 3 + usize::from(controller_runtime_id.is_some() || ui_runtime_id.is_some());
    let keys_valid = object.keys().all(|key| {
        matches!(
            key.as_str(),
            "schema_version" | "product" | "ready" | "desktop_runtime_id" | "desktop_ui_runtime_id"
        )
    });
    if (controller_runtime_id.is_some() && ui_runtime_id.is_some())
        || object.len() != expected_len
        || !keys_valid
        || object.get("schema_version").and_then(serde_json::Value::as_u64) != Some(1)
        || object.get("product").and_then(serde_json::Value::as_str) != Some("avibe")
        || object.get("ready").and_then(serde_json::Value::as_bool) != Some(true)
        || controller_runtime_id
            .or(ui_runtime_id)
            .is_some_and(|value| !is_runtime_id(value))
    {
        return None;
    }
    Some(controller_runtime_id.map(str::to_owned))
}

fn parse_runtime_identity_mismatch_body(body: &str) -> Option<String> {
    let Ok(payload) = serde_json::from_str::<serde_json::Value>(body) else {
        return None;
    };
    let object = payload.as_object()?;
    let runtime_id = object.get("desktop_runtime_id")?.as_str()?;
    if object.len() != 5
        || object.get("schema_version").and_then(serde_json::Value::as_u64) != Some(1)
        || object.get("product").and_then(serde_json::Value::as_str) != Some("avibe")
        || object.get("ready").and_then(serde_json::Value::as_bool) != Some(false)
        || object.get("code").and_then(serde_json::Value::as_str) != Some("runtime_identity_mismatch")
        || !is_runtime_id(runtime_id)
    {
        return None;
    }
    Some(runtime_id.to_owned())
}

fn is_runtime_id(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{Read, Write};
    use std::net::{Shutdown, TcpListener, TcpStream};
    use std::sync::atomic::{AtomicBool, Ordering};
    use std::sync::Arc;

    const READY_BODY: &str = r#"{"schema_version":1,"product":"avibe","ready":true}"#;
    const EXTERNAL_CONTROLLER_BUNDLED_UI_READY_BODY: &str =
        include_str!("../../../tests/fixtures/desktop_ready_external_controller_bundled_ui.json");

    struct TestServer {
        origin: LoopbackOrigin,
        contacted: Arc<AtomicBool>,
        stop: Arc<AtomicBool>,
        handle: Option<std::thread::JoinHandle<()>>,
    }

    impl TestServer {
        fn start(response: Vec<u8>) -> Self {
            let listener = TcpListener::bind("127.0.0.1:0").expect("test listener binds");
            listener.set_nonblocking(true).expect("listener is nonblocking");
            let address = listener.local_addr().expect("listener has an address");
            let origin = LoopbackOrigin::parse(&format!("http://{address}")).expect("test origin is loopback");
            let contacted = Arc::new(AtomicBool::new(false));
            let stop = Arc::new(AtomicBool::new(false));
            let thread_contacted = contacted.clone();
            let thread_stop = stop.clone();
            let handle = std::thread::spawn(move || {
                while !thread_stop.load(Ordering::SeqCst) {
                    match listener.accept() {
                        Ok((mut stream, _)) => {
                            thread_contacted.store(true, Ordering::SeqCst);
                            stream.set_nonblocking(false).expect("test request stream is blocking");
                            stream
                                .set_read_timeout(Some(Duration::from_secs(1)))
                                .expect("test request reads are bounded");
                            let mut request = Vec::new();
                            while !request.windows(4).any(|bytes| bytes == b"\r\n\r\n") {
                                let mut chunk = [0_u8; 1024];
                                let read = stream.read(&mut chunk).expect("test request reads");
                                assert!(read > 0, "test request contains complete headers");
                                request.extend_from_slice(&chunk[..read]);
                                assert!(request.len() <= 16 * 1024, "test request headers are bounded");
                            }
                            stream.write_all(&response).expect("test response writes");
                            break;
                        }
                        Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                            std::thread::sleep(Duration::from_millis(5));
                        }
                        Err(error) => panic!("test listener failed: {error}"),
                    }
                }
            });
            Self {
                origin,
                contacted,
                stop,
                handle: Some(handle),
            }
        }

        fn finish(mut self) -> bool {
            self.stop.store(true, Ordering::SeqCst);
            if let Some(handle) = self.handle.take() {
                handle.join().expect("test server exits");
            }
            self.contacted.load(Ordering::SeqCst)
        }
    }

    fn response(status: &str, headers: &[(&str, String)], body: &[u8]) -> Vec<u8> {
        let mut bytes = format!("HTTP/1.1 {status}\r\nConnection: close\r\n").into_bytes();
        for (name, value) in headers {
            bytes.extend_from_slice(format!("{name}: {value}\r\n").as_bytes());
        }
        bytes.extend_from_slice(b"\r\n");
        bytes.extend_from_slice(body);
        bytes
    }

    #[test]
    fn test_server_waits_for_complete_request_headers() {
        let expected_response = response("200 OK", &[], READY_BODY.as_bytes());
        let server = TestServer::start(expected_response.clone());
        let mut client =
            TcpStream::connect(server.origin.as_str().trim_start_matches("http://")).expect("test client connects");
        client
            .set_read_timeout(Some(Duration::from_millis(50)))
            .expect("test client reads are bounded");

        for fragment in [b"".as_slice(), b"GET /ready HTTP/1.1\r\nHost: localhost\r\n".as_slice()] {
            client.write_all(fragment).expect("test request fragment writes");
            let mut pending_response = [0_u8; 1];
            let error = client
                .read(&mut pending_response)
                .expect_err("test server waits until the headers are complete");
            assert!(matches!(
                error.kind(),
                std::io::ErrorKind::WouldBlock | std::io::ErrorKind::TimedOut
            ));
        }

        client.write_all(b"\r\n").expect("test request headers complete");
        client
            .set_read_timeout(Some(Duration::from_secs(2)))
            .expect("test response reads are bounded");
        let mut actual_response = Vec::new();
        client.read_to_end(&mut actual_response).expect("test response reads");
        assert_eq!(actual_response, expected_response);
        assert!(server.finish());
    }

    #[test]
    fn test_server_rejects_truncated_request_headers() {
        let mut server = TestServer::start(Vec::new());
        let mut client =
            TcpStream::connect(server.origin.as_str().trim_start_matches("http://")).expect("test client connects");
        client
            .write_all(b"GET /ready HTTP/1.1\r\n")
            .expect("test request writes");
        client.shutdown(Shutdown::Write).expect("test request ends");
        let failure = server
            .handle
            .take()
            .expect("test server has a thread")
            .join()
            .expect_err("truncated headers fail the test server");
        assert_eq!(
            failure.downcast_ref::<&str>(),
            Some(&"test request contains complete headers")
        );
    }

    #[test]
    fn test_server_rejects_oversized_request_headers() {
        let mut server = TestServer::start(Vec::new());
        let mut client =
            TcpStream::connect(server.origin.as_str().trim_start_matches("http://")).expect("test client connects");
        client
            .write_all(&vec![b'x'; 16 * 1024 + 1])
            .expect("test request writes");
        let failure = server
            .handle
            .take()
            .expect("test server has a thread")
            .join()
            .expect_err("oversized headers fail the test server");
        assert_eq!(
            failure.downcast_ref::<&str>(),
            Some(&"test request headers are bounded")
        );
    }

    fn tagged_ready_body(runtime_id: &str) -> String {
        format!(r#"{{"schema_version":1,"product":"avibe","ready":true,"desktop_runtime_id":"{runtime_id}"}}"#)
    }

    fn mismatch_body(runtime_id: &str) -> String {
        format!(
            r#"{{"schema_version":1,"product":"avibe","ready":false,"code":"runtime_identity_mismatch","desktop_runtime_id":"{runtime_id}"}}"#
        )
    }

    async fn served_presence(status: &str, body: &[u8], expected: Option<&str>) -> Presence {
        let server = TestServer::start(response(status, &[("Content-Length", body.len().to_string())], body));
        let probe = HttpHealthProbe::new(Duration::from_secs(2)).expect("probe builds");
        let presence = probe.presence(&server.origin, expected).await;
        assert!(server.finish());
        presence
    }

    #[test]
    fn readiness_payloads_parse_to_the_controller_identity_only() {
        let ours = "a".repeat(64);
        assert_eq!(parse_readiness_body(READY_BODY), Some(None));
        assert_eq!(parse_readiness_body(&tagged_ready_body(&ours)), Some(Some(ours)));
        // A UI started from a bundled tree, serving an external Controller.
        assert_eq!(
            parse_readiness_body(EXTERNAL_CONTROLLER_BUNDLED_UI_READY_BODY),
            Some(None)
        );
        assert_eq!(
            parse_readiness_body(&format!(
                r#"{{"schema_version":1,"product":"avibe","ready":true,"desktop_ui_runtime_id":"{}"}}"#,
                "b".repeat(64)
            )),
            Some(None)
        );
    }

    #[test]
    fn accepts_only_the_exact_runtime_identity_mismatch_payload() {
        let runtime_id = "a".repeat(64);
        assert_eq!(
            parse_runtime_identity_mismatch_body(&mismatch_body(&runtime_id)),
            Some(runtime_id)
        );
        assert_eq!(
            parse_runtime_identity_mismatch_body(
                r#"{"schema_version":1,"product":"avibe","ready":false,"code":"runtime_identity_mismatch"}"#
            ),
            None
        );
        for invalid in ["", "bad", &"A".repeat(64)] {
            assert!(parse_runtime_identity_mismatch_body(&mismatch_body(invalid)).is_none());
        }
        assert!(parse_runtime_identity_mismatch_body(
            r#"{"schema_version":1,"product":"avibe","ready":false,"code":"controller_unavailable"}"#
        )
        .is_none());
    }

    #[test]
    fn rejects_ui_only_starting_stale_and_unrelated_bodies() {
        let bodies = [
            "",
            "ok",
            "<html><body>hello</body></html>",
            "{}",
            r#"{"status":"ok"}"#,
            r#"{"ready":true}"#,
            r#"{"schema_version":1,"product":"other","ready":true}"#,
            r#"{"schema_version":2,"product":"avibe","ready":true}"#,
            r#"{"ready":false,"code":"controller_unavailable"}"#,
            r#"{"schema_version":1,"product":"avibe","ready":true,"extra":1}"#,
            r#"{"schema_version":1,"product":"avibe","ready":true,"desktop_runtime_id":"short"}"#,
            r#"{"schema_version":1,"product":"avibe","ready":true,"desktop_ui_runtime_id":"short"}"#,
            r#"{"schema_version":1,"product":"avibe","ready":true,"desktop_ui_runtime_id":null}"#,
            r#"{"schema_version":1,"product":"avibe","ready":true,"desktop_ui_runtime_id":"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"}"#,
            r#"{"schema_version":1,"product":"avibe","ready":true,"desktop_ui_runtime_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","extra":1}"#,
            r#"{"schema_version":1,"product":"avibe","ready":true,"desktop_runtime_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","desktop_ui_runtime_id":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}"#,
            r#"{"ready":"true"}"#,
            "[]",
        ];
        for body in bodies {
            assert!(
                parse_readiness_body(body).is_none(),
                "body {body:?} must not be adopted"
            );
        }
    }

    #[test]
    fn probe_construction_does_not_need_a_server() {
        HttpHealthProbe::new(Duration::from_secs(2)).expect("probe builds");
    }

    #[tokio::test]
    async fn presence_is_decided_by_the_controller_identity_against_the_expected_one() {
        let ours = "a".repeat(64);
        let other = "b".repeat(64);
        let cases = [
            (
                "200 OK",
                tagged_ready_body(&ours),
                Some(ours.as_str()),
                Presence::Mine { ready: true },
            ),
            (
                "503 Service Unavailable",
                mismatch_body(&ours),
                Some(ours.as_str()),
                Presence::Mine { ready: false },
            ),
            (
                "200 OK",
                tagged_ready_body(&other),
                Some(ours.as_str()),
                Presence::Foreign {
                    runtime_id: other.clone(),
                    ready: true,
                },
            ),
            (
                "503 Service Unavailable",
                mismatch_body(&other),
                Some(ours.as_str()),
                Presence::Foreign {
                    runtime_id: other.clone(),
                    ready: false,
                },
            ),
            // A shell without a bundled Runtime has no identity of its own.
            (
                "200 OK",
                tagged_ready_body(&ours),
                None,
                Presence::Foreign {
                    runtime_id: ours.clone(),
                    ready: true,
                },
            ),
            (
                "200 OK",
                READY_BODY.to_owned(),
                Some(ours.as_str()),
                Presence::Unmanaged,
            ),
            // A UI identity never makes an external Controller this shell's.
            (
                "200 OK",
                EXTERNAL_CONTROLLER_BUNDLED_UI_READY_BODY.to_owned(),
                Some(ours.as_str()),
                Presence::Unmanaged,
            ),
            // The body proves readiness only with its own status.
            (
                "500 Internal Server Error",
                tagged_ready_body(&ours),
                Some(ours.as_str()),
                Presence::Unknown,
            ),
            ("200 OK", mismatch_body(&ours), Some(ours.as_str()), Presence::Unknown),
            (
                "503 Service Unavailable",
                r#"{"schema_version":1,"product":"avibe","ready":false,"code":"controller_unavailable"}"#.to_owned(),
                Some(ours.as_str()),
                Presence::Unknown,
            ),
            ("200 OK", "ok".to_owned(), Some(ours.as_str()), Presence::Unknown),
        ];
        for (status, body, expected, presence) in cases {
            assert_eq!(
                served_presence(status, body.as_bytes(), expected).await,
                presence,
                "{status} {body}"
            );
        }
    }

    #[tokio::test]
    async fn a_refused_connection_is_absent() {
        let listener = TcpListener::bind("127.0.0.1:0").expect("test listener binds");
        let origin = LoopbackOrigin::parse(&format!("http://{}", listener.local_addr().expect("address")))
            .expect("test origin is loopback");
        drop(listener);
        let probe = HttpHealthProbe::new(crate::bootstrap::DEFAULT_PROBE_TIMEOUT).expect("probe builds");

        assert_eq!(probe.presence(&origin, Some(&"a".repeat(64))).await, Presence::Absent);
    }

    #[test]
    fn no_other_connect_failure_proves_absence() {
        // The shape reqwest reports: a connect error caused by the socket's error.
        #[derive(Debug)]
        struct Connect(std::io::Error);
        impl std::fmt::Display for Connect {
            fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
                formatter.write_str("tcp connect error")
            }
        }
        impl std::error::Error for Connect {
            fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
                Some(&self.0)
            }
        }

        for (kind, refused) in [
            (std::io::ErrorKind::ConnectionRefused, true),
            (std::io::ErrorKind::PermissionDenied, false),
            (std::io::ErrorKind::AddrNotAvailable, false),
            (std::io::ErrorKind::TimedOut, false),
        ] {
            assert_eq!(is_connection_refused(&Connect(kind.into())), refused, "{kind:?}");
        }
    }

    #[tokio::test]
    async fn the_probe_does_not_follow_redirects() {
        let target = TestServer::start(response(
            "200 OK",
            &[("Content-Length", READY_BODY.len().to_string())],
            READY_BODY.as_bytes(),
        ));
        let redirect = TestServer::start(response(
            "302 Found",
            &[("Location", format!("{}/ready", target.origin.as_str()))],
            &[],
        ));
        let probe = HttpHealthProbe::new(Duration::from_secs(2)).expect("probe builds");

        assert_eq!(probe.presence(&redirect.origin, None).await, Presence::Unknown);
        assert!(redirect.finish());
        assert!(!target.finish(), "the redirected listener must never be contacted");
    }

    #[tokio::test]
    async fn the_probe_rejects_an_oversized_streamed_body() {
        let body = vec![b'x'; MAX_READINESS_BYTES + 1];
        let server = TestServer::start(response("200 OK", &[], &body));
        let probe = HttpHealthProbe::new(Duration::from_secs(2)).expect("probe builds");

        assert_eq!(probe.presence(&server.origin, None).await, Presence::Unknown);
        assert!(server.finish());
    }
}
