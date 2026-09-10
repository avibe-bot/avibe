use std::collections::VecDeque;
use std::io::{Read, Write};
use std::net::TcpListener;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use avibe_runtime_host::notifications::{HttpNotificationTransport, NotificationTransport, SseDecoder};
use avibe_runtime_host::LoopbackOrigin;
use serde_json::json;

struct FakeServer {
    origin: LoopbackOrigin,
    requests: Arc<Mutex<Vec<String>>>,
    stopped: Arc<AtomicBool>,
    thread: Option<std::thread::JoinHandle<()>>,
}

impl FakeServer {
    fn start(responses: impl IntoIterator<Item = Vec<u8>>) -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        listener.set_nonblocking(true).unwrap();
        let origin = LoopbackOrigin::parse(&format!("http://{}", listener.local_addr().unwrap())).unwrap();
        let requests = Arc::new(Mutex::new(Vec::new()));
        let received = requests.clone();
        let stopped = Arc::new(AtomicBool::new(false));
        let stop = stopped.clone();
        let mut responses = responses.into_iter().collect::<VecDeque<_>>();
        let thread = std::thread::spawn(move || {
            while !stop.load(Ordering::SeqCst) {
                match listener.accept() {
                    Ok((mut stream, _)) => {
                        stream.set_nonblocking(false).unwrap();
                        stream.set_read_timeout(Some(Duration::from_millis(50))).unwrap();
                        stream.set_write_timeout(Some(Duration::from_secs(1))).unwrap();
                        let mut request = Vec::new();
                        let deadline = Instant::now() + Duration::from_secs(3);
                        while !request.windows(4).any(|chunk| chunk == b"\r\n\r\n") {
                            if stop.load(Ordering::SeqCst) {
                                return;
                            }
                            assert!(
                                Instant::now() < deadline,
                                "request headers must arrive before the deadline"
                            );
                            let mut buffer = [0; 1024];
                            match stream.read(&mut buffer) {
                                Ok(0) => panic!("request headers were truncated"),
                                Ok(length) => request.extend_from_slice(&buffer[..length]),
                                Err(error)
                                    if matches!(
                                        error.kind(),
                                        std::io::ErrorKind::WouldBlock | std::io::ErrorKind::TimedOut
                                    ) =>
                                {
                                    continue
                                }
                                Err(error) => panic!("fake request failed: {error}"),
                            }
                            assert!(request.len() <= 16 * 1024);
                        }
                        received.lock().unwrap().push(String::from_utf8(request).unwrap());
                        let response = responses.pop_front().expect("only the expected requests are allowed");
                        let _ = stream.write_all(&response);
                    }
                    Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                        std::thread::sleep(Duration::from_millis(1));
                    }
                    Err(error) => panic!("fake listener failed: {error}"),
                }
            }
        });
        Self {
            origin,
            requests,
            stopped,
            thread: Some(thread),
        }
    }

    fn transport(&self) -> HttpNotificationTransport {
        HttpNotificationTransport::new(self.origin.clone()).unwrap()
    }
}

impl Drop for FakeServer {
    fn drop(&mut self) {
        self.stopped.store(true, Ordering::SeqCst);
        if let Some(thread) = self.thread.take() {
            let result = thread.join();
            if !std::thread::panicking() {
                result.expect("fake server exited cleanly");
            }
        }
    }
}

fn response(status: &str, content_type: &str, body: &[u8]) -> Vec<u8> {
    let mut response = format!(
        "HTTP/1.1 {status}\r\nContent-Type: {content_type}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
        body.len()
    )
    .into_bytes();
    response.extend_from_slice(body);
    response
}

