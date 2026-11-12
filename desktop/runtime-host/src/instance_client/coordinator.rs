//! One owner for account state, credential writes and selection generations.
use std::{
    collections::HashSet,
    sync::{
        atomic::{AtomicBool, AtomicU64, Ordering},
        Arc, Mutex,
    },
};

use async_trait::async_trait;
use reqwest::Method;

use super::{
    account::{AccountFlow, Passkeys, CONTROL_PLANE},
    enrollment::EnrollmentFlow,
    protocol::*,
    selection::Selection,
    storage::{ConnectionRecord, ConnectionStore, Credentials},
    transport::{Epoch, NativeSession, Transport},
};
use crate::LoopbackOrigin;

#[async_trait]
pub trait ClientView: Send + Sync {
    fn changed(&self, state: &ClientState);
    /// Immediately invalidate all pending window/cookie/download callbacks.
    fn invalidate(&self, generation: u64);
    /// Local lifecycle invalidation may interrupt a physical store replacement.
    /// Restore that staged presentation without displacing a committed remote.
    fn local_rebound(&self, _epoch: Epoch) {}
    /// A staged view now matches the published committed identity.
    fn committed(&self, _epoch: Epoch) {}
    /// Stage a fresh disposable store; check the epoch before every UI callback.
    async fn commit(
        &self,
        epoch: Epoch,
        target: ClientTarget,
        cookies: Vec<cookie::Cookie<'static>>,
    ) -> Result<(), ClientError>;
    /// Cancel presentation and restore the last committed route, without auth.
    async fn restore(&self, epoch: Epoch, target: ClientTarget) -> Result<(), ClientError>;
    /// Erase and destroy every Desktop-owned instance store, including offline.
    async fn signed_out(&self, epoch: Epoch, local: ClientTarget) -> Result<(), ClientError>;
}

#[derive(Clone)]
struct Inner {
    state: ClientState,
    account: AccountFlow,
    local: Option<NativeSession>,
    enrollment: EnrollmentFlow,
    selection: Option<Selection>,
    record: ConnectionRecord,
}

pub struct Coordinator {
    inner: Mutex<Inner>,
    generation: Arc<AtomicU64>,
    operation: tokio::sync::Mutex<()>,
    credential_io: tokio::sync::Mutex<HashSet<String>>,
    signing_out: AtomicBool,
    transport: Arc<dyn Transport>,
    credentials: Arc<dyn Credentials>,
    store: ConnectionStore,
    passkeys: Arc<dyn Passkeys>,
    view: Arc<dyn ClientView>,
}

impl Coordinator {
    pub fn new(
        state: ClientState,
        record: ConnectionRecord,
        store: ConnectionStore,
        transport: Arc<dyn Transport>,
        credentials: Arc<dyn Credentials>,
        passkeys: Arc<dyn Passkeys>,
        view: Arc<dyn ClientView>,
    ) -> Self {
        let generation = Arc::new(AtomicU64::new(state.generation));
        let credential_ids = record.account_id.iter().cloned().collect();
        Self {
            inner: Mutex::new(Inner {
                state,
                account: AccountFlow::new(transport.clone()),
                local: None,
                enrollment: EnrollmentFlow::default(),
                selection: None,
                record,
            }),
            generation,
            operation: tokio::sync::Mutex::new(()),
            credential_io: tokio::sync::Mutex::new(credential_ids),
            signing_out: AtomicBool::new(false),
            transport,
            credentials,
            store,
            passkeys,
            view,
        }
    }
    pub fn state(&self) -> ClientState {
        self.inner.lock().expect("client state").state.clone()
    }
    pub fn epoch(&self) -> Epoch {
        Epoch::new(self.generation.clone(), self.generation.load(Ordering::SeqCst))
    }

