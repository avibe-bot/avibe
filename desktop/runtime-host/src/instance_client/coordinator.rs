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
    account::{AccountFlow, Passkeys, ProviderAttempt, SystemBrowser, CONTROL_PLANE},
    enrollment::EnrollmentFlow,
    loopback::CallbackCopy,
    protocol::*,
    selection::Selection,
    storage::{ConnectionRecord, ConnectionStore, Credentials, InstanceRecency},
    transport::{Epoch, NativeSession, Transport},
};
mod updates;
use super::instance_update::InstanceUpdate;
use crate::LoopbackOrigin;
use updates::CommittedUpdate;

#[async_trait]
pub trait ClientView: Send + Sync {
    fn changed(&self, state: &ClientState);
    /// Only the native shell implements local updater intents. They never use
    /// the selected Runtime's session, cookies or account persistence.
    fn desktop_update(&self, _action: ClientAction, _epoch: Epoch) -> Result<(), ClientError> {
        Err(ClientError::new("invalid_action"))
    }
    /// The committed instance's cookies as its WebView holds them, for exactly
    /// `origin`. Read only to recover the shell's own instance requests after
    /// the instance rotated or renewed its sign-in through the page.
    fn instance_cookies(&self, _epoch: &Epoch, _origin: &url::Url) -> Option<Vec<cookie::Cookie<'static>>> {
        None
    }
    /// Immediately invalidate all pending window/cookie/download callbacks.
    fn invalidate(&self, generation: u64);
    /// Local lifecycle invalidation may interrupt a physical store replacement.
    /// Restore that staged presentation without displacing a committed remote.
    fn local_rebound(&self, _epoch: Epoch) {}
    /// A staged view now matches the published committed identity.
    fn committed(&self, epoch: Epoch) -> Result<(), ClientError> {
        epoch.check()
    }
    /// Whether this exact operation crossed irreversible native retirement.
    fn terminal_commit(&self, _epoch: &Epoch) -> bool {
        false
    }
    /// Retire an unpublished candidate without restoring a retired document.
    async fn abort_commit(&self, _epoch: Epoch) -> Result<(), ClientError> {
        Ok(())
    }
    /// Stage a fresh disposable store; check the epoch before every UI callback.
    async fn commit(
        &self,
        epoch: Epoch,
        target: ClientTarget,
        cookies: Vec<cookie::Cookie<'static>>,
    ) -> Result<(), ClientError>;
    /// Cancel presentation and restore the last committed route, without auth.
    async fn restore(&self, epoch: Epoch, target: ClientTarget, preserve_document: bool) -> Result<(), ClientError>;
    /// Erase and destroy every Desktop-owned instance store, including offline.
    async fn signed_out(&self, epoch: Epoch, local: ClientTarget, cleanup_failed: bool) -> Result<(), ClientError>;
    /// Presentation only, after the cleanup error is readable by get_state.
    async fn signout_failed(&self, _epoch: Epoch) -> Result<(), ClientError> {
        Ok(())
    }
}

#[derive(Clone)]
struct Inner {
    state: ClientState,
    account: AccountFlow,
    local: Option<NativeSession>,
    enrollment: EnrollmentFlow,
    selection: Option<Selection>,
    update_session: Option<Arc<CommittedUpdate>>,
    record: ConnectionRecord,
    recency: InstanceRecency,
    restore_allowed: bool,
    committed_view_generation: Option<u64>,
    display_account: Option<String>,
}

pub struct Coordinator {
    inner: Mutex<Inner>,
    generation: Arc<AtomicU64>,
    action_generation: Arc<AtomicU64>,
    operation: tokio::sync::Mutex<()>,
    credential_io: tokio::sync::Mutex<CredentialWrites>,
    signing_out: AtomicBool,
    restore_started: AtomicBool,
    /// Public sign-in switch reads: the last one started, and the newest one
    /// shown. A read never waits on or for the account; only its own order counts.
    sign_in_reads: AtomicU64,
    sign_in_shown: AtomicU64,
    transport: Arc<dyn Transport>,
    credentials: Arc<dyn Credentials>,
    store: ConnectionStore,
    passkeys: Arc<dyn Passkeys>,
    browser: Arc<dyn SystemBrowser>,
    view: Arc<dyn ClientView>,
}

/// A build with no system browser offers no provider sign-in.
struct NoBrowser;
impl SystemBrowser for NoBrowser {
    fn open(&self, _url: &url::Url) -> Result<(), ClientError> {
        Err(ClientError::new("provider_unavailable"))
    }
    /// Never shown: with no browser the attempt ends before any redirect.
    fn callback_copy(&self, _locale: UiLocale) -> CallbackCopy {
        CallbackCopy::default()
    }
}

