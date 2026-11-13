//! Account protocol adapter. Secret inputs and cookie updates remain native.
use std::sync::Arc;

use async_trait::async_trait;
use reqwest::{header::HeaderMap, Method};
use serde::Deserialize;
use serde_json::{json, Value};
use url::Url;

use super::{
    protocol::*,
    transport::{Epoch, NativeSession, Response, Transport},
};

pub const CONTROL_PLANE: &str = "https://avibe.bot/";
pub const EMAIL_REDIRECT: &str = "https://avibe.bot/auth/callback?source=desktop-app";

#[async_trait]
pub trait Passkeys: Send + Sync {
    /// The implementation uses a native system sheet, never a WebView bridge.
    async fn assert(&self, options: Value, epoch: Epoch) -> Result<Value, ClientError>;
}

#[derive(Clone)]
pub struct AccountFlow {
    pub session: NativeSession,
    provider: Option<NativeSession>,
    provider_key: Option<String>,
    email: Option<String>,
    enrollment_token: Option<String>,
    transport: Arc<dyn Transport>,
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
    pub fn new(transport: Arc<dyn Transport>) -> Self {
        Self {
            session: NativeSession::new(CONTROL_PLANE.parse().expect("fixed origin"), transport.clone()),
            provider: None,
            provider_key: None,
            email: None,
            enrollment_token: None,
            transport,
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

    pub async fn request_otp(
        &mut self,
        email: String,
        epoch: Epoch,
        state: &mut ClientState,
    ) -> Result<(), ClientError> {
        if state.account.status == AccountStatus::SignedIn {
            return Err(ClientError::new("invalid_action"));
        }
        let email = email.trim().to_owned();
        if email.len() > 320 || !email.contains('@') || email.chars().any(char::is_control) {
            return Err(ClientError::new("invalid_email"));
        }
        let response = self.session.request(Method::GET, "/api/config/auth", None).await?;
        let config = successful_json(response)?;
        let url =
            Url::parse(string(&config, "supabase_url")?).map_err(|_| ClientError::new("invalid_auth_provider"))?;
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
        let key = string(&config, "supabase_anon_key")?.to_owned();
        let mut provider = NativeSession::new(url, self.transport.clone());
        provider.fence(epoch);
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
        let response = self.session.request(Method::POST, "/api/desktop/session", Some(json!({
            "access_token":string(&credentials,"access_token")?, "refresh_token":string(&credentials,"refresh_token")?,
        }))).await?;
        self.clear_transient();
        Self::project(response, state)
    }

    pub async fn discover(&mut self, state: &mut ClientState) -> Result<(), ClientError> {
        let response = self.session.request(Method::GET, "/api/mobile/session", None).await?;
        Self::project(response, state)
    }

    fn project(response: Response, state: &mut ClientState) -> Result<(), ClientError> {
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
                methods: factor.methods,
            };
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
fn provider_headers(key: &str) -> Result<HeaderMap, ClientError> {
    let mut headers = HeaderMap::new();
    headers.insert(
        "apikey",
        key.parse().map_err(|_| ClientError::new("invalid_auth_provider"))?,
    );
    Ok(headers)
}