    /// Local lifecycle supplies only its validated loopback origin. Rebinding
    /// cannot overwrite a committed remote target or reuse the old local jar.
    pub fn local_origin(&self, origin: Option<LoopbackOrigin>) {
        let mut inner = self.inner.lock().expect("client state");
        let url = origin.map(|origin| origin.navigation_url());
        if inner.local.as_ref().map(|local| &local.origin) == url.as_ref() {
            return;
        }
        let value = self.generation.fetch_add(1, Ordering::SeqCst) + 1;
        inner.state.generation = value;
        inner.selection = None;
        inner.state.consent = None;
        inner.state.connection.pending = None;
        inner.state.connection.status = committed_status(&inner.state);
        inner.local = url.map(|url| NativeSession::new(url, self.transport.clone()));
        if inner.local.is_none() {
            inner.state.local.remote_access = RemoteAccessState::Unavailable;
        }
        inner.state.revision += 1;
        let state = inner.state.clone();
        drop(inner);
        self.view.invalidate(value);
        self.view.changed(&state);
        self.view.local_rebound(Epoch::new(self.generation.clone(), value));
    }
    pub async fn refresh_local(&self) {
        let mut working = self.inner.lock().expect("client state").clone();
        let epoch = Epoch::new(self.generation.clone(), working.state.generation);
        let Some(local) = &mut working.local else {
            return;
        };
        local.fence(epoch.clone());
        let result = EnrollmentFlow::read(local, &mut working.state).await;
        let state = {
            let mut current = self.inner.lock().expect("client state");
            if epoch.check().is_err() {
                return;
            }
            match result {
                Ok(_) => {
                    current.state.local = working.state.local;
                    if current.state.connection.committed.is_local() {
                        current.state.connection.committed = working.state.connection.committed;
                    }
                }
                Err(_) => current.state.local.remote_access = RemoteAccessState::Unavailable,
            }
            current.state.revision += 1;
            current.state.clone()
        };
        self.view.changed(&state);
    }
    pub fn desktop_update(&self, update: serde_json::Value) {
        let mut inner = self.inner.lock().expect("client state");
        inner.state.desktop.update = update;
        inner.state.revision += 1;
        let state = inner.state.clone();
        drop(inner);
        self.view.changed(&state);
    }
    fn advance(&self, expected: Option<u64>) -> Result<Epoch, ClientError> {
        let mut inner = self.inner.lock().expect("client state");
        if expected.is_some_and(|value| value != inner.state.generation) {
            return Err(ClientError::new("stale_generation"));
        }
        let value = self.generation.fetch_add(1, Ordering::SeqCst) + 1;
        inner.state.generation = value;
        inner.state.revision += 1;
        drop(inner);
        self.view.invalidate(value);
        Ok(Epoch::new(self.generation.clone(), value))
    }
    fn reply(&self, id: String, result: Result<Option<DirectResult>, ClientError>) -> ClientReply {
        let (direct, error) = match result {
            Ok(direct) => (direct, None),
            Err(error) => (None, Some(error)),
        };
        ClientReply {
            version: 1,
            request_id: id,
            ok: error.is_none(),
            state: self.state(),
            result: direct,
            error,
        }
    }

