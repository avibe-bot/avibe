//! Public, nonsecret Desktop bridge contract. Credentials are never fields here.
use serde::{Deserialize, Serialize};
use url::Url;

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
pub struct Account {
    pub id: String,
    pub email: String,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum Relationship {
    Owner,
    Shared,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
pub struct Instance {
    pub id: String,
    pub name: String,
    pub host: String,
    pub url: String,
    pub relationship: Relationship,
    pub owner_email: Option<String>,
    pub available: bool,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum ClientTarget {
    Local {
        name: String,
        instance_id: Option<String>,
    },
    Remote {
        name: String,
        instance_id: String,
        canonical_origin: String,
    },
}
impl ClientTarget {
    pub fn is_local(&self) -> bool {
        matches!(self, Self::Local { .. })
    }
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum AccountStatus {
    SignedOut,
    Verifying,
    SecondFactorRequired,
    SignedIn,
}
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum SecondFactorMethod {
    Totp,
    Passkey,
}
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum SecondFactorGate {
    Challenge,
    Enroll,
}
#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct AccountState {
    pub status: AccountStatus,
    pub identity: Option<Account>,
    pub email: Option<String>,
    pub gate: Option<SecondFactorGate>,
    pub methods: Vec<SecondFactorMethod>,
}
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum ConnectionStatus {
    Local,
    Switching,
    RemoteConnected,
    RemoteUnreachable,
}
#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct ConnectionState {
    pub status: ConnectionStatus,
    pub committed: ClientTarget,
    pub pending: Option<ClientTarget>,
}
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum EnrollmentStatus {
    Unknown,
    Unpaired,
    Registering,
    PairedThisAccount,
    Conflict,
    Failed,
    PairedTunnelUnavailable,
}
#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct EnrollmentState {
    pub status: EnrollmentStatus,
    pub instance_id: Option<String>,
    pub error_code: Option<String>,
}
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum CatalogStatus {
    Idle,
    Loading,
    Ready,
    Error,
}
#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct CatalogState {
    pub status: CatalogStatus,
    pub instances: Vec<Instance>,
}
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum RemoteAccessState {
    Off,
    Starting,
    On,
    Unavailable,
}
#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct LocalState {
    pub name: String,
    pub remote_access: RemoteAccessState,
}
#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct DesktopState {
    pub version: String,
    pub update: serde_json::Value,
}
#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct ConsentState {
    pub instance_id: String,
    pub instance_name: String,
    pub host: String,
    pub scope: String,
}
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum RemoteAuxiliary {
    Local,
    DisabledIncomplete,
}
#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct ClientState {
    pub version: u8,
    pub revision: u64,
    pub generation: u64,
    pub account: AccountState,
    pub connection: ConnectionState,
    pub enrollment: EnrollmentState,
    pub catalog: CatalogState,
    pub local: LocalState,
    pub desktop: DesktopState,
    pub consent: Option<ConsentState>,
    pub remote_auxiliary: RemoteAuxiliary,
}
impl ClientState {
    pub fn new(name: String, version: String, update: serde_json::Value) -> Self {
        Self {
            version: 1,
            revision: 0,
            generation: 0,
            account: AccountState {
                status: AccountStatus::SignedOut,
                identity: None,
                email: None,
                gate: None,
                methods: vec![],
            },
            connection: ConnectionState {
                status: ConnectionStatus::Local,
                committed: ClientTarget::Local {
                    name: name.clone(),
                    instance_id: None,
                },
                pending: None,
            },
            enrollment: EnrollmentState {
                status: EnrollmentStatus::Unknown,
                instance_id: None,
                error_code: None,
            },
            catalog: CatalogState {
                status: CatalogStatus::Idle,
                instances: vec![],
            },
            local: LocalState {
                name,
                remote_access: RemoteAccessState::Off,
            },
            desktop: DesktopState { version, update },
            consent: None,
            remote_auxiliary: RemoteAuxiliary::Local,
        }
    }
    /// Runtime pages receive no catalog, enrollment or consent transaction.
    pub fn runtime_projection(&self) -> serde_json::Value {
        serde_json::json!({
            "version": self.version, "revision":self.revision, "generation":self.generation,
            "account": {"status":self.account.status,"identity":self.account.identity,"email":self.account.email},
            "connection":self.connection,"local":self.local,"desktop":self.desktop,
            "remote_auxiliary":self.remote_auxiliary,
        })
    }
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ClientRequest {
    pub version: u8,
    pub request_id: String,
    pub expected_generation: u64,
    pub action: ClientAction,
}
#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(tag = "type", rename_all = "snake_case", deny_unknown_fields)]
pub enum ClientAction {
    GetState,
    RequestOtp { email: String },
    VerifyOtp { code: String },
    VerifySecondFactor { method: CodeMethod, code: String },
    VerifyPasskey,
    BeginTotpEnrollment,
    FinishTotpEnrollment { code: String },
    RefreshInstances,
    RetryEnrollment,
    RetryLocalRemoteAccess,
    SelectInstance { instance_id: String },
    DecideConsent { approve: bool },
    Cancel,
    ReturnLocal,
    SignOut,
}
#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum CodeMethod {
    Totp,
    Recovery,
}
#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct ClientError {
    pub code: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub retry_after_seconds: Option<u8>,
}
impl ClientError {
    pub fn new(code: impl Into<String>) -> Self {
        Self {
            code: code.into(),
            retry_after_seconds: None,
        }
    }
    pub fn pending(code: &str, delay: &serde_json::Value) -> Self {
        let delay = delay.as_u64().filter(|n| (1..=60).contains(n)).map(|n| n as u8);
        if delay.is_some() {
            Self {
                code: code.to_owned(),
                retry_after_seconds: delay,
            }
        } else {
            Self::new("invalid_response")
        }
    }
}
impl From<reqwest::Error> for ClientError {
    fn from(_: reqwest::Error) -> Self {
        Self::new("network_unavailable")
    }
}
#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum DirectResult {
    TotpEnrollment {
        uri: String,
        #[serde(rename = "qrSvg")]
        qr_svg: String,
        secret: String,
    },
    TotpEnrolled {
        method: TotpMethod,
        #[serde(rename = "recoveryCodes", skip_serializing_if = "Option::is_none")]
        recovery_codes: Option<Vec<String>>,
    },
}
#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct TotpMethod {
    pub id: String,
    pub kind: String,
    pub name: String,
    pub created_at: String,
    pub last_used_at: Option<String>,
}
#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct ClientReply {
    pub version: u8,
    pub request_id: String,
    pub ok: bool,
    pub state: ClientState,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<ClientError>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub result: Option<DirectResult>,
}

