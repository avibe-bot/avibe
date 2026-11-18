//! Account protocol adapter. Secret inputs and cookie updates remain native.
use std::sync::Arc;

use async_trait::async_trait;
use reqwest::{
    header::{HeaderMap, HeaderValue, ACCEPT},
    Method,
};
use serde::Deserialize;
use serde_json::{json, Value};
use url::Url;

use super::{
    loopback::{Callback, Loopback, Secrets, WAIT},
    protocol::*,
    transport::{Epoch, NativeSession, Response, Transport},
};

pub const CONTROL_PLANE: &str = "https://avibe.bot/";
pub const EMAIL_REDIRECT: &str = "https://avibe.bot/auth/callback?source=desktop-app";

#[async_trait]
pub trait Passkeys: Send + Sync {
    /// The implementation uses a native system sheet, never a WebView bridge.
    async fn assert(&self, options: Value, epoch: Epoch) -> Result<Value, ClientError>;
    /// Whether this OS can run a discoverable passkey assertion at all. Passkey
    /// sign-in is offered only when this and avibe.bot's public switch agree.
    fn supported(&self) -> bool {
        true
    }
}

/// Opens a provider's authorization page in the person's default browser.
pub trait SystemBrowser: Send + Sync {
    /// `url` is always Supabase's `/auth/v1/authorize` on the configured auth origin.
    fn open(&self, url: &Url) -> Result<(), ClientError>;
}

/// A provider sign-in between its start and its code. Its secrets stay here;
/// dropping it releases the listener's port.
pub struct ProviderAttempt {
    supabase: NativeSession,
    key: String,
    secrets: Secrets,
    loopback: Option<Loopback>,
    authorize: Url,
}
impl ProviderAttempt {
    /// Opens Supabase's authorization page in the default browser and waits,
    /// holding no lock, for this attempt's callback. A newer generation (another
    /// attempt, an account change, return_local, abort, a closed window) ends it.
    pub async fn browse(&mut self, browser: &dyn SystemBrowser, epoch: &Epoch) -> Result<String, ClientError> {
        epoch.check()?;
        let loopback = self.loopback.take().ok_or_else(|| ClientError::new("invalid_action"))?;
        browser.open(&self.authorize)?;
        match loopback.wait(&self.secrets.state, epoch, WAIT).await? {
            Callback::Code(code) => Ok(code),
            Callback::EmailUnverified => Err(ClientError::new("provider_email_unverified")),
            Callback::Failed => Err(ClientError::new("invalid_or_expired")),
        }
    }
}

/// avibe.bot's passkey sign-in context: no continuation, next page or language.
fn primary_passkey_context() -> Value {
    json!({"continuation": null, "next": null, "lang": null})
}

#[derive(Clone)]
pub struct AccountFlow {
    pub session: NativeSession,
    provider: Option<NativeSession>,
    provider_key: Option<String>,
    email: Option<String>,
    enrollment_token: Option<String>,
    /// Instance ids in the order of the last catalog avibe.bot sent: pins,
    /// then newest. Published order is derived from it, never replaces it.
    server_order: Vec<String>,
    transport: Arc<dyn Transport>,
    /// Whether this system can run the passkey sheet: a second factor offers
    /// passkey only then.
    passkey_sheet: bool,
    /// The stored account this flow continues: the one restored at launch,
    /// or the one it signed in. Only that account's key may hold an
    /// unfinished sign-in (a second factor still owed); a new first factor
    /// clears it.
    pub resumes: Option<String>,
}

#[derive(Deserialize)]
struct SessionProjection {
    account: Account,
    instances: Vec<Instance>,
}
#[derive(Deserialize)]
struct FactorProjection {
    email: String,
    gate: SecondFactorGate,
    methods: Vec<SecondFactorMethod>,
}

impl AccountFlow {
    pub fn new(transport: Arc<dyn Transport>, passkey_sheet: bool) -> Self {
        Self {
            session: NativeSession::new(CONTROL_PLANE.parse().expect("fixed origin"), transport.clone()),
            provider: None,
            provider_key: None,
            email: None,
            enrollment_token: None,
            server_order: vec![],
            transport,
            passkey_sheet,
            resumes: None,
        }
    }

