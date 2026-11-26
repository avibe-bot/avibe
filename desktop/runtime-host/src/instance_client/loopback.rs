//! A state-validated provider callback and its bounded, read-only result page.
//! Authentication ends once; its ephemeral port remains reachable for ten minutes.
mod page;
pub use page::CallbackCopy;

use std::time::{Duration, Instant};

use base64::{engine::general_purpose::URL_SAFE_NO_PAD, Engine as _};
use sha2::{Digest, Sha256};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::{TcpListener, TcpStream},
};
use url::Url;

use super::{protocol::ClientError, transport::Epoch};

pub const CALLBACK_PATH: &str = "/callback";
/// Longest time a browser sign-in may take before the attempt ends: GoTrue's default PKCE flow
/// state lives 300 s after authorize, so a later code could not be redeemed anyway.
pub const WAIT: Duration = Duration::from_secs(300);
/// Presentation only: this never extends authentication or permits another exchange.
const RESULT_GRACE: Duration = Duration::from_secs(600);
/// How often an idle wait looks at the account generation.
const TICK: Duration = Duration::from_millis(250);
/// One request's head must arrive within this time and size.
const READ_TIMEOUT: Duration = Duration::from_secs(5);
const MAX_REQUEST_HEAD: usize = 8 * 1024;
/// Stray or invalid connections a transaction tolerates before it ends.
const MAX_CONNECTIONS: usize = 32;
const MAX_VALUE: usize = 4 * 1024;

/// 32 random bytes as canonical unpadded base64url (43 characters).
pub fn random_token() -> Result<String, ClientError> {
    let mut bytes = [0u8; 32];
    getrandom::fill(&mut bytes).map_err(|_| ClientError::new("provider_unavailable"))?;
    Ok(URL_SAFE_NO_PAD.encode(bytes))
}

/// The PKCE pair and state of one attempt; they stay in native memory.
pub struct Secrets {
    pub verifier: String,
    pub challenge: String,
    pub state: String,
}
impl Secrets {
    pub fn new() -> Result<Self, ClientError> {
        let verifier = random_token()?;
        let challenge = URL_SAFE_NO_PAD.encode(Sha256::digest(verifier.as_bytes()));
        Ok(Self {
            verifier,
            challenge,
            state: random_token()?,
        })
    }
}

/// A safe failure category for the App, with bounded secondary browser detail.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ProviderFailure {
    pub code: &'static str,
    pub description: Option<String>,
}
impl ProviderFailure {
    pub fn from_code(code: &str) -> Self {
        Self {
            code: match code {
                "provider_account_conflict" => "provider_account_conflict",
                "provider_email_unverified" => "provider_email_unverified",
                "provider_denied" => "provider_denied",
                "provider_server_error" => "provider_server_error",
                _ => "provider_failed",
            },
            description: None,
        }
    }
}

#[derive(Debug, PartialEq, Eq)]
pub enum Callback {
    Code(String),
    Failed(ProviderFailure),
}

#[derive(Clone, Debug)]
pub enum ResultPage {
    Complete,
    VerifyInApp,
    Failed(ProviderFailure),
    Cancelled,
    TimedOut,
}

#[derive(Debug, PartialEq, Eq)]
enum Verdict {
    Done(Callback),
    Fragment,
    NotFound,
    Rejected,
}

/// Owns the listener until the complete native account action settles. Dropping an
/// unfinished owner cancels authentication but still gives late browsers an answer.
pub struct Loopback {
    listener: Option<TcpListener>,
    port: u16,
    copy: CallbackCopy,
    pending: Option<TcpStream>,
    result: Option<ResultPage>,
    waited: bool,
}

impl Loopback {
    pub async fn bind(copy: &CallbackCopy) -> Result<Self, ClientError> {
        let listener = TcpListener::bind(("127.0.0.1", 0))
            .await
            .map_err(|_| ClientError::new("provider_unavailable"))?;
        let port = listener
            .local_addr()
            .map_err(|_| ClientError::new("provider_unavailable"))?
            .port();
        Ok(Self {
            listener: Some(listener),
            port,
            copy: copy.clone(),
            pending: None,
            result: None,
            waited: false,
        })
    }