    pub async fn request(&self, request: ClientRequest) -> ClientReply {
        if request.version != 1 || request.request_id.is_empty() || request.request_id.len() > 128 {
            return self.reply(request.request_id, Err(ClientError::new("invalid_request")));
        }
        if matches!(request.action, ClientAction::GetState) {
            return self.reply(request.request_id, Ok(None));
        }
        // Escape always fences old work. It can never restore old credentials.
        if matches!(
            request.action,
            ClientAction::Cancel | ClientAction::ReturnLocal | ClientAction::SignOut
        ) {
            let expected = matches!(request.action, ClientAction::SignOut).then_some(request.expected_generation);
            let result = self.escape(request.action, expected).await.map(|_| None);
            return self.reply(request.request_id, result);
        }
        if self.signing_out.load(Ordering::SeqCst) {
            return self.reply(request.request_id, Err(ClientError::new("busy")));
        }
        let Ok(_operation) = self.operation.try_lock() else {
            return self.reply(request.request_id, Err(ClientError::new("busy")));
        };
        if request.expected_generation != self.generation.load(Ordering::SeqCst) {
            return self.reply(request.request_id, Err(ClientError::new("stale_generation")));
        }
        let epoch = if matches!(
            request.action,
            ClientAction::RequestOtp { .. } | ClientAction::SelectInstance { .. }
        ) {
            match self.advance(Some(request.expected_generation)) {
                Ok(epoch) => epoch,
                Err(error) => return self.reply(request.request_id, Err(error)),
            }
        } else {
            Epoch::new(self.generation.clone(), request.expected_generation)
        };
        let mut working = self.inner.lock().expect("client state").clone();
        working.account.fence(epoch.clone());
        if let Some(local) = &mut working.local {
            local.fence(epoch.clone());
        }
        if let Some(selection) = &mut working.selection {
            selection.session.fence(epoch.clone());
        }
        let result = self.execute(&mut working, request.action, &epoch).await;
        if epoch.check().is_err() {
            return self.reply(request.request_id, Err(ClientError::new("stale_generation")));
        }
        if result
            .as_ref()
            .err()
            .is_some_and(|error| matches!(error.code.as_str(), "signed_out" | "account_changed"))
        {
            let _ = self.escape(ClientAction::SignOut, None).await;
            return self.reply(request.request_id, result);
        }
        if result.is_err() {
            working.selection = None;
            working.state.consent = None;
            working.state.connection.pending = None;
            working.state.connection.status = committed_status(&working.state);
        }
        let persistence = self.persist(&mut working, &epoch).await;
        if epoch.check().is_err() {
            return self.reply(request.request_id, Err(ClientError::new("stale_generation")));
        }
        self.view.committed(epoch);
        self.view.changed(&self.state());
        self.reply(
            request.request_id,
            match (result, persistence) {
                // One-time recovery codes must survive an unrelated storage failure.
                (Ok(Some(direct)), _) => Ok(Some(direct)),
                (result, Ok(())) => result,
                (_, Err(error)) => Err(error),
            },
        )
    }

    async fn execute(
        &self,
        working: &mut Inner,
        action: ClientAction,
        epoch: &Epoch,
    ) -> Result<Option<DirectResult>, ClientError> {
        let mut direct = None;
        let mut authenticated = false;
        match action {
            ClientAction::RequestOtp { email } => {
                working
                    .account
                    .request_otp(email, epoch.clone(), &mut working.state)
                    .await?
            }
            ClientAction::VerifyOtp { code } => {
                working.account.verify_otp(code, &mut working.state).await?;
                authenticated = true;
            }
            ClientAction::VerifySecondFactor { method, code } => {
                working.account.verify_factor(method, code, &mut working.state).await?;
                authenticated = true;
            }
            ClientAction::VerifyPasskey => {
                working
                    .account
                    .verify_passkey(self.passkeys.as_ref(), epoch.clone(), &mut working.state)
                    .await?;
                authenticated = true;
            }
            ClientAction::BeginTotpEnrollment => direct = Some(working.account.begin_totp(&working.state).await?),
            ClientAction::FinishTotpEnrollment { code } => {
                direct = Some(working.account.finish_totp(code, &mut working.state).await?);
                authenticated = true;
            }
            ClientAction::RefreshInstances => {
                if working.state.account.status == AccountStatus::SignedOut {
                    return Err(ClientError::new("authentication_required"));
                }
                working.account.discover(&mut working.state).await?;
            }
            ClientAction::RetryEnrollment => Self::enroll(working).await?,
            ClientAction::RetryLocalRemoteAccess => {
                working.account.discover(&mut working.state).await?;
                if working.state.account.status != AccountStatus::SignedIn {
                    return Err(ClientError::new("authentication_required"));
                }
                EnrollmentFlow::retry_access(
                    working
                        .local
                        .as_mut()
                        .ok_or_else(|| ClientError::new("local_runtime_unavailable"))?,
                    &mut working.state,
                )
                .await?;
            }
            ClientAction::SelectInstance { instance_id } => {
                if working.state.account.status != AccountStatus::SignedIn {
                    return Err(ClientError::new("authentication_required"));
                }
                working.selection = Some(
                    Selection::begin(
                        &mut working.account,
                        &mut working.state,
                        &instance_id,
                        self.transport.clone(),
                        epoch.clone(),
                    )
                    .await?,
                );
                self.authorize(working, epoch, "inspect").await?;
            }
            ClientAction::DecideConsent { approve } => {
                if working.state.consent.is_none() {
                    return Err(ClientError::new("invalid_action"));
                }
                self.authorize(working, epoch, if approve { "approve" } else { "deny" })
                    .await?;
            }
            _ => return Err(ClientError::new("invalid_action")),
        }
        if authenticated && working.state.account.status == AccountStatus::SignedIn {
            // Registration failure must not block discovery or local use.
            if Self::enroll(working).await.is_ok() {
                working.account.discover(&mut working.state).await?;
            }
        }
        epoch.check()?;
        Ok(direct)
    }
    async fn enroll(working: &mut Inner) -> Result<(), ClientError> {
        let result = match &mut working.local {
            Some(local) => {
                working
                    .enrollment
                    .register(&mut working.account, local, &mut working.state)
                    .await
            }
            None => Err(ClientError::new("local_runtime_unavailable")),
        };
        if let Err(error) = &result {
            working.state.enrollment.status =
                if matches!(error.code.as_str(), "local_instance_conflict" | "local_binding_changed") {
                    EnrollmentStatus::Conflict
                } else {
                    EnrollmentStatus::Failed
                };
            working.state.enrollment.error_code = Some(error.code.clone());
        }
        result
    }
    async fn authorize(&self, working: &mut Inner, epoch: &Epoch, decision: &str) -> Result<(), ClientError> {
        let selection = working
            .selection
            .as_mut()
            .ok_or_else(|| ClientError::new("invalid_action"))?;
        let ready = selection
            .authorize(&mut working.account, &mut working.state, decision)
            .await?;
        if ready {
            let target = selection.target()?;
            self.view
                .commit(epoch.clone(), target.clone(), selection.session.runtime_cookies()?)
                .await?;
            epoch.check()?;
            working.state.connection = ConnectionState {
                status: ConnectionStatus::RemoteConnected,
                committed: target,
                pending: None,
            };
            working.state.remote_auxiliary = RemoteAuxiliary::DisabledIncomplete;
            working.selection = None;
        }
        Ok(())
    }

