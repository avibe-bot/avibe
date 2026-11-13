//! Local registration uses the Runtime's existing private recovery owner.
use std::time::{Duration, Instant};

use reqwest::{header::HeaderMap, Method};
use serde::Deserialize;
use serde_json::{json, Value};

use super::{
    account::{string, successful_json, AccountFlow},
    protocol::*,
    transport::{NativeSession, Response},
};

#[derive(Clone, Deserialize)]
pub struct LocalInstance {
    pub schema_version: u8,
    pub installation_id: Option<String>,
    pub device_name: String,
    pub binding_revision: String,
    pub pairing: Option<LocalPairing>,
    pub remote_access: LocalAccess,
    pub enrollment: Option<LocalEnrollment>,
}
#[derive(Clone, Deserialize)]
pub struct LocalPairing {
    pub instance_id: String,
    pub backend_url: String,
    pub public_url: String,
}
#[derive(Clone, Deserialize)]
pub struct LocalAccess {
    pub configured_enabled: bool,
    pub requested: bool,
    pub running: bool,
}
#[derive(Clone, Deserialize)]
pub struct LocalEnrollment {
    pub operation_id: String,
    pub account_id: String,
    pub state: String,
}

#[derive(Clone, Default)]
pub struct EnrollmentFlow {
    retry_at: Option<Instant>,
}
impl EnrollmentFlow {
    pub async fn read(local: &mut NativeSession, state: &mut ClientState) -> Result<LocalInstance, ClientError> {
        let response = local.request(Method::GET, "/api/desktop/local-instance", None).await?;
        if response.status == 404 {
            return Err(ClientError::new("local_runtime_upgrade_required"));
        }
        let value = successful_json(response)?;
        let descriptor: LocalInstance =
            serde_json::from_value(value).map_err(|_| ClientError::new("invalid_local_descriptor"))?;
        if descriptor.schema_version != 1 || descriptor.binding_revision.is_empty() || descriptor.device_name.is_empty()
        {
            return Err(ClientError::new("invalid_local_descriptor"));
        }
        state.local.name = descriptor.device_name.clone();
        state.local.remote_access = if !descriptor.remote_access.requested {
            RemoteAccessState::Off
        } else if descriptor.remote_access.running {
            RemoteAccessState::On
        } else {
            RemoteAccessState::Starting
        };
        if let ClientTarget::Local { name, instance_id } = &mut state.connection.committed {
            *name = descriptor.device_name.clone();
            *instance_id = descriptor.pairing.as_ref().map(|p| p.instance_id.clone());
        }
        Ok(descriptor)
    }

