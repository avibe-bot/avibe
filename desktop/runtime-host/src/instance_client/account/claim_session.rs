//! A first-party start contains no OTP. Only its original caller can move the
//! prepared submission across the forward boundary. After a successful bind,
//! recovery is a different object with only a session discovery operation.
use super::{
    email_code::PreparedCode, Account, AccountFlow, AccountState, AccountStatus, CatalogState, ClientError,
    ClientState, CodeMethod, Epoch, SecondFactorGate, SecondFactorMethod,
};

mod totp;
use crate::instance_client::transport::FactorCookies;
use serde_json::{json, Value};
pub(in crate::instance_client) use totp::EnrolledCodes;

pub(in crate::instance_client) struct PreparedClaimCode {
    prepared: PreparedCode,
    flow: Box<AccountFlow>,
    account: Account,
}
#[derive(Clone, Copy)]
pub(in crate::instance_client) enum BindingBase {
    SignedIn,
    OwedFactor,
}
impl BindingBase {
    pub(in crate::instance_client) fn matches(
        self,
        original: &Account,
        flow: &AccountFlow,
        state: &AccountState,
    ) -> bool {
        if flow.resumes.as_ref() != Some(&original.id) || !flow.session.is_account() {
            return false;
        }
        match self {
            Self::SignedIn => state.status == AccountStatus::SignedIn && state.identity.as_ref() == Some(original),
            Self::OwedFactor => {
                state.status == AccountStatus::SecondFactorRequired
                    && state.identity.is_none()
                    && state.gate.is_some()
                    && flow.session.has_discovery_credentials()
                    && flow.factor_owner.as_ref().is_some_and(|m| {
                        m.subject() == Some(original.id.as_str())
                            && m.matches(&original.email, Some(&original.id))
                            && state.email.as_deref().is_some_and(|e| m.matches(e, None))
                    })
            }
        }
    }
}
pub(in crate::instance_client) struct PreparedFactor {
    flow: Box<AccountFlow>,
    account: Account,
    cookies: FactorCookies,
    body: Value,
    recovery: bool,
}
pub(super) fn factor_refusal(r: &super::Response) -> ClientError {
    if r.status == 429 {
        return super::email_code::limited(super::email_code::retry_after(r));
    }
    #[derive(serde::Deserialize)]
    #[serde(deny_unknown_fields)]
    struct Denied {
        error: String,
    }
    if r.status == 400 && serde_json::from_slice::<Denied>(&r.body).is_ok_and(|d| d.error == "invalid") {
        ClientError::new("invalid_or_expired")
    } else {
        ClientError::new("factor_delivery_unknown")
    }
}
impl PreparedFactor {
    pub(in crate::instance_client) fn code(
        flow: &AccountFlow,
        state: &AccountState,
        a: &Account,
        method: CodeMethod,
        code: String,
    ) -> Result<Self, ClientError> {
        if !BindingBase::OwedFactor.matches(a, flow, state)
            || state.gate != Some(SecondFactorGate::Challenge)
            || !flow
                .session
                .same_request_cookies("/api/mobile/session", "/api/two-factor/verify")
        {
            return Err(ClientError::new("invalid_action"));
        }
        let valid = match method {
            CodeMethod::Totp => {
                state.methods.contains(&SecondFactorMethod::Totp)
                    && code.len() == 6
                    && code.bytes().all(|b| b.is_ascii_digit())
            }
            // Recovery is a challenge fallback, not an advertised enrolled method.
            CodeMethod::Recovery => !code.trim().is_empty() && code.encode_utf16().count() <= 32,
        };
        if !valid {
            return Err(ClientError::new("invalid_action"));
        }
        Ok(Self {
            flow: Box::new(flow.clone()),
            account: a.clone(),
            cookies: flow
                .session
                .factor_cookies()
                .ok_or_else(|| ClientError::new("account_changed"))?,
            body: json!({"method":method,"code":code}),
            recovery: matches!(method, CodeMethod::Recovery),
        })
    }
    pub(in crate::instance_client) async fn forward(mut self, epoch: Epoch) -> Result<BoundClaimSession, ClientError> {
        self.flow.fence(epoch);
        let passkey = self.body["method"] == "passkey";
        let response = self
            .flow
            .session
            .factor_post("/api/two-factor/verify", self.body, &self.cookies)
            .await
            .map_err(|_| ClientError::new("factor_delivery_unknown"))?;
        // No upstream text or recovery-code count becomes an identity witness.
        #[derive(serde::Deserialize)]
        #[serde(deny_unknown_fields)]
        struct Accepted {
            ok: bool,
            #[serde(rename = "recoveryCodesRemaining")]
            remaining: Option<u32>,
        }
        let accepted = response.status == 200
            && serde_json::from_slice::<Accepted>(&response.body)
                .is_ok_and(|a| a.ok && (a.remaining.is_none() || self.recovery));
        if !accepted {
            let error = factor_refusal(&response);
            return Err(if passkey && error.code == "invalid_or_expired" {
                ClientError::new("invalid_passkey_assertion")
            } else {
                error
            });
        }
        Ok(BoundClaimSession {
            flow: self.flow,
            account: self.account,
            base: BindingBase::OwedFactor,
        })
    }
}
/// A challenge never contributes response cookies to the ordinary account. Only
/// its token and original allow-list survive the disposable request jar.
#[derive(serde::Deserialize)]
#[serde(deny_unknown_fields)]
pub(in crate::instance_client) struct FactorChallenge {
    options: Value,
    token: String,
}
impl FactorChallenge {
    pub(in crate::instance_client) fn credentials(
        flow: &AccountFlow,
        state: &AccountState,
        account: &Account,
    ) -> Result<FactorCookies, ClientError> {
        if !BindingBase::OwedFactor.matches(account, flow, state)
            || state.gate != Some(SecondFactorGate::Challenge)
            || !state.methods.contains(&SecondFactorMethod::Passkey)
            || !flow
                .session
                .same_request_cookies("/api/mobile/session", "/api/two-factor/passkey-challenge")
            || !flow
                .session
                .same_request_cookies("/api/mobile/session", "/api/two-factor/verify")
        {
            return Err(ClientError::new("invalid_action"));
        }
        flow.session
            .factor_cookies()
            .ok_or_else(|| ClientError::new("account_changed"))
    }
    pub(in crate::instance_client) async fn fetch(
        mut jar: super::NativeSession,
        cookies: &FactorCookies,
    ) -> Result<Self, ClientError> {
        let invalid = || ClientError::new("invalid_passkey_challenge");
        let r = jar
            .factor_post("/api/two-factor/passkey-challenge", json!({}), cookies)
            .await?;
        if r.status == 429 {
            return Err(factor_refusal(&r));
        }
        if r.status != 200 {
            return Err(invalid());
        }
        let result: Self = serde_json::from_slice(&r.body).map_err(|_| invalid())?;
        let o = &result.options;
        if result.token.is_empty()
            || result.token.len() > 4096
            || o["rpId"] != "avibe.bot"
            || o["userVerification"] != "required"
            || o["challenge"].as_str().is_none_or(str::is_empty)
            || !o["allowCredentials"].as_array().is_some_and(|ids| {
                !ids.is_empty()
                    && ids.len() <= 100
                    && ids
                        .iter()
                        .all(|d| d["type"] == "public-key" && d["id"].as_str().is_some_and(|s| !s.is_empty()))
            })
        {
            return Err(invalid());
        }
        Ok(result)
    }
    pub(in crate::instance_client) async fn assertion(
        &self,
        platform: &dyn super::Passkeys,
        epoch: Epoch,
    ) -> Result<Value, ClientError> {
        epoch.check()?;
        let response = platform.assert(self.options.clone(), epoch.clone()).await?;
        epoch.check()?;
        if response["type"] != "public-key"
            || response["id"] != response["rawId"]
            || !response["response"].is_object()
            || !self.options["allowCredentials"]
                .as_array()
                .is_some_and(|ids| ids.iter().any(|d| d["id"] == response["id"]))
        {
            return Err(ClientError::new("invalid_passkey_assertion"));
        }
        Ok(response)
    }
    pub(in crate::instance_client) fn prepare(
        self,
        flow: &AccountFlow,
        account: &Account,
        cookies: FactorCookies,
        response: Value,
    ) -> PreparedFactor {
        PreparedFactor {
            flow: Box::new(flow.clone()),
            account: account.clone(),
            cookies,
            body: json!({"method":"passkey","token":self.token,"response":response}),
            recovery: false,
        }
    }
}