    pub fn redirect_uri(&self, state: &str) -> Url {
        let mut url = Url::parse(&format!("http://127.0.0.1:{}{CALLBACK_PATH}", self.port)).expect("loopback URL");
        url.query_pairs_mut().append_pair("state", state);
        url
    }

    /// Finalized by the coordinator, after persistence, never merely on code arrival.
    pub fn complete(&mut self, result: ResultPage) {
        if self.result.is_none() {
            self.result = Some(result);
        }
    }

    pub async fn wait(&mut self, state: &str, epoch: &Epoch, wait: Duration) -> Result<Callback, ClientError> {
        if std::mem::replace(&mut self.waited, true) {
            return Err(ClientError::new("invalid_action"));
        }
        let deadline = Instant::now() + wait;
        let host = format!("127.0.0.1:{}", self.port);
        let mut connections = 0usize;
        loop {
            epoch.check()?;
            let remaining = deadline.saturating_duration_since(Instant::now());
            if remaining.is_zero() {
                self.complete(ResultPage::TimedOut);
                return Err(ClientError::new("provider_timeout"));
            }
            let accepted = match tokio::time::timeout(
                remaining.min(TICK),
                self.listener.as_ref().expect("owned listener").accept(),
            )
            .await
            {
                Err(_) => continue,
                Ok(accepted) => accepted,
            };
            connections += 1;
            if connections > MAX_CONNECTIONS {
                self.complete(ResultPage::Failed(ProviderFailure::from_code("provider_failed")));
                return Err(ClientError::new("provider_failed"));
            }
            let Ok((mut stream, peer)) = accepted else { continue };
            if !peer.ip().is_loopback() {
                continue;
            }
            let head = tokio::time::timeout(remaining.min(READ_TIMEOUT), read_head(&mut stream))
                .await
                .ok()
                .flatten();
            if let Err(error) = epoch.check() {
                self.pending = Some(stream);
                return Err(error);
            }
            if Instant::now() >= deadline {
                self.pending = Some(stream);
                self.complete(ResultPage::TimedOut);
                return Err(ClientError::new("provider_timeout"));
            }
            let Some(head) = head else { continue };
            match judge(&head, &host, state) {
                Verdict::Done(callback) => {
                    // Hold the first browser response through token exchange and persistence.
                    // A cancelled or failed native action cannot leave a success page behind.
                    self.pending = Some(stream);
                    if let Callback::Failed(failure) = &callback {
                        self.complete(ResultPage::Failed(failure.clone()));
                    }
                    return Ok(callback);
                }
                Verdict::Fragment => send_page(&mut stream, "200 OK", &page::fragment(&self.copy)).await,
                Verdict::NotFound => send_page(&mut stream, "404 Not Found", "").await,
                Verdict::Rejected => send_page(&mut stream, "400 Bad Request", "").await,
            }
        }
    }
}

impl Drop for Loopback {
    fn drop(&mut self) {
        let Some(listener) = self.listener.take() else { return };
        // Runtime shutdown is App exit: no background responder survives it.
        let Ok(runtime) = tokio::runtime::Handle::try_current() else {
            return;
        };
        let body = page::result(&self.copy, &self.result.take().unwrap_or(ResultPage::Cancelled));
        let pending = self.pending.take();
        runtime.spawn(serve_result(listener, pending, body, RESULT_GRACE));
    }
}

/// Only rendered HTML reaches this task: no state, code, verifier, or account jar.
async fn serve_result(listener: TcpListener, pending: Option<TcpStream>, body: String, grace: Duration) {
    let work = async {
        let body = std::sync::Arc::new(body);
        let mut replies = tokio::task::JoinSet::new();
        if let Some(mut stream) = pending {
            let body = body.clone();
            replies.spawn(async move {
                send_page(&mut stream, "200 OK", &body).await;
            });
        }
        loop {
            // Bound idle preconnects without letting one block every ordinary browser request.
            if replies.len() >= MAX_CONNECTIONS {
                let _ = replies.join_next().await;
            }
            tokio::select! {
                Some(_) = replies.join_next(), if !replies.is_empty() => {},
                accepted = listener.accept() => {
                    let Ok((mut stream, _)) = accepted else { break };
                    let body = body.clone();
                    replies.spawn(async move {
                        if tokio::time::timeout(READ_TIMEOUT, read_head(&mut stream)).await.ok().flatten().is_some() {
                            send_page(&mut stream, "200 OK", &body).await;
                        }
                    });
                }
            }
        }
    };
    // Dropping the future also aborts its JoinSet; no socket outlives this deadline.
    let _ = tokio::time::timeout(grace, work).await;
}

