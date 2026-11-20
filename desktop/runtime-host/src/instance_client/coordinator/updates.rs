//! Update actions on the committed remote instance.
//!
//! They use the instance sign-in the app made when it opened that instance (a
//! native cookie jar held in memory, never persisted) and the instance's
//! released HTTP contract, exactly as its own page would: `Origin`, the
//! session cookie and the CSRF double-submit. The instance decides; nothing
//! here widens what an account may do there.
//!
//! These never take the account operation lock for their HTTP work: a
//! standalone install keeps its request open for about a minute, and account
//! actions must not wait for it. Admission still checks under that lock that no
//! selection is in progress, and every publication checks the generation and
//! the exact committed session, so a reply that arrives after the selection
//! changed publishes nothing.
use super::*;
use crate::instance_client::instance_update::{self, Action, Phase, Receipt, Status};
use reqwest::header::{HeaderMap, HeaderValue, ORIGIN, SET_COOKIE};
use serde_json::{json, Value};
use url::Url;

/// Required to recognise the jar; the WebView store is consulted only when this
/// one has stopped working, and only for the exact committed origin.
const INSTANCE_COOKIES: [&str; 3] = [
    "__Host-vibe_remote_session",
    "__Host-vibe_oauth_device",
    "vibe_csrf_token",
];

pub(super) struct CommittedUpdate {
    /// The jar and a counter of its replacements: a slow reply never
    /// overwrites a rotation a later reply already stored.
    jar: Mutex<(u64, NativeSession)>,
    reading: tokio::sync::Mutex<()>,
    sending: tokio::sync::Mutex<()>,
}

enum Outcome {
    Ok(Value),
    Unauthorized,
    Refused(String),
}

impl CommittedUpdate {
    pub(super) fn new(session: NativeSession) -> Self {
        let mut session = session;
        session.unfenced();
        Self {
            jar: Mutex::new((0, session)),
            reading: Default::default(),
            sending: Default::default(),
        }
    }
    fn origin(&self) -> Url {
        self.jar.lock().expect("instance jar").1.origin.clone()
    }
    fn adopt(&self, cookies: Vec<cookie::Cookie<'static>>) -> bool {
        let mut jar = self.jar.lock().expect("instance jar");
        let origin = jar.1.origin.clone();
        let cookies: Vec<_> = cookies
            .into_iter()
            .filter(|cookie| INSTANCE_COOKIES.contains(&cookie.name()))
            .collect();
        if !cookies.iter().any(|c| c.name() == INSTANCE_COOKIES[0]) {
            return false;
        }
        let next = jar.0 + 1;
        jar.1.store_cookies(cookies, &origin);
        jar.0 = next;
        true
    }
    async fn http(
        &self,
        epoch: &Epoch,
        method: Method,
        path: &str,
        body: Option<Value>,
    ) -> Result<Outcome, ClientError> {
        let (revision, mut session) = self.jar.lock().expect("instance jar").clone();
        session.fence(epoch.clone());
        let mut headers = HeaderMap::new();
        if method == Method::POST {
            // Origin is also set by the transport for every non-GET request.
            let origin = session.origin.origin().ascii_serialization();
            headers.insert(
                ORIGIN,
                HeaderValue::from_str(&origin).map_err(|_| ClientError::new("invalid_origin"))?,
            );
            let Some(token) = session.cookie_value("vibe_csrf_token") else {
                return Ok(Outcome::Unauthorized);
            };
            let Ok(token) = HeaderValue::from_str(&token) else {
                return Ok(Outcome::Unauthorized);
            };
            headers.insert("x-vibe-csrf-token", token);
        }
        let response = match session.request_with_headers(method, path, body, headers).await {
            Ok(response) => response,
            Err(error) if error.code == "stale_generation" => return Err(error),
            Err(_) => return Err(ClientError::new("instance_unreachable")),
        };
        if response.headers.contains_key(SET_COOKIE) {
            let mut jar = self.jar.lock().expect("instance jar");
            epoch.check()?;
            if jar.0 == revision {
                session.unfenced();
                *jar = (revision + 1, session);
            }
        }
        let body = response.json().unwrap_or(Value::Null);
        if matches!(response.status, 401 | 403) {
            return Ok(Outcome::Unauthorized);
        }
        if !response.success() {
            let code = body.get("error").or_else(|| body.get("code")).and_then(Value::as_str);
            return Ok(Outcome::Refused(instance_update::error_code(code)));
        }
        if body.is_null() {
            return Err(ClientError::new("instance_unreachable"));
        }
        Ok(Outcome::Ok(body))
    }
    /// `/api/session` first: the instance must still say this account, on this
    /// instance, may read it, or nothing it reports is shown.
    async fn read(
        &self,
        epoch: &Epoch,
        sub: &str,
        instance: &str,
    ) -> Result<Result<(Value, bool), Status>, ClientError> {
        let identity = match self.http(epoch, Method::GET, "/api/session", None).await {
            Ok(Outcome::Ok(identity)) => identity,
            Ok(Outcome::Unauthorized) => return Ok(Err(Status::Unauthorized)),
            Ok(Outcome::Refused(_)) => return Ok(Err(Status::Unreachable)),
            Err(error) if error.code == "stale_generation" => return Err(error),
            Err(_) => return Ok(Err(Status::Unreachable)),
        };
        if identity["remote"] == true
            && identity["authenticated"] == true
            && identity["authorization_state"] == "unavailable"
        {
            return Ok(Err(Status::Unreachable));
        }
        if identity["remote"] != true
            || identity["authenticated"] != true
            || identity["authorization_state"] != "current"
            || identity["sub"].as_str() != Some(sub)
            || identity["instance_id"].as_str() != Some(instance)
            || identity["capabilities"]["can_read_instance"] != true
        {
            return Ok(Err(Status::Unauthorized));
        }
        let can_manage = identity["capabilities"]["can_manage_instance"] == true;
        match self.http(epoch, Method::GET, "/api/version", None).await {
            Ok(Outcome::Ok(data)) => Ok(instance_update::snapshot(&data)
                .map(|snapshot| (snapshot, can_manage))
                .map_err(|_| Status::Unreachable)),
            Ok(Outcome::Unauthorized) => Ok(Err(Status::Unauthorized)),
            Ok(Outcome::Refused(_)) => Ok(Err(Status::Unreachable)),
            Err(error) if error.code == "stale_generation" => Err(error),
            Err(_) => Ok(Err(Status::Unreachable)),
        }
    }
}