    async fn persist(&self, working: &mut Inner, epoch: &Epoch) -> Result<(), ClientError> {
        let mut owned = self.credential_io.lock().await;
        epoch.check()?;
        if working.state.account.status == AccountStatus::SignedIn {
            let account = working
                .state
                .account
                .identity
                .as_ref()
                .ok_or_else(|| ClientError::new("authentication_required"))?;
            working.record.account_id = Some(account.id.clone());
        }
        working.record.last_committed = working.state.connection.committed.clone();
        if let Some(id) = &working.record.account_id {
            // Own the key before the blocking write starts. Sign-out waits for
            // this lock and erases even a write whose public commit was fenced.
            owned.insert(id.clone());
        }
        let bytes = working.account.session.persist()?;
        let record = working.record.clone();
        let credentials = self.credentials.clone();
        let store = self.store.clone();
        let checked = epoch.clone();
        let (saved, previous) = tokio::task::spawn_blocking(move || {
            let previous = match &record.account_id {
                Some(id) => match credentials.read(id) {
                    Ok(previous) => Some(previous),
                    Err(error) => return (Err(error), None),
                },
                None => None,
            };
            let result = (|| {
                checked.check()?;
                if let Some(id) = &record.account_id {
                    credentials.write(id, &bytes)?;
                }
                checked.check()?;
                store.save(&record)
            })();
            (result, previous)
        })
        .await
        .map_err(|_| ClientError::new("credentials_unavailable"))?;
        let rollback_record = {
            let mut current = self.inner.lock().expect("client state");
            if epoch.check().is_err() {
                Some(current.record.clone())
            } else {
                if let Err(error) = &saved {
                    working.state.enrollment.error_code = Some(error.code.clone());
                }
                // Lifecycle facts may change during the network/storage wait.
                working.local = current.local.as_ref().map(|local| {
                    if working.local.as_ref().is_some_and(|old| old.origin == local.origin) {
                        working.local.clone().expect("matched local")
                    } else {
                        local.clone()
                    }
                });
                working.state.desktop = current.state.desktop.clone();
                working.state.revision = current.state.revision + 1;
                *current = working.clone();
                None
            }
        };
        if let Some(record) = rollback_record {
            // Keep credential_io through public commit or rollback. Escape
            // waits on this lock; cancellation cannot leave an unpublished
            // first credential or overwrite an existing committed jar.
            let id = working.record.account_id.clone();
            let credentials = self.credentials.clone();
            let store = self.store.clone();
            tokio::task::spawn_blocking(move || {
                if let (Some(id), Some(previous)) = (id, previous) {
                    match previous {
                        Some(bytes) => credentials.write(&id, &bytes)?,
                        None => credentials.erase(&id)?,
                    }
                }
                store.save(&record)
            })
            .await
            .map_err(|_| ClientError::new("credentials_unavailable"))??;
            return Err(ClientError::new("stale_generation"));
        }
        saved
    }