async fn send_page(stream: &mut TcpStream, status: &str, body: &str) {
    let reply = format!(
        "HTTP/1.1 {status}\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: {}\r\nCache-Control: no-store\r\nReferrer-Policy: no-referrer\r\nContent-Security-Policy: default-src 'none'; script-src 'sha256-{}'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'\r\nX-Content-Type-Options: nosniff\r\nConnection: close\r\n\r\n{body}",
        body.len(), page::script_hash()
    );
    let _ = tokio::time::timeout(READ_TIMEOUT, async {
        stream.write_all(reply.as_bytes()).await?;
        stream.shutdown().await
    })
    .await;
}

/// Reads up to the end of the request head, bounded in size.
async fn read_head(stream: &mut TcpStream) -> Option<String> {
    let mut buffer = Vec::with_capacity(1024);
    let mut chunk = [0u8; 1024];
    loop {
        let read = stream.read(&mut chunk).await.ok()?;
        if read == 0 {
            return None;
        }
        buffer.extend_from_slice(&chunk[..read]);
        if buffer.len() > MAX_REQUEST_HEAD {
            return None;
        }
        if buffer.windows(4).any(|window| window == b"\r\n\r\n") {
            return String::from_utf8(buffer).ok();
        }
    }
}

/// The exact request this transaction accepts. Anything else is not ours.
fn judge(head: &str, host: &str, state: &str) -> Verdict {
    let mut lines = head.split("\r\n");
    let Some(request_line) = lines.next() else {
        return Verdict::Rejected;
    };
    let mut parts = request_line.split(' ');
    let (Some(method), Some(target), Some(version), None) = (parts.next(), parts.next(), parts.next(), parts.next())
    else {
        return Verdict::Rejected;
    };
    if method != "GET" || !matches!(version, "HTTP/1.1" | "HTTP/1.0") {
        return Verdict::Rejected;
    }
    let mut hosts = lines
        .take_while(|line| !line.is_empty())
        .filter_map(|line| line.split_once(':'))
        .filter(|(name, _)| name.trim().eq_ignore_ascii_case("host"))
        .map(|(_, value)| value.trim());
    if hosts.next() != Some(host) || hosts.next().is_some() {
        return Verdict::Rejected;
    }
    let (path, query) = target.split_once('?').unwrap_or((target, ""));
    if path != CALLBACK_PATH {
        return Verdict::NotFound;
    }
    if query_state_only(query, state) {
        return Verdict::Fragment;
    }
    parse_callback(query, state).map_or(Verdict::Rejected, Verdict::Done)
}

fn query_state_only(query: &str, expected: &str) -> bool {
    let pairs: Vec<_> = url::form_urlencoded::parse(query.as_bytes()).collect();
    pairs.len() == 1 && pairs[0].0 == "state" && constant_time_eq(pairs[0].1.as_bytes(), expected.as_bytes())
}

