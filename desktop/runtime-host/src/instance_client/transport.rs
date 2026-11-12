//! Native HTTP with explicit authority and independently owned cookie stores.
use super::protocol::ClientError;
use async_trait::async_trait;
use cookie_store::CookieStore;
use reqwest::{
    header::{HeaderMap, HeaderValue, CONTENT_TYPE, COOKIE, ORIGIN, SET_COOKIE},
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

pub struct HttpTransport {
    client: reqwest::Client,
}
impl HttpTransport {
    pub fn new() -> Result<Self, ClientError> {
        Ok(Self {
            client: reqwest::Client::builder()
                .no_proxy()
                .redirect(reqwest::redirect::Policy::none())
                .timeout(Duration::from_secs(20))
                .build()?,
        })
    }
}
#[async_trait]
impl Transport for HttpTransport {
    async fn send(&self, request: Request) -> Result<Response, ClientError> {
        let mut builder = self
            .client
            .request(request.method, request.url)
            .headers(request.headers);
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

/// Cloned for each fenced transaction; a late response cannot mutate the owner.
#[derive(Clone)]
pub struct NativeSession {
    pub origin: Url,
    cookies: CookieStore,
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
impl NativeSession {
    pub fn new(origin: Url, transport: Arc<dyn Transport>) -> Self {
        Self {
            origin,
            cookies: CookieStore::default(),
            transport,
            epoch: None,
        }
    }
    pub fn restore(origin: Url, transport: Arc<dyn Transport>, bytes: &[u8]) -> Result<Self, ClientError> {
        let cookies =
            cookie_store::serde::json::load(bytes).map_err(|_| ClientError::new("credentials_unavailable"))?;
        let result = Self {
            origin,
            cookies,
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
        cookie_store::serde::json::save_incl_expired_and_nonpersistent(&self.cookies, &mut bytes)
            .map_err(|_| ClientError::new("credentials_unavailable"))?;
        Ok(bytes)
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
    pub async fn request_with_headers(
        &mut self,
        method: Method,
        path: &str,
        body: Option<Value>,
        mut headers: HeaderMap,
    ) -> Result<Response, ClientError> {
        self.check()?;
        let url = self
            .origin
            .join(path)
            .map_err(|_| ClientError::new("invalid_request"))?;
        if url.origin() != self.origin.origin()
            || !url.username().is_empty()
            || url.password().is_some()
            || url.fragment().is_some()
        {
            return Err(ClientError::new("invalid_request"));
        }
        let cookie = self
            .cookies
            .get_request_values(&url)
            .map(|(name, value)| format!("{name}={value}"))
            .collect::<Vec<_>>()
            .join("; ");
        if !cookie.is_empty() {
            headers.insert(
                COOKIE,
                HeaderValue::from_str(&cookie).map_err(|_| ClientError::new("invalid_cookie"))?,
            );
        }
        if !matches!(method, Method::GET | Method::HEAD) {
            headers.insert(
                ORIGIN,
                HeaderValue::from_str(self.origin.origin().ascii_serialization().as_str())
                    .map_err(|_| ClientError::new("invalid_origin"))?,
            );
            headers.insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
        }
        let response = self
            .transport
            .send(Request {
                method,
                url: url.clone(),
                headers,
                body,
            })
            .await?;
        self.check()?;
        for header in response.headers.get_all(SET_COOKIE) {
            let raw = header.to_str().map_err(|_| ClientError::new("invalid_cookie"))?;
            let cookie = cookie::Cookie::parse(raw).map_err(|_| ClientError::new("invalid_cookie"))?;
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
            self.cookies.store_response_cookies(std::iter::once(cookie), &url);
        }
        Ok(response)
    }
    /// Required Runtime cookies only. Host-only scope is retained in the jar;
    /// native platform APIs require the explicit exact host for projection.
    pub fn runtime_cookies(&self) -> Result<Vec<cookie::Cookie<'static>>, ClientError> {
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
