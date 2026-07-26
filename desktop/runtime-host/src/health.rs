//! Readiness probing against the Avibe Web UI server.

use std::time::Duration;

use async_trait::async_trait;

use crate::origin::LoopbackOrigin;

const MAX_READINESS_BYTES: usize = 1024;

/// Answers one question: are the Avibe UI and Controller serving this origin?
///
/// The answer is deliberately a bare `bool`. Transport errors and response
/// bodies stay inside the probe so nothing from the network reaches the
/// bootstrap UI.
#[async_trait]
pub trait HealthProbe: Send + Sync {
    async fn is_healthy(&self, origin: &LoopbackOrigin) -> bool;
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
    async fn is_healthy(&self, origin: &LoopbackOrigin) -> bool {
        let Ok(mut response) = self.client.get(origin.readiness_url()).send().await else {
            return false;
        };
        if !response.status().is_success() {
            return false;
        }
        if response
            .content_length()
            .is_some_and(|length| length > MAX_READINESS_BYTES as u64)
        {
            return false;
        }

        let mut body = Vec::new();
        loop {
            let chunk = match response.chunk().await {
                Ok(Some(chunk)) => chunk,
                Ok(None) => break,
                Err(_) => return false,
            };
            let Some(length) = body.len().checked_add(chunk.len()) else {
                return false;
            };
            if length > MAX_READINESS_BYTES {
                return false;
            }
            body.extend_from_slice(&chunk);
        }
        let Ok(body) = std::str::from_utf8(&body) else {
            return false;
        };
        is_avibe_readiness_body(body)
    }
}

/// Whether a `/ready` body proves both the UI and Controller are ready.
///
/// The Python endpoint performs the authoritative service-lock and internal IPC
/// checks. Rust accepts only its exact affirmative payload.
pub fn is_avibe_readiness_body(body: &str) -> bool {
    let Ok(payload) = serde_json::from_str::<serde_json::Value>(body) else {
        return false;
    };
    payload.as_object().is_some_and(|object| {
        object.len() == 3
            && object.get("schema_version").and_then(serde_json::Value::as_u64) == Some(1)
            && object.get("product").and_then(serde_json::Value::as_str) == Some("avibe")
            && object.get("ready").and_then(serde_json::Value::as_bool) == Some(true)
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{Read, Write};
    use std::net::TcpListener;
    use std::sync::atomic::{AtomicBool, Ordering};
    use std::sync::Arc;

    const READY_BODY: &str = r#"{"schema_version":1,"product":"avibe","ready":true}"#;

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
                            let _ = stream.set_read_timeout(Some(Duration::from_secs(1)));
                            let mut request = [0_u8; 4096];
                            let _ = stream.read(&mut request);
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
    fn accepts_the_exact_runtime_readiness_payload() {
        assert!(is_avibe_readiness_body(
            r#"{"schema_version":1,"product":"avibe","ready":true}"#
        ));
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
            r#"{"ready":"true"}"#,
            "[]",
        ];
        for body in bodies {
            assert!(!is_avibe_readiness_body(body), "body {body:?} must not be adopted");
        }
    }

    #[test]
    fn probe_construction_does_not_need_a_server() {
        HttpHealthProbe::new(Duration::from_secs(2)).expect("probe builds");
    }

    #[tokio::test]
    async fn the_probe_accepts_the_exact_body_from_its_loopback_listener() {
        let server = TestServer::start(response(
            "200 OK",
            &[("Content-Length", READY_BODY.len().to_string())],
            READY_BODY.as_bytes(),
        ));
        let probe = HttpHealthProbe::new(Duration::from_secs(2)).expect("probe builds");

        assert!(probe.is_healthy(&server.origin).await);
        assert!(server.finish());
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

        assert!(!probe.is_healthy(&redirect.origin).await);
        assert!(redirect.finish());
        assert!(!target.finish(), "the redirected listener must never be contacted");
    }

    #[tokio::test]
    async fn the_probe_rejects_an_oversized_streamed_body() {
        let body = vec![b'x'; MAX_READINESS_BYTES + 1];
        let server = TestServer::start(response("200 OK", &[], &body));
        let probe = HttpHealthProbe::new(Duration::from_secs(2)).expect("probe builds");

        assert!(!probe.is_healthy(&server.origin).await);
        assert!(server.finish());
    }
}
