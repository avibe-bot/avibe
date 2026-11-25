//! Native HTTP with explicit authority and independently owned cookie stores.
mod ceremony;
#[cfg(test)]
mod tests;
use super::{account::CONTROL_PLANE, protocol::ClientError};
use async_trait::async_trait;
use ceremony::{Ceremony, Fields, Route};
use cookie_store::CookieStore;
use reqwest::{
    header::{HeaderMap, HeaderValue, AUTHORIZATION, CONTENT_TYPE, COOKIE, HOST, ORIGIN, SET_COOKIE},
    Method,
};
use serde_json::Value;
use std::{
    sync::{
        atomic::{AtomicU64, Ordering},
        Arc,
    },
    time::Duration,
};
use url::Url;

pub struct Request {
    pub method: Method,
    pub url: Url,
    pub headers: HeaderMap,
    pub body: Option<Value>,
}
pub struct Response {
    pub status: u16,
    pub headers: HeaderMap,
    pub body: Vec<u8>,
}
impl Response {
    pub fn json(&self) -> Result<Value, ClientError> {
        serde_json::from_slice(&self.body).map_err(|_| ClientError::new("invalid_response"))
    }
    pub fn success(&self) -> bool {
        (200..300).contains(&self.status)
    }
    pub fn error(&self) -> ClientError {
        // Native errors contain codes, never upstream descriptions/URLs/tokens.
        let body = self.json().unwrap_or(Value::Null);
        let code = body
            .get("error")
            .or_else(|| body.get("error_code"))
            .or_else(|| body.get("code"))
            .and_then(Value::as_str)
            .filter(|s| {
                s.len() <= 100
                    && s.bytes()
                        .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == b'_')
            });
        ClientError::new(code.unwrap_or("request_failed"))
    }
}
#[async_trait]
pub trait Transport: Send + Sync {
    async fn send(&self, request: Request) -> Result<Response, ClientError>;
}

/// The longest a standalone install request may stay open.
pub const INSTALL_TIMEOUT: Duration = Duration::from_secs(15 * 60);

pub struct HttpTransport {
    client: reqwest::Client,
    user_agent: String,
}
impl HttpTransport {
    fn builder(user_agent: &str) -> reqwest::ClientBuilder {
        reqwest::Client::builder()
            .user_agent(user_agent)
            .no_proxy()
            .redirect(reqwest::redirect::Policy::none())
            .connect_timeout(Duration::from_secs(20))
    }
    /// `app_version` is the Desktop app's package version. Every request,
    /// the instance's `/auth/callback` included, names the app as
    /// `AvibeDesktop/<version>`: the instance labels the sign-in it records
    /// from that (`vibe/device_metadata.py`). A label, never a credential.
    pub fn new(app_version: &str) -> Result<Self, ClientError> {
        let user_agent = format!("AvibeDesktop/{app_version}");
        Ok(Self {
            client: Self::builder(&user_agent).build()?,
            user_agent,
        })
    }
}
#[async_trait]
impl Transport for HttpTransport {
    async fn send(&self, request: Request) -> Result<Response, ClientError> {
        // The released standalone upgrade endpoint answers only after the
        // installer finishes (about a minute). Give it a long, finite limit
        // rather than the 20 s every other request has.
        let long_install = request.method == Method::POST && request.url.path() == "/api/upgrade";
        // Neither a one-use credential exchange nor a reviewed grant is replayable,
        // including reqwest's protocol-NACK retries. Other traffic keeps its pool.
        let client = if ceremony::one_shot(&request.method, &request.url) {
            Self::builder(&self.user_agent).retry(reqwest::retry::never()).build()?
        } else {
            self.client.clone()
        };
        let builder = client
            .request(request.method, request.url)
            .headers(request.headers)
            .timeout(if long_install {
                INSTALL_TIMEOUT
            } else {
                Duration::from_secs(20)
            });
        let mut builder = builder;
        if let Some(body) = request.body {
            builder = builder.json(&body);
        }
        let mut response = builder.send().await?;
        let status = response.status().as_u16();
        let headers = response.headers().clone();
        if response.content_length().is_some_and(|n| n > 2_000_000) {
            return Err(ClientError::new("response_too_large"));
        }
        let mut body = Vec::new();
        while let Some(chunk) = response.chunk().await? {
            if body.len() + chunk.len() > 2_000_000 {
                return Err(ClientError::new("response_too_large"));
            }
            body.extend_from_slice(&chunk);
        }
        Ok(Response { status, headers, body })
    }
}