/// Supabase's callback grammar (GoTrue v2.198.0): our `state`, plus `code`, or
/// `error` with optional `error_code` and `error_description`. No other key,
/// no repeated key, no oversized value, and exactly one of code/error. `state`,
/// `code` and `error` are never empty; GoTrue sets `error_description` (and an
/// API error's `error_code`) even when the provider gave none, so those two may
/// be empty: a provider's bare `error=access_denied` arrives as
/// `error=access_denied&error_description=`.
fn parse_callback(query: &str, expected_state: &str) -> Option<Callback> {
    const KEYS: [&str; 5] = ["state", "code", "error", "error_code", "error_description"];
    const MAY_BE_EMPTY: [&str; 2] = ["error_code", "error_description"];
    let mut values: [Option<String>; 5] = Default::default();
    for (key, value) in url::form_urlencoded::parse(query.as_bytes()) {
        let index = KEYS.iter().position(|known| *known == key)?;
        if values[index].is_some()
            || (value.is_empty() && !MAY_BE_EMPTY.contains(&KEYS[index]))
            || value.len() > MAX_VALUE
        {
            return None;
        }
        values[index] = Some(value.into_owned());
    }
    let [state, code, error, error_code, description] = values;
    if !constant_time_eq(state?.as_bytes(), expected_state.as_bytes()) {
        return None;
    }
    match (code, error) {
        (Some(code), None) if error_code.is_none() && description.is_none() => Some(Callback::Code(code)),
        (None, Some(error)) => {
            let code = if error_code.as_deref() == Some("provider_email_needs_verification") {
                "provider_email_unverified"
            } else if error_code.as_deref() == Some("unexpected_failure")
                && description.as_ref().is_some_and(|text| {
                    text.to_ascii_lowercase()
                        .contains("multiple accounts with the same email address in the same linking domain")
                })
            {
                "provider_account_conflict"
            } else if error == "access_denied" {
                "provider_denied"
            } else if matches!(error.as_str(), "server_error" | "temporarily_unavailable") {
                "provider_server_error"
            } else {
                "provider_failed"
            };
            Some(Callback::Failed(ProviderFailure {
                code,
                description: description.filter(|text| !text.is_empty()),
            }))
        }
        _ => None,
    }
}