struct Admitted {
    epoch: Epoch,
    session: Arc<CommittedUpdate>,
    instance_id: String,
    account_id: String,
}

impl Coordinator {
    /// Only the committed instance, with its own session, under the caller's
    /// current generation, and never while a selection is under way.
    fn admit_instance(&self, generation: u64) -> Result<Admitted, ClientError> {
        let epoch = Epoch::new(self.generation.clone(), generation);
        let _admission = self.operation.try_lock().map_err(|_| ClientError::new("busy"))?;
        let inner = self.inner.lock().expect("client state");
        epoch.check()?;
        if generation != inner.state.generation {
            return Err(ClientError::new("stale_generation"));
        }
        if inner.selection.is_some() || inner.state.connection.pending.is_some() {
            return Err(ClientError::new("busy"));
        }
        let ClientTarget::Remote { instance_id, .. } = &inner.state.connection.committed else {
            return Err(ClientError::new("instance_unavailable"));
        };
        let session = inner
            .update_session
            .clone()
            .ok_or_else(|| ClientError::new("instance_unavailable"))?;
        let account_id = match (&inner.state.account.status, &inner.state.account.identity) {
            (AccountStatus::SignedIn, Some(account)) if inner.display_account.as_ref() == Some(&account.id) => {
                account.id.clone()
            }
            _ => return Err(ClientError::new("instance_unavailable")),
        };
        if inner.state.instance_update.as_ref().map(|u| &u.instance_id) != Some(instance_id) {
            return Err(ClientError::new("instance_unavailable"));
        }
        Ok(Admitted {
            epoch,
            session,
            instance_id: instance_id.clone(),
            account_id,
        })
    }

    /// Publishes only for the exact session it was admitted with.
    fn publish_instance(
        &self,
        admitted: &Admitted,
        change: impl FnOnce(&mut InstanceUpdate) -> Result<(), ClientError>,
    ) -> Result<(), ClientError> {
        let state = {
            let mut inner = self.inner.lock().expect("client state");
            admitted.epoch.check()?;
            if !inner
                .update_session
                .as_ref()
                .is_some_and(|session| Arc::ptr_eq(session, &admitted.session))
            {
                return Err(ClientError::new("stale_generation"));
            }
            let update = inner
                .state
                .instance_update
                .as_mut()
                .filter(|update| update.instance_id == admitted.instance_id)
                .ok_or_else(|| ClientError::new("stale_generation"))?;
            let before = update.clone();
            change(update)?;
            if *update == before {
                return Ok(());
            }
            inner.state.revision += 1;
            inner.state.clone()
        };
        self.view.changed(&state);
        Ok(())
    }

