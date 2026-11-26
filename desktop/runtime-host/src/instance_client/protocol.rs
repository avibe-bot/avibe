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
    #[serde(default)]
    pub slug: Option<String>,
    #[serde(default)]
    pub instance_id: Option<String>,
    #[serde(default)]
    pub remote_access_url: Option<String>,
    pub remote_access: RemoteAccessState,
    /// This computer's Runtime is too old for this Desktop: it cannot describe
    /// itself, or has no claim protocol. Cleared once it can.
    #[serde(default)]
    pub runtime_too_old: bool,
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
    #[serde(default)]
    pub current_instance: Option<Instance>,
    pub local: LocalState,
    pub desktop: DesktopState,
    #[serde(default)]
    pub instance_update: Option<super::instance_update::InstanceUpdate>,
    pub consent: Option<ConsentState>,
    pub remote_auxiliary: RemoteAuxiliary,
    /// The ways in avibe.bot's public configuration switches on; empty until read.
    #[serde(default)]
    pub sign_in: SignInMethodsState,
}
/// avibe.bot's public sign-in switches as native last read them (`/api/config/auth`).
/// A switch that is missing or false offers nothing.
#[derive(Clone, Debug, Default, Deserialize, Serialize, PartialEq, Eq)]
pub struct SignInMethodsState {
    pub providers: Vec<SignInProvider>,
    pub passkey: bool,
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
                slug: None,
                instance_id: None,
                remote_access_url: None,
                remote_access: RemoteAccessState::Off,
                runtime_too_old: false,
            },
            desktop: DesktopState { version, update },
            instance_update: None,
            current_instance: None,
            consent: None,
            remote_auxiliary: RemoteAuxiliary::Local,
            sign_in: SignInMethodsState::default(),
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
#[derive(Clone, Deserialize, Serialize)]
#[serde(tag = "type", rename_all = "snake_case", deny_unknown_fields)]
pub enum ClientAction {
    GetState,
    RequestOtp {
        email: String,
    },
    SignInPassword {
        email: String,
        password: String,
    },
    VerifyOtp {
        code: String,
    },
    VerifySecondFactor {
        method: CodeMethod,
        code: String,
    },
    VerifyPasskey,
    /// Google, GitHub or Apple in the system browser, back through loopback PKCE.
    /// `locale` is the page's language: the browser's last page speaks it.
    SignInWithProvider {
        provider: SignInProvider,
        #[serde(default)]
        locale: UiLocale,
    },
    /// Passkey sign-in with no email first (not the second-factor passkey).
    SignInWithPasskey,
    /// Ends an unfinished sign-in attempt without leaving the account window.
    AbortSignIn,
    /// Reads avibe.bot's public sign-in switches into `sign_in`.
    RefreshSignInMethods,
    BeginTotpEnrollment,
    FinishTotpEnrollment {
        code: String,
    },
    RefreshInstances,
    RetryEnrollment,
    RetryLocalRemoteAccess,
    SelectInstance {
        instance_id: String,
    },
    DecideConsent {
        approve: bool,
    },
    InstanceUpdateRefresh {},
    InstanceUpdateCheck {},
    InstanceUpdateSetPrereleases {
        enabled: bool,
    },
    InstanceUpdateSetAutoUpdate {
        enabled: bool,
    },
    InstanceUpdateInstall {
        target_version: String,
    },
    UpdateCheck {},
    UpdateSetPrereleases {
        enabled: bool,
    },
    UpdateSetAutoUpdate {
        enabled: bool,
    },
    UpdateInstall {
        target_version: String,
    },
    /// Upgrades this computer's Runtime when it is too old for this Desktop
    /// (`local_runtime_too_old`), as the Workbench's own update would, and waits
    /// until this Desktop has the upgraded Runtime again. The shell answers it
    /// (`Coordinator::upgrade_local_runtime`), with the Runtime host.
    UpgradeLocalRuntime {},
    Cancel,
    /// A completed sign-in returns to the selected instance's home.
    ReturnHome,
    ReturnLocal,
    SignOut {
        #[serde(default, skip_serializing_if = "Option::is_none")]
        intent_id: Option<String>,
    },
}
// Actions can carry authentication material. Debug never formats their fields.
impl std::fmt::Debug for ClientAction {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        let name = match self {
            Self::GetState => "GetState",
            Self::RequestOtp { .. } => "RequestOtp",
            Self::SignInPassword { .. } => "SignInPassword",
            Self::VerifyOtp { .. } => "VerifyOtp",
            Self::VerifySecondFactor { .. } => "VerifySecondFactor",
            Self::VerifyPasskey => "VerifyPasskey",
            Self::SignInWithProvider { .. } => "SignInWithProvider",
            Self::SignInWithPasskey => "SignInWithPasskey",
            Self::AbortSignIn => "AbortSignIn",
            Self::RefreshSignInMethods => "RefreshSignInMethods",
            Self::BeginTotpEnrollment => "BeginTotpEnrollment",
            Self::FinishTotpEnrollment { .. } => "FinishTotpEnrollment",
            Self::RefreshInstances => "RefreshInstances",
            Self::RetryEnrollment => "RetryEnrollment",
            Self::RetryLocalRemoteAccess => "RetryLocalRemoteAccess",
            Self::SelectInstance { .. } => "SelectInstance",
            Self::DecideConsent { .. } => "DecideConsent",
            Self::InstanceUpdateRefresh {} => "InstanceUpdateRefresh",
            Self::InstanceUpdateCheck {} => "InstanceUpdateCheck",
            Self::InstanceUpdateSetPrereleases { .. } => "InstanceUpdateSetPrereleases",
            Self::InstanceUpdateSetAutoUpdate { .. } => "InstanceUpdateSetAutoUpdate",
            Self::InstanceUpdateInstall { .. } => "InstanceUpdateInstall",
            Self::UpdateCheck { .. } => "UpdateCheck",
            Self::UpdateSetPrereleases { .. } => "UpdateSetPrereleases",
            Self::UpdateSetAutoUpdate { .. } => "UpdateSetAutoUpdate",
            Self::UpdateInstall { .. } => "UpdateInstall",
            Self::UpgradeLocalRuntime {} => "UpgradeLocalRuntime",
            Self::Cancel => "Cancel",
            Self::ReturnHome => "ReturnHome",
            Self::ReturnLocal => "ReturnLocal",
            Self::SignOut { .. } => "SignOut",
        };
        f.write_str(name)
    }
}
impl ClientAction {
    pub fn is_instance_update(&self) -> bool {
        matches!(
            self,
            Self::InstanceUpdateRefresh {}
                | Self::InstanceUpdateCheck {}
                | Self::InstanceUpdateSetPrereleases { .. }
                | Self::InstanceUpdateSetAutoUpdate { .. }
                | Self::InstanceUpdateInstall { .. }
        )
    }
    pub fn is_desktop_update(&self) -> bool {
        matches!(
            self,
            Self::UpdateCheck {}
                | Self::UpdateSetPrereleases { .. }
                | Self::UpdateSetAutoUpdate { .. }
                | Self::UpdateInstall { .. }
        )
    }
}
#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum CodeMethod {
    Totp,
    Recovery,
}
/// The language of the page that asked; the product's two catalogs.
#[derive(Clone, Copy, Debug, Default, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum UiLocale {
    #[default]
    En,
    Zh,
}