    async fn escape(&self, action: ClientAction, expected: Option<u64>) -> Result<(), ClientError> {
        let signing_out = matches!(action, ClientAction::SignOut);
        if signing_out && self.signing_out.swap(true, Ordering::SeqCst) {
            return Err(ClientError::new("busy"));
        }
        let _signout_guard = SignoutGuard {
            flag: &self.signing_out,
            active: signing_out,
        };
        let epoch = self.advance(expected)?;
        let (target, record, mut old_session) = {
            let mut inner = self.inner.lock().expect("client state");
            inner.selection = None;
            inner.state.consent = None;
            inner.state.connection.pending = None;
            inner.account.clear_transient();
            let old_session = inner.account.session.clone();
            if signing_out {
                inner.account = AccountFlow::new(self.transport.clone());
                inner.state.account = AccountState {
                    status: AccountStatus::SignedOut,
                    identity: None,
                    email: None,
                    gate: None,
                    methods: vec![],
                };
                inner.state.catalog = CatalogState {
                    status: CatalogStatus::Idle,
                    instances: vec![],
                };
                inner.record.account_id = None;
            } else if inner.state.account.status != AccountStatus::SignedIn {
                inner.account = AccountFlow::new(self.transport.clone());
                inner.state.account = AccountState {
                    status: AccountStatus::SignedOut,
                    identity: None,
                    email: None,
                    gate: None,
                    methods: vec![],
                };
            }
            if signing_out || matches!(action, ClientAction::ReturnLocal) {
                inner.state.connection.committed = ClientTarget::Local {
                    name: inner.state.local.name.clone(),
                    instance_id: inner.state.enrollment.instance_id.clone(),
                };
            }
            let target = inner.state.connection.committed.clone();
            inner.state.connection.status = if target.is_local() {
                ConnectionStatus::Local
            } else {
                ConnectionStatus::RemoteConnected
            };
            inner.state.remote_auxiliary = if target.is_local() {
                RemoteAuxiliary::Local
            } else {
                RemoteAuxiliary::DisabledIncomplete
            };
            inner.record.last_committed = target.clone();
            (target, inner.record.clone(), old_session)
        };
        self.view.changed(&self.state());
        let mut owned = self.credential_io.lock().await;
        let ids: Vec<_> = if signing_out {
            owned.iter().cloned().collect()
        } else {
            vec![]
        };
        let credentials = self.credentials.clone();
        let store = self.store.clone();
        let write_epoch = epoch.clone();
        let cleanup = tokio::task::spawn_blocking(move || {
            // Clearing restoration metadata comes first: an erasure error must
            // never allow restart to revive a signed-out credential.
            // A later local escape may supersede selection but never cancel
            // erasure. New sign-in is blocked until this cleanup finishes.
            let saved = if signing_out || write_epoch.check().is_ok() {
                store.save(&record)
            } else {
                Ok(())
            };
            let erased = if signing_out {
                let mut result = Ok(());
                for id in ids {
                    if let Err(error) = credentials.erase(&id) {
                        result = Err(error);
                    }
                }
                result
            } else {
                Ok(())
            };
            saved.and(erased)
        })
        .await
        .map_err(|_| ClientError::new("credentials_unavailable"))?;
        if signing_out && cleanup.is_ok() {
            owned.clear();
        }
        drop(owned);
        let view_result = if signing_out {
            self.view.signed_out(epoch.clone(), target).await
        } else {
            epoch.check()?;
            self.view.restore(epoch.clone(), target).await
        };
        if signing_out {
            // Device-local best effort revocation never delays local erasure.
            old_session.unfenced();
            tokio::spawn(async move {
                let _ = old_session.request(Method::DELETE, "/api/mobile/session", None).await;
            });
        }
        view_result.and(cleanup)
    }

