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
        let response = local.request(Method::GET, "/api/desktop/local-instance", None).await?;
        if response.status == 404 {
            return Err(ClientError::new("local_runtime_upgrade_required"));
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