    pub async fn register(
        &mut self,
        account: &mut AccountFlow,
        local: &mut NativeSession,
        state: &mut ClientState,
    ) -> Result<(), ClientError> {
        account.discover(state).await?;
        if state.account.status != AccountStatus::SignedIn {
            return Err(ClientError::new("authentication_required"));
        }
        if let Some(remaining) = self
            .retry_at
            .and_then(|deadline| deadline.checked_duration_since(Instant::now()))
        {
            return Err(ClientError {
                code: "registration_pending".into(),
                retry_after_seconds: Some(remaining.as_secs().clamp(1, 60) as u8),
            });
        }
        let account_id = state
            .account
            .identity
            .as_ref()
            .ok_or_else(|| ClientError::new("authentication_required"))?
            .id
            .clone();
        let mut descriptor = Self::read(local, state).await?;
        let prepared = successful_json(
            local_mutation(
                local,
                "/api/desktop/local-instance/prepare",
                json!({"account_id":account_id,"expected_binding_revision":descriptor.binding_revision}),
            )
            .await?,
        )?;
        descriptor = serde_json::from_value(prepared["local"].clone())
            .map_err(|_| ClientError::new("invalid_local_descriptor"))?;
        if descriptor.pairing.is_none() {
            let enrollment = descriptor
                .enrollment
                .as_ref()
                .ok_or_else(|| ClientError::new("invalid_local_descriptor"))?;
            if enrollment.account_id != account_id
                || Some(enrollment.operation_id.as_str()) != prepared["operation_id"].as_str()
            {
                return Err(ClientError::new("local_instance_conflict"));
            }
            // Runtime owns persisted request recovery, including after restart.
            if matches!(enrollment.state.as_str(), "redeeming" | "reconciling" | "failed") {
                let response = local_mutation(
                    local,
                    "/api/remote-access/vibe-cloud/pair",
                    json!({"pairing_key":"","backend_url":"https://avibe.bot"}),
                )
                .await?;
                if response.success() {
                    descriptor = Self::read(local, state).await?;
                } else if !(response.status == 400
                    && response.json()?["error"] == "pairing_key_expired"
                    && response.json()?["status"] == 400)
                {
                    return self.pair_error(response);
                }
            }
        }
        state.enrollment.status = EnrollmentStatus::Registering;
        state.enrollment.error_code = None;
        let response = account
            .session
            .request(
                Method::POST,
                "/api/desktop/instances/ensure-local",
                Some(json!({
                    "installation_id":descriptor.installation_id,
                    "operation_id":prepared["operation_id"],
                    "device_name":descriptor.device_name,
                    "local_instance_id":descriptor.pairing.as_ref().map(|pairing|&pairing.instance_id),
                })),
            )
            .await?;
        if response.status == 202 {
            let body = response.json()?;
            if body["outcome"] != "pending" || body["operation_id"] != prepared["operation_id"] {
                return Err(ClientError::new("invalid_response"));
            }
            return Err(self.pending("registration_pending", &body["retry_after_seconds"]));
        }
        let result = successful_json(response)?;
        let expected = match result["outcome"].as_str() {
            Some("already_paired") if result["owner_match"] == true => {
                let id = string(&result, "instance_id")?;
                if descriptor
                    .pairing
                    .as_ref()
                    .is_none_or(|pairing| pairing.instance_id != id)
                {
                    return Err(ClientError::new("local_binding_changed"));
                }
                id.to_owned()
            }
            Some("pairing_required") if descriptor.pairing.is_none() => {
                if result["operation_id"] != prepared["operation_id"] {
                    return Err(ClientError::new("local_instance_conflict"));
                }
                let expected = string(&result["instance"], "id")?.to_owned();
                let response = local_mutation(
                    local,
                    "/api/remote-access/vibe-cloud/pair",
                    json!({
                        "pairing_key":string(&result["pairing"],"pairing_key")?,
                        "backend_url":"https://avibe.bot", "device_name":descriptor.device_name,
                        "desktop_enrollment":{
                            "installation_id":descriptor.installation_id,"operation_id":prepared["operation_id"],
                            "account_id":account_id,"expected_binding_revision":descriptor.binding_revision,
                        },
                    }),
                )
                .await?;
                if !response.success() {
                    return self.pair_error(response);
                }
                if response.json()?["pairing"]["ok"] != true {
                    return Err(ClientError::new("invalid_response"));
                }
                expected
            }
            _ => return Err(ClientError::new("invalid_response")),
        };
        let applied = Self::read(local, state).await?;
        if applied
            .pairing
            .as_ref()
            .is_none_or(|pairing| pairing.instance_id != expected)
        {
            return Err(ClientError::new("local_binding_changed"));
        }
        state.enrollment.instance_id = Some(expected);
        state.enrollment.status = if applied.remote_access.requested && !applied.remote_access.running {
            state.local.remote_access = RemoteAccessState::Unavailable;
            EnrollmentStatus::PairedTunnelUnavailable
        } else {
            EnrollmentStatus::PairedThisAccount
        };
        self.retry_at = None;
        Ok(())
    }

    fn pending(&mut self, code: &str, delay: &Value) -> ClientError {
        let error = ClientError::pending(code, delay);
        if let Some(delay) = error.retry_after_seconds {
            self.retry_at = Some(Instant::now() + Duration::from_secs(delay.into()));
        }
        error
    }
    fn pair_error(&mut self, response: Response) -> Result<(), ClientError> {
        let body = response.json()?;
        if response.status == 400
            && body["error"] == "pairing_redeem_pending"
            && body["status"] == 409
            && body["pairing"]["recoverable"] == true
            && body["pairing"]["cause"] == "pairing_redeem_pending"
        {
            return Err(self.pending("pairing_redeem_pending", &body["retry_after_seconds"]));
        }
        Err(response.error())
    }
    pub async fn retry_access(local: &mut NativeSession, state: &mut ClientState) -> Result<(), ClientError> {
        if state.enrollment.status != EnrollmentStatus::PairedTunnelUnavailable {
            return Err(ClientError::new("invalid_action"));
        }
        successful_json(local_mutation(local, "/api/remote-access/start", json!({})).await?)?;
        let descriptor = Self::read(local, state).await?;
        if descriptor.pairing.as_ref().map(|p| &p.instance_id) != state.enrollment.instance_id.as_ref() {
            return Err(ClientError::new("local_binding_changed"));
        }
        if !descriptor.remote_access.running {
            return Err(ClientError::new("tunnel_unavailable"));
        }
        state.enrollment.status = EnrollmentStatus::PairedThisAccount;
        state.enrollment.error_code = None;
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