    pub fn fence(&mut self, epoch: Epoch) {
        self.session.fence(epoch.clone());
        if let Some(provider) = &mut self.provider {
            provider.fence(epoch);
        }
    }

    pub fn clear_transient(&mut self) {
        self.provider = None;
        self.provider_key = None;
        self.email = None;
        self.enrollment_token = None;
    }

    fn begin_first_factor(&mut self, epoch: Epoch, state: &mut ClientState) -> Result<(), ClientError> {
        if state.account.status == AccountStatus::SignedIn {
            return Err(ClientError::new("invalid_action"));
        }
        self.clear_transient();
        self.resumes = None;
        // A replacement attempt cannot carry the previous email's verifier or
        // MFA cookie. Failure leaves a usable signed-out state on reload.
        self.session = NativeSession::new(CONTROL_PLANE.parse().expect("fixed origin"), self.transport.clone());
        self.session.fence(epoch);
        self.server_order.clear();
        state.account = AccountState {
            status: AccountStatus::SignedOut,
            identity: None,
            email: None,
            gate: None,
            methods: vec![],
        };
        state.catalog = CatalogState {
            status: CatalogStatus::Idle,
            instances: vec![],
        };
        Ok(())
    }

    pub async fn request_otp(
        &mut self,
        email: String,
        epoch: Epoch,
        state: &mut ClientState,
    ) -> Result<(), ClientError> {
        self.begin_first_factor(epoch.clone(), state)?;
        let email = validated_email(email)?;
        let (mut provider, key) = self.provider(epoch).await?;
        let mut path = Url::parse("https://placeholder.test/auth/v1/otp").expect("fixed URL");
        path.query_pairs_mut().append_pair("redirect_to", EMAIL_REDIRECT);
        let response = provider
            .request_with_headers(
                Method::POST,
                &path[url::Position::BeforePath..],
                Some(json!({"email": email, "create_user": true})),
                provider_headers(&key)?,
            )
            .await?;
        if !response.success() {
            return Err(response.error());
        }
        self.provider = Some(provider);
        self.provider_key = Some(key);
        self.email = Some(email.clone());
        self.enrollment_token = None;
        state.account = AccountState {
            status: AccountStatus::Verifying,
            identity: None,
            email: Some(email),
            gate: None,
            methods: vec![],
        };
        Ok(())
    }

    async fn provider(&mut self, epoch: Epoch) -> Result<(NativeSession, String), ClientError> {
        let response = self.session.request(Method::GET, "/api/config/auth", None).await?;
        let config = successful_json(response)?;
        supabase_session(&config, self.transport.clone(), epoch)
    }

    /// Google, GitHub or Apple, first half: reads avibe.bot's public switch for
    /// `provider`, draws this attempt's PKCE secrets and binds its loopback
    /// listener. Nothing of the account changes; the coordinator runs the
    /// browser round trip (`ProviderAttempt::browse`) without its operation lock.
    pub async fn start_provider_sign_in(
        provider: SignInProvider,
        transport: Arc<dyn Transport>,
        epoch: Epoch,
    ) -> Result<ProviderAttempt, ClientError> {
        let config = auth_config(transport.clone(), Some(epoch.clone())).await?;
        if config.get(provider.enabled_flag()).and_then(Value::as_bool) != Some(true) {
            return Err(ClientError::new("provider_unavailable"));
        }
        let (supabase, key) = supabase_session(&config, transport, epoch)?;
        let secrets = Secrets::new()?;
        // Bound before the browser opens, so the redirect always has a listener.
        let loopback = Loopback::bind().await?;
        let mut authorize = Url::parse(string(&config, "supabase_url")?)
            .and_then(|origin| origin.join("/auth/v1/authorize"))
            .map_err(|_| ClientError::new("invalid_auth_provider"))?;
        authorize
            .query_pairs_mut()
            .append_pair("provider", provider.id())
            .append_pair("redirect_to", loopback.redirect_uri(&secrets.state).as_str())
            .append_pair("code_challenge", &secrets.challenge)
            .append_pair("code_challenge_method", "s256");
        Ok(ProviderAttempt {
            supabase,
            key,
            secrets,
            loopback: Some(loopback),
            authorize,
        })
    }

