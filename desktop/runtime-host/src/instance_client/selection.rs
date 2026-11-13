//! Catalog-bound instance OIDC. Queries/codes/cookies never enter public state.
use std::sync::Arc;

use reqwest::{header::LOCATION, Method};
use serde_json::json;
use url::Url;

use super::{
    account::{string, successful_json, AccountFlow, CONTROL_PLANE},
    protocol::*,
    transport::{Epoch, NativeSession, Transport},
};

#[derive(Clone)]
pub struct Selection {
    pub instance: Instance,
    pub session: NativeSession,
    query: String,
    transaction_state: String,
}

impl Selection {
    pub async fn begin(
        account: &mut AccountFlow,
        state: &mut ClientState,
        id: &str,
        transport: Arc<dyn Transport>,
        epoch: Epoch,
    ) -> Result<Self, ClientError> {
        account.discover(state).await?;
        if state.account.status != AccountStatus::SignedIn {
            return Err(ClientError::new("authentication_required"));
        }
        let instance = state
            .catalog
            .instances
            .iter()
            .find(|instance| instance.id == id)
            .cloned()
            .ok_or_else(|| ClientError::new("instance_access_denied"))?;
        let origin = instance_origin(&instance)?;
        let mut session = NativeSession::new(origin, transport);
        session.fence(epoch);
        let response = session.request(Method::GET, "/auth/login?next=/", None).await?;
        if !matches!(response.status, 302 | 303 | 307) {
            return Err(ClientError::new("instance_authorization_unavailable"));
        }
        let location = response
            .headers
            .get(LOCATION)
            .and_then(|header| header.to_str().ok())
            .ok_or_else(|| ClientError::new("invalid_authorization_redirect"))?;
        let url = Url::parse(location).map_err(|_| ClientError::new("invalid_authorization_redirect"))?;
        let control = Url::parse(CONTROL_PLANE).expect("fixed origin");
        if url.origin() != control.origin()
            || url.path() != "/oauth/authorize"
            || url.fragment().is_some()
            || !url.username().is_empty()
            || url.password().is_some()
        {
            return Err(ClientError::new("invalid_authorization_redirect"));
        }
        let transaction_state = one_query(&url, "state")?;
        let callback = Url::parse(&one_query(&url, "redirect_uri")?)
            .map_err(|_| ClientError::new("invalid_authorization_redirect"))?;
        if callback.origin() != session.origin.origin()
            || callback.path() != "/auth/callback"
            || callback.query().is_some()
            || callback.fragment().is_some()
            || !callback.username().is_empty()
            || callback.password().is_some()
        {
            return Err(ClientError::new("invalid_authorization_redirect"));
        }
        state.connection.status = ConnectionStatus::Switching;
        state.connection.pending = Some(Self::target_for(&instance)?);
        Ok(Self {
            instance,
            session,
            query: url
                .query()
                .ok_or_else(|| ClientError::new("invalid_authorization_redirect"))?
                .into(),
            transaction_state,
        })
    }

    pub fn target(&self) -> Result<ClientTarget, ClientError> {
        Self::target_for(&self.instance)
    }
    fn target_for(instance: &Instance) -> Result<ClientTarget, ClientError> {
        Ok(ClientTarget::Remote {
            name: instance.name.clone(),
            instance_id: instance.id.clone(),
            canonical_origin: instance_origin(instance)?.origin().ascii_serialization(),
        })
    }

    /// Returns true only after callback completion and current identity checks.
    pub async fn authorize(
        &mut self,
        account: &mut AccountFlow,
        state: &mut ClientState,
        decision: &str,
    ) -> Result<bool, ClientError> {
        if !matches!(decision, "inspect" | "approve" | "deny") {
            return Err(ClientError::new("invalid_action"));
        }
        let result = successful_json(
            account
                .session
                .request(
                    Method::POST,
                    "/api/desktop/instance-authorization",
                    Some(json!({"authorize_query":self.query,"decision":decision})),
                )
                .await?,
        )?;
        match result["status"].as_str() {
            Some("consent_required") if decision == "inspect" => {
                if result["account"]["id"].as_str()
                    != state.account.identity.as_ref().map(|account| account.id.as_str())
                    || result["instance"]["id"] != self.instance.id
                    || result["instance"]["host"] != self.instance.host
                {
                    return Err(ClientError::new("instance_identity_mismatch"));
                }
                state.consent = Some(ConsentState {
                    instance_id: self.instance.id.clone(),
                    instance_name: self.instance.name.clone(),
                    host: self.instance.host.clone(),
                    scope: string(&result, "scope")?.to_owned(),
                });
                Ok(false)
            }
            Some("denied") if decision == "deny" => Err(ClientError::new("consent_denied")),
            Some("authorized") if decision != "deny" => {
                let callback = Url::parse(string(&result, "redirect_url")?)
                    .map_err(|_| ClientError::new("invalid_authorization_redirect"))?;
                if callback.origin() != self.session.origin.origin()
                    || callback.path() != "/auth/callback"
                    || callback.fragment().is_some()
                    || !callback.username().is_empty()
                    || callback.password().is_some()
                    || one_query(&callback, "state")? != self.transaction_state
                    || one_query(&callback, "code")?.is_empty()
                {
                    return Err(ClientError::new("invalid_authorization_redirect"));
                }
                let response = self.session.request(Method::GET, callback.as_str(), None).await?;
                if !response.success() && !matches!(response.status, 302 | 303) {
                    return Err(response.error());
                }
                if let Some(location) = response.headers.get(LOCATION) {
                    let next = self
                        .session
                        .origin
                        .join(
                            location
                                .to_str()
                                .map_err(|_| ClientError::new("invalid_authorization_redirect"))?,
                        )
                        .map_err(|_| ClientError::new("invalid_authorization_redirect"))?;
                    if next.origin() != self.session.origin.origin()
                        || !next.username().is_empty()
                        || next.password().is_some()
                    {
                        return Err(ClientError::new("invalid_authorization_redirect"));
                    }
                }
                self.readiness(state).await?;
                state.consent = None;
                Ok(true)
            }
            _ => Err(ClientError::new("invalid_response")),
        }
    }

    pub async fn readiness(&mut self, state: &ClientState) -> Result<(), ClientError> {
        let session = successful_json(self.session.request(Method::GET, "/api/session", None).await?)?;
        if session.get("instance_id").is_none() {
            return Err(ClientError::new("instance_upgrade_required"));
        }
        if session["remote"] != true
            || session["authenticated"] != true
            || session["authorization_state"] != "current"
            || session["sub"].as_str() != state.account.identity.as_ref().map(|account| account.id.as_str())
            || session["instance_id"] != self.instance.id
            || session["capabilities"]["can_read_instance"] != true
        {
            return Err(ClientError::new("instance_identity_mismatch"));
        }
        self.session.runtime_cookies()?;
        Ok(())
    }
}

fn one_query(url: &Url, name: &str) -> Result<String, ClientError> {
    let values: Vec<_> = url
        .query_pairs()
        .filter(|(key, _)| key == name)
        .map(|(_, value)| value.into_owned())
        .collect();
    if values.len() != 1 || values[0].is_empty() {
        return Err(ClientError::new("invalid_authorization_redirect"));
    }
    Ok(values.into_iter().next().expect("checked length"))
}
