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
    loopback::{Callback, CallbackCopy, Loopback, ProviderFailure, ResultPage, Secrets, WAIT},
    protocol::*,
    transport::{Epoch, NativeSession, Response, Transport},
};

mod claim_session;
mod email_code;
pub(super) use claim_session::{
    BindingBase, BoundClaimSession, DiscoveredSession, EnrolledCodes, FactorChallenge, PreparedClaimCode,
    PreparedFactor,
};
pub(super) use email_code::validate_code;
use email_code::{Mailbox, TypedCode};

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
    /// What the browser's last page says, in `locale`, from the product catalogs.
    fn callback_copy(&self, locale: UiLocale) -> CallbackCopy;
}

/// A provider sign-in between its start and its code. Its secrets stay here;
/// dropping it retains only a bounded, read-only browser result responder.
pub struct ProviderAttempt {
    supabase: NativeSession,
    key: String,
    secrets: Secrets,
    loopback: Loopback,
    authorize: Url,
    redemption_started: bool,
}
impl ProviderAttempt {
    pub fn complete(&mut self, reply: &ClientReply) {
        let result = match reply.error.as_ref() {
            Some(error) if error.code == "stale_generation" => ResultPage::Cancelled,
            Some(error) => ResultPage::Failed(ProviderFailure::from_code(&error.code)),
            None if reply.state.account.status == AccountStatus::SignedIn => ResultPage::Complete,
            None if reply.state.account.status == AccountStatus::SecondFactorRequired => ResultPage::VerifyInApp,
            None => ResultPage::Failed(ProviderFailure::from_code("provider_failed")),
        };
        self.loopback.complete(result);
    }

    /// Opens Supabase's authorization page in the default browser and waits,
    /// holding no lock, for this attempt's callback. A newer generation (another
    /// attempt, an account change, return_local, abort, a closed window) ends it.
    pub async fn browse(&mut self, browser: &dyn SystemBrowser, epoch: &Epoch) -> Result<String, ClientError> {
        epoch.check()?;
        let loopback = &mut self.loopback;
        browser.open(&self.authorize)?;
        match loopback.wait(&self.secrets.state, epoch, WAIT).await? {
            Callback::Code(code) => Ok(code),
            Callback::Failed(failure) => Err(ClientError::new(failure.code)),
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
    typed_code: Option<TypedCode>,
    // A hosted MFA projection intentionally has no subject. Hold the original
    // mailbox/expected subject through MFA until an authenticated 200 read.
    factor_owner: Option<Mailbox>,
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
    /// that succeeds clears it.
    pub resumes: Option<String>,
    /// The jar of a passkey sign-in avibe.bot accepted whose session read
    /// failed, kept beside the current attempt: the next passkey sign-in reads
    /// that session again rather than asking for another passkey (backend
    /// passkey-sign-in §6, as the iPhone does). Any first factor that succeeds
    /// drops it; so does a `401` on the read.
    accepted_passkey: Option<NativeSession>,
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
            session: NativeSession::account(transport.clone()),
            typed_code: None,
            factor_owner: None,
            enrollment_token: None,
            server_order: vec![],
            transport,
            passkey_sheet,
            resumes: None,
            accepted_passkey: None,
        }
    }

    pub fn fence(&mut self, epoch: Epoch) {
        self.session.fence(epoch);
    }

    /// The claim reader uses only this native account jar, never a provider attempt,
    /// pending sign-in token or recoverable passkey jar from an account transaction.
    pub(super) fn for_claim(&self, epoch: Epoch) -> Self {
        let mut flow = Self::new(self.transport.clone(), self.passkey_sheet);
        flow.session = self.session.clone();
        flow.session.fence(epoch);
        flow
    }

    pub fn clear_transient(&mut self) {
        self.typed_code = None;
        self.factor_owner = None;
        self.enrollment_token = None;
    }

    /// A first factor runs on a copy of this flow and of the account state,
    /// with a jar of its own: a replacement attempt never carries the previous
    /// email's verifier or MFA cookie. Only its success replaces the flow and
    /// the state (`commit`). A failure leaves the previous attempt, its jar,
    /// its code and the published state exactly as they were. The caller's
    /// generation still fences both: an attempt that something newer replaced
    /// (another first factor, an escape) commits nothing.
    fn first_factor(&self, epoch: Epoch, state: &ClientState) -> Result<(AccountFlow, ClientState), ClientError> {
        let mut flow = self.clone();
        let mut next = state.clone();
        flow.begin_first_factor(epoch, &mut next)?;
        Ok((flow, next))
    }

    /// A first factor succeeded: its flow and its account state replace these.
    fn commit(&mut self, flow: AccountFlow, next: ClientState, state: &mut ClientState) {
        *self = flow;
        state.account = next.account;
        state.catalog = next.catalog;
    }