/// An exact catalog tuple is required in addition to namespace validation.
pub fn instance_origin(instance: &Instance) -> Result<Url, ClientError> {
    let url = Url::parse(&instance.url).map_err(|_| ClientError::new("invalid_instance_origin"))?;
    let host = url
        .host_str()
        .ok_or_else(|| ClientError::new("invalid_instance_origin"))?;
    let slug = host.strip_suffix(".avibe.bot").unwrap_or("");
    if instance.id.is_empty()
        || host != instance.host
        || slug.is_empty()
        || slug.len() > 63
        || slug.starts_with('-')
        || slug.ends_with('-')
        || !slug
            .bytes()
            .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == b'-')
        || url.scheme() != "https"
        || url.port().is_some()
        || !url.username().is_empty()
        || url.password().is_some()
        || url.path() != "/"
        || url.query().is_some()
        || url.fragment().is_some()
    {
        return Err(ClientError::new("invalid_instance_origin"));
    }
    Ok(url)
}

pub fn account_view(url: &Url, owned_bundle_origin: &Url) -> Option<&'static str> {
    if url.scheme() != owned_bundle_origin.scheme()
        || url.host_str() != owned_bundle_origin.host_str()
        || url.port() != owned_bundle_origin.port()
        || url.path() != "/account/index.html"
        || url.fragment().is_some()
        || !url.username().is_empty()
        || url.password().is_some()
    {
        return None;
    }
    let pairs: Vec<_> = url.query_pairs().collect();
    if pairs.len() > 1 || pairs.first().is_some_and(|(key, _)| key != "view") {
        return None;
    }
    match pairs.first().map(|(_, value)| value.as_ref()).unwrap_or("account") {
        "account" => Some("account"),
        "instances" => Some("instances"),
        "setup-complete" => Some("setup-complete"),
        "sign-out" => Some("sign-out"),
        _ => None,
    }
}

pub fn presentation_view(url: &Url) -> Option<&'static str> {
    if url.scheme() != "avibe"
        || url.host_str() != Some("desktop")
        || url.query().is_some()
        || url.fragment().is_some()
        || !url.username().is_empty()
        || url.password().is_some()
        || url.port().is_some()
    {
        return None;
    }
    match url.path() {
        "/account" => Some("account"),
        "/instances" => Some("instances"),
        "/setup-complete" => Some("setup-complete"),
        "/sign-out" => Some("sign-out"),
        _ => None,
    }
}