    /// Google, GitHub or Apple, second half, under the operation lock: a new
    /// first factor that redeems the browser's code with this attempt's own
    /// verifier, then the email code's session endpoint and two-step gate.
    pub async fn finish_provider_sign_in(
        &mut self,
        attempt: ProviderAttempt,
        code: String,
        epoch: Epoch,
        state: &mut ClientState,
    ) -> Result<(), ClientError> {
        self.begin_first_factor(epoch.clone(), state)?;
        let ProviderAttempt {
            mut supabase,
            key,
            secrets,
            ..
        } = attempt;
        supabase.fence(epoch);
        let response = supabase
            .request_with_headers(
                Method::POST,
                "/auth/v1/token?grant_type=pkce",
                Some(json!({"auth_code": code, "code_verifier": secrets.verifier})),
                provider_headers(&key)?,
            )
            .await?;
        if !response.success() {
            return Err(ClientError::new(match response.status {
                429 => "rate_limited",
                400..=499 => "invalid_or_expired",
                _ => "request_failed",
            }));
        }
        let credentials = response.json()?;
        self.establish(credentials, state).await
    }

    /// avibe.bot's public sign-in switches. A switch that is missing or not
    /// `true` offers nothing (`passkey_login_enabled` included). Public data
    /// from the fixed control plane: no account generation fences it.
    pub async fn read_sign_in_methods(transport: Arc<dyn Transport>) -> Result<SignInMethodsState, ClientError> {
        let config = auth_config(transport, None).await?;
        let on = |flag: &str| config.get(flag).and_then(Value::as_bool) == Some(true);
        Ok(SignInMethodsState {
            providers: [SignInProvider::Google, SignInProvider::Github, SignInProvider::Apple]
                .into_iter()
                .filter(|provider| on(provider.enabled_flag()))
                .collect(),
            passkey: on("passkey_login_enabled"),
        })
    }

    /// Passkey sign-in with no email first: avibe.bot's primary passkey flow,
    /// a discoverable assertion with no allow-list, then session discovery.
    /// The second-factor passkey stays `verify_passkey`.
    pub async fn sign_in_with_passkey(
        &mut self,
        passkeys: &dyn Passkeys,
        epoch: Epoch,
        state: &mut ClientState,
    ) -> Result<(), ClientError> {
        self.begin_first_factor(epoch.clone(), state)?;
        let mut headers = HeaderMap::new();
        headers.insert(ACCEPT, HeaderValue::from_static("application/json"));
        let response = self
            .session
            .request_with_headers(
                Method::POST,
                "/api/passkey-primary/options",
                Some(json!({"context": primary_passkey_context()})),
                headers.clone(),
            )
            .await?;
        if !response.success() {
            return Err(primary_passkey_error(&response));
        }
        let started = response.json()?;
        let flow = string(&started, "flow")?.to_owned();
        let mut options = started["options"].clone();
        if started["ok"] != true || options["rpId"] != "avibe.bot" {
            return Err(ClientError::new("invalid_passkey_challenge"));
        }
        // Discoverable: the person picks the account in the system sheet.
        if let Some(options) = options.as_object_mut() {
            options.remove("allowCredentials");
        }
        let assertion = match passkeys.assert(options, epoch.clone()).await {
            Ok(assertion) => assertion,
            Err(error) => {
                // Best effort: an abandoned flow also expires on its own.
                let _ = self
                    .session
                    .request_with_headers(
                        Method::POST,
                        "/api/passkey-primary/cancel",
                        Some(json!({"flow": flow})),
                        headers,
                    )
                    .await;
                return Err(error);
            }
        };
        epoch.check()?;
        let response = self
            .session
            .request_with_headers(
                Method::POST,
                "/api/passkey-primary/verify",
                Some(json!({"flow": flow, "response": assertion, "context": primary_passkey_context()})),
                headers,
            )
            .await?;
        if !response.success() {
            return Err(primary_passkey_error(&response));
        }
        self.discover(state).await
    }