struct CredentialWrites {
    ids: HashSet<String>,
    cleanup_error: Option<ClientError>,
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
        let action_generation = Arc::new(AtomicU64::new(state.generation));
        let credential_ids = record.account_id.iter().cloned().collect();
        let recency = store.load_recency();
        Self {
            inner: Mutex::new(Inner {
                state,
                account: AccountFlow::new(transport.clone(), passkeys.supported()),
                local: None,
                enrollment: EnrollmentFlow::default(),
                selection: None,
                update_session: None,
                record,
                recency,
                restore_allowed: true,
                committed_view_generation: None,
                display_account: None,
            }),
            generation,
            action_generation,
            operation: tokio::sync::Mutex::new(()),
            credential_io: tokio::sync::Mutex::new(CredentialWrites {
                ids: credential_ids,
                cleanup_error: None,
            }),
            signing_out: AtomicBool::new(false),
            restore_started: AtomicBool::new(false),
            sign_in_reads: AtomicU64::new(0),
            sign_in_shown: AtomicU64::new(0),
            transport,
            credentials,
            store,
            passkeys,
            browser: Arc::new(NoBrowser),
            view,
        }
    }

    /// The person's default browser for provider sign-in.
    pub fn with_browser(mut self, browser: Arc<dyn SystemBrowser>) -> Self {
        self.browser = browser;
        self
    }

    /// Ends an unfinished sign-in in place: a running first factor (a browser or
    /// passkey wait), an email code waiting, or an owed second factor ("sign in
    /// with a different account"). The window stays; the attempt's session is
    /// dropped. A stored account that never finished signing in (restored at
    /// launch, owing its second factor) is forgotten as the iPhone signs out:
    /// its record first, then its keychain entry, so a restart cannot bring it
    /// back. A signed-in account cannot be ended this way; the waiting call
    /// sees its generation move and drops whatever arrives.
    async fn abort_sign_in(&self) -> Result<(), ClientError> {
        let (epoch, forget) = {
            let mut inner = self.inner.lock().expect("client state");
            if inner.state.account.status == AccountStatus::SignedIn {
                return Err(ClientError::new("invalid_action"));
            }
            let epoch = self.advance_locked(&mut inner, None, false)?;
            inner.account = AccountFlow::new(self.transport.clone(), self.passkeys.supported());
            inner.state.account = AccountState {
                status: AccountStatus::SignedOut,
                identity: None,
                email: None,
                gate: None,
                methods: vec![],
            };
            let forget = inner.record.account_id.take().map(|id| (id, inner.record.clone()));
            (epoch, forget)
        };
        self.view.invalidate(epoch.value);
        self.view.changed(&self.state());
        let Some((id, record)) = forget else {
            return Ok(());
        };
        // The credential owner sign-out uses: a write in flight finishes first.
        let mut owned = self.credential_io.lock().await;
        let credentials = self.credentials.clone();
        let store = self.store.clone();
        let erased_id = id.clone();
        let cleanup = tokio::task::spawn_blocking(move || {
            let saved = store.save(&record);
            let erased = credentials.erase(&erased_id);
            saved.and(erased)
        })
        .await
        .map_err(|_| ClientError::new("credentials_unavailable"))
        .and_then(|result| result);
        // On failure the record no longer names the account in memory, so no
        // later sign-in is written under its key, and the key stays owned for
        // a sign-out to erase. The error is the reply: nothing reports success.
        if cleanup.is_ok() {
            owned.ids.remove(&id);
        }
        cleanup
    }

    /// Reads avibe.bot's public sign-in switches into the published state.
    /// Public data from the fixed control plane: it takes no operation lock,
    /// touches no account, and no account change fences it. Reads publish in
    /// the order they started, so a slow earlier read never replaces a newer one.
    async fn refresh_sign_in_methods(&self) -> Result<(), ClientError> {
        let read = self.sign_in_reads.fetch_add(1, Ordering::SeqCst) + 1;
        let mut methods = AccountFlow::read_sign_in_methods(self.transport.clone()).await?;
        // Passkey sign-in needs this OS's passkey sheet as well as avibe.bot's switch.
        methods.passkey = methods.passkey && self.passkeys.supported();
        let state = {
            let mut inner = self.inner.lock().expect("client state");
            if read < self.sign_in_shown.load(Ordering::SeqCst) {
                return Ok(());
            }
            self.sign_in_shown.store(read, Ordering::SeqCst);
            if inner.state.sign_in == methods {
                return Ok(());
            }
            inner.state.sign_in = methods;
            inner.state.revision += 1;
            inner.state.clone()
        };
        self.view.changed(&state);
        Ok(())
    }

    /// Provider sign-in's start and browser round trip (#3946). The operation
    /// lock is held only to start the attempt; the wait holds none, so runtime
    /// monitoring and updates keep running, and the code is committed later
    /// under the lock. Starting advances the generation, which retires any
    /// earlier attempt; a newer one in turn ends this wait.
    async fn browse_provider(
        &self,
        provider: SignInProvider,
        locale: UiLocale,
        expected: u64,
        restoring: bool,
    ) -> Result<(Epoch, ProviderAttempt, String), ClientError> {
        let (epoch, mut attempt) = {
            if self.signing_out.load(Ordering::SeqCst) {
                return Err(ClientError::new("busy"));
            }
            let Ok(_operation) = self.operation.try_lock() else {
                return Err(ClientError::new("busy"));
            };
            if let Some(error) = self.credential_io.lock().await.cleanup_error.clone() {
                return Err(error);
            }
            if self.state().account.status == AccountStatus::SignedIn {
                return Err(ClientError::new("invalid_action"));
            }
            let epoch = self.advance(Some(expected), restoring)?;
            let copy = self.browser.callback_copy(locale);
            match AccountFlow::start_provider_sign_in(provider, self.transport.clone(), epoch.clone(), &copy).await {
                Ok(attempt) => (epoch, attempt),
                Err(error) => {
                    self.end_provider_attempt(&epoch);
                    return Err(error);
                }
            }
        };
        match attempt.browse(self.browser.as_ref(), &epoch).await {
            Ok(code) => Ok((epoch, attempt, code)),
            Err(error) => {
                self.end_provider_attempt(&epoch);
                Err(error)
            }
        }
    }

    /// An attempt that ended without a code changes nothing of the account, as
    /// any failed first factor: the previous attempt and its code stay. It
    /// publishes the generation it advanced to, unless something newer owns
    /// the account now.
    fn end_provider_attempt(&self, epoch: &Epoch) {
        let state = {
            let mut inner = self.inner.lock().expect("client state");
            if epoch.check().is_err() {
                return;
            }
            inner.state.revision += 1;
            inner.state.clone()
        };
        self.view.changed(&state);
    }
    pub fn state(&self) -> ClientState {
        self.inner.lock().expect("client state").state.clone()
    }
    pub fn epoch(&self) -> Epoch {
        Epoch::new(self.generation.clone(), self.generation.load(Ordering::SeqCst))
    }
    /// A local lifecycle change interrupts callbacks but does not replace an
    /// unfinished presentation. Accepted account/selection/escape actions do.
    pub fn presentation_owner(&self) -> u64 {
        self.action_generation.load(Ordering::SeqCst)
    }
    pub fn presentation_epoch(&self, owner: u64) -> Result<Epoch, ClientError> {
        let inner = self.inner.lock().expect("client state");
        if self.action_generation.load(Ordering::SeqCst) != owner {
            return Err(ClientError::new("stale_generation"));
        }
        Ok(Epoch::new(self.generation.clone(), inner.state.generation))
    }
    /// Receipt for the view selection that reached public state publication.
    /// Target equality cannot distinguish a reconnect to the same instance.
    pub fn committed_view_generation(&self) -> Option<u64> {
        self.inner.lock().expect("client state").committed_view_generation
    }

    /// Actual physical destruction retires presentation authority immediately.
    /// A subsequent shell recreation uses the existing safe-local escape owner.
    pub fn window_destroyed(&self) -> u64 {
        let epoch = self.advance(None, false).expect("unconditional window retirement");
        let mut inner = self.inner.lock().expect("client state");
        inner.selection = None;
        inner.state.consent = None;
        inner.state.connection.pending = None;
        inner.state.connection.status = committed_status(&inner.state);
        epoch.value
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
        inner.state.local.instance_id = None;
        inner.state.local.slug = None;
        inner.state.local.remote_access_url = None;
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
        // Share transaction ownership with account/enrollment publication.
        // Otherwise an older account clone can resurrect invalidated proof.
        let _operation = self.operation.lock().await;
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
                Err(_) => {
                    current.state.local.remote_access = RemoteAccessState::Unavailable;
                    current.state.local.instance_id = None;
                    current.state.local.remote_access_url = None;
                }
            }
            refresh_display(&mut current);
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
    fn advance(&self, expected: Option<u64>, restoring: bool) -> Result<Epoch, ClientError> {
        let mut inner = self.inner.lock().expect("client state");
        let epoch = self.advance_locked(&mut inner, expected, restoring)?;
        drop(inner);
        self.view.invalidate(epoch.value);
        Ok(epoch)
    }
    fn advance_locked(&self, inner: &mut Inner, expected: Option<u64>, restoring: bool) -> Result<Epoch, ClientError> {
        if expected.is_some_and(|value| value != inner.state.generation) || (restoring && !inner.restore_allowed) {
            return Err(ClientError::new("stale_generation"));
        }
        if !restoring {
            inner.restore_allowed = false;
        }
        let value = self.generation.fetch_add(1, Ordering::SeqCst) + 1;
        inner.state.generation = value;
        inner.state.revision += 1;
        self.action_generation.store(value, Ordering::SeqCst);
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
        self.request_owned(request, false).await
    }

    /// A native link captures both values before scheduling its local return.
    /// Only lifecycle may advance generation while this action owner remains.
    /// Completion receives the actual accepted owner, not expected + 1.
    /// User Cancel/ReturnLocal keep their unconditional escape semantics.
    pub async fn return_local_if_current(
        &self,
        expected_generation: u64,
        action_owner: u64,
    ) -> Result<u64, ClientError> {
        self.escape(ClientAction::ReturnLocal, Some(expected_generation), Some(action_owner))
            .await
    }

    async fn request_owned(&self, request: ClientRequest, restoring: bool) -> ClientReply {
        if request.version != 1 || request.request_id.is_empty() || request.request_id.len() > 128 {
            return self.reply(request.request_id, Err(ClientError::new("invalid_request")));
        }
        if matches!(request.action, ClientAction::GetState) {
            let error = self.credential_io.lock().await.cleanup_error.clone();
            return self.reply(request.request_id, error.map_or(Ok(None), Err));
        }
        // Escape always fences old work. It can never restore old credentials.
        if matches!(
            request.action,
            ClientAction::Cancel | ClientAction::ReturnLocal | ClientAction::SignOut { .. }
        ) {
            let expected =
                matches!(request.action, ClientAction::SignOut { .. }).then_some(request.expected_generation);
            let result = self.escape(request.action, expected, None).await.map(|_| None);
            return self.reply(request.request_id, result);
        }
        // Like an escape, it never waits for the operation it ends.
        if matches!(request.action, ClientAction::AbortSignIn) {
            let result = self.abort_sign_in().await.map(|_| None);
            return self.reply(request.request_id, result);
        }
        if matches!(request.action, ClientAction::RefreshSignInMethods) {
            let result = self.refresh_sign_in_methods().await.map(|_| None);
            return self.reply(request.request_id, result);
        }
        // An OS or build with no passkey sheet refuses before anything starts:
        // no generation move, no request, no system class touched.
        if matches!(request.action, ClientAction::SignInWithPasskey) && !self.passkeys.supported() {
            return self.reply(request.request_id, Err(ClientError::new("passkey_unavailable")));
        }
        let mut provider = None;
        if let ClientAction::SignInWithProvider {
            provider: which,
            locale,
        } = &request.action
        {
            match self
                .browse_provider(*which, *locale, request.expected_generation, restoring)
                .await
            {
                Ok(found) => provider = Some(found),
                Err(error) => return self.reply(request.request_id, Err(error)),
            }
        }
        if self.signing_out.load(Ordering::SeqCst) {
            return self.reply(request.request_id, Err(ClientError::new("busy")));
        }
        if request.action.is_instance_update() {
            let result = self.instance_action(request.expected_generation, request.action).await;
            return self.reply(request.request_id, result.map(|_| None));
        }
        let _operation = if provider.is_some() {
            // The person finished in the browser: wait for short work rather than refuse.
            self.operation.lock().await
        } else {
            let Ok(operation) = self.operation.try_lock() else {
                return self.reply(request.request_id, Err(ClientError::new("busy")));
            };
            operation
        };
        if request.action.is_desktop_update() {
            let epoch = Epoch::new(self.generation.clone(), request.expected_generation);
            let result = epoch
                .check()
                .and_then(|_| self.view.desktop_update(request.action, epoch.clone()))
                .and_then(|_| epoch.check())
                .map(|_| None);
            // The updater publishes its own live projection. An account-state
            // clone/persist here could overwrite progress or mutate credentials.
            return self.reply(request.request_id, result);
        }
        if let Some(error) = self.credential_io.lock().await.cleanup_error.clone() {
            return self.reply(request.request_id, Err(error));
        }
        // A provider attempt already advanced the generation; it commits only if
        // nothing newer has since.
        let expected = provider
            .as_ref()
            .map_or(request.expected_generation, |(epoch, ..)| epoch.value);
        {
            let mut inner = self.inner.lock().expect("client state");
            if expected != inner.state.generation || (restoring && !inner.restore_allowed) {
                drop(inner);
                return self.reply(request.request_id, Err(ClientError::new("stale_generation")));
            }
            if !restoring {
                inner.restore_allowed = false;
            }
        }
        let (epoch, provider) = match provider {
            Some((epoch, attempt, code)) => (Some(epoch), Some((attempt, code))),
            None => (None, None),
        };
        let epoch = if let Some(epoch) = epoch {
            epoch
        } else if matches!(
            request.action,
            ClientAction::RequestOtp { .. }
                | ClientAction::SignInPassword { .. }
                | ClientAction::SignInWithPasskey
                | ClientAction::SelectInstance { .. }
        ) {
            match self.advance(Some(request.expected_generation), restoring) {
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
        let return_after_auth = !restoring
            && matches!(
                request.action,
                ClientAction::SignInPassword { .. }
                    | ClientAction::SignInWithProvider { .. }
                    | ClientAction::SignInWithPasskey
                    | ClientAction::VerifyOtp { .. }
                    | ClientAction::VerifySecondFactor { .. }
                    | ClientAction::VerifyPasskey
                    | ClientAction::FinishTotpEnrollment { .. }
                    | ClientAction::RetryEnrollment
            );
        let owner = self.presentation_owner();
        let result = self.execute(&mut working, request.action, &epoch, provider).await;
        if epoch.check().is_err() || (result.is_err() && self.view.terminal_commit(&epoch)) {
            let error = result.err().unwrap_or_else(|| ClientError::new("stale_generation"));
            self.recover_commit(&epoch, owner, &error).await;
            return self.reply(request.request_id, Err(error));
        }
        if result
            .as_ref()
            .err()
            .is_some_and(|error| matches!(error.code.as_str(), "signed_out" | "account_changed"))
        {
            let cleanup = self.escape(ClientAction::SignOut { intent_id: None }, None, None).await;
            return self.reply(request.request_id, cleanup.and(result));
        }
        if let Err(error) = &result {
            self.recover_commit(&epoch, owner, error).await;
            working.selection = None;
            working.state.consent = None;
            working.state.connection.pending = None;
            working.state.connection.status = committed_status(&working.state);
        }
        let persistence = self.persist(&mut working, &epoch).await;
        if persistence.is_err() || epoch.check().is_err() {
            let error = persistence
                .as_ref()
                .err()
                .cloned()
                .unwrap_or_else(|| ClientError::new("stale_generation"));
            self.recover_commit(&epoch, owner, &error).await;
        } else if return_after_auth
            && result.is_ok()
            && !matches!(&result,
            Ok(Some(DirectResult::TotpEnrolled { recovery_codes: Some(codes), .. })) if !codes.is_empty())
        {
            let state = self.state();
            if state.account.status == AccountStatus::SignedIn
                && state.enrollment.status == EnrollmentStatus::PairedThisAccount
                && state.enrollment.error_code.is_none()
                && self.presentation_epoch(owner).is_ok()
            {
                // Native initiation owns this return; no late UI reply can
                // issue an unconditional Cancel against a successor.
                if let Err(error) = self.escape(ClientAction::Cancel, Some(epoch.value), Some(owner)).await {
                    return self.reply(request.request_id, Err(error));
                }
            }
        }
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
        provider: Option<(ProviderAttempt, String)>,
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
            ClientAction::SignInPassword { email, password } => {
                working
                    .account
                    .sign_in_password(email, password, epoch.clone(), &mut working.state)
                    .await?;
                authenticated = true;
            }
            ClientAction::VerifyOtp { code } => {
                working.account.verify_otp(code, &mut working.state).await?;
                authenticated = true;
            }
            ClientAction::SignInWithProvider { .. } => {
                let (attempt, code) = provider.ok_or_else(|| ClientError::new("invalid_action"))?;
                working
                    .account
                    .finish_provider_sign_in(attempt, code, epoch.clone(), &mut working.state)
                    .await?;
                authenticated = true;
            }
            ClientAction::SignInWithPasskey => {
                working
                    .account
                    .sign_in_with_passkey(self.passkeys.as_ref(), epoch.clone(), &mut working.state)
                    .await?;
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
                let discovery = working.account.discover(&mut working.state).await;
                // TOTP recovery codes are delivered only once. A subsequent
                // catalog failure must not discard that successful direct reply.
                if direct.is_none() {
                    discovery?;
                }
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
            working.committed_view_generation = Some(epoch.value);
            working.state.current_instance = Some(selection.instance.clone());
            working.state.instance_update = Some(InstanceUpdate::loading(selection.instance.id.clone()));
            working.update_session = Some(Arc::new(CommittedUpdate::new(selection.session.clone())));
            working.display_account = working
                .state
                .account
                .identity
                .as_ref()
                .map(|account| account.id.clone());
            working.selection = None;
        }
        Ok(())
    }

    async fn persist(&self, working: &mut Inner, epoch: &Epoch) -> Result<(), ClientError> {
        let mut owned = self.credential_io.lock().await;
        epoch.check()?;
        if let Some(error) = &owned.cleanup_error {
            return Err(error.clone());
        }
        let signed_in = working.state.account.status == AccountStatus::SignedIn;
        let stored = working.record.account_id.clone();
        if signed_in {
            let account = working
                .state
                .account
                .identity
                .as_ref()
                .ok_or_else(|| ClientError::new("authentication_required"))?;
            working.record.account_id = Some(account.id.clone());
            working.account.resumes = Some(account.id.clone());
        }
        // The key this commit writes: the signed-in account's, or the stored
        // account's when this unfinished sign-in continues it. Another
        // unfinished sign-in writes no key and leaves the stored record as is.
        let written_id = if signed_in || working.record.account_id == working.account.resumes {
            working.record.account_id.clone()
        } else {
            None
        };
        // A different account that signed in retires the stored one, in three
        // phases under credential_io: (1) its key and record are written,
        // touching nothing of the old account; (2) the final epoch check and
        // the in-memory commit are the one linearization point; (3) only once
        // (2) committed is the old key erased. Every failure before (2) rolls
        // back with the old account untouched; a failure in (3) never
        // restores the old record or undoes the committed account.
        let retired_id = stored.filter(|old| signed_in && working.record.account_id.as_ref() != Some(old));
        let committed_id = working.record.account_id.clone();
        working.record.last_committed = working.state.connection.committed.clone();
        if let Some(id) = &written_id {
            // Own the key before the blocking write starts. Sign-out waits for
            // this lock and erases even a write whose public commit was fenced.
            owned.ids.insert(id.clone());
        }
        let bytes = working.account.session.persist()?;
        let record = working.record.clone();
        let credentials = self.credentials.clone();
        let store = self.store.clone();
        let checked = epoch.clone();
        let write_id = written_id.clone();
        // (1) A task that never returns (a panic) is a failure before (2):
        // it rolls back like any other, with what it may have written unknown.
        let (mut saved, previous) = tokio::task::spawn_blocking(move || {
            let previous = match &write_id {
                Some(id) => match credentials.read(id) {
                    Ok(previous) => Some(previous),
                    Err(error) => return (Err(error), None),
                },
                None => None,
            };
            let result = (|| {
                checked.check()?;
                if let Some(id) = &write_id {
                    credentials.write(id, &bytes)?;
                }
                checked.check()?;
                store.save(&record)
            })();
            (result, previous)
        })
        .await
        .unwrap_or_else(|_| (Err(ClientError::new("credentials_unavailable")), None));
        let mut recency = None;
        let rollback_record = {
            let mut current = self.inner.lock().expect("client state");
            let opened = working.committed_view_generation != current.committed_view_generation;
            if !opened {
                working.state.instance_update.clone_from(&current.state.instance_update);
                working.update_session.clone_from(&current.update_session);
            }
            if saved.is_ok() && epoch.check().is_ok() && opened {
                saved = self.view.committed(epoch.clone());
            }
            if epoch.check().is_err() || saved.is_err() {
                let error = saved
                    .as_ref()
                    .err()
                    .cloned()
                    .unwrap_or_else(|| ClientError::new("stale_generation"));
                let record = current.record.clone();
                // First-factor/one-time-result state stays readable on a
                // storage error. A failed target selection never publishes.
                if epoch.check().is_ok() && !opened {
                    working.state.enrollment.error_code = Some(error.code.clone());
                    working.record = record.clone();
                    working.state.sign_in = current.state.sign_in.clone();
                    working.state.revision = current.state.revision + 1;
                    refresh_display(working);
                    *current = working.clone();
                }
                Some((record, error))
            } else {
                working.local = current.local.as_ref().map(|local| {
                    if working.local.as_ref().is_some_and(|old| old.origin == local.origin) {
                        working.local.clone().expect("matched local")
                    } else {
                        local.clone()
                    }
                });
                working.state.desktop = current.state.desktop.clone();
                // The public switches are not the account's: a read that
                // landed during this operation stays.
                working.state.sign_in = current.state.sign_in.clone();
                working.state.revision = current.state.revision + 1;
                if opened {
                    if let (Some(account), ClientTarget::Remote { instance_id, .. }) =
                        (&working.state.account.identity, &working.state.connection.committed)
                    {
                        working.recency.opened(&account.id, instance_id);
                        recency = Some(working.recency.clone());
                    }
                }
                refresh_display(working);
                order_catalog(working);
                // (2) The linearization point.
                *current = working.clone();
                None
            }
        };
        if rollback_record.is_none() {
            if let Some(old) = retired_id {
                // (3) Still under credential_io: no sign-out, forget or other
                // persist can touch a key until this ends.
                let credentials = self.credentials.clone();
                let erase_id = old.clone();
                let erased = tokio::task::spawn_blocking(move || credentials.erase(&erase_id))
                    .await
                    .unwrap_or_else(|_| Err(ClientError::new("credentials_unavailable")));
                match erased {
                    Ok(()) => {
                        owned.ids.remove(&old);
                    }
                    // The old key stays owned for a sign-out to erase. The
                    // error shows only while the committed account is still
                    // the one signed in; a sign-out in between has already
                    // replaced that state and must not inherit it.
                    Err(error) => {
                        let mut current = self.inner.lock().expect("client state");
                        let still_committed = current.state.account.status == AccountStatus::SignedIn
                            && current.state.account.identity.as_ref().map(|account| &account.id)
                                == committed_id.as_ref();
                        if still_committed {
                            current.state.enrollment.error_code = Some(error.code.clone());
                            current.state.revision += 1;
                        }
                    }
                }
            }
        }
        if let Some(recency) = recency {
            // Order is a local preference: a failed write keeps this run's
            // order in memory and never fails the committed selection.
            let store = self.store.clone();
            let _ = tokio::task::spawn_blocking(move || store.save_recency(&recency)).await;
        }
        if let Some((record, error)) = rollback_record {
            // Keep credential_io through public commit or rollback. Escape
            // waits on this lock; cancellation cannot leave an unpublished
            // first credential or overwrite an existing committed jar.
            let id = written_id;
            let credentials = self.credentials.clone();
            let store = self.store.clone();
            let rollback = tokio::task::spawn_blocking(move || {
                let erased = if let (Some(id), Some(previous)) = (id, previous) {
                    match previous {
                        Some(bytes) => credentials.write(&id, &bytes),
                        None => credentials.erase(&id),
                    }
                } else {
                    Ok(())
                };
                // Restore metadata even when credential rollback fails.
                store.save(&record).and(erased)
            })
            .await
            .map_err(|_| ClientError::new("credentials_unavailable"))
            .and_then(|result| result);
            if let Err(error) = rollback {
                owned.cleanup_error = Some(error.clone());
                let state = {
                    let mut current = self.inner.lock().expect("client state");
                    current.state.enrollment.error_code = Some(error.code.clone());
                    current.state.revision += 1;
                    current.state.clone()
                };
                self.view.changed(&state);
                return Err(error);
            }
            return Err(error);
        }
        saved
    }

    async fn recover_commit(&self, epoch: &Epoch, owner: u64, error: &ClientError) {
        let terminal = self.view.terminal_commit(epoch);
        let cleanup = self.view.abort_commit(epoch.clone()).await;
        if let Err(cleanup) = &cleanup {
            self.credential_io.lock().await.cleanup_error = Some(cleanup.clone());
        }
        if !terminal || self.presentation_epoch(owner).is_err() {
            return;
        }
        let Ok((recovery_owner, recovery)) = self
            .escape_outcome(ClientAction::ReturnLocal, Some(epoch.value), Some(owner))
            .await
        else {
            return;
        };
        if self.presentation_epoch(recovery_owner).is_err() {
            return;
        }
        if let Err(recovery) = recovery {
            self.credential_io.lock().await.cleanup_error = Some(recovery);
        }
        {
            let mut inner = self.inner.lock().expect("client state");
            if self.action_generation.load(Ordering::SeqCst) != recovery_owner {
                return;
            }
            inner.state.enrollment.error_code = Some(error.code.clone());
            inner.state.revision += 1;
        }
        let _ = self.view.signout_failed(self.epoch()).await;
        self.view.changed(&self.state());
    }

    async fn escape(
        &self,
        action: ClientAction,
        expected: Option<u64>,
        native_owner: Option<u64>,
    ) -> Result<u64, ClientError> {
        let (owner, result) = self.escape_outcome(action, expected, native_owner).await?;
        result.map(|()| owner)
    }

    async fn escape_outcome(
        &self,
        action: ClientAction,
        expected: Option<u64>,
        native_owner: Option<u64>,
    ) -> Result<(u64, Result<(), ClientError>), ClientError> {
        let signing_out = matches!(action, ClientAction::SignOut { .. });
        if signing_out && self.signing_out.swap(true, Ordering::SeqCst) {
            return Err(ClientError::new("busy"));
        }
        let _signout_guard = SignoutGuard {
            flag: &self.signing_out,
            active: signing_out,
        };
        let (epoch, target, record, mut old_session) = {
            let mut inner = self.inner.lock().expect("client state");
            // Acceptance and state mutation share one owner/critical section.
            // invalidate may call back or let a newer action run; it must not
            // leave an older escape able to overwrite that action afterward.
            let expected = if let Some(owner) = native_owner {
                // The received link already has action authority. Binding a
                // local Runtime before this task runs does not replace it.
                // Check both under the acceptance lock: never refresh the
                // owner, accept a future generation, or retry a failed escape.
                if self.action_generation.load(Ordering::SeqCst) != owner
                    || expected.is_none_or(|value| value > inner.state.generation)
                {
                    return Err(ClientError::new("stale_generation"));
                }
                None
            } else {
                expected
            };
            let epoch = self.advance_locked(&mut inner, expected, false)?;
            inner.selection = None;
            inner.state.consent = None;
            inner.state.connection.pending = None;
            inner.account.clear_transient();
            let old_session = inner.account.session.clone();
            if signing_out {
                inner.account = AccountFlow::new(self.transport.clone(), self.passkeys.supported());
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
                inner.account = AccountFlow::new(self.transport.clone(), self.passkeys.supported());
                inner.state.account = AccountState {
                    status: AccountStatus::SignedOut,
                    identity: None,
                    email: None,
                    gate: None,
                    methods: vec![],
                };
            }
            if signing_out || matches!(action, ClientAction::ReturnLocal) {
                inner.update_session = None;
                inner.state.instance_update = None;
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
            refresh_display(&mut inner);
            (epoch, target, inner.record.clone(), old_session)
        };
        self.view.invalidate(epoch.value);
        self.view.changed(&self.state());
        let mut owned = self.credential_io.lock().await;
        let ids: Vec<_> = if signing_out {
            owned.ids.iter().cloned().collect()
        } else {
            vec![]
        };
        let credentials = self.credentials.clone();
        let store = self.store.clone();
        // Lifecycle interrupts document callbacks, not this accepted escape's
        // restoration record. A later explicit action still fences the write.
        let write_epoch = Epoch::new(self.action_generation.clone(), epoch.value);
        let prior_error = owned.cleanup_error.clone();
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
                prior_error.map_or(Ok(()), Err)
            };
            saved.and(erased)
        })
        .await
        .map_err(|_| ClientError::new("credentials_unavailable"))
        .and_then(|result| result);
        if signing_out {
            if cleanup.is_ok() {
                owned.ids.clear();
            }
            owned.cleanup_error = cleanup.as_ref().err().cloned();
        }
        drop(owned);
        let view_result = if signing_out {
            self.view.signed_out(epoch.clone(), target, cleanup.is_err()).await
        } else {
            self.presentation_epoch(epoch.value)?;
            self.view
                .restore(epoch.clone(), target, matches!(action, ClientAction::Cancel))
                .await
        };
        if signing_out {
            // Device-local best effort revocation never delays local erasure.
            old_session.unfenced();
            tokio::spawn(async move {
                let _ = old_session.request(Method::DELETE, "/api/mobile/session", None).await;
            });
        }
        let result = view_result.and(cleanup);
        if signing_out {
            // Native store retirement is part of the same cleanup obligation
            // as credential erasure. Only a successful sign-out retry clears
            // that obligation; no new account work can pass a failed owner.
            self.credential_io.lock().await.cleanup_error = result.as_ref().err().cloned();
            if result.is_err() && self.presentation_epoch(epoch.value).is_ok() {
                // A newly loaded receiver can immediately get_state. Store the
                // error first; never rely on a later projection-only event.
                let _ = self.view.signout_failed(epoch.clone()).await;
            }
        }
        let state = {
            let mut inner = self.inner.lock().expect("client state");
            if epoch.check().is_ok() {
                inner.state.enrollment.error_code = result.as_ref().err().map(|error| error.code.clone());
                inner.state.revision += 1;
            }
            inner.state.clone()
        };
        self.view.changed(&state);
        Ok((epoch.value, result))
    }

    /// Called once at native initialization; cached URLs never authorize restore.
    pub async fn restore_account(&self) -> Result<(), ClientError> {
        if self.restore_started.swap(true, Ordering::SeqCst) {
            return Ok(());
        }
        let restore = self.inner.lock().expect("client state").record.last_committed.clone();
        let result = loop {
            let result = self.restore_saved_account(&restore).await;
            // Only local_origin can invalidate an epoch without cancelling the
            // startup owner. User actions and cleanup failures never retry.
            if result.as_ref().is_err_and(|error| error.code == "stale_generation")
                && self.inner.lock().expect("client state").restore_allowed
            {
                continue;
            }
            break result;
        };
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

    async fn restore_saved_account(&self, restore: &ClientTarget) -> Result<(), ClientError> {
        let _operation = self.operation.lock().await;
        if let Some(error) = self.credential_io.lock().await.cleanup_error.clone() {
            return Err(error);
        }
        let epoch = self.advance(None, true)?;
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
        working.account.resumes = Some(id.clone());
        working.account.fence(epoch.clone());
        if let Err(error) = working.account.discover(&mut working.state).await {
            if matches!(error.code.as_str(), "signed_out" | "account_changed") {
                self.escape(ClientAction::SignOut { intent_id: None }, None, None)
                    .await?;
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
            self.escape(ClientAction::SignOut { intent_id: None }, None, None)
                .await?;
            return Err(ClientError::new("account_changed"));
        }
        self.persist(&mut working, &epoch).await?;
        self.view.changed(&self.state());
        drop(_operation);
        if let ClientTarget::Remote {
            instance_id,
            canonical_origin,
            ..
        } = restore.clone()
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
                    .request_owned(
                        ClientRequest {
                            version: 1,
                            request_id: "native-restore".into(),
                            expected_generation: state.generation,
                            action: ClientAction::SelectInstance { instance_id },
                        },
                        true,
                    )
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

/// Current remote first, then this account's instances opened on this device,
/// most recent first, then avibe.bot's order (pins, then newest).
fn order_catalog(inner: &mut Inner) {
    let current = match &inner.state.connection.committed {
        ClientTarget::Remote { instance_id, .. } => Some(instance_id.as_str()),
        ClientTarget::Local { .. } => None,
    };
    let recent = inner
        .state
        .account
        .identity
        .as_ref()
        .map_or(&[][..], |account| inner.recency.for_account(&account.id));
    let server = inner.account.server_order();
    let rank = |ids: &[String], id: &str| ids.iter().position(|item| item == id).unwrap_or(usize::MAX);
    inner.state.catalog.instances.sort_by_cached_key(|instance| {
        (
            current != Some(instance.id.as_str()),
            rank(recent, &instance.id),
            rank(server, &instance.id),
        )
    });
}

fn committed_status(state: &ClientState) -> ConnectionStatus {
    if state.connection.committed.is_local() {
        ConnectionStatus::Local
    } else {
        ConnectionStatus::RemoteConnected
    }
}

/// Display metadata has no connection authority and is never persisted.
fn refresh_display(inner: &mut Inner) {
    let account = inner
        .state
        .account
        .identity
        .as_ref()
        .filter(|_| inner.state.account.status == AccountStatus::SignedIn)
        .map(|identity| identity.id.clone());
    inner.state.local.slug = account.as_ref().and_then(|_| {
        let id = inner.state.local.instance_id.as_ref()?;
        inner
            .state
            .catalog
            .instances
            .iter()
            .find(|instance| &instance.id == id)
            .map(|instance| instance.name.clone())
    });
    if account.is_none() || account != inner.display_account {
        inner.state.current_instance = None;
    }
    inner.display_account = account.clone();
    let ClientTarget::Remote {
        instance_id,
        canonical_origin,
        ..
    } = &inner.state.connection.committed
    else {
        inner.state.current_instance = None;
        return;
    };
    let matches = |instance: &Instance| {
        instance.id == *instance_id
            && instance_origin(instance).is_ok_and(|origin| origin.origin().ascii_serialization() == *canonical_origin)
    };
    if account.is_none() {
        return;
    }
    if let Some(instance) = inner
        .state
        .catalog
        .instances
        .iter()
        .find(|instance| instance.id == *instance_id)
    {
        inner.state.current_instance = matches(instance).then(|| instance.clone());
    } else if !inner.state.current_instance.as_ref().is_some_and(matches) {
        inner.state.current_instance = None;
    }
}