    fn begin_first_factor(&mut self, epoch: Epoch, state: &mut ClientState) -> Result<(), ClientError> {
        if state.account.status == AccountStatus::SignedIn {
            return Err(ClientError::new("invalid_action"));
        }
        self.clear_transient();
        self.resumes = None;
        self.accepted_passkey = None;
        self.session = NativeSession::account(self.transport.clone());
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
        if let Some(old) = &self.typed_code {
            old.can_resend()?;
        }
        let (mut flow, mut next) = self.first_factor(epoch.clone(), state)?;
        let mailbox = Mailbox::login(&email)?;
        let email = mailbox.email().to_owned();
        flow.send_code(mailbox, epoch, || Ok(())).await?;
        next.account = AccountState {
            status: AccountStatus::Verifying,
            identity: None,
            email: Some(email),
            gate: None,
            methods: vec![],
        };
        self.commit(flow, next, state);
        Ok(())
    }

    async fn send_code(
        &mut self,
        mailbox: Mailbox,
        epoch: Epoch,
        before_delivery: impl FnOnce() -> Result<(), ClientError>,
    ) -> Result<(), ClientError> {
        let config = auth_config(self.transport.clone(), Some(epoch.clone()))
            .await
            .map_err(email_code::transport_error)?;
        let (issuer, key) =
            supabase_session(&config, self.transport.clone(), epoch).map_err(email_code::transport_error)?;
        let headers = provider_headers(&key)?;
        before_delivery()?; // discovery may have outlived this document, ticket, or account
        self.typed_code = Some(mailbox.deliver(issuer, headers).await?);
        Ok(())
    }