    pub async fn sign_in_password(
        &mut self,
        email: String,
        password: String,
        epoch: Epoch,
        state: &mut ClientState,
    ) -> Result<(), ClientError> {
        self.begin_first_factor(epoch.clone(), state)?;
        let email = validated_email(email)?;
        if password.is_empty() || password.len() > 4096 {
            return Err(ClientError::new("invalid_credentials"));
        }
        let (mut provider, key) = self.provider(epoch).await.map_err(password_error)?;
        let response = provider
            .request_with_headers(
                Method::POST,
                "/auth/v1/token?grant_type=password",
                Some(json!({"email":email,"password":password})),
                provider_headers(&key)?,
            )
            .await?;
        if !response.success() {
            let body = response.json().unwrap_or(Value::Null);
            let code = body["error_code"]
                .as_str()
                .or(body["code"].as_str())
                .or(body["error"].as_str());
            let code = if response.status == 429 {
                "rate_limited"
            } else {
                match code {
                    Some("invalid_credentials" | "invalid_grant") => "invalid_credentials",
                    Some("over_request_rate_limit" | "over_email_send_rate_limit") => "rate_limited",
                    Some("email_not_confirmed") => "email_not_confirmed",
                    _ => "request_failed",
                }
            };
            return Err(ClientError::new(code));
        }
        self.establish(response.json()?, state).await.map_err(password_error)
    }

    async fn establish(&mut self, credentials: Value, state: &mut ClientState) -> Result<(), ClientError> {
        let response = self.session.request(Method::POST, "/api/desktop/session", Some(json!({
            "access_token":string(&credentials,"access_token")?, "refresh_token":string(&credentials,"refresh_token")?,
        }))).await?;
        self.clear_transient();
        self.project(response, state)
    }

    pub async fn verify_otp(&mut self, code: String, state: &mut ClientState) -> Result<(), ClientError> {
        if state.account.status != AccountStatus::Verifying || code.is_empty() || code.len() > 16 {
            return Err(ClientError::new("invalid_action"));
        }
        let email = self.email.as_ref().ok_or_else(|| ClientError::new("invalid_action"))?;
        let provider = self
            .provider
            .as_mut()
            .ok_or_else(|| ClientError::new("invalid_action"))?;
        let response = provider
            .request_with_headers(
                Method::POST,
                "/auth/v1/verify",
                Some(json!({"email":email,"token":code,"type":"email"})),
                provider_headers(
                    self.provider_key
                        .as_deref()
                        .ok_or_else(|| ClientError::new("invalid_action"))?,
                )?,
            )
            .await?;
        let credentials = successful_json(response)?;
        self.establish(credentials, state).await
    }

    pub async fn discover(&mut self, state: &mut ClientState) -> Result<(), ClientError> {
        let response = self.session.request(Method::GET, "/api/mobile/session", None).await?;
        self.project(response, state)
    }

    pub fn server_order(&self) -> &[String] {
        &self.server_order
    }

    fn project(&mut self, response: Response, state: &mut ClientState) -> Result<(), ClientError> {
        if response.status == 200 {
            let result: SessionProjection =
                serde_json::from_slice(&response.body).map_err(|_| ClientError::new("invalid_response"))?;
            if result.account.id.is_empty() || result.account.email.is_empty() {
                return Err(ClientError::new("invalid_response"));
            }
            if state
                .account
                .identity
                .as_ref()
                .is_some_and(|old| old.id != result.account.id)
            {
                return Err(ClientError::new("account_changed"));
            }
            let mut ids = std::collections::HashSet::new();
            for instance in &result.instances {
                instance_origin(instance)?;
                if !ids.insert(&instance.id) {
                    return Err(ClientError::new("invalid_response"));
                }
            }
            state.account = AccountState {
                status: AccountStatus::SignedIn,
                email: Some(result.account.email.clone()),
                identity: Some(result.account),
                gate: None,
                methods: vec![],
            };
            self.server_order = result.instances.iter().map(|instance| instance.id.clone()).collect();
            state.catalog = CatalogState {
                status: CatalogStatus::Ready,
                instances: result.instances,
            };
            Ok(())
        } else if response.status == 403 && response.error().code == "two_factor_required" {
            let factor: FactorProjection =
                serde_json::from_slice(&response.body).map_err(|_| ClientError::new("invalid_response"))?;
            state.account = AccountState {
                status: AccountStatus::SecondFactorRequired,
                identity: None,
                email: Some(factor.email),
                gate: Some(factor.gate),
                // A system with no passkey sheet starts at what it can run.
                methods: factor
                    .methods
                    .into_iter()
                    .filter(|method| self.passkey_sheet || *method != SecondFactorMethod::Passkey)
                    .collect(),
            };
            self.server_order.clear();
            state.catalog = CatalogState {
                status: CatalogStatus::Idle,
                instances: vec![],
            };
            Ok(())
        } else if response.status == 401 {
            Err(ClientError::new("signed_out"))
        } else {
            Err(response.error())
        }
    }