fn constant_time_eq(left: &[u8], right: &[u8]) -> bool {
    left.len() == right.len() && left.iter().zip(right).fold(0u8, |acc, (a, b)| acc | (a ^ b)) == 0
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::{atomic::AtomicU64, Arc};

    const STATE: &str = "s1-state";

    fn copy() -> CallbackCopy {
        CallbackCopy {
            done: "Done <here> & now".into(),
            failed: "没有完成".into(),
            denied: "没有授权".into(),
            cancelled: "Cancelled".into(),
            timed_out: "Timed out".into(),
            server_error: "Service failed".into(),
            account_conflict: "Multiple accounts".into(),
            ..Default::default()
        }
    }
    /// The listener tests run one at a time, so no other one's socket is on
    /// the port a test probes.
    static PORTS: tokio::sync::Mutex<()> = tokio::sync::Mutex::const_new(());

    #[test]
    fn pkce_challenge_is_s256_of_the_verifier() {
        let secrets = Secrets::new().unwrap();
        assert_eq!(secrets.verifier.len(), 43);
        assert_eq!(secrets.state.len(), 43);
        assert_ne!(secrets.verifier, secrets.state);
        assert_eq!(
            secrets.challenge,
            URL_SAFE_NO_PAD.encode(Sha256::digest(secrets.verifier.as_bytes()))
        );
    }

    #[test]
    fn callback_grammar_is_exact() {
        assert_eq!(
            parse_callback("state=s1-state&code=abc", STATE),
            Some(Callback::Code("abc".into()))
        );
        assert_eq!(
            parse_callback("code=abc&state=s1-state", STATE),
            Some(Callback::Code("abc".into()))
        );
        assert_eq!(
            parse_callback(
                "state=s1-state&error=access_denied&error_code=provider_email_needs_verification&error_description=x",
                STATE
            ),
            Some(Callback::Failed(ProviderFailure {
                code: "provider_email_unverified",
                description: Some("x".into())
            }))
        );
        assert_eq!(
            parse_callback("state=s1-state&error=server_error", STATE),
            Some(Callback::Failed(ProviderFailure::from_code("provider_server_error")))
        );
        // A provider's bare refusal, as GoTrue v2.198.0 forwards it (empty description).
        assert_eq!(
            parse_callback("error=access_denied&error_description=&state=s1-state", STATE),
            Some(Callback::Failed(ProviderFailure::from_code("provider_denied")))
        );
        assert_eq!(
            parse_callback(
                "error=server_error&error_code=&error_description=&state=s1-state",
                STATE
            ),
            Some(Callback::Failed(ProviderFailure::from_code("provider_server_error")))
        );
        for bad in [
            "",
            "code=abc",
            "state=other&code=abc",
            "state=s1-state",
            "state=s1-state&code=abc&error=x",
            "state=s1-state&code=abc&code=def",
            "state=s1-state&state=s1-state&code=abc",
            "state=s1-state&code=",
            "state=s1-state&code=abc&sb=",
            "state=s1-state&code=abc&error_code=x",
            "state=s1-state&code=abc&error_description=",
            "state=s1-state&code=abc&unknown=1",
            "state=s1-state&error=",
            "state=s1-state&error=&error_description=x",
            "state=&code=abc",
            "state=s1-state&error=access_denied&error_description=&error_description=",
        ] {
            assert_eq!(parse_callback(bad, STATE), None, "{bad}");
        }
        let long = "a".repeat(MAX_VALUE + 1);
        assert_eq!(parse_callback(&format!("state=s1-state&code={long}"), STATE), None);
    }

    #[test]
    fn request_must_be_this_transactions_get() {
        let host = "127.0.0.1:4100";
        let ok = |line: &str, headers: &str| judge(&format!("{line}\r\n{headers}\r\n\r\n"), host, STATE);
        assert_eq!(
            ok("GET /callback?state=s1-state&code=c HTTP/1.1", "Host: 127.0.0.1:4100"),
            Verdict::Done(Callback::Code("c".into()))
        );
        assert_eq!(
            ok("POST /callback?state=s1-state&code=c HTTP/1.1", "Host: 127.0.0.1:4100"),
            Verdict::Rejected
        );
        assert_eq!(
            ok("GET /callback?state=s1-state&code=c HTTP/1.1", "Host: localhost:4100"),
            Verdict::Rejected
        );
        assert_eq!(
            ok(
                "GET /callback?state=s1-state&code=c HTTP/1.1",
                "Host: 127.0.0.1:4100\r\nHost: 127.0.0.1:4100"
            ),
            Verdict::Rejected
        );
        assert_eq!(
            ok("GET /favicon.ico HTTP/1.1", "Host: 127.0.0.1:4100"),
            Verdict::NotFound
        );
        assert_eq!(
            ok("GET /callback/x?state=s1-state&code=c HTTP/1.1", "Host: 127.0.0.1:4100"),
            Verdict::NotFound
        );
        assert_eq!(
            ok("GET /callback?state=s1-state&code=c", "Host: 127.0.0.1:4100"),
            Verdict::Rejected
        );
    }

    async fn get(port: u16, target: &str) -> String {
        let mut stream = TcpStream::connect(("127.0.0.1", port)).await.unwrap();
        stream
            .write_all(format!("GET {target} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n").as_bytes())
            .await
            .unwrap();
        let mut reply = String::new();
        tokio::time::timeout(Duration::from_secs(3), stream.read_to_string(&mut reply))
            .await
            .unwrap()
            .unwrap();
        reply
    }

    #[tokio::test]
    async fn consumes_only_once_and_serves_immutable_result_to_every_late_path() {
        let _ports = PORTS.lock().await;
        let epoch = Epoch::new(Arc::new(AtomicU64::new(7)), 7);
        let mut loopback = Loopback::bind(&copy()).await.unwrap();
        let port = loopback.port;
        let waiting = tokio::spawn(async move {
            let callback = loopback.wait(STATE, &epoch, Duration::from_secs(5)).await.unwrap();
            assert_eq!(callback, Callback::Code("secret-code".into()));
            assert_eq!(
                loopback.wait(STATE, &epoch, WAIT).await.unwrap_err().code,
                "invalid_action"
            );
            loopback.complete(ResultPage::Complete);
            loopback.complete(ResultPage::Cancelled); // terminal result cannot change
        });
        assert!(get(port, "/favicon.ico").await.starts_with("HTTP/1.1 404"));
        let wrong = get(port, "/callback?state=other&code=leaked").await;
        assert!(wrong.starts_with("HTTP/1.1 400") && !wrong.contains("leaked"));
        let done = get(port, "/callback?state=s1-state&code=secret-code").await;
        assert!(done.starts_with("HTTP/1.1 200") && !done.contains("secret-code"));
        assert!(done.contains("Done &lt;here&gt; &amp; now"), "{done}");
        waiting.await.unwrap();
        for target in [
            "/callback",
            "/callback?state=s1-state&code=secret-code",
            "/favicon.ico",
            "/prefetch",
            "/callback?state=wrong&error=server_error",
        ] {
            assert_eq!(get(port, target).await, done);
        }
    }

    #[tokio::test]
    async fn errors_are_readable_escaped_and_immutable() {
        let epoch = Epoch::new(Arc::new(AtomicU64::new(5)), 5);
        let mut loopback = Loopback::bind(&copy()).await.unwrap();
        let port = loopback.port;
        let waiting = tokio::spawn(async move { loopback.wait(STATE, &epoch, WAIT).await });
        let reply = get(
            port,
            "/callback?state=s1-state&error=server_error&error_description=%3Cscript%3Eevil%3C%2Fscript%3E",
        )
        .await;
        assert!(reply.starts_with("HTTP/1.1 200") && reply.contains("Service failed"));
        assert!(reply.contains("&lt;script&gt;evil&lt;/script&gt;"));
        assert!(!reply.contains("<script>evil"));
        assert!(matches!(waiting.await.unwrap(), Ok(Callback::Failed(f)) if f.code == "provider_server_error"));
        assert_eq!(get(port, "/callback?state=s1-state&code=late").await, reply);
    }

    #[tokio::test]
    async fn cancellation_timeout_and_dropped_owner_keep_a_page_without_authentication() {
        for ending in ["cancel", "timeout", "drop"] {
            let generation = Arc::new(AtomicU64::new(3));
            let epoch = Epoch::new(generation.clone(), 3);
            let mut loopback = Loopback::bind(&copy()).await.unwrap();
            let port = loopback.port;
            if ending == "cancel" {
                generation.store(4, std::sync::atomic::Ordering::SeqCst);
            }
            if ending != "drop" {
                let err = loopback
                    .wait(STATE, &epoch, Duration::from_millis(20))
                    .await
                    .unwrap_err();
                assert_eq!(
                    err.code,
                    if ending == "cancel" {
                        "stale_generation"
                    } else {
                        "provider_timeout"
                    }
                );
            }
            drop(loopback);
            let page = get(port, "/callback?state=s1-state&code=late").await;
            assert!(page.starts_with("HTTP/1.1 200"));
            assert!(page.contains(if ending == "timeout" { "Timed out" } else { "Cancelled" }));
            assert_eq!(get(port, "/callback").await, page);
        }
    }

    #[tokio::test]
    async fn grace_deadline_releases_the_listener_and_idle_connections() {
        let listener = TcpListener::bind(("127.0.0.1", 0)).await.unwrap();
        let port = listener.local_addr().unwrap().port();
        let serving = tokio::spawn(serve_result(listener, None, "final".into(), Duration::from_millis(100)));
        let mut idle = TcpStream::connect(("127.0.0.1", port)).await.unwrap();
        assert!(get(port, "/callback").await.ends_with("final"));
        serving.await.unwrap();
        let mut data = [0];
        assert_eq!(
            tokio::time::timeout(Duration::from_secs(1), idle.read(&mut data))
                .await
                .unwrap()
                .unwrap(),
            0
        );
        // Rebinding proves the listener released its address without relying on a race-prone connect.
        let _rebound = TcpListener::bind(("127.0.0.1", port)).await.unwrap();
    }

    #[test]
    fn account_conflict_and_fragment_bridge_preserve_state_validation() {
        let conflict = parse_callback("state=s1-state&error=server_error&error_code=unexpected_failure&error_description=Multiple+accounts+with+the+same+email+address+in+the+same+linking+domain+detected%3A+default", STATE).unwrap();
        assert!(matches!(conflict, Callback::Failed(f) if f.code == "provider_account_conflict"));
        assert_eq!(
            judge(
                "GET /callback?state=s1-state HTTP/1.1\r\nHost: 127.0.0.1:4100\r\n\r\n",
                "127.0.0.1:4100",
                STATE
            ),
            Verdict::Fragment
        );
        assert_eq!(
            judge(
                "GET /callback?state=wrong HTTP/1.1\r\nHost: 127.0.0.1:4100\r\n\r\n",
                "127.0.0.1:4100",
                STATE
            ),
            Verdict::Rejected
        );
    }
}