    // Private account half only: the claim owner still has to witness and fence
    // this identity, adopt the new ordinary session independently, and re-read.
    pub(super) async fn send_claim_code(
        &mut self,
        witnessed: &Account,
        epoch: Epoch,
        before_delivery: impl FnOnce() -> Result<(), ClientError>,
    ) -> Result<(), ClientError> {
        if let Some(old) = &self.typed_code {
            old.can_resend()?;
        }
        self.send_code(Mailbox::same_account(witnessed)?, epoch, before_delivery)
            .await
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
        copy: &CallbackCopy,
    ) -> Result<ProviderAttempt, ClientError> {
        let config = auth_config(transport.clone(), Some(epoch.clone())).await?;
        if config.get(provider.enabled_flag()).and_then(Value::as_bool) != Some(true) {
            return Err(ClientError::new("provider_unavailable"));
        }
        let (supabase, key) = supabase_session(&config, transport, epoch)?;
        let secrets = Secrets::new()?;
        // Bound before the browser opens, so the redirect always has a listener.
        let loopback = Loopback::bind(copy).await?;
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
            loopback,
            authorize,
            redemption_started: false,
        })
    }

    /// Google, GitHub or Apple, second half, under the operation lock: a new
    /// first factor that redeems the browser's code with this attempt's own
    /// verifier, then ordinary native token establishment and the two-step gate.
    pub async fn finish_provider_sign_in(
        &mut self,
        attempt: &mut ProviderAttempt,
        code: String,
        epoch: Epoch,
        state: &mut ClientState,
    ) -> Result<(), ClientError> {
        // The retained result-page owner must not make the PKCE attempt reusable.
        if std::mem::replace(&mut attempt.redemption_started, true) {
            return Err(ClientError::new("invalid_action"));
        }
        let (mut flow, mut next) = self.first_factor(epoch.clone(), state)?;
        flow.redeem_provider_code(attempt, code, epoch, &mut next).await?;
        self.commit(flow, next, state);
        Ok(())
    }

    async fn redeem_provider_code(
        &mut self,
        attempt: &mut ProviderAttempt,
        code: String,
        epoch: Epoch,
        state: &mut ClientState,
    ) -> Result<(), ClientError> {
        let ProviderAttempt {
            supabase, key, secrets, ..
        } = attempt;
        supabase.fence(epoch);
        let response = supabase
            .request_with_headers(
                Method::POST,
                "/auth/v1/token?grant_type=pkce",
                Some(json!({"auth_code": code, "code_verifier": secrets.verifier})),
                provider_headers(key)?,
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
        let (mut flow, mut next) = self.first_factor(epoch.clone(), state)?;
        if let Some(accepted) = self.accepted_passkey.clone() {
            flow.session = accepted;
            flow.session.fence(epoch);
        } else {
            flow.run_primary_passkey(passkeys, epoch).await?;
        }
        // avibe.bot accepted the passkey: its session is in this jar. A read that
        // fails keeps the jar aside for the next attempt, not in place of the
        // current one; a `401` ends it.
        match flow.discover(&mut next).await {
            Ok(()) => {
                self.commit(flow, next, state);
                Ok(())
            }
            Err(error) => {
                self.accepted_passkey = (error.code != "signed_out").then_some(flow.session);
                Err(error)
            }
        }
    }

    /// avibe.bot's primary passkey options, the system sheet and verify, in
    /// this attempt's jar. Success means avibe.bot accepted the passkey.
    async fn run_primary_passkey(&mut self, passkeys: &dyn Passkeys, epoch: Epoch) -> Result<(), ClientError> {
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
        let body = response.json()?;
        if response.status != 200 || body["ok"] != true || !body["next"].is_string() {
            return Err(ClientError::new("invalid_response"));
        }
        Ok(())
    }

    pub async fn sign_in_password(
        &mut self,
        email: String,
        password: String,
        epoch: Epoch,
        state: &mut ClientState,
    ) -> Result<(), ClientError> {
        let (mut flow, mut next) = self.first_factor(epoch.clone(), state)?;
        flow.password_first_factor(email, password, epoch, &mut next).await?;
        self.commit(flow, next, state);
        Ok(())
    }

    async fn password_first_factor(
        &mut self,
        email: String,
        password: String,
        epoch: Epoch,
        state: &mut ClientState,
    ) -> Result<(), ClientError> {
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
        if state.account.status != AccountStatus::Verifying {
            return Err(ClientError::new("invalid_action"));
        }
        self.read_code_session(Some(&code), state).await
    }

    pub(super) async fn read_code_session(
        &mut self,
        code: Option<&str>,
        state: &mut ClientState,
    ) -> Result<(), ClientError> {
        let delivery = self
            .typed_code
            .clone()
            .ok_or_else(|| ClientError::new("invalid_action"))?;
        let mailbox = delivery.mailbox.clone();
        let mut jar = delivery.session(&self.session, code).await?;
        let read = jar
            .request(Method::GET, "/api/mobile/session", None)
            .await
            .map_err(email_code::transport_error)?;
        if let Err(e) = self.project_for(read, state, Some(&mailbox)) {
            // This was discovery of an isolated candidate. Its denial must not
            // erase an unrelated saved account through coordinator sign-out.
            if matches!(e.code.as_str(), "signed_out" | "account_changed") {
                delivery.discard_bound();
                return Err(ClientError::new("fresh_code_required"));
            }
            return Err(e);
        }
        self.session = jar;
        self.typed_code = None;
        // Only a subject frozen before a successful, server-bound step-up may
        // resume its old credential key while MFA still owes its projection.
        if let Some(subject) = mailbox.subject() {
            self.resumes = Some(subject.to_owned());
        }
        self.factor_owner = (state.account.status == AccountStatus::SecondFactorRequired).then_some(mailbox);
        Ok(())
    }

    pub async fn discover(&mut self, state: &mut ClientState) -> Result<(), ClientError> {
        if self.typed_code.is_some() {
            return self.read_code_session(None, state).await;
        }
        if let Some(owner) = self.factor_owner.clone() {
            let mut jar = self.session.clone();
            let response = jar
                .request(Method::GET, "/api/mobile/session", None)
                .await
                .map_err(email_code::transport_error)?;
            self.project_for(response, state, Some(&owner))?;
            self.session = jar;
            if state.account.status == AccountStatus::SignedIn {
                self.factor_owner = None;
            }
            return Ok(());
        }
        let response = self.session.request(Method::GET, "/api/mobile/session", None).await?;
        self.project(response, state)
    }

    pub fn server_order(&self) -> &[String] {
        &self.server_order
    }

    fn project(&mut self, response: Response, state: &mut ClientState) -> Result<(), ClientError> {
        self.project_for(response, state, None)
    }
    fn project_for(
        &mut self,
        response: Response,
        state: &mut ClientState,
        owner: Option<&Mailbox>,
    ) -> Result<(), ClientError> {
        if response.status == 200 {
            let result: SessionProjection =
                serde_json::from_slice(&response.body).map_err(|_| ClientError::new("invalid_response"))?;
            if owner.is_some_and(|m| !m.matches(&result.account.email, Some(&result.account.id))) {
                return Err(ClientError::new("account_changed"));
            }
            if result.account.id.is_empty() || result.account.email.is_empty() {
                return Err(ClientError::new("invalid_response"));
            }
            if self.resumes.as_ref().is_some_and(|id| id != &result.account.id)
                || state
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
            if owner.is_some_and(|m| !m.matches(&factor.email, None)) {
                return Err(ClientError::new("account_changed"));
            }
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
        } else if owner.is_some() {
            Err(ClientError::new("request_failed"))
        } else {
            let error = response.error();
            // These two outcomes retire credentials. Only the HTTP 401 or
            // the authenticated identity comparison above can establish them.
            if matches!(error.code.as_str(), "signed_out" | "account_changed") {
                Err(ClientError::new("request_failed"))
            } else {
                Err(error)
            }
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
            .map(|seconds| seconds.clamp(1, 255));
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