    pub async fn verify_factor(
        &mut self,
        method: CodeMethod,
        code: String,
        state: &mut ClientState,
    ) -> Result<(), ClientError> {
        if state.account.status != AccountStatus::SecondFactorRequired
            || state.account.gate != Some(SecondFactorGate::Challenge)
            || code.is_empty()
            || code.len() > 256
        {
            return Err(ClientError::new("invalid_action"));
        }
        successful_json(
            self.session
                .request(
                    Method::POST,
                    "/api/two-factor/verify",
                    Some(json!({"method":method,"code":code})),
                )
                .await?,
        )?;
        self.discover(state).await
    }

    pub async fn verify_passkey(
        &mut self,
        passkeys: &dyn Passkeys,
        epoch: Epoch,
        state: &mut ClientState,
    ) -> Result<(), ClientError> {
        if state.account.status != AccountStatus::SecondFactorRequired
            || state.account.gate != Some(SecondFactorGate::Challenge)
            || !state.account.methods.contains(&SecondFactorMethod::Passkey)
        {
            return Err(ClientError::new("invalid_action"));
        }
        // No challenge for a sheet this system cannot show.
        if !passkeys.supported() {
            return Err(ClientError::new("passkey_unavailable"));
        }
        let challenge = successful_json(
            self.session
                .request(Method::POST, "/api/two-factor/passkey-challenge", Some(json!({})))
                .await?,
        )?;
        if challenge["options"]["rpId"] != "avibe.bot" {
            return Err(ClientError::new("invalid_passkey_challenge"));
        }
        let response = passkeys.assert(challenge["options"].clone(), epoch.clone()).await?;
        epoch.check()?;
        successful_json(
            self.session
                .request(
                    Method::POST,
                    "/api/two-factor/verify",
                    Some(json!({"method":"passkey","token":string(&challenge,"token")?,"response":response})),
                )
                .await?,
        )?;
        self.discover(state).await
    }

    pub async fn begin_totp(&mut self, state: &ClientState) -> Result<DirectResult, ClientError> {
        if state.account.status != AccountStatus::SecondFactorRequired
            || state.account.gate != Some(SecondFactorGate::Enroll)
        {
            return Err(ClientError::new("invalid_action"));
        }
        let result = successful_json(
            self.session
                .request(Method::POST, "/api/two-factor/totp/setup", Some(json!({})))
                .await?,
        )?;
        let direct = DirectResult::TotpEnrollment {
            uri: string(&result, "uri")?.into(),
            qr_svg: string(&result, "qrSvg")?.into(),
            secret: string(&result, "secret")?.into(),
        };
        self.enrollment_token = Some(string(&result, "token")?.into());
        Ok(direct)
    }

    pub async fn finish_totp(&mut self, code: String, state: &mut ClientState) -> Result<DirectResult, ClientError> {
        if state.account.status != AccountStatus::SecondFactorRequired
            || state.account.gate != Some(SecondFactorGate::Enroll)
            || code.len() > 16
        {
            return Err(ClientError::new("invalid_action"));
        }
        let token = self
            .enrollment_token
            .as_ref()
            .ok_or_else(|| ClientError::new("invalid_action"))?;
        let result = successful_json(
            self.session
                .request(
                    Method::POST,
                    "/api/two-factor/totp",
                    Some(json!({"token":token,"code":code})),
                )
                .await?,
        )?;
        let method =
            serde_json::from_value(result["method"].clone()).map_err(|_| ClientError::new("invalid_response"))?;
        let recovery_codes = result
            .get("recoveryCodes")
            .map(|v| serde_json::from_value(v.clone()))
            .transpose()
            .map_err(|_| ClientError::new("invalid_response"))?;
        self.enrollment_token = None;
        // Preserve the one-time direct result if subsequent discovery fails.
        // The next explicit refresh can recover account projection.
        let _ = self.discover(state).await;
        Ok(DirectResult::TotpEnrolled { method, recovery_codes })
    }
}

