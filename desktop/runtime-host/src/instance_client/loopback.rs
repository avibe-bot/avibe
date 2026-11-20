//! Provider sign-in's return to the Desktop app: a one-transaction loopback
//! listener (RFC 8252 §7.3) and the PKCE secrets it is bound to.
//!
//! The listener binds `127.0.0.1:0` only, before the browser opens. It answers
//! exactly `GET /callback` with this transaction's `Host` and `state`, consumes
//! the first such answer once, and is dropped (port released) on success,
//! failure, cancel, timeout or generation change. Request bytes, the code and
//! the verifier are never logged or reflected; replies are fixed text.
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

/// How the provider's round trip ended, from a callback that carried this state.
#[derive(Debug, PartialEq, Eq)]
pub enum Callback {
    Code(String),
    /// Supabase's `error_code=provider_email_needs_verification`.
    EmailUnverified,
    /// Any other error Supabase returned (`error` with its fixed companions).
    Failed,
}

/// One request's verdict: answered for this transaction, or not ours.
#[derive(Debug, PartialEq, Eq)]
enum Verdict {
    Done(Callback),
    NotFound,
    Rejected,
}

/// What the browser shows once the redirect arrives, in the page's language:
/// the native catalog's `desktopBootstrap.providerCallback`, as plain text.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct CallbackCopy {
    /// The sign-in reached Avibe.
    pub done: String,
    /// The provider or the person ended it without a code.
    pub failed: String,
}

pub struct Loopback {
    listener: TcpListener,
    port: u16,
    done: String,
    failed: String,
}

impl Loopback {
    /// Binds the IPv4 loopback address only, on a port the OS picks.
    pub async fn bind(copy: &CallbackCopy) -> Result<Self, ClientError> {
        let listener = TcpListener::bind(("127.0.0.1", 0))
            .await
            .map_err(|_| ClientError::new("provider_unavailable"))?;
        let port = listener
            .local_addr()
            .map_err(|_| ClientError::new("provider_unavailable"))?
            .port();
        Ok(Self {
            listener,
            port,
            done: page(&copy.done),
            failed: page(&copy.failed),
        })
    }

    /// `http://127.0.0.1:<port>/callback?state=<state>`; Supabase keeps this
    /// query and adds `code`, or `error` with `error_code`/`error_description`.
    pub fn redirect_uri(&self, state: &str) -> Url {
        let mut url = Url::parse(&format!("http://127.0.0.1:{}{CALLBACK_PATH}", self.port)).expect("loopback URL");
        url.query_pairs_mut().append_pair("state", state);
        url
    }

    /// Waits for this transaction's callback. Ends on the first valid answer,
    /// the deadline, the generation moving, or too many stray connections.
    pub async fn wait(self, state: &str, epoch: &Epoch, wait: Duration) -> Result<Callback, ClientError> {
        let deadline = Instant::now() + wait;
        let host = format!("127.0.0.1:{}", self.port);
        let mut connections = 0usize;
        loop {
            epoch.check()?;
            let remaining = deadline.saturating_duration_since(Instant::now());
            if remaining.is_zero() {
                return Err(ClientError::new("provider_timeout"));
            }
            let accepted = match tokio::time::timeout(remaining.min(TICK), self.listener.accept()).await {
                Err(_) => continue,
                Ok(accepted) => accepted,
            };
            connections += 1;
            if connections > MAX_CONNECTIONS {
                return Err(ClientError::new("invalid_or_expired"));
            }
            let Ok((stream, peer)) = accepted else { continue };
            if !peer.ip().is_loopback() {
                continue;
            }
            let verdict = respond(stream, &host, state, (&self.done, &self.failed)).await;
            if let Some(Verdict::Done(callback)) = verdict {
                // A late generation change still wins over a delivered answer.
                epoch.check()?;
                return Ok(callback);
            }
        }
    }
}