// A programmatic import/old decoder must not splice a second named cookie
// into the outgoing header via an innocent-looking name or value.
fn pair_is_safe(name: &str, value: &str) -> bool {
    !name.is_empty()
        && name
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"!#$%&'*+-.^_`|~".contains(&b))
        && !value.bytes().any(|b| b.is_ascii_control() || matches!(b, b';' | b','))
}

/// In-memory comparison only; never log or serialize a request credential.
#[derive(PartialEq, Eq)]
pub(super) struct FactorCookies(Vec<String>);

/// Cloned for each fenced transaction; a late response cannot mutate the owner.
#[derive(Clone)]
pub struct NativeSession {
    pub origin: Url,
    cookies: CookieStore,
    // Never serialized, projected or imported. Generic (including issuer) jars
    // cannot acquire this role just by naming the hosted authority.
    ceremony: Option<Ceremony>,
    transport: Arc<dyn Transport>,
    epoch: Option<Epoch>,
}
#[derive(Clone)]
pub struct Epoch {
    current: Arc<AtomicU64>,
    pub value: u64,
}
impl Epoch {
    pub fn new(current: Arc<AtomicU64>, value: u64) -> Self {
        Self { current, value }
    }
    pub fn check(&self) -> Result<(), ClientError> {
        if self.current.load(Ordering::SeqCst) == self.value {
            Ok(())
        } else {
            Err(ClientError::new("stale_generation"))
        }
    }
}
// Filter by name at every stored path, including expired entries. Never edit the live jar on save.
fn remove_memory_only_cookies(store: &mut CookieStore) {
    let keys: Vec<_> = store
        .iter_any()
        .filter(|cookie| {
            matches!(
                cookie.name(),
                "__Host-avibe-sign-in-proof" | "__Host-avibe-sign-in-start"
            )
        })
        .map(|cookie| {
            (
                String::from(&cookie.domain),
                String::from(&cookie.path),
                cookie.name().to_owned(),
            )
        })
        .collect();
    for (domain, path, name) in keys {
        store.remove(&domain, &path, &name);
    }
}

impl NativeSession {
    pub fn new(origin: Url, transport: Arc<dyn Transport>) -> Self {
        Self {
            origin,
            cookies: CookieStore::default(),
            ceremony: None,
            transport,
            epoch: None,
        }
    }
    pub(super) fn account(transport: Arc<dyn Transport>) -> Self {
        let mut session = Self::new(CONTROL_PLANE.parse().expect("fixed origin"), transport);
        session.ceremony = Some(Ceremony::default());
        session
    }
    pub(super) fn is_account(&self) -> bool {
        self.ceremony.is_some() && self.origin.as_str() == CONTROL_PLANE
    }
    pub(super) fn has_discovery_credentials(&self) -> bool {
        self.is_account()
            && self
                .origin
                .join("/api/mobile/session")
                .is_ok_and(|url| self.cookies.get_request_values(&url).any(|(_, v)| !v.is_empty()))
    }
    pub(super) fn account_attempt(&self, accepted: Option<&Self>) -> Result<Self, ClientError> {
        self.check()?;
        if !self.is_account()
            || accepted.is_some_and(|jar| !jar.is_account() || !Arc::ptr_eq(&jar.transport, &self.transport))
        {
            return Err(ClientError::new("invalid_backend"));
        }
        let mut jar = accepted
            .cloned()
            .unwrap_or_else(|| Self::account(self.transport.clone()));
        jar.epoch = self.epoch.clone();
        Ok(jar)
    }
    pub(super) fn restore_account(transport: Arc<dyn Transport>, bytes: &[u8]) -> Result<Self, ClientError> {
        let mut session = Self::restore(CONTROL_PLANE.parse().expect("fixed origin"), transport, bytes)?;
        session.ceremony = Some(Ceremony::default());
        Ok(session)
    }
    pub fn restore(origin: Url, transport: Arc<dyn Transport>, bytes: &[u8]) -> Result<Self, ClientError> {
        let mut result = Self::decode_released_jar(origin, transport, bytes)?;
        // Older versions may have saved a proof or an unfinished ceremony.
        remove_memory_only_cookies(&mut result.cookies);
        Ok(result)
    }