pub(in crate::instance_client) struct BoundClaimSession {
    flow: Box<AccountFlow>,
    account: Account,
    base: BindingBase,
}
impl AccountFlow {
    pub(in crate::instance_client) async fn prepare_claim_code(
        self: Box<Self>,
        account: &Account,
        code: &str,
    ) -> Result<PreparedClaimCode, ClientError> {
        let delivery = self
            .typed_code
            .as_ref()
            .filter(|d| {
                d.mailbox.subject() == Some(account.id.as_str()) && d.mailbox.matches(&account.email, Some(&account.id))
            })
            .ok_or_else(|| ClientError::new("account_changed"))?;
        let prepared = delivery.prepare(&self.session, code).await?;
        Ok(PreparedClaimCode {
            prepared,
            flow: self,
            account: account.clone(),
        })
    }
}
impl PreparedClaimCode {
    // Transfer the original non-cloneable start jar, not just the base jar. A
    // closed claim document must not hide an already-dispatched bind response.
    pub(in crate::instance_client) fn fence(&mut self, epoch: super::Epoch) {
        self.flow.fence(epoch.clone());
        self.prepared.fence(epoch);
    }
    pub(in crate::instance_client) async fn forward(self) -> Result<BoundClaimSession, ClientError> {
        // forward records the freshly bound jar on THIS delivery. A cookie from
        // the prior account jar, or just a successful start, cannot mint this value.
        self.prepared.forward().await?;
        Ok(BoundClaimSession {
            flow: self.flow,
            account: self.account,
            base: BindingBase::SignedIn,
        })
    }
}
pub(in crate::instance_client) struct DiscoveredSession {
    pub(in crate::instance_client) flow: AccountFlow,
    pub(in crate::instance_client) account: AccountState,
    pub(in crate::instance_client) catalog: CatalogState,
}
impl DiscoveredSession {
    // An MFA projection has no ID. Only this server-bound delivery and the
    // private mailbox retained by read_code_session name its credential key.
    pub(in crate::instance_client) fn belongs_to(&self, original: &Account) -> bool {
        if self.flow.resumes.as_deref() != Some(original.id.as_str()) || !self.flow.session.has_discovery_credentials()
        {
            return false;
        }
        let Ok(mailbox) = super::Mailbox::same_account(original) else {
            return false;
        };
        match self.account.status {
            AccountStatus::SignedIn => self
                .account
                .identity
                .as_ref()
                .is_some_and(|a| mailbox.matches(&a.email, Some(&a.id))),
            AccountStatus::SecondFactorRequired => BindingBase::OwedFactor.matches(original, &self.flow, &self.account),
            _ => false,
        }
    }
}
impl BoundClaimSession {
    pub(in crate::instance_client) async fn discover(
        &self,
        mut state: ClientState,
    ) -> Result<DiscoveredSession, ClientError> {
        let mut next = self.flow.as_ref().clone();
        match self.base {
            BindingBase::SignedIn => {
                if state.account.status != AccountStatus::SignedIn
                    || state.account.identity.as_ref() != Some(&self.account)
                {
                    return Err(ClientError::new("account_changed"));
                }
                next.read_code_session(None, &mut state).await?;
            }
            BindingBase::OwedFactor => {
                if !self.base.matches(&self.account, &next, &state.account) {
                    return Err(ClientError::new("account_changed"));
                }
                // factor_owner pins BOTH the mailbox and the immutable subject on this GET.
                next.discover(&mut state).await?;
                if state.account.status != AccountStatus::SignedIn {
                    return Err(ClientError::new("two_factor_required"));
                }
            }
        }
        let found = DiscoveredSession {
            flow: next,
            account: state.account,
            catalog: state.catalog,
        };
        if !found.belongs_to(&self.account) {
            return Err(ClientError::new("account_changed"));
        }
        Ok(found)
    }
}