async fn respond(mut stream: TcpStream, host: &str, state: &str, (done, failed): (&str, &str)) -> Option<Verdict> {
    let head = tokio::time::timeout(READ_TIMEOUT, read_head(&mut stream))
        .await
        .ok()??;
    let verdict = judge(&head, host, state);
    let (status, body) = match &verdict {
        Verdict::Done(Callback::Code(_)) => ("200 OK", done),
        Verdict::Done(_) => ("200 OK", failed),
        Verdict::NotFound => ("404 Not Found", ""),
        Verdict::Rejected => ("400 Bad Request", ""),
    };
    let reply = format!(
        "HTTP/1.1 {status}\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: {}\r\nCache-Control: no-store\r\nReferrer-Policy: no-referrer\r\nContent-Security-Policy: default-src 'none'\r\nX-Content-Type-Options: nosniff\r\nConnection: close\r\n\r\n{body}",
        body.len()
    );
    let _ = tokio::time::timeout(READ_TIMEOUT, stream.write_all(reply.as_bytes())).await;
    let _ = stream.shutdown().await;
    Some(verdict)
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
    parse_callback(query, state).map_or(Verdict::Rejected, Verdict::Done)
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
        (None, Some(_)) => Some(if error_code.as_deref() == Some("provider_email_needs_verification") {
            Callback::EmailUnverified
        } else {
            Callback::Failed
        }),
        _ => None,
    }
}

fn constant_time_eq(left: &[u8], right: &[u8]) -> bool {
    left.len() == right.len() && left.iter().zip(right).fold(0u8, |acc, (a, b)| acc | (a ^ b)) == 0
}