    pub(super) async fn instance_action(&self, generation: u64, action: ClientAction) -> Result<(), ClientError> {
        let admitted = self.admit_instance(generation)?;
        match action {
            ClientAction::InstanceUpdateRefresh {} => self.refresh_instance(&admitted).await,
            action => self.send_instance(&admitted, action).await,
        }
    }

    async fn refresh_instance(&self, admitted: &Admitted) -> Result<(), ClientError> {
        let session = &admitted.session;
        // Reads coalesce: a poll and a click do not double the requests.
        let Ok(_reading) = session.reading.try_lock() else {
            return Ok(());
        };
        // The receipt as this read starts: only a read that starts after a
        // reply was published can show its effect.
        let before = self
            .inner
            .lock()
            .expect("client state")
            .state
            .instance_update
            .as_ref()
            .and_then(|update| update.request.clone());
        let mut result = session
            .read(&admitted.epoch, &admitted.account_id, &admitted.instance_id)
            .await?;
        if result == Err(Status::Unauthorized) {
            // The instance page in the WebView keeps the sign-in alive and
            // receives any rotation; this jar sees only its own traffic. Take
            // the WebView's cookies for this exact origin once and check the
            // identity again. Nothing is sent on their strength but reads.
            let origin = session.origin();
            let view = self.view.clone();
            let epoch = admitted.epoch.clone();
            let cookies = tokio::task::spawn_blocking(move || view.instance_cookies(&epoch, &origin))
                .await
                .ok()
                .flatten();
            if cookies.is_some_and(|cookies| session.adopt(cookies)) {
                result = session
                    .read(&admitted.epoch, &admitted.account_id, &admitted.instance_id)
                    .await?;
            }
        }
        self.publish_instance(admitted, |update| {
            // Observed in the same critical section as the receipt: an action
            // publishes `sending` only while it holds this lock.
            let in_flight = session.sending.try_lock().is_err();
            match &result {
                Ok((snapshot, can_manage)) => {
                    update.status = Status::Ready;
                    update.snapshot = Some(snapshot.clone());
                    update.can_manage = *can_manage;
                    update.fetched_at = Some(instance_update::now());
                }
                // An instance that is restarting keeps its last answer on
                // display; one that refuses this account shows nothing.
                Err(Status::Unauthorized) => {
                    update.status = Status::Unauthorized;
                    update.snapshot = None;
                    update.can_manage = false;
                }
                Err(status) => update.status = *status,
            }
            update.reconcile(in_flight);
            update.request_seen = result.is_ok()
                && update.request.is_some()
                && update.request == before
                && before.as_ref().is_some_and(|request| request.phase != Phase::Sending);
            Ok(())
        })
    }