/// The other accounts Desktop signs in with, as avibe.bot's login card lists them.
#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum SignInProvider {
    Google,
    Github,
    Apple,
}
impl SignInProvider {
    /// Supabase's provider name.
    pub fn id(self) -> &'static str {
        match self {
            Self::Google => "google",
            Self::Github => "github",
            Self::Apple => "apple",
        }
    }
    /// avibe.bot's public `/api/config/auth` switch for this provider.
    pub fn enabled_flag(self) -> &'static str {
        match self {
            Self::Google => "google_login_enabled",
            Self::Github => "github_login_enabled",
            Self::Apple => "apple_login_enabled",
        }
    }
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct ClientError {
    pub code: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub retry_after_seconds: Option<u64>,
    // Keep the monotonic admission deadline private; the wire carries whole seconds.
    #[serde(skip)]
    pub(super) retry_until: Option<tokio::time::Instant>,
}
impl ClientError {
    pub fn new(code: impl Into<String>) -> Self {
        Self {
            code: code.into(),
            retry_after_seconds: None,
            retry_until: None,
        }
    }
    pub fn pending(code: &str, delay: &serde_json::Value) -> Self {
        let delay = delay.as_u64().filter(|n| (1..=60).contains(n));
        if delay.is_some() {
            Self {
                code: code.to_owned(),
                retry_after_seconds: delay,
                retry_until: None,
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
    SignOutIntent {
        intent_id: String,
        generation: u64,
    },
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
    /// `upgrade_local_runtime`: `upgraded` (the Runtime answers a newer version,
    /// this Desktop has it again, and it has the claim protocol: repeat the step
    /// that needed it) or `desktop_update` (Desktop manages that Runtime: its
    /// own update is the upgrade).
    LocalRuntimeUpgrade {
        state: String,
    },
}
impl DirectResult {
    /// Ephemeral native presentation receipt; never part of ClientState.
    pub fn new_sign_out_intent(generation: u64) -> Self {
        Self::SignOutIntent {
            intent_id: uuid::Uuid::new_v4().to_string(),
            generation,
        }
    }
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
    let mut view = None;
    let mut locale = None;
    for (key, value) in url.query_pairs() {
        match key.as_ref() {
            "view" if view.is_none() => view = Some(value.into_owned()),
            "locale" if locale.is_none() && matches!(value.as_ref(), "en" | "zh") => locale = Some(value),
            _ => return None,
        }
    }
    match view.as_deref().unwrap_or("account") {
        "account" => Some("account"),
        "claim" => Some("claim"),
        "instances" => Some("instances"),
        "setup-complete" => Some("setup-complete"),
        "sign-out" => Some("sign-out"),
        "updates" => Some("updates"),
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
        "/claim" => Some("claim"),
        "/instances" => Some("instances"),
        "/setup-complete" => Some("setup-complete"),
        "/sign-out" => Some("sign-out"),
        "/updates" => Some("updates"),
        _ => None,
    }
}

/// Presentation destination; ordinary cancellation preserves the exact document.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum RestoreDestination {
    Document,
    Home,
    Local,
}
