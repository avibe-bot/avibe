//! Released loopback descriptor. A display hint, never authority to bind or to grant an account claim.
use reqwest::{header::HeaderMap, Method};
use serde::Deserialize;
use serde_json::Value;

use super::{
    account::{string, successful_json},
    protocol::*,
    transport::{NativeSession, Response},
};

// Deserialize only fields that are displayed or validated. In particular, the installation
// identity and the legacy enrollment guard stay in the Runtime even on the compatibility wire.
#[derive(Deserialize)]
struct LocalHint {
    schema_version: u8,
    device_name: String,
    binding_revision: String,
    pairing: Option<PairingHint>,
    remote_access: AccessHint,
}
#[derive(Deserialize)]
struct PairingHint {
    instance_id: String,
    backend_url: String,
    public_url: String,
}
#[derive(Deserialize)]
struct AccessHint {
    requested: bool,
    running: bool,
}

pub struct EnrollmentFlow;
impl EnrollmentFlow {
    pub async fn read(local: &mut NativeSession, state: &mut ClientState) -> Result<(), ClientError> {
        state.local.instance_id = None;
        state.local.remote_access_url = None;
        state.local.runtime_too_old = false;
        let response = local.request(Method::GET, "/api/desktop/local-instance", None).await?;
        if response.status == 404 {
            // A Runtime from before this description (3.2.6rc16) still says which
            // instance it is bound to on its remote-access status, from the same
            // loopback origin: its own word, never a guess from a name or URL.
            state.local.runtime_too_old = true;
            state.local.instance_id = Self::bound_instance(local).await;
            return Err(ClientError::new(crate::instance_client::machine::TOO_OLD));
        }
        if !response.success() {
            return Err(ClientError::new("local_runtime_unavailable"));
        }
        let descriptor: LocalHint =
            serde_json::from_slice(&response.body).map_err(|_| ClientError::new("invalid_local_descriptor"))?;
        if descriptor.schema_version != 1 || descriptor.binding_revision.is_empty() || descriptor.device_name.is_empty()
        {
            return Err(ClientError::new("invalid_local_descriptor"));
        }
        if let Some(pairing) = &descriptor.pairing {
            let url = url::Url::parse(&pairing.public_url).map_err(|_| ClientError::new("invalid_local_descriptor"))?;
            let instance = Instance {
                id: pairing.instance_id.clone(),
                name: descriptor.device_name.clone(),
                host: url.host_str().unwrap_or("").into(),
                url: pairing.public_url.clone(),
                relationship: Relationship::Owner,
                owner_email: None,
                available: true,
            };
            instance_origin(&instance).map_err(|_| ClientError::new("invalid_local_descriptor"))?;
            let backend =
                url::Url::parse(&pairing.backend_url).map_err(|_| ClientError::new("invalid_local_descriptor"))?;
            if backend.as_str() != "https://avibe.bot/" {
                return Err(ClientError::new("invalid_local_descriptor"));
            }
        }
        state.local.name = descriptor.device_name.clone();
        state.local.remote_access = if !descriptor.remote_access.requested {
            RemoteAccessState::Off
        } else if descriptor.remote_access.running {
            RemoteAccessState::On
        } else {
            RemoteAccessState::Starting
        };
        state.local.instance_id = descriptor.pairing.as_ref().map(|p| p.instance_id.clone());
        if state.local.remote_access == RemoteAccessState::On {
            state.local.remote_access_url = descriptor.pairing.as_ref().map(|p| p.public_url.clone());
        }
        if let ClientTarget::Local { name, instance_id } = &mut state.connection.committed {
            *name = descriptor.device_name;
            *instance_id = state.local.instance_id.clone();
        }
        Ok(())
    }
}

impl EnrollmentFlow {
    /// A Runtime too old to describe itself still has its pairing in the
    /// owner's config projection (`remote_access.vibe_cloud`, credentials
    /// removed, unchanged since 3.2.0): its instance ID counts only while that
    /// projection says it is paired. Remote-access status has no ID.
    async fn bound_instance(local: &mut NativeSession) -> Option<String> {
        let response = local.request(Method::GET, "/api/config", None).await.ok()?;
        if !response.success() {
            return None;
        }
        let config: Value = serde_json::from_slice(&response.body).ok()?;
        let cloud = config.get("remote_access")?.get("vibe_cloud")?;
        if cloud.get("paired")?.as_bool() != Some(true) {
            return None;
        }
        let id = cloud.get("instance_id")?.as_str()?.trim();
        (!id.is_empty() && id.len() <= 120 && id.bytes().all(|b| b.is_ascii_graphic())).then(|| id.to_owned())
    }
}

pub async fn local_mutation(local: &mut NativeSession, path: &str, body: Value) -> Result<Response, ClientError> {
    let token = successful_json(local.request(Method::GET, "/api/csrf-token", None).await?)?;
    let token = string(&token, "csrf_token")?;
    if local.cookie_value("vibe_csrf_token").as_deref() != Some(token) {
        return Err(ClientError::new("invalid_csrf_response"));
    }
    let mut headers = HeaderMap::new();
    headers.insert(
        "X-Vibe-CSRF-Token",
        token.parse().map_err(|_| ClientError::new("invalid_csrf_response"))?,
    );
    local
        .request_with_headers(Method::POST, path, Some(body), headers)
        .await
}