    /// Called once at native initialization; cached URLs never authorize restore.
    pub async fn restore_account(&self) -> Result<(), ClientError> {
        let result = self.restore_saved_account().await;
        if let Err(error) = &result {
            if error.code != "stale_generation" {
                let state = {
                    let mut inner = self.inner.lock().expect("client state");
                    inner.state.enrollment.error_code = Some(error.code.clone());
                    inner.state.revision += 1;
                    inner.state.clone()
                };
                self.view.changed(&state);
            }
        }
        result
    }

    async fn restore_saved_account(&self) -> Result<(), ClientError> {
        let _operation = self.operation.lock().await;
        let epoch = self.advance(None)?;
        let mut working = self.inner.lock().expect("client state").clone();
        let Some(id) = working.record.account_id.clone() else {
            return Ok(());
        };
        let credentials = self.credentials.clone();
        let credential_id = id.clone();
        let bytes = tokio::task::spawn_blocking(move || credentials.read(&credential_id))
            .await
            .map_err(|_| ClientError::new("credentials_unavailable"))??
            .ok_or_else(|| ClientError::new("credentials_unavailable"))?;
        epoch.check()?;
        working.account.session = NativeSession::restore(
            CONTROL_PLANE.parse().expect("fixed origin"),
            self.transport.clone(),
            &bytes,
        )?;
        working.account.fence(epoch.clone());
        if let Err(error) = working.account.discover(&mut working.state).await {
            if matches!(error.code.as_str(), "signed_out" | "account_changed") {
                let _ = self.escape(ClientAction::SignOut, None).await;
            }
            return Err(error);
        }
        if working.state.account.status == AccountStatus::SignedIn
            && working
                .state
                .account
                .identity
                .as_ref()
                .is_none_or(|account| account.id != id)
        {
            let _ = self.escape(ClientAction::SignOut, None).await;
            return Err(ClientError::new("account_changed"));
        }
        let restore = working.record.last_committed.clone();
        self.persist(&mut working, &epoch).await?;
        self.view.changed(&self.state());
        drop(_operation);
        if let ClientTarget::Remote {
            instance_id,
            canonical_origin,
            ..
        } = restore
        {
            let state = self.state();
            if state.account.status == AccountStatus::SignedIn
                && state.catalog.instances.iter().any(|instance| {
                    instance.id == instance_id
                        && instance_origin(instance)
                            .is_ok_and(|url| url.origin().ascii_serialization() == canonical_origin)
                })
            {
                let reply = self
                    .request(ClientRequest {
                        version: 1,
                        request_id: "native-restore".into(),
                        expected_generation: state.generation,
                        action: ClientAction::SelectInstance { instance_id },
                    })
                    .await;
                if let Some(error) = reply.error {
                    return Err(error);
                }
            }
        }
        Ok(())
    }
}

struct SignoutGuard<'a> {
    flag: &'a AtomicBool,
    active: bool,
}
impl Drop for SignoutGuard<'_> {
    fn drop(&mut self) {
        if self.active {
            self.flag.store(false, Ordering::SeqCst);
        }
    }
}

fn committed_status(state: &ClientState) -> ConnectionStatus {
    if state.connection.committed.is_local() {
        ConnectionStatus::Local
    } else {
        ConnectionStatus::RemoteConnected
    }
}