/// One line of catalog copy as the whole page, escaped.
fn page(text: &str) -> String {
    let escaped = text.replace('&', "&amp;").replace('<', "&lt;").replace('>', "&gt;");
    format!("<!doctype html><meta charset=\"utf-8\"><title>Avibe</title><p>{escaped}</p>")
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
            Some(Callback::EmailUnverified)
        );
        assert_eq!(
            parse_callback("state=s1-state&error=server_error", STATE),
            Some(Callback::Failed)
        );
        // A provider's bare refusal, as GoTrue v2.198.0 forwards it (empty description).
        assert_eq!(
            parse_callback("error=access_denied&error_description=&state=s1-state", STATE),
            Some(Callback::Failed)
        );
        assert_eq!(
            parse_callback(
                "error=server_error&error_code=&error_description=&state=s1-state",
                STATE
            ),
            Some(Callback::Failed)
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

    /// A late callback to a finished transaction gets no Avibe page: nothing
    /// in this process serves its port any more. Whether the port refuses the
    /// connection cannot be the check here: on macOS a socket becomes
    /// close-on-exec only after it exists, so a child that another test in
    /// this binary spawns at that moment (the launcher and receipt tests run
    /// Python) inherits the listener and keeps the port open after this
    /// process closes its own (reproduced: 3 of 20 runs beside those tests,
    /// 0 of 200 alone).
    async fn unanswered(port: u16) -> bool {
        let late = async {
            let Ok(mut stream) = TcpStream::connect(("127.0.0.1", port)).await else {
                return String::new();
            };
            let request = format!("GET /callback?state={STATE}&code=late HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n");
            let _ = stream.write_all(request.as_bytes()).await;
            let mut reply = String::new();
            let _ = stream.read_to_string(&mut reply).await;
            reply
        };
        let reply = tokio::time::timeout(Duration::from_millis(500), late)
            .await
            .unwrap_or_default();
        !reply.contains("<title>Avibe</title>")
    }

    async fn get(port: u16, target: &str) -> String {
        let mut stream = TcpStream::connect(("127.0.0.1", port)).await.unwrap();
        stream
            .write_all(format!("GET {target} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n").as_bytes())
            .await
            .unwrap();
        let mut reply = String::new();
        stream.read_to_string(&mut reply).await.unwrap();
        reply
    }

    #[tokio::test]
    async fn consumes_the_first_valid_callback_once_and_releases_the_port() {
        let _ports = PORTS.lock().await;
        let generation = Arc::new(AtomicU64::new(7));
        let epoch = Epoch::new(generation.clone(), 7);
        let loopback = Loopback::bind(&copy()).await.unwrap();
        let port = loopback.port;
        assert_eq!(loopback.redirect_uri(STATE).host_str(), Some("127.0.0.1"));
        let waiting = tokio::spawn(async move { loopback.wait(STATE, &epoch, Duration::from_secs(5)).await });
        // Stray requests neither succeed nor end the wait; replies never echo the query.
        assert!(get(port, "/favicon.ico").await.starts_with("HTTP/1.1 404"));
        let wrong = get(port, "/callback?state=other&code=leaked").await;
        assert!(wrong.starts_with("HTTP/1.1 400") && !wrong.contains("leaked"));
        let done = get(port, "/callback?state=s1-state&code=secret-code").await;
        assert!(done.starts_with("HTTP/1.1 200") && !done.contains("secret-code"));
        // The catalog's line, escaped: the copy is text, never markup.
        assert!(done.ends_with("<p>Done &lt;here&gt; &amp; now</p>"), "{done}");
        assert!(matches!(waiting.await.unwrap(), Ok(Callback::Code(code)) if code == "secret-code"));
        // The listener is gone with the transaction: a late callback cannot land.
        assert!(unanswered(port).await);
    }

    /// A provider's bare refusal ends the wait at once with the failure page.
    #[tokio::test]
    async fn a_bare_provider_refusal_ends_the_wait_at_once() {
        let _ports = PORTS.lock().await;
        let generation = Arc::new(AtomicU64::new(5));
        let epoch = Epoch::new(generation, 5);
        let loopback = Loopback::bind(&copy()).await.unwrap();
        let port = loopback.port;
        let started = Instant::now();
        let waiting = tokio::spawn(async move { loopback.wait(STATE, &epoch, Duration::from_secs(3)).await });
        let reply = get(port, "/callback?error=access_denied&error_description=&state=s1-state").await;
        assert!(reply.starts_with("HTTP/1.1 200"), "{reply}");
        // The catalog's failure line, alone: one language, no other text.
        assert!(reply.ends_with("<title>Avibe</title><p>没有完成</p>"), "{reply}");
        assert!(matches!(waiting.await.unwrap(), Ok(Callback::Failed)));
        assert!(started.elapsed() < Duration::from_secs(2), "{:?}", started.elapsed());
        assert!(unanswered(port).await);
    }

    #[tokio::test]
    async fn a_generation_change_ends_the_wait() {
        let _ports = PORTS.lock().await;
        let generation = Arc::new(AtomicU64::new(3));
        let epoch = Epoch::new(generation.clone(), 3);
        let loopback = Loopback::bind(&copy()).await.unwrap();
        let port = loopback.port;
        let waiting = tokio::spawn(async move { loopback.wait(STATE, &epoch, Duration::from_secs(5)).await });
        generation.store(4, std::sync::atomic::Ordering::SeqCst);
        assert_eq!(waiting.await.unwrap().unwrap_err().code, "stale_generation");
        assert!(unanswered(port).await);
    }

    #[tokio::test]
    async fn a_deadline_and_a_connection_cap_end_the_wait() {
        let _ports = PORTS.lock().await;
        let generation = Arc::new(AtomicU64::new(1));
        let loopback = Loopback::bind(&copy()).await.unwrap();
        let epoch = Epoch::new(generation.clone(), 1);
        assert_eq!(
            loopback
                .wait(STATE, &epoch, Duration::from_millis(300))
                .await
                .unwrap_err()
                .code,
            "provider_timeout"
        );
        let loopback = Loopback::bind(&copy()).await.unwrap();
        let port = loopback.port;
        let epoch = Epoch::new(generation, 1);
        let waiting = tokio::spawn(async move { loopback.wait(STATE, &epoch, Duration::from_secs(10)).await });
        for _ in 0..=MAX_CONNECTIONS {
            if TcpStream::connect(("127.0.0.1", port)).await.is_err() {
                break;
            }
        }
        assert_eq!(waiting.await.unwrap().unwrap_err().code, "invalid_or_expired");
    }
}