#[tokio::test]
async fn production_framing_uses_only_the_adopted_origin_and_reconnect_never_sends_a_replay_cursor() {
    let event = b": stream connected\n\nevent: connected\ndata: {}\n\n: ping\n\nid: never-replay\nevent: vaults.updated\ndata: {\"type\":\"vaults.updated\",\"data\":{\"request_id\":\"request\",\"request_status\":\"pending\"}}\n\n";
    let server = FakeServer::start([
        response("200 OK", "text/event-stream; charset=utf-8", event),
        response("200 OK", "text/event-stream", b": stream connected\n\n"),
    ]);
    let transport = server.transport();
    let mut first = transport.connect().await.unwrap();
    let mut decoder = SseDecoder::new();
    let mut frames = Vec::new();
    while let Some(chunk) = first.next_chunk().await.unwrap() {
        frames.extend(decoder.push(&chunk));
    }
    assert_eq!(frames.len(), 2);
    assert_eq!(frames[1].event, "vaults.updated");
    let mut second = transport.connect().await.unwrap();
    while second.next_chunk().await.unwrap().is_some() {}
    let requests = server.requests.lock().unwrap();
    assert_eq!(requests.len(), 2);
    for request in requests.iter() {
        let normalized = request.to_ascii_lowercase();
        assert!(request.starts_with("GET /api/events HTTP/1.1\r\n"));
        assert!(normalized.contains("accept: text/event-stream\r\n"));
        for forbidden in ["last-event-id:", "authorization:", "cookie:"] {
            assert!(!normalized.contains(forbidden));
        }
    }
}

#[tokio::test]
async fn stream_and_detail_redirects_never_delegate_trust_to_another_origin() {
    let target = FakeServer::start([]);
    let redirect = format!(
        "HTTP/1.1 302 Found\r\nLocation: {}/api/events\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",
        target.origin.as_str()
    )
    .into_bytes();
    let server = FakeServer::start([redirect.clone(), redirect]);
    let transport = server.transport();
    assert!(transport.connect().await.is_err());
    assert!(transport.run_timestamps("run").await.is_none());
    assert!(target.requests.lock().unwrap().is_empty());
    assert_eq!(server.requests.lock().unwrap().len(), 2);
}

#[tokio::test]
async fn stream_requires_a_successful_sse_response_not_an_arbitrary_live_http_endpoint() {
    let server = FakeServer::start([
        response("200 OK", "text/html", b"<html>not SSE</html>"),
        response("503 Unavailable", "text/event-stream", b": unavailable\n\n"),
        response("204 No Content", "text/event-stream", b""),
    ]);
    for _ in 0..3 {
        assert!(server.transport().connect().await.is_err());
    }
}

#[tokio::test]
async fn refetch_requires_the_requested_run_and_bounds_untrusted_detail_bodies() {
    let valid = json!({"ok": true, "run": {
        "id": "run", "started_at": "2026-09-10T12:00:00Z", "updated_at": "2026-09-10T12:00:30Z",
    }});
    let server = FakeServer::start([
        response("200 OK", "application/json", valid.to_string().as_bytes()),
        response(
            "200 OK",
            "application/json",
            json!({"ok": true, "run": {"id": "another"}}).to_string().as_bytes(),
        ),
        response("404 Not Found", "application/json", b"{}"),
        response("200 OK", "application/json", b"not-json"),
        response("200 OK", "application/json", &vec![b'x'; 256 * 1024 + 1]),
        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 999999999\r\nConnection: close\r\n\r\n"
            .to_vec(),
    ]);
    let transport = server.transport();
    let detail = transport.run_timestamps("run").await.unwrap();
    assert_eq!(detail.started_at, Some("2026-09-10T12:00:00Z".into()));
    assert_eq!(detail.updated_at, Some("2026-09-10T12:00:30Z".into()));
    for _ in 0..5 {
        assert!(transport.run_timestamps("run").await.is_none());
    }
    for request in server.requests.lock().unwrap().iter() {
        assert!(request.starts_with("GET /api/harness/runs/run HTTP/1.1\r\n"));
    }
}

#[tokio::test]
async fn identifiers_are_encoded_as_one_path_segment_not_runtime_selected_routes() {
    let server = FakeServer::start([response("404 Not Found", "application/json", b"{}")]);
    let transport = server.transport();
    for key in ["", ".", ".."] {
        assert!(transport.run_timestamps(key).await.is_none());
    }
    assert!(server.requests.lock().unwrap().is_empty());
    assert!(transport
        .run_timestamps("../settings?token=secret#片段")
        .await
        .is_none());
    let requests = server.requests.lock().unwrap();
    assert_eq!(requests.len(), 1);
    assert!(
        requests[0].starts_with("GET /api/harness/runs/..%2Fsettings%3Ftoken=secret%23%E7%89%87%E6%AE%B5 HTTP/1.1\r\n")
    );
}