pub fn string<'a>(value: &'a Value, key: &str) -> Result<&'a str, ClientError> {
    value
        .get(key)
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .ok_or_else(|| ClientError::new("invalid_response"))
}
pub fn successful_json(response: Response) -> Result<Value, ClientError> {
    if response.success() {
        response.json()
    } else {
        Err(response.error())
    }
}
/// avibe.bot's public auth configuration, read with no account cookie. An
/// account operation fences its read to its epoch; the public switches don't.
async fn auth_config(transport: Arc<dyn Transport>, epoch: Option<Epoch>) -> Result<Value, ClientError> {
    let mut control_plane = NativeSession::new(CONTROL_PLANE.parse().expect("fixed origin"), transport);
    if let Some(epoch) = epoch {
        control_plane.fence(epoch);
    }
    let response = control_plane.request(Method::GET, "/api/config/auth", None).await?;
    successful_json(response)
}

/// The Supabase origin avibe.bot names, as a session of its own.
fn supabase_session(
    config: &Value,
    transport: Arc<dyn Transport>,
    epoch: Epoch,
) -> Result<(NativeSession, String), ClientError> {
    let url = Url::parse(string(config, "supabase_url")?).map_err(|_| ClientError::new("invalid_auth_provider"))?;
    if url.scheme() != "https"
        || url.port().is_some()
        || url.host_str().is_none()
        || !url.username().is_empty()
        || url.password().is_some()
        || url.path() != "/"
        || url.query().is_some()
        || url.fragment().is_some()
    {
        return Err(ClientError::new("invalid_auth_provider"));
    }
    let key = string(config, "supabase_anon_key")?.to_owned();
    let mut provider = NativeSession::new(url, transport);
    provider.fence(epoch);
    Ok((provider, key))
}

/// avibe.bot's primary passkey answers (`lib/passkey-primary/http.ts`), as the flow's codes.
fn primary_passkey_error(response: &Response) -> ClientError {
    let error = response.error();
    let code = match (response.status, error.code.as_str()) {
        (429, _) => "rate_limited",
        // Off. A 403 is the HTTP boundary refusing the request, not an audience
        // answer: that one is verify's `verification_failed`.
        (503, _) => "provider_unavailable",
        (400, "verification_failed") => "invalid",
        (409, "flow_cancelled") | (410, "flow_expired") => "passkey_cancelled",
        _ => "request_failed",
    };
    let mut mapped = ClientError::new(code);
    if code == "rate_limited" {
        mapped.retry_after_seconds = response
            .headers
            .get("retry-after")
            .and_then(|value| value.to_str().ok())
            .and_then(|value| value.parse::<u64>().ok())
            .map(|seconds| seconds.clamp(1, 255) as u8);
    }
    mapped
}

fn provider_headers(key: &str) -> Result<HeaderMap, ClientError> {
    let mut headers = HeaderMap::new();
    headers.insert(
        "apikey",
        key.parse().map_err(|_| ClientError::new("invalid_auth_provider"))?,
    );
    Ok(headers)
}

fn validated_email(email: String) -> Result<String, ClientError> {
    let email = email.trim().to_owned();
    if email.len() > 320 || !email.contains('@') || email.chars().any(char::is_control) {
        return Err(ClientError::new("invalid_email"));
    }
    Ok(email)
}

fn password_error(error: ClientError) -> ClientError {
    match error.code.as_str() {
        "invalid_response"
        | "invalid_auth_provider"
        | "invalid_cookie"
        | "invalid_cookie_scope"
        | "network_unavailable"
        | "response_too_large"
        | "stale_generation"
        | "signed_out"
        | "account_changed" => error,
        _ => ClientError::new("request_failed"),
    }
}