    async fn send_instance(&self, admitted: &Admitted, action: ClientAction) -> Result<(), ClientError> {
        let session = &admitted.session;
        let _sending = session.sending.try_lock().map_err(|_| ClientError::new("busy"))?;
        let (kind, mut path, mut body, target) = match action {
            ClientAction::InstanceUpdateCheck {} => (Action::Check, "/api/update/check", json!({}), None),
            ClientAction::InstanceUpdateSetPrereleases { enabled } => (
                Action::Prereleases,
                "/api/update/prereleases",
                json!({ "enabled": enabled }),
                None,
            ),
            ClientAction::InstanceUpdateSetAutoUpdate { enabled } => (
                Action::AutoUpdate,
                "/api/update/auto-update",
                json!({ "enabled": enabled }),
                None,
            ),
            ClientAction::InstanceUpdateInstall { target_version } => (
                Action::Install,
                "/api/upgrade",
                json!({ "confirm_interrupt": true }),
                Some(target_version),
            ),
            _ => return Err(ClientError::new("invalid_action")),
        };
        let now = instance_update::utc_now();
        self.publish_instance(admitted, |update| {
            if update.status != Status::Ready || !update.can_manage {
                return Err(ClientError::new("instance_unauthorized"));
            }
            let snapshot = update
                .snapshot
                .as_ref()
                .ok_or_else(|| ClientError::new("instance_unavailable"))?;
            let listed = |name: &str| {
                snapshot["host_actions"]
                    .as_array()
                    .is_some_and(|actions| actions.iter().any(|action| action == name))
            };
            let name = match kind {
                Action::Install => "install",
                Action::Check => "check",
                Action::Prereleases => "prereleases",
                Action::AutoUpdate => "auto_update",
            };
            if snapshot.get("host_actions").is_some() && !listed(name) {
                return Err(ClientError::new("requires_host"));
            }
            if let Some(target) = &target {
                // The exact version on screen at the click, or nothing.
                if snapshot["has_update"] != true || snapshot["latest"].as_str() != Some(target.as_str()) {
                    return Err(ClientError::new("target_mismatch"));
                }
                if update.unsettled(now)
                    && update
                        .request
                        .as_ref()
                        .is_some_and(|request| request.action == Action::Install)
                    || !snapshot["install"].is_null()
                {
                    return Err(ClientError::new("update_in_progress"));
                }
                if snapshot["managed_by"] == "desktop" {
                    path = "/api/desktop/update/request";
                    body["target_version"] = json!(target);
                }
            }
            update.request_seen = false;
            update.install_from = snapshot["current"].as_str().map(str::to_owned);
            update.install_ref = None;
            update.request = Some(Receipt {
                action: kind,
                phase: Phase::Sending,
                target: target.clone(),
                error_code: None,
                updated_at: instance_update::now(),
            });
            Ok(())
        })?;
        // Ends when the instance answers, the transport's limit passes, or the
        // generation it was sent under ends; dropping it releases `sending`.
        let request = session.http(&admitted.epoch, Method::POST, path, Some(body));
        tokio::pin!(request);
        let outcome = loop {
            match tokio::time::timeout(std::time::Duration::from_millis(500), &mut request).await {
                Ok(outcome) => break outcome,
                Err(_) => admitted.epoch.check()?,
            }
        };
        let failure = match &outcome {
            Err(error) if error.code == "stale_generation" => return Err(error.clone()),
            Err(error) => Some(error.code.clone()),
            Ok(Outcome::Unauthorized) => Some("instance_unauthorized".to_owned()),
            Ok(Outcome::Refused(code)) => Some(code.clone()),
            // `/api/upgrade` answers 200 with `ok: false` when its installer fails.
            Ok(Outcome::Ok(reply)) if reply["ok"] != true => Some(instance_update::error_code(
                reply.get("code").and_then(Value::as_str).or(match kind {
                    Action::Install => Some("install_failed"),
                    _ => None,
                }),
            )),
            Ok(Outcome::Ok(_)) => None,
        };
        self.publish_instance(admitted, |update| {
            let Some(receipt) = update
                .request
                .as_mut()
                .filter(|receipt| receipt.phase == Phase::Sending)
            else {
                return Ok(());
            };
            receipt.phase = if failure.is_none() {
                Phase::Accepted
            } else {
                Phase::Failed
            };
            receipt.error_code = failure.clone();
            receipt.updated_at = instance_update::now();
            if let Ok(Outcome::Ok(reply)) = &outcome {
                update.install_ref = reply["request_id"].as_str().map(str::to_owned);
            }
            if failure.as_deref() == Some("instance_unauthorized") {
                update.status = Status::Unauthorized;
                update.snapshot = None;
                update.can_manage = false;
            }
            Ok(())
        })?;
        match failure {
            Some(code) => Err(ClientError::new(code)),
            None => Ok(()),
        }
    }

    /// The shell's own reread: while its popup is open, or while something it
    /// sent has not settled. An instance that refuses the app's sign-in is not
    /// reread on a timer; the next popup opening reads it once.
    pub async fn poll_instance_update(&self, popup_open: bool) {
        let wanted = |update: &InstanceUpdate| {
            update.status != Status::Unauthorized && (popup_open || update.unsettled(instance_update::utc_now()))
        };
        self.reread_instance_update(wanted).await;
    }
    /// One read when the popup opens.
    pub async fn refresh_instance_update(&self) {
        self.reread_instance_update(|_| true).await;
    }
    async fn reread_instance_update(&self, wanted: impl FnOnce(&InstanceUpdate) -> bool) {
        let (generation, wanted) = {
            let inner = self.inner.lock().expect("client state");
            (
                inner.state.generation,
                inner.state.instance_update.as_ref().is_some_and(wanted),
            )
        };
        if wanted {
            let _ = self
                .instance_action(generation, ClientAction::InstanceUpdateRefresh {})
                .await;
        }
    }
}