    // The released restore implementation. Keep its input and error semantics for downgrade
    // checks. This is private so callers cannot bypass the post-restore proof filter above.
    fn decode_released_jar(origin: Url, transport: Arc<dyn Transport>, bytes: &[u8]) -> Result<Self, ClientError> {
        let cookies =
            cookie_store::serde::json::load(bytes).map_err(|_| ClientError::new("credentials_unavailable"))?;
        let mut result = Self {
            origin,
            cookies,
            ceremony: None,
            transport,
            epoch: None,
        };
        // Imported credentials belong to this one exact authority.
        if result.cookies.iter_any().any(|cookie| {
            !matches!(&cookie.domain,
            cookie_store::CookieDomain::HostOnly(host) if Some(host.as_str()) == result.origin.host_str())
        }) {
            return Err(ClientError::new("credentials_unavailable"));
        }
        // Historical jars predate the separate slots. Keep their ordinary session;
        // never promote a decoded entry to a ceremony (including path variants).
        let historical: Vec<_> = result
            .cookies
            .iter_any()
            .filter(|c| ceremony::reserved(c.name()))
            .map(|c| (String::from(&c.domain), String::from(&c.path), c.name().to_owned()))
            .collect();
        for (domain, path, name) in historical {
            result.cookies.remove(&domain, &path, &name);
        }
        Ok(result)
    }
    pub fn fence(&mut self, epoch: Epoch) {
        self.epoch = Some(epoch);
    }
    pub fn unfenced(&mut self) {
        self.epoch = None;
    }
    fn check(&self) -> Result<(), ClientError> {
        self.epoch.as_ref().map_or(Ok(()), Epoch::check)
    }
    pub fn persist(&self) -> Result<Vec<u8>, ClientError> {
        let mut bytes = Vec::new();
        let mut eligible = self.cookies.clone();
        remove_memory_only_cookies(&mut eligible);
        cookie_store::serde::json::save_incl_expired_and_nonpersistent(&eligible, &mut bytes)
            .map_err(|_| ClientError::new("credentials_unavailable"))?;
        Ok(bytes)
    }
    /// Adopt cookies another store of this same origin holds (the selected
    /// instance's WebView). Each is narrowed to this exact host, as responses are.
    pub fn store_cookies(&mut self, cookies: Vec<cookie::Cookie<'static>>, url: &Url) {
        if self.ceremony.is_some() || url.origin() != self.origin.origin() {
            return;
        }
        for mut cookie in cookies {
            if ceremony::reserved(cookie.name()) || !pair_is_safe(cookie.name(), cookie.value()) {
                continue;
            }
            if cookie
                .domain()
                .is_some_and(|domain| Some(domain.trim_start_matches('.')) != self.origin.host_str())
            {
                continue;
            }
            cookie.unset_domain();
            self.cookies.store_response_cookies(std::iter::once(cookie), url);
        }
    }
    /// A response without its own account ID may use a separate session read as its
    /// identity witness only when both fixed paths send the same credentials.
    pub(super) fn same_request_cookies(&self, first: &str, second: &str) -> bool {
        let (Ok(a), Ok(b)) = (self.origin.join(first), self.origin.join(second)) else {
            return false;
        };
        a.origin() == self.origin.origin()
            && b.origin() == a.origin()
            && self
                .cookies
                .get_request_values(&a)
                .eq(self.cookies.get_request_values(&b))
    }
    pub fn cookie_value(&self, name: &str) -> Option<String> {
        self.cookies
            .get_request_values(&self.origin)
            .find(|(key, _)| *key == name)
            .map(|(_, value)| value.to_owned())
    }
    pub async fn request(&mut self, method: Method, path: &str, body: Option<Value>) -> Result<Response, ClientError> {
        self.request_with_headers(method, path, body, HeaderMap::new()).await
    }
    pub(super) fn factor_cookies(&self) -> Option<FactorCookies> {
        if !self.is_account() {
            return None;
        }
        let url = self.origin.join("/api/mobile/session").ok()?;
        let mut has_value = false;
        let pairs = self
            .cookies
            .get_request_values(&url)
            .map(|(k, v)| {
                has_value |= !v.is_empty();
                format!("{k}={v}")
            })
            .collect();
        has_value.then_some(FactorCookies(pairs))
    }
    /// A qualified owner-conflict read must have sent the very same ordinary
    /// credential as account discovery. Never use a cookie supplied by its reply.
    pub(super) fn claim_read_cookies(&self) -> Option<FactorCookies> {
        self.factor_cookies()
            .filter(|_| self.same_request_cookies("/api/v1/claims/read", "/api/mobile/session"))
    }
    pub(super) async fn read_claim_ticket(
        &mut self,
        body: Value,
        expected: Option<&FactorCookies>,
    ) -> Result<Response, ClientError> {
        if !self.is_account() {
            return Err(ClientError::new("invalid_backend"));
        }
        self.send_checked(
            Method::POST,
            "/api/v1/claims/read",
            Some(body),
            HeaderMap::new(),
            expected,
        )
        .await
    }
    /// Only a GET at the account origin, using the exact credentials frozen for discovery.
    pub(super) async fn read_frozen_account(&mut self, expected: &FactorCookies) -> Result<Response, ClientError> {
        if !self.is_account() {
            return Err(ClientError::new("invalid_backend"));
        }
        self.send_checked(
            Method::GET,
            "/api/mobile/session",
            None,
            HeaderMap::new(),
            Some(expected),
        )
        .await
    }
    pub(super) async fn factor_post(
        &mut self,
        path: &'static str,
        body: Value,
        expected: &FactorCookies,
    ) -> Result<Response, ClientError> {
        if !self.is_account()
            || !matches!(
                path,
                "/api/two-factor/passkey-challenge"
                    | "/api/two-factor/verify"
                    | "/api/two-factor/totp/setup"
                    | "/api/two-factor/totp"
            )
        {
            return Err(ClientError::new("invalid_action"));
        }
        self.send_checked(Method::POST, path, Some(body), HeaderMap::new(), Some(expected))
            .await
    }
    pub async fn request_with_headers(
        &mut self,
        method: Method,
        path: &str,
        body: Option<Value>,
        headers: HeaderMap,
    ) -> Result<Response, ClientError> {
        self.send_checked(method, path, body, headers, None).await
    }
    async fn send_checked(
        &mut self,
        method: Method,
        path: &str,
        body: Option<Value>,
        mut headers: HeaderMap,
        expected: Option<&FactorCookies>,
    ) -> Result<Response, ClientError> {
        self.check()?;
        let url = self
            .origin
            .join(path)
            .map_err(|_| ClientError::new("invalid_request"))?;
        if (self.ceremony.is_some() && (self.origin.as_str() != CONTROL_PLANE || headers.contains_key(AUTHORIZATION)))
            || headers.contains_key(COOKIE)
            || headers.contains_key(HOST)
            || url.origin() != self.origin.origin()
            || !url.username().is_empty()
            || url.password().is_some()
            || url.fragment().is_some()
        {
            return Err(ClientError::new("invalid_request"));
        }
        let route = Route::request(&method, &url, body.as_ref());
        let mut pairs = Vec::new();
        for (name, value) in self.cookies.get_request_values(&url) {
            if ceremony::reserved(name) || !pair_is_safe(name, value) {
                return Err(ClientError::new("invalid_cookie"));
            }
            pairs.push(format!("{name}={value}"));
        }
        if let Some(ceremony) = self.ceremony.as_mut() {
            if let Some(pair) = ceremony.outbound(route, body.as_ref())? {
                pairs.push(pair);
            }
        }
        if expected.is_some_and(|c| c.0 != pairs) {
            return Err(ClientError::new("account_changed"));
        }
        if !pairs.is_empty() {
            let mut cookie =
                HeaderValue::from_str(&pairs.join("; ")).map_err(|_| ClientError::new("invalid_cookie"))?;
            cookie.set_sensitive(true);
            headers.insert(COOKIE, cookie);
        }
        if !matches!(method, Method::GET | Method::HEAD) {
            headers.insert(
                ORIGIN,
                HeaderValue::from_str(self.origin.origin().ascii_serialization().as_str())
                    .map_err(|_| ClientError::new("invalid_origin"))?,
            );
            headers.insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
        }
        let mut response = self
            .transport
            .send(Request {
                method,
                url: url.clone(),
                headers,
                body,
            })
            .await?;
        self.check()?;
        let mut fields = Fields::default();
        let mut ordinary = Vec::new();
        for header in response.headers.get_all(SET_COOKIE) {
            let raw = header.to_str().map_err(|_| ClientError::new("invalid_cookie"))?;
            let cookie = cookie::Cookie::parse(raw).map_err(|_| ClientError::new("invalid_cookie"))?;
            if fields.capture(cookie.name(), raw) || (self.ceremony.is_some() && route == Route::Start) {
                // A typed-code start creates a transaction, never an account
                // session. Discovery must rely on cookies from completion.
                continue;
            }
            if !pair_is_safe(cookie.name(), cookie.value()) {
                return Err(ClientError::new("invalid_cookie"));
            }
            if cookie
                .domain()
                .is_some_and(|domain| Some(domain.trim_start_matches('.')) != self.origin.host_str())
            {
                return Err(ClientError::new("invalid_cookie_scope"));
            }
            // Scope is narrowed to the response host even if a Domain attribute
            // names that same host. Account and instance stores never share.
            let mut cookie = cookie.into_owned();
            cookie.unset_domain();
            ordinary.push((cookie, header.clone()));
        }
        if let Some(ceremony) = self.ceremony.as_mut() {
            ceremony.receive(route, &response, fields);
        }
        response.headers.remove(SET_COOKIE);
        for (cookie, header) in ordinary {
            self.cookies.store_response_cookies(std::iter::once(cookie), &url);
            // Instance update callers use Set-Cookie presence; account callers
            // never need the raw Set-Cookie (including ordinary session material).
            if self.ceremony.is_none() {
                response.headers.append(SET_COOKIE, header);
            }
        }
        Ok(response)
    }
    /// Required Runtime cookies only. Host-only scope is retained in the jar;
    /// native platform APIs require the explicit exact host for projection.
    pub fn runtime_cookies(&self) -> Result<Vec<cookie::Cookie<'static>>, ClientError> {
        if self.ceremony.is_some() {
            return Err(ClientError::new("invalid_instance_origin"));
        }
        let host = self
            .origin
            .host_str()
            .ok_or_else(|| ClientError::new("invalid_instance_origin"))?;
        let mut cookies = Vec::new();
        for entry in self.cookies.iter_unexpired() {
            if !matches!(
                entry.name(),
                "__Host-vibe_remote_session" | "__Host-vibe_oauth_device" | "vibe_csrf_token"
            ) {
                continue;
            }
            if !entry.domain.matches(&self.origin) {
                return Err(ClientError::new("invalid_cookie_scope"));
            }
            let mut cookie = (**entry).clone().into_owned();
            cookie.set_domain(host.to_owned());
            cookie.set_path("/");
            if cookie.name().starts_with("__Host-")
                && (cookie.secure() != Some(true) || cookie.http_only() != Some(true))
            {
                return Err(ClientError::new("invalid_cookie_scope"));
            }
            cookies.push(cookie);
        }
        if !cookies
            .iter()
            .any(|cookie| cookie.name() == "__Host-vibe_remote_session")
        {
            return Err(ClientError::new("instance_session_missing"));
        }
        Ok(cookies)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};

    /// The instance tells a Desktop sign-in from a browser's by this header
    /// on its `/auth/callback`; without it the sign-in is listed as an
    /// unnamed browser.
    #[tokio::test]
    async fn every_request_names_the_desktop_app_and_its_version() {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let server = tokio::spawn(async move {
            let (mut stream, _) = listener.accept().await.unwrap();
            let mut received = Vec::new();
            let mut buffer = [0u8; 1024];
            while !received.windows(4).any(|window| window == b"\r\n\r\n") {
                let read = stream.read(&mut buffer).await.unwrap();
                assert!(read > 0, "the request ended before its headers did");
                received.extend_from_slice(&buffer[..read]);
            }
            stream
                .write_all(b"HTTP/1.1 302 Found\r\nLocation: /\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                .await
                .unwrap();
            String::from_utf8(received).unwrap()
        });
        let transport = HttpTransport::new("0.4.2-rc.3").unwrap();
        let response = transport
            .send(Request {
                method: Method::GET,
                url: Url::parse(&format!("http://{address}/auth/callback?code=c&state=s")).unwrap(),
                headers: HeaderMap::new(),
                body: None,
            })
            .await
            .unwrap();
        assert_eq!(response.status, 302);
        let head = server.await.unwrap();
        let agents: Vec<&str> = head
            .lines()
            .filter_map(|line| line.split_once(':'))
            .filter(|(name, _)| name.eq_ignore_ascii_case("user-agent"))
            .map(|(_, value)| value.trim())
            .collect();
        // The instance matches `AvibeDesktop/` exactly, case included.
        assert_eq!(agents, ["AvibeDesktop/0.4.2-rc.3"], "{head}");
    }
}
