//! Native-owned account presentation and disposable instance window lifecycle.
use async_trait::async_trait;
use avibe_runtime_host::{
    deep_link::DeepLinkReturn,
    instance_client::{
        account::{Passkeys, SystemBrowser},
        coordinator::{
            claims::{ClaimAction, ClaimCaller, ClaimDocument, ClaimRequest},
            AccountRecoveryRequest, ClientView, Coordinator,
        },
        loopback::CallbackCopy,
        protocol::*,
        storage::{ConnectionRecord, ConnectionStore, Credentials},
        transport::{Epoch, HttpTransport, Transport},
    },
    LoopbackOrigin,
};
use std::sync::{Arc, Mutex};
use tauri::{AppHandle, Manager, Webview};
use url::Url;

use crate::{Shell, MAIN_WINDOW};
const EVENT: &str = "avibe:desktop-client-state";

pub(crate) struct Client {
    pub coordinator: Arc<Coordinator>,
    view: Arc<NativeView>,
    presentations: Mutex<PresentationQueue>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
struct PresentationIntent {
    id: u64,
    view: &'static str,
    owner: u64,
    guarded: bool,
    locale: Option<&'static str>,
}
#[derive(Clone, Debug, PartialEq, Eq)]
struct GuardAttempt {
    sequence: u64,
    intent: PresentationIntent,
    receiver: String,
}
impl GuardAttempt {
    fn token(&self) -> String {
        format!("{}-{}", self.intent.id, self.sequence)
    }
}
#[derive(Default)]
struct PresentationQueue {
    sequence: u64,
    pending: Option<PresentationIntent>,
    scheduled: bool,
    retrying: bool,
    guard_sequence: u64,
    guard: Option<GuardAttempt>,
    approval: Option<GuardAttempt>,
    window: u64,
    document: u64,
    source: Option<PresentationSource>,
    claim_workbench: Option<(String, u64)>,
    link: Option<(u64, DeepLinkReturn)>,
    logout: Option<LogoutReceipt>,
}
#[derive(Clone, Debug, PartialEq, Eq)]
struct PresentationSource {
    window: u64,
    document: u64,
    label: String,
    url: String,
}
/// An already admitted presentation, retained through off-handler popup work.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct PopupPresentation {
    intent: PresentationIntent,
    source: PresentationSource,
}
struct LogoutReceipt {
    result: DirectResult,
    owner: u64,
    generation: u64,
    window: u64,
    document: u64,
    destination: Url,
}
impl PresentationQueue {
    fn enqueue(&mut self, view: &'static str, owner: u64, guarded: bool, locale: Option<&'static str>) {
        self.logout = None;
        self.source = None;
        self.claim_workbench = None;
        self.sequence += 1;
        self.guard = None;
        self.approval = None;
        self.pending = Some(PresentationIntent {
            id: self.sequence,
            view,
            owner,
            guarded,
            locale,
        });
    }
    fn window_created(&mut self) -> u64 {
        self.retire_document_guard();
        self.window += 1;
        self.document = 0;
        self.logout = None;
        self.window
    }
    fn document_started(&mut self, window: u64, url: &Url) {
        if window != self.window {
            return;
        }
        self.retire_document_guard();
        self.document += 1;
        if self
            .logout
            .as_ref()
            .is_some_and(|r| r.window != window || r.document != self.document || r.destination != *url)
        {
            self.logout = None;
        }
    }
    fn retire_document_guard(&mut self) {
        // Equal URLs do not make a reloaded document the receiver that was
        // asked to approve. A late callback cannot re-create this authority.
        if let Some(attempt) = self.guard.take() {
            self.complete(attempt.intent.id);
        }
        self.approval = None;
        if self.source.take().is_some() {
            self.pending = None;
        }
    }
    fn logout_result(&mut self, owner: u64, generation: u64, receiver: &Url) -> Option<DirectResult> {
        if self
            .logout
            .as_ref()
            .is_some_and(|r| r.owner != owner || r.generation != generation)
        {
            self.logout = None;
        }
        self.logout
            .as_ref()
            .filter(|r| r.window == self.window && r.document == self.document && r.destination == *receiver)
            .map(|r| r.result.clone())
    }
    fn consume_logout(&mut self, id: &str, owner: u64, generation: u64, receiver: &Url) -> bool {
        let matches = matches!(self.logout_result(owner, generation, receiver), Some(DirectResult::SignOutIntent { intent_id, .. }) if intent_id == id);
        if matches {
            self.logout = None;
        }
        matches
    }

    fn current(&mut self, owner: u64) -> Option<PresentationIntent> {
        if self.pending.is_some_and(|intent| intent.owner != owner) {
            self.pending = None;
            self.guard = None;
        }
        if self
            .approval
            .as_ref()
            .is_some_and(|attempt| attempt.intent.owner != owner)
        {
            self.approval = None;
        }
        self.pending
    }
    fn complete(&mut self, id: u64) {
        if self.pending.is_some_and(|intent| intent.id == id) {
            self.pending = None;
            self.guard = None;
        }
    }
    fn begin_guard(&mut self, intent: PresentationIntent, receiver: String) -> Option<GuardAttempt> {
        if self.pending != Some(intent) || self.guard.is_some() {
            return None;
        }
        self.guard_sequence += 1;
        let attempt = GuardAttempt {
            sequence: self.guard_sequence,
            intent,
            receiver,
        };
        self.guard = Some(attempt.clone());
        Some(attempt)
    }
    fn guard_result(&mut self, attempt: &GuardAttempt, result: &str) {
        if self.guard.as_ref() != Some(attempt) || self.pending != Some(attempt.intent) {
            return;
        }
        match serde_json::from_str::<String>(result).as_deref() {
            Ok("dispatched") => {
                self.complete(attempt.intent.id);
                // A consumed event may await asynchronous user approval. Keep
                // its exact authority until that URL arrives or is superseded.
                self.approval = Some(attempt.clone());
            }
            Ok("not_dispatched") => self.guard = None,
            // Missing/malformed/unknown callbacks may follow a shown guard.
            // Never automatically repeat a possible unsaved-work prompt.
            _ => {}
        }
    }
    fn guard_authority(&self, token: &str, view: &str, owner: u64, receiver: &Url) -> Option<GuardAttempt> {
        self.guard
            .as_ref()
            .or(self.approval.as_ref())
            .filter(|attempt| {
                attempt.token() == token
                    && attempt.intent.view == view
                    && attempt.intent.owner == owner
                    && attempt.receiver == receiver.as_str()
            })
            .cloned()
    }
    fn owns_guard_authority(&self, attempt: &GuardAttempt) -> bool {
        self.guard.as_ref() == Some(attempt) || self.approval.as_ref() == Some(attempt)
    }
    fn owns_intent(&self, id: u64) -> bool {
        self.pending.is_some_and(|intent| intent.id == id)
            || self.guard.as_ref().is_some_and(|attempt| attempt.intent.id == id)
            || self.approval.as_ref().is_some_and(|attempt| attempt.intent.id == id)
    }
    fn source_matches(&self, label: &str, url: &Url) -> bool {
        self.source.as_ref().is_some_and(|source| {
            source.window == self.window
                && source.document == self.document
                && source.label == label
                && source.url == url.as_str()
        })
    }
    fn approve(&mut self, attempt: &GuardAttempt, locale: Option<&'static str>, label: &str, url: &Url) -> bool {
        if !self.owns_guard_authority(attempt) || attempt.receiver != url.as_str() {
            return false;
        }
        let continuation = if self.link.as_ref().is_some_and(|(id, _)| *id == attempt.intent.id) {
            self.link.take()
        } else {
            None
        };
        self.enqueue(attempt.intent.view, attempt.intent.owner, false, locale);
        if let Some((_, ticket)) = continuation {
            self.link = Some((self.pending.expect("new intent").id, ticket));
        }
        self.source = Some(PresentationSource {
            window: self.window,
            document: self.document,
            label: label.into(),
            url: url.to_string(),
        });
        true
    }
    fn can_start(&self, intent: PresentationIntent, label: &str, url: &Url) -> bool {
        self.pending == Some(intent)
            && ((intent.view != "claim" && self.source.is_none()) || self.source_matches(label, url))
    }
    fn popup_is_current(&self, presentation: &PopupPresentation) -> bool {
        self.sequence == presentation.intent.id
            && self.window == presentation.source.window
            && self.document == presentation.source.document
    }
    fn cancel(&mut self) {
        // Also retire a completed presentation whose popup is still loading.
        self.sequence += 1;
        self.pending = None;
        self.guard = None;
        self.approval = None;
        self.source = None;
        self.logout = None;
    }
}
struct Presentation {
    state: ClientState,
    account: bool,
    staging: bool,
    pending_commit: Option<u64>,
    remote: Option<Url>,
    locale: Option<String>,
}
impl Presentation {
    /// Whether bootstrap may navigate the main window: only while it shows this
    /// computer's Workbench. An account document (its account, instances or
    /// claim view), another computer's instance, or a commit in progress keeps
    /// the window through a Runtime restart; recovery only unbinds and binds
    /// the local origin under it.
    fn bootstrap_owns_window(&self) -> bool {
        !self.account && self.remote.is_none() && !self.staging && self.pending_commit.is_none()
    }
}
struct NativeView {
    app: AppHandle,
    bundle: Url,
    local_origin: Arc<Mutex<Option<LoopbackOrigin>>>,
    presentation: Mutex<Presentation>,
    windows: Arc<tokio::sync::Mutex<()>>,
    navigation: Mutex<Option<Navigation>>,
    views: Mutex<Views>,
}
#[derive(Default)]
struct Views {
    next: u64,
    runtime: Option<RuntimeDocument>,
    candidate: Option<RuntimeDocument>,
    retired: Vec<RuntimeDocument>,
    terminal: Option<u64>,
    account: Option<AccountDocument>,
}
impl Views {
    // Capture the actual Runtime document, never a candidate or an instance hidden by a sheet.
    fn workbench(&self, label: &str, window: u64) -> Option<(String, u64)> {
        let runtime = self.runtime.as_ref()?;
        (self.account.is_none()
            && self.candidate.is_none()
            && !runtime.disposable
            && runtime.window == window
            && runtime.label == label)
            .then(|| (runtime.label.clone(), runtime.document))
    }
    fn return_matches(&self, account: &AccountDocument) -> bool {
        self.runtime.as_ref().is_some_and(|runtime| {
            runtime.window == account.window
                && account.returning.as_ref() == Some(&(runtime.label.clone(), runtime.document))
        })
    }
    fn check_return(&self) -> Result<(), ClientError> {
        if self
            .account
            .as_ref()
            .is_some_and(|account| !self.return_matches(account))
        {
            return Err(ClientError::new("instance_session_missing"));
        }
        Ok(())
    }
    /// Home replaces the retained document. Its account return receipt must
    /// already be retired, so a later Cancel restores the current Runtime
    /// rather than trying to restore an irreversibly replaced document.
    fn home_destination(&self, epoch: &Epoch, label: &str, current: &Url) -> Result<Url, ClientError> {
        epoch.check()?;
        if self.account.is_some() || self.runtime.as_ref().is_none_or(|runtime| runtime.label != label) {
            return Err(ClientError::new("stale_generation"));
        }
        Ok(workbench_home(current))
    }
    /// The role on screen in the main window. An account document is on
    /// screen from the moment `open_account` shows it, including while it
    /// reloads (`loaded` is false from commit to finish). Until then, even when
    /// it replaces a shown account document, the Runtime is the visible role.
    fn displayed(&self) -> Option<&str> {
        self.account
            .as_ref()
            .filter(|account| account.shown)
            .map(|account| account.label.as_str())
            .or(self.runtime.as_ref().map(|runtime| runtime.label.as_str()))
    }
}
struct RuntimeDocument {
    label: String,
    window: u64,
    destination: Url,
    disposable: bool,
    directory: Option<tempfile::TempDir>,
    owner: u64,
    document: u64,
}
impl RuntimeDocument {
    fn allows_navigation(&self, destination: &Url, bundle: &Url) -> bool {
        destination.as_str() == "about:blank"
            || (!self.disposable && self.destination == *bundle && destination == &self.destination)
            || (destination.origin() == self.destination.origin()
                && destination.username().is_empty()
                && destination.password().is_none())
    }
}
struct AccountDocument {
    claim: Option<ClaimDocument>,
    claim_visibility: u64,
    sheet_open: SheetOpen,
    label: String,
    window: u64,
    destination: Url,
    document: u64,
    admitted: bool,
    loaded: bool,
    /// Set once `open_account` has shown this document; never cleared.
    shown: bool,
    token: Option<String>,
    returning: Option<(String, u64)>,
    owner: u64,
}
struct Navigation {
    label: String,
    epoch: Epoch,
    destination: Url,
    completed: tokio::sync::oneshot::Sender<Result<(), ClientError>>,
}
fn failure(_: impl std::fmt::Display) -> ClientError {
    ClientError::new("window_unavailable")
}

/// The bundled origin the account pages (and the update popup) are served from.
pub(crate) fn bundle(app: &AppHandle) -> Option<Url> {
    app.try_state::<Client>().map(|client| client.view.bundle.clone())
}

pub(crate) fn enabled<R: tauri::Runtime>(app: &AppHandle<R>) -> bool {
    app.try_state::<Client>().is_some()
}
pub(crate) fn local_selected(app: &AppHandle) -> bool {
    app.try_state::<Client>()
        .is_none_or(|client| client.coordinator.state().connection.committed.is_local())
}
pub(crate) fn foreground_local(app: &AppHandle) -> bool {
    app.try_state::<Client>().is_none_or(|client| {
        let state = client.view.presentation.lock().expect("presentation");
        !state.account && state.remote.is_none() && client.view.windows.try_lock().is_ok()
    })
}
pub(crate) fn presentation_owner(app: &AppHandle) -> Option<u64> {
    app.try_state::<Client>()
        .map(|client| client.coordinator.presentation_owner())
}
pub(crate) fn presentation_busy(app: &AppHandle) -> bool {
    app.try_state::<Client>().is_some_and(|client| {
        let view = client.view.presentation.lock().expect("presentation");
        view.staging || view.pending_commit.is_some() || client.view.windows.try_lock().is_err()
    })
}
/// Bootstrap may navigate an idle local window only while holding the same
/// serialization as replacement/rollback. A busy owner receives the new local
/// origin and finishes its own navigation and visibility under that origin.
pub(crate) fn bootstrap_navigation(app: &AppHandle) -> Option<tokio::sync::OwnedMutexGuard<()>> {
    let client = app.try_state::<Client>()?;
    let guard = client.view.windows.clone().try_lock_owned().ok()?;
    let state = client.view.presentation.lock().expect("presentation");
    state.bootstrap_owns_window().then_some(guard)
}
pub(crate) fn epoch(app: &AppHandle) -> Option<u64> {
    app.try_state::<Client>()
        .map(|client| client.coordinator.state().generation)
}
pub(crate) fn epoch_matches(app: &AppHandle, generation: u64) -> bool {
    epoch(app) == Some(generation)
}
#[cfg(windows)]
pub(crate) fn download_epoch(app: &AppHandle, label: &str, current: &Url, target: &Url) -> Option<Epoch> {
    let client = app.try_state::<Client>()?;
    let state = client.view.presentation.lock().expect("presentation");
    if state.account || state.staging || state.pending_commit.is_some() || current.origin() != target.origin() {
        return None;
    }
    match (&state.remote, &state.state.connection.committed) {
        (None, ClientTarget::Local { .. }) => {}
        (Some(origin), ClientTarget::Remote { canonical_origin, .. })
            if state.state.account.status == AccountStatus::SignedIn
                && origin.origin().ascii_serialization() == *canonical_origin => {}
        _ => return None,
    }
    drop(state);
    caller_can_present(app, label, current).then(|| client.coordinator.epoch())
}
/// Destroying the last WebView during a store replacement is not a request to
/// quit the shell or stop its local Runtime. Explicit application exits retain
/// their normal lifecycle owner.
pub fn replacing_window_exit(app: &AppHandle, code: Option<i32>) -> bool {
    code.is_none()
        && app
            .try_state::<Client>()
            .is_some_and(|client| client.view.windows.try_lock().is_err())
}

/// Detection reads packaged bytes. A page-written feature marker grants nothing.
pub(crate) fn init(app: &AppHandle, bundle: &Url) -> Result<(), ClientError> {
    if enabled(app) {
        return Ok(());
    }
    if !((bundle.scheme() == "tauri" && bundle.host_str() == Some("localhost"))
        || (bundle.scheme() == "http" && bundle.host_str() == Some("tauri.localhost")))
    {
        return Ok(());
    }
    let Some(asset) = app.asset_resolver().get("account/index.html".into()) else {
        return Ok(());
    };
    if !compatible_asset(asset.bytes()) {
        return Ok(());
    }
    let path = app
        .path()
        .app_local_data_dir()
        .map_err(failure)?
        .join("instance-client.json");
    let mut store = ConnectionStore::new(path);
    crate::device_description::prepare(app);
    let device_name = std::env::var("COMPUTERNAME")
        .or_else(|_| std::env::var("HOSTNAME"))
        .unwrap_or_else(|_| app.package_info().name.clone());
    let name = device_name.as_str();
    let mut state = ClientState::new(
        name.into(),
        app.package_info().version.to_string(),
        crate::updater::client_snapshot(app),
    );
    let record = match store.load(name) {
        Ok(record) => record,
        Err(error) => {
            store = store.read_only();
            state.enrollment.error_code = Some(error.code);
            ConnectionRecord {
                schema_version: 1,
                profile_id: "unavailable".into(),
                account_id: None,
                last_committed: state.connection.committed.clone(),
            }
        }
    };
    let credentials = Arc::new(crate::instance_credentials::NativeCredentials::new(
        &app.config().identifier,
        &record.profile_id,
    ));
    let coordinator = attach(
        app,
        NativeClientConfig {
            bundle: bundle.clone(),
            state,
            record,
            store,
            local_origin: app.state::<Shell>().active_origin.clone(),
        },
        Arc::new(HttpTransport::new(&app.package_info().version.to_string())?),
        credentials,
        Arc::new(crate::instance_passkeys::NativePasskeys::new(app.clone())),
    );
    tauri::async_runtime::spawn(async move {
        if let Err(error) = coordinator.restore_account().await {
            eprintln!("desktop account restore: {}", error.code);
        }
    });
    Ok(())
}
/// Native assembly inputs. The shell supplies its existing immutable local
/// lifecycle authority; network and credential services never come from IPC.
pub struct NativeClientConfig {
    pub bundle: Url,
    pub state: ClientState,
    pub record: ConnectionRecord,
    pub store: ConnectionStore,
    pub local_origin: Arc<Mutex<Option<LoopbackOrigin>>>,
}
pub fn attach(
    app: &AppHandle,
    config: NativeClientConfig,
    transport: Arc<dyn Transport>,
    credentials: Arc<dyn Credentials>,
    passkeys: Arc<dyn Passkeys>,
) -> Arc<Coordinator> {
    let view = Arc::new(NativeView {
        app: app.clone(),
        bundle: config.bundle,
        local_origin: config.local_origin,
        presentation: Mutex::new(Presentation {
            state: config.state.clone(),
            account: false,
            staging: false,
            pending_commit: None,
            remote: None,
            locale: None,
        }),
        windows: Arc::new(tokio::sync::Mutex::new(())),
        navigation: Mutex::new(None),
        views: Mutex::new(Views::default()),
    });
    let coordinator = Arc::new(
        Coordinator::new(
            config.state,
            config.record,
            config.store,
            transport,
            credentials,
            passkeys,
            view.clone(),
        )
        .with_browser(Arc::new(DefaultBrowser)),
    );
    app.manage(Client {
        coordinator: coordinator.clone(),
        view,
        presentations: Mutex::new(PresentationQueue::default()),
    });
    // The selected instance's section is reread every few seconds while the
    // popup shows it, or while an action sent from this shell is unsettled.
    let polling = app.clone();
    tauri::async_runtime::spawn(async move {
        loop {
            tokio::time::sleep(INSTANCE_UPDATE_POLL).await;
            let open = polling
                .get_webview_window(crate::update_popover::UPDATE_POPOVER)
                .is_some_and(|window| window.is_visible().unwrap_or(false));
            let Some(client) = polling.try_state::<Client>() else {
                return;
            };
            client.coordinator.poll_instance_update(open).await;
        }
    });
    coordinator
}
/// Provider sign-in opens in the person's default browser, never in a WebView:
/// providers refuse embedded sign-in, and the browser keeps its own sessions.
struct DefaultBrowser;
impl SystemBrowser for DefaultBrowser {
    fn open(&self, url: &Url) -> Result<(), ClientError> {
        // Only Supabase's authorization page, over https, reaches the browser.
        if url.scheme() != "https" || url.path() != "/auth/v1/authorize" {
            return Err(ClientError::new("invalid_auth_provider"));
        }
        tauri_plugin_opener::open_url(url.as_str(), None::<&str>).map_err(|_| ClientError::new("provider_unavailable"))
    }
    fn callback_copy(&self, locale: UiLocale) -> CallbackCopy {
        provider_callback_copy(locale)
    }
}

/// The browser's last page in the asking page's language, from the product catalogs.
fn provider_callback_copy(locale: UiLocale) -> CallbackCopy {
    let tag = match locale {
        UiLocale::En => "en",
        UiLocale::Zh => "zh",
    };
    let catalog = crate::native_catalog_for_locales([tag.to_owned()]).provider_callback;
    CallbackCopy {
        done: catalog.done,
        failed: catalog.failed,
    }
}

const INSTANCE_UPDATE_POLL: std::time::Duration = std::time::Duration::from_secs(4);

/// Read the selected instance's update state now (the popup just opened).
pub(crate) fn refresh_instance_update(app: &AppHandle) {
    if let Some(client) = app.try_state::<Client>() {
        let coordinator = client.coordinator.clone();
        tauri::async_runtime::spawn(async move { coordinator.refresh_instance_update().await });
    }
}
fn compatible_asset(bytes: &[u8]) -> bool {
    // The producer emits this fixed static assembly marker. Do not inspect DOM.
    std::str::from_utf8(bytes)
        .is_ok_and(|html| html.contains("<meta name=\"avibe-desktop-client-version\" content=\"1\""))
}
pub(crate) fn local_origin(app: &AppHandle, origin: Option<LoopbackOrigin>) {
    if let Some(client) = app.try_state::<Client>() {
        client.coordinator.local_origin(origin);
        let coordinator = client.coordinator.clone();
        tauri::async_runtime::spawn(async move {
            coordinator.refresh_local().await;
        });
    }
}
pub(crate) fn updated(app: &AppHandle, update: serde_json::Value) {
    if let Some(client) = app.try_state::<Client>() {
        client.coordinator.desktop_update(update);
    }
}
pub fn initial_script(app: &AppHandle) -> Option<String> {
    let client = app.try_state::<Client>()?;
    Some(client.view.script(true, None))
}
pub(crate) fn native_copy(key: &str) -> Option<String> {
    let chinese = sys_locale::get_locales()
        .find_map(|locale| {
            if locale.starts_with("zh") {
                Some(true)
            } else if locale.starts_with("en") {
                Some(false)
            } else {
                None
            }
        })
        .unwrap_or(false);
    let source = if chinese {
        crate::ZH_PRODUCT_CATALOG
    } else {
        crate::EN_PRODUCT_CATALOG
    };
    serde_json::from_str::<serde_json::Value>(source).ok()?["desktopClient"][key]
        .as_str()
        .map(str::to_owned)
}
pub(crate) fn menu_present(app: &AppHandle, view: &'static str) {
    let _ = present_guarded(app, view);
}

pub(crate) fn present_guarded(app: &AppHandle, view: &'static str) -> bool {
    let Some(client) = app.try_state::<Client>() else {
        return false;
    };
    present_owned(
        app,
        view,
        client.coordinator.presentation_owner(),
        true,
        None,
        None,
        None,
    )
}
pub(crate) fn return_local_for_link(app: &AppHandle) {
    let Some(client) = app.try_state::<Client>() else {
        return;
    };
    let coordinator = client.coordinator.clone();
    retire_stale_link(app);
    let Some(ticket) = app
        .state::<Mutex<crate::DeepLinks>>()
        .lock()
        .ok()
        .and_then(|mut links| {
            links.claim_local_return(coordinator.state().generation, coordinator.presentation_owner())
        })
    else {
        return;
    };
    if client.view.presentation.lock().expect("presentation").account {
        {
            let mut queue = client.presentations.lock().expect("presentation intent");
            queue.enqueue("account", ticket.action_owner, true, None);
            queue.link = Some((queue.pending.expect("new intent").id, ticket));
        }
        schedule_presentation(app);
        ensure_presentation_retry(app);
        return;
    }
    let app = app.clone();
    tauri::async_runtime::spawn(async move {
        let result = coordinator
            .return_local_if_current(ticket.expected_generation, ticket.action_owner)
            .await;
        let accepted_owner = result
            .ok()
            .filter(|owner| coordinator.presentation_epoch(*owner).is_ok());
        if let Ok(mut links) = app.state::<Mutex<crate::DeepLinks>>().lock() {
            links.finish_local_return(&ticket, accepted_owner);
        }
        crate::apply_pending_deep_link(&app);
    });
}

/// The queue owns the single dormant link continuation. Consumed is not
/// approved: it remains correlated until a URL arrives or authority retires.
fn retire_stale_link(app: &AppHandle) {
    let Some(client) = app.try_state::<Client>() else {
        return;
    };
    let mut queue = client.presentations.lock().expect("presentation intent");
    queue.current(client.coordinator.presentation_owner());
    let Some((id, ticket)) = &queue.link else { return };
    let Some(links) = app.try_state::<Mutex<crate::DeepLinks>>() else {
        return;
    };
    let mut links = links.lock().expect("deep links");
    if !queue.owns_intent(*id) || !links.owns_local_return(ticket) {
        let (id, ticket) = queue.link.take().expect("link continuation");
        if queue.owns_intent(id) {
            queue.cancel();
        }
        links.finish_local_return(&ticket, None);
    }
}
/// Per-physical-window callbacks cannot revive a retired document with the reused main label.
pub fn window_created(app: &AppHandle) -> Option<u64> {
    let client = app.try_state::<Client>()?;
    retire_claim_presentation(app);
    let window = client
        .presentations
        .lock()
        .expect("presentation intent")
        .window_created();
    retire_stale_link(app);
    let mut views = client.view.views.lock().expect("views");
    views.runtime = Some(RuntimeDocument {
        label: MAIN_WINDOW.into(),
        window,
        destination: client.view.bundle.clone(),
        disposable: false,
        directory: None,
        owner: client.coordinator.presentation_owner(),
        document: 0,
    });
    drop(views);
    Some(window)
}
pub(crate) fn window_destroyed(app: &AppHandle) {
    let Some(client) = app.try_state::<Client>() else {
        return;
    };
    let owner = client.coordinator.window_destroyed();
    {
        let mut queue = client.presentations.lock().expect("presentation intent");
        queue.window_created();
        queue.pending = None;
        queue.source = None;
    }
    retire_stale_link(app);
    {
        let mut views = client.view.views.lock().expect("views");
        views.account = None;
        let runtime = views.runtime.take();
        let candidate = views.candidate.take();
        views.retired.extend(runtime);
        views.retired.extend(candidate);
        views.terminal = None;
    }
    {
        let mut state = client.view.presentation.lock().expect("presentation");
        state.account = false;
        state.remote = None;
        state.staging = false;
        state.pending_commit = None;
    }
    let view = client.view.clone();
    let coordinator = client.coordinator.clone();
    tauri::async_runtime::spawn(async move {
        // No cookies or URL snapshot can revive the destroyed document.
        // Existing escape/storage ownership persists the safe-local fallback.
        let _ = coordinator.return_local_if_current(owner, owner).await;
        let _windows = view.windows.lock().await;
        let retired = std::mem::take(&mut view.views.lock().expect("views").retired);
        for role in retired {
            let _ = view.retire_runtime(role).await;
        }
    });
}
pub fn document_started(app: &AppHandle, window: u64, url: &Url) {
    let Some(client) = app.try_state::<Client>() else {
        return;
    };
    if client.presentations.lock().expect("presentation intent").window != window {
        return;
    }
    runtime_document_started(app, MAIN_WINDOW, url);
}
pub(crate) fn runtime_document_started(app: &AppHandle, label: &str, url: &Url) {
    let Some(client) = app.try_state::<Client>() else {
        return;
    };
    let retiring_apps = client
        .view
        .views
        .lock()
        .expect("views")
        .runtime
        .as_ref()
        .is_some_and(|role| role.label == label);
    if retiring_apps {
        crate::app_windows::close_all(app);
    }
    let current = {
        let mut views = client.view.views.lock().expect("views");
        if let Some(runtime) = views.runtime.as_mut().filter(|runtime| runtime.label == label) {
            runtime.document += 1;
            if let Some(sheet) = views
                .account
                .as_mut()
                .filter(|a| account_view(&a.destination, &client.view.bundle) == Some("claim"))
            {
                // A same-origin reload is still a different Workbench. Don't transfer the original action.
                sheet.returning = None;
                sheet.claim.take();
            }
        }
        if let Some(candidate) = views.candidate.as_mut().filter(|candidate| candidate.label == label) {
            candidate.document += 1;
        }
        views.account.is_none() && views.runtime.as_ref().is_some_and(|runtime| runtime.label == label)
    };
    if current {
        let mut queue = client.presentations.lock().expect("presentation intent");
        let window = queue.window;
        queue.document_started(window, url);
    }
    retire_stale_link(app);
}
pub fn page_loaded(app: &AppHandle) {
    if let Some(client) = app.try_state::<Client>() {
        client.view.changed(&client.coordinator.state());
    }
}
/// Physical main survives child creation. Callers must choose the actual role,
/// rather than relying on Tauri's single-WebView-window conversion.
pub fn active_webview(app: &AppHandle) -> Option<Webview> {
    let Some(client) = app.try_state::<Client>() else {
        return app.get_webview(MAIN_WINDOW);
    };
    let views = client.view.views.lock().expect("views");
    let label = views
        .account
        .as_ref()
        .filter(|view| view.loaded)
        .map(|view| &view.label)
        .or(views.runtime.as_ref().map(|view| &view.label))?;
    app.get_webview(label)
}
/// The web view the user sees, for showing and focusing. Unlike
/// `active_webview`, a shown account document that is reloading is still the
/// answer: revealing or focusing the Runtime under it would send keystrokes to
/// a covered Workbench.
pub(crate) fn displayed_webview(app: &AppHandle) -> Option<Webview> {
    let Some(client) = app.try_state::<Client>() else {
        return app.get_webview(MAIN_WINDOW);
    };
    let views = client.view.views.lock().expect("views");
    let label = views.displayed()?;
    app.get_webview(label)
}
pub(crate) fn runtime_webview(app: &AppHandle) -> Option<Webview> {
    let Some(client) = app.try_state::<Client>() else {
        return app.get_webview(MAIN_WINDOW);
    };
    let label = client.view.views.lock().expect("views").runtime.as_ref()?.label.clone();
    app.get_webview(&label)
}
pub(crate) fn is_current_runtime<R: tauri::Runtime>(view: &Webview<R>) -> bool {
    let app = view.app_handle();
    if !enabled(app) {
        return view.label() == MAIN_WINDOW;
    }
    let client = app.state::<Client>();
    let views = client.view.views.lock().expect("views");
    let role = views.account.is_none()
        && views
            .runtime
            .as_ref()
            .is_some_and(|runtime| runtime.label == view.label());
    drop(views);
    let state = client.view.presentation.lock().expect("presentation");
    role && !state.account && !state.staging && state.pending_commit.is_none() && client.view.windows.try_lock().is_ok()
}
/// The shown Workbench document and the Runtime origin it must have, while it
/// is the current Runtime (no account page, not staging or committing) and its
/// URL is at that origin. Ask Vibey (macOS Services) delivers into nothing else.
#[cfg(target_os = "macos")]
pub(crate) fn current_workbench(app: &AppHandle) -> Option<(Webview, Url)> {
    let view = runtime_webview(app)?;
    if !is_current_runtime(&view) {
        return None;
    }
    let client = app.try_state::<Client>()?;
    let origin = {
        let state = client.view.presentation.lock().ok()?;
        match &state.remote {
            Some(remote) => remote.clone(),
            None => client.view.local_origin.lock().ok()?.as_ref()?.navigation_url(),
        }
    };
    let actual = view.url().ok()?;
    let same = matches!(actual.scheme(), "http" | "https")
        && actual.origin() == origin.origin()
        && actual.username().is_empty()
        && actual.password().is_none();
    same.then_some((view, origin))
}

/// App windows inherit one published Runtime document, never a URL supplied by
/// a page. Keep the receipt private to native owners; it grants no client IPC.
#[derive(Clone)]
pub(crate) struct AppRuntime {
    label: String,
    window: u64,
    owner: u64,
    document: u64,
    action: u64,
    pub origin: Url,
    pub disposable: bool,
    pub epoch: Epoch,
}

impl AppRuntime {
    pub fn current(&self, app: &AppHandle) -> bool {
        self.receipt_current(app)
            && app
                .get_webview(&self.label)
                .is_some_and(|view| view.url().is_ok_and(|url| app_origin_matches(&self.origin, &url)))
    }

    /// `current` without the WebView's URL: the epoch, the presentation owner
    /// and the published Runtime role. It takes only atomics and std mutexes
    /// that no holder keeps across a wait for the main thread, so it may be
    /// called on the main thread, inside a WebView call, where a URL getter
    /// would re-enter the event loop. The caller checks the URL itself.
    pub fn receipt_current(&self, app: &AppHandle) -> bool {
        let Some(client) = app.try_state::<Client>() else {
            return false;
        };
        if self.epoch.check().is_err() || client.coordinator.presentation_owner() != self.action {
            return false;
        }
        let views = client.view.views.lock().expect("views");
        let role = views.account.is_none()
            && views.runtime.as_ref().is_some_and(|role| {
                role.label == self.label
                    && role.window == self.window
                    && role.owner == self.owner
                    && role.document == self.document
                    && role.disposable == self.disposable
            });
        drop(views);
        let state = client.view.presentation.lock().expect("presentation");
        let admitted = role && !state.account && !state.staging && state.pending_commit.is_none();
        drop(state);
        admitted
    }

    pub fn webview(&self, app: &AppHandle) -> Option<Webview> {
        self.current(app).then(|| app.get_webview(&self.label)).flatten()
    }

    pub fn belongs_to(&self, label: &str, window: u64) -> bool {
        self.label == label && self.window == window
    }

    pub fn same_document(&self, other: &Self) -> bool {
        self.label == other.label
            && self.window == other.window
            && self.owner == other.owner
            && self.document == other.document
            && self.origin == other.origin
            && self.epoch.value == other.epoch.value
            && self.action == other.action
    }
}

fn app_origin_matches(origin: &Url, url: &Url) -> bool {
    matches!(url.scheme(), "http" | "https")
        && url.origin() == origin.origin()
        && url.username().is_empty()
        && url.password().is_none()
}

pub(crate) fn app_runtime(view: &Webview) -> Option<AppRuntime> {
    let app = view.app_handle();
    let client = app.try_state::<Client>()?;
    let actual = view.url().ok()?;
    let state = client.view.presentation.lock().ok()?;
    let origin = match &state.remote {
        Some(remote) => remote.clone(),
        None => client.view.local_origin.lock().ok()?.as_ref()?.navigation_url(),
    };
    drop(state);
    if !app_origin_matches(&origin, &actual) {
        return None;
    }
    let views = client.view.views.lock().ok()?;
    let role = views.runtime.as_ref().filter(|role| role.label == view.label())?;
    let runtime = AppRuntime {
        label: role.label.clone(),
        window: role.window,
        owner: role.owner,
        document: role.document,
        action: client.coordinator.presentation_owner(),
        origin,
        disposable: role.disposable,
        epoch: client.coordinator.epoch(),
    };
    drop(views);
    // Capture the published source even while another App owns the serializer.
    // This receipt grants no effects: launch waits for that same lock and
    // revalidates the exact role, document and action before using it.
    runtime.current(app).then_some(runtime)
}

pub(crate) async fn lock_app_runtime(app: &AppHandle) -> Option<tokio::sync::OwnedMutexGuard<()>> {
    let windows = app.try_state::<Client>()?.view.windows.clone();
    Some(windows.lock_owned().await)
}
pub(crate) fn is_main_view(app: &AppHandle, label: &str) -> bool {
    if label == MAIN_WINDOW {
        return true;
    }
    app.try_state::<Client>().is_some_and(|client| {
        let views = client.view.views.lock().expect("views");
        views.runtime.as_ref().is_some_and(|view| view.label == label)
            || views.candidate.as_ref().is_some_and(|view| view.label == label)
            || views.account.as_ref().is_some_and(|view| view.label == label)
    })
}
/// The per-window callback captures the generation at construction. A late
/// event from a retired window with the reused main label cannot complete a
/// successor's navigation, including a reconnect to the same instance.
pub(crate) fn navigation_loaded(app: &AppHandle, label: &str, generation: u64, url: &Url) {
    let Some(client) = app.try_state::<Client>() else {
        return;
    };
    let mut pending = client.view.navigation.lock().expect("navigation");
    if pending.as_ref().is_some_and(|pending| {
        pending.label == label
            && pending.epoch.value == generation
            && pending.epoch.check().is_ok()
            && if account_view(&pending.destination, &client.view.bundle).is_some() {
                account_view(url, &client.view.bundle).is_some()
            } else if pending.destination == client.view.bundle {
                *url == client.view.bundle
            } else {
                pending.destination.origin() == url.origin()
            }
    }) {
        if let Some(pending) = pending.take() {
            let _ = pending.completed.send(Ok(()));
        }
    }
}
fn trusted_page(client: &Client, window: &Webview) -> bool {
    if window.window().label() != MAIN_WINDOW {
        return false;
    }
    let destination = client
        .view
        .views
        .lock()
        .expect("views")
        .account
        .as_ref()
        .filter(|owned| owned.label == window.label() && owned.admitted)
        .map(|owned| owned.destination.clone());
    destination.is_some_and(|destination| {
        window
            .url()
            .is_ok_and(|url| url == destination && account_view(&url, &client.view.bundle).is_some())
    })
}
/// Who may drive this computer's updater through the desktop client: the account
/// page in the main window, or the update popup on its own page while it is open.
/// `desktop_client_request` has already checked the caller; this is the updater's
/// own second check.
fn updater_caller<R: tauri::Runtime>(
    app: &AppHandle<R>,
    bundle: &Url,
    main_account_page: impl FnOnce() -> bool,
) -> bool {
    main_account_page()
        || app
            .get_webview_window(crate::update_popover::UPDATE_POPOVER)
            .is_some_and(|window| crate::update_popover::is_page(window.as_ref(), bundle))
}

/// Private claim commands have their own capability and direct reply. The popup and
/// general account state channel do not even have the command in their allowlist.
#[tauri::command]
pub(crate) async fn desktop_claim_review(
    window: Webview,
    app: AppHandle,
    request: ClaimRequest,
) -> Result<Option<serde_json::Value>, ClientError> {
    let deny = || ClientError::new("untrusted_desktop_caller");
    let client = app.try_state::<Client>().ok_or_else(deny)?;
    let shell = app.try_state::<Shell>().ok_or_else(deny)?;
    let physical = window.window();
    if physical.label() != MAIN_WINDOW || shell.exit_cleanup_started.load(std::sync::atomic::Ordering::SeqCst) {
        return Err(deny());
    }
    // Snapshot before any platform getter. A hide or same-URL reload during the getter
    // must not be recaptured as the document that made the request.
    let observe = matches!(&request.action, ClaimAction::ObserveCurrentBinding {});
    let open = matches!(&request.action, ClaimAction::Open {});
    let generation = request.expected_generation;
    let (receipt, observed) = {
        let views = client.view.views.lock().expect("views");
        let role = views.account.as_ref().ok_or_else(deny)?;
        (
            role.private_receipt(window.label(), &client.view.bundle, observe)
                .ok_or_else(deny)?,
            role.claim.as_ref().and_then(|d| d.caller().ok()),
        )
    };
    if client.presentations.lock().expect("presentation intent").window != receipt.window {
        return Err(deny());
    }
    let visible = || physical.is_visible().unwrap_or(false) && !physical.is_minimized().unwrap_or(true);
    if !visible() || window.url().ok().as_ref() != Some(&receipt.destination) {
        // A real observed hide/navigation is retirement even if the next OS
        // sample sees a restored window. Never reacquire a successor to retire.
        if let Some(observed) = observed {
            observed.retire();
        }
        return Err(deny());
    }
    let cancel = request.action.cancels();
    let opens_document = request.action.opens_document();
    let locale = receipt
        .destination
        .query_pairs()
        .find_map(|(k, v)| (k == "locale").then(|| v.into_owned()))
        .ok_or_else(deny)?;
    let caller = {
        // Cancel only attenuates the matching receipt. Keep all the actual
        // role checks, without refusing revocation because presentation I/O
        // has the serializer. No private model is returned by cancellation.
        let _windows = if cancel {
            None
        } else {
            Some(client.view.windows.try_lock().map_err(|_| deny())?)
        };
        let mut views = client.view.views.lock().expect("views");
        let presentation = client.view.presentation.lock().expect("presentation");
        if shell.exit_cleanup_started.load(std::sync::atomic::Ordering::SeqCst)
            || !presentation.account
            || presentation.staging
            || presentation.pending_commit.is_some()
        {
            return Err(deny());
        }
        let role = views
            .account
            .as_mut()
            .filter(|a| a.private_receipt(window.label(), &client.view.bundle, observe).as_ref() == Some(&receipt))
            .ok_or_else(deny)?;
        if open && !role.sheet_open.take(generation, receipt.visibility) {
            return Err(deny());
        }
        if observe {
            role.sheet_open.unbound = None;
        }
        if opens_document {
            role.claim = Some(
                client
                    .coordinator
                    .claim_document(request.expected_generation)
                    .map_err(|_| deny())?,
            );
        }
        role.claim.as_ref().ok_or_else(deny)?.caller().map_err(|_| deny())?
    };
    // The short opening lease exists already, even before observation dispatch.
    // Monitor the original physical window during I/O, not just after it returns.
    if opens_document {
        watch_claim_visibility(physical.clone(), caller.clone());
    }
    let reply = client
        .coordinator
        .claim_review(&shell.host, &caller, &locale, request)
        .await;
    let reply = reply.map_err(|_| {
        if opens_document {
            caller.retire(); // this invocation owns its fresh opening lease
        }
        // An unadmitted action owns no result or ongoing operation. In particular,
        // presentation contention cannot let it revoke another action's receipt.
        deny()
    })?;
    // This read cannot carry a reply through a replacement or into a hidden window.
    if !visible()
        || window.url().ok().as_ref() != Some(&receipt.destination)
        || shell.exit_cleanup_started.load(std::sync::atomic::Ordering::SeqCst)
        || client.presentations.lock().expect("presentation intent").window != receipt.window
    {
        caller.retire();
        return Err(deny());
    }
    let _windows = if cancel {
        None
    } else {
        Some(client.view.windows.try_lock().map_err(|_| {
            caller.retire();
            deny()
        })?)
    };
    let mut views = client.view.views.lock().expect("views");
    let state = client.view.presentation.lock().expect("presentation");
    if !(state.account
        && !state.staging
        && state.pending_commit.is_none()
        && views
            .account
            .as_ref()
            .and_then(|a| a.private_receipt(window.label(), &client.view.bundle, observe))
            .as_ref()
            == Some(&receipt))
    {
        caller.retire();
        return Err(deny());
    }
    // Only an admitted, qualified private reply can carry account-specific
    // errors or retry information. Admission failures carry no such facts.
    let delivered = reply.deliver().inspect_err(|error| {
        if error.code == avibe_runtime_host::instance_client::machine::TOO_OLD {
            // Published after this command lets go of the presentation locks.
            let coordinator = client.coordinator.clone();
            tauri::async_runtime::spawn(async move { coordinator.note_runtime_too_old() });
        }
    })?;
    if observe && account_view(&receipt.destination, &client.view.bundle) == Some("claim") {
        let role = views.account.as_mut().ok_or_else(deny)?;
        role.sheet_open
            .observed(generation, receipt.visibility, delivered.as_ref());
    }
    Ok(delivered)
}
// Native close/replacement/reload revoke synchronously. OS-level Hide/Minimize need
// not generate a Tauri visibility event on either desktop. While this one attempt
// exists only, observe its physical window (never query or change keyboard focus).
fn watch_claim_visibility(window: tauri::Window, caller: ClaimCaller) {
    tauri::async_runtime::spawn(async move {
        loop {
            tokio::time::sleep(std::time::Duration::from_millis(250)).await;
            if !caller.monitoring() {
                return;
            }
            if !window.is_visible().unwrap_or(false) || window.is_minimized().unwrap_or(true) {
                caller.retire();
                return;
            }
        }
    });
}
#[derive(PartialEq, Eq)]
struct ClaimReceipt {
    window: u64,
    document: u64,
    visibility: u64,
    destination: Url,
}
#[derive(Default)]
struct SheetOpen {
    unbound: Option<(u64, u64)>,
    spent: bool,
}
impl SheetOpen {
    fn observed(&mut self, generation: u64, visibility: u64, result: Option<&serde_json::Value>) {
        self.unbound = (!self.spent
            && result.and_then(|r| r.get("state")).and_then(|v| v.as_str()) == Some("no_published_binding"))
        .then_some((generation, visibility));
    }
    fn take(&mut self, generation: u64, visibility: u64) -> bool {
        if self.spent || self.unbound.take() != Some((generation, visibility)) {
            return false;
        }
        self.spent = true;
        true
    }
}
impl AccountDocument {
    fn claim_receipt(&self, label: &str, bundle: &Url) -> Option<ClaimReceipt> {
        self.private_receipt(label, bundle, false)
    }
    fn private_receipt(&self, label: &str, bundle: &Url, observe: bool) -> Option<ClaimReceipt> {
        if !self.shown
            || !self.admitted
            || !self.loaded
            || self.document != 1
            || self.token.is_none()
            || self.label != label
            || !matches!(
                (bundle.scheme(), bundle.host_str(), bundle.port()),
                ("tauri", Some("localhost"), None) | ("http", Some("tauri.localhost"), None)
            )
            || !match account_view(&self.destination, bundle) {
                Some("claim") => self.returning.is_some(),
                Some("account") => observe,
                _ => false,
            }
        {
            return None;
        }
        Some(ClaimReceipt {
            window: self.window,
            document: self.document,
            visibility: self.claim_visibility,
            destination: self.destination.clone(),
        })
    }
}
/// Account recovery belongs to the ordinary account, not a claim lifetime. Reuse
/// the visible bundled document's role stamp, but never reserve or retire a claim.
fn in_recovery_document<T>(
    window: &Webview,
    shell: &Shell,
    client: &Client,
    stamp: &ClaimReceipt,
    admitted: impl FnOnce() -> Result<T, ClientError>,
) -> Result<T, ClientError> {
    let deny = || ClientError::new("untrusted_desktop_caller");
    let physical = window.window();
    if physical.label() != MAIN_WINDOW
        || !physical.is_visible().unwrap_or(false)
        || physical.is_minimized().unwrap_or(true)
        || window.url().ok().as_ref() != Some(&stamp.destination)
        || client.presentations.lock().expect("presentation intent").window != stamp.window
    {
        return Err(deny());
    }
    let _windows = client.view.windows.try_lock().map_err(|_| deny())?;
    let views = client.view.views.lock().expect("views");
    let p = client.view.presentation.lock().expect("presentation");
    if shell.exit_cleanup_started.load(std::sync::atomic::Ordering::SeqCst)
        || !p.account
        || p.staging
        || p.pending_commit.is_some()
        || views
            .account
            .as_ref()
            .and_then(|a| a.claim_receipt(window.label(), &client.view.bundle))
            .as_ref()
            != Some(stamp)
    {
        return Err(deny());
    }
    admitted()
}

#[tauri::command]
pub(crate) async fn desktop_account_recovery(
    window: Webview,
    app: AppHandle,
    request: AccountRecoveryRequest,
) -> Result<serde_json::Value, ClientError> {
    let deny = || ClientError::new("untrusted_desktop_caller");
    let client = app.try_state::<Client>().ok_or_else(deny)?;
    let shell = app.try_state::<Shell>().ok_or_else(deny)?;
    let stamp = client
        .view
        .views
        .lock()
        .expect("views")
        .account
        .as_ref()
        .and_then(|a| a.claim_receipt(window.label(), &client.view.bundle))
        .ok_or_else(deny)?;
    let work = in_recovery_document(&window, &shell, &client, &stamp, || {
        client.coordinator.admit_account_recovery(request)
    })?;
    let reply = work.wait().await;
    in_recovery_document(&window, &shell, &client, &stamp, || reply?.deliver())
}

pub(crate) fn retire_claim_presentation(app: &AppHandle) {
    if let Some(client) = app.try_state::<Client>() {
        if let Some(role) = client.view.views.lock().expect("views").account.as_mut() {
            role.claim_visibility += 1;
            role.claim.take();
        }
    }
}

#[tauri::command]
pub async fn desktop_client_request(
    window: Webview,
    app: AppHandle,
    request: ClientRequest,
) -> Result<ClientReply, String> {
    let client = app.try_state::<Client>().ok_or("desktop_client_unavailable")?;
    // Popup requests never enter the Account receipt/return owner. Keep the
    // original native receiver across GetState's credential-I/O suspension;
    // a new popup with the same label is a different receiver.
    if window.label() == crate::update_popover::UPDATE_POPOVER {
        let receipt = crate::update_popover::admit(&window, &client.view.bundle, &request.action)
            .ok_or("untrusted_desktop_caller")?;
        let reply = client.coordinator.request(request).await;
        return if crate::update_popover::still_current(&window, &client.view.bundle, &receipt) {
            Ok(reply)
        } else {
            Err("untrusted_desktop_caller".into())
        };
    }
    if !trusted_page(&client, &window) {
        return Err("untrusted_desktop_caller".into());
    }
    let coordinator = client.coordinator.clone();
    let receiver = window.url().map_err(|_| "untrusted_desktop_caller")?;
    // This computer's Runtime, upgraded with the Runtime host: from the account
    // document (Instances, Account) or its claim view, never the popup.
    if matches!(request.action, ClientAction::UpgradeLocalRuntime {}) {
        let shell = app.try_state::<Shell>().ok_or("desktop_client_unavailable")?;
        let result = if request.version == 1 && request.expected_generation == coordinator.state().generation {
            coordinator.upgrade_local_runtime(&shell.host).await
        } else {
            Err(ClientError::new("stale_generation"))
        };
        if !trusted_page(&client, &window) || window.url().ok().as_ref() != Some(&receiver) {
            return Err("untrusted_desktop_caller".into());
        }
        let (result, error) = match result {
            Ok(result) => (Some(result), None),
            Err(error) => (None, Some(error)),
        };
        return Ok(ClientReply {
            version: 1,
            request_id: request.request_id,
            ok: error.is_none(),
            state: coordinator.state(),
            result,
            error,
        });
    }
    if matches!(request.action, ClientAction::FinishTotpEnrollment { .. })
        && request.version == 1
        && !request.request_id.is_empty()
        && request.request_id.len() <= 128
        && request.expected_generation == coordinator.state().generation
    {
        // Finalization can issue material without advancing the action owner.
        // Serialize its admission with an approved presentation's actual start.
        // Once replacement/escape has started it is too late to issue material;
        // while it is merely queued, this explicit action supersedes it.
        let admission = client.view.windows.try_lock().ok();
        if admission.is_none()
            || client.view.presentation.lock().expect("presentation").staging
            || !trusted_page(&client, &window)
        {
            return Ok(ClientReply {
                version: 1,
                request_id: request.request_id,
                ok: false,
                state: coordinator.state(),
                result: None,
                error: Some(ClientError::new("busy")),
            });
        }
        client.presentations.lock().expect("presentation intent").cancel();
        retire_stale_link(&app);
    }
    let get_state = matches!(request.action, ClientAction::GetState);
    let document = {
        let mut queue = client.presentations.lock().expect("presentation intent");
        if let ClientAction::SignOut { intent_id: Some(id) } = &request.action {
            if request.version != 1
                || request.request_id.is_empty()
                || request.request_id.len() > 128
                || request.expected_generation != coordinator.state().generation
                || !queue.consume_logout(
                    id,
                    coordinator.presentation_owner(),
                    request.expected_generation,
                    &receiver,
                )
            {
                return Ok(ClientReply {
                    version: 1,
                    request_id: request.request_id,
                    ok: false,
                    state: coordinator.state(),
                    result: None,
                    error: Some(ClientError::new("invalid_sign_out_intent")),
                });
            }
        } else if !get_state {
            queue.logout = None;
        }
        (queue.window, queue.document)
    };
    let mut reply = coordinator.request(request).await;
    if !trusted_page(&client, &window) || {
        let queue = client.presentations.lock().expect("presentation intent");
        (queue.window, queue.document) != document
    } {
        return Err("untrusted_desktop_caller".into());
    }
    if get_state && reply.ok && trusted_page(&client, &window) && window.url().ok().as_ref() == Some(&receiver) {
        let mut queue = client.presentations.lock().expect("presentation intent");
        if (queue.window, queue.document) == document {
            reply.result = queue.logout_result(coordinator.presentation_owner(), reply.state.generation, &receiver);
        }
    }
    Ok(reply)
}

/// Only the current main document can request enumerated presentation. This is
/// separate from navigation admission and never performs the presented action.
pub fn caller_can_present(app: &AppHandle, label: &str, current: &Url) -> bool {
    let Some(client) = app.try_state::<Client>() else {
        return false;
    };
    if client.view.windows.try_lock().is_err() {
        return false;
    }
    if active_webview(app).is_none_or(|view| view.label() != label) {
        return false;
    }
    let (account, remote) = {
        let view = client.view.presentation.lock().expect("presentation");
        if view.staging || view.pending_commit.is_some() {
            return false;
        }
        (view.account, view.remote.clone())
    };
    if account {
        return app.get_webview(label).is_some_and(|view| trusted_page(&client, &view));
    }
    if let Some(origin) = remote {
        return current.origin() == origin.origin();
    }
    client
        .view
        .local_origin
        .lock()
        .ok()
        .and_then(|origin| origin.clone())
        .is_some_and(|origin| origin.matches_url_origin(current))
}
/// Source authority is independent of physical-window admission. A committed
/// document can request presentation while the window serializer is occupied,
/// but a hidden, not-yet-published candidate cannot acquire that authority.
fn claim_source_allowed(state: &Presentation, local: Option<&LoopbackOrigin>, source: &Url) -> bool {
    !state.account
        && !state.staging
        && state.pending_commit.is_none()
        && state.state.connection.pending.is_none()
        && state.remote.is_none()
        && state.state.connection.status == ConnectionStatus::Local
        && state.state.connection.committed.is_local()
        && local.is_some_and(|origin| origin.matches_url_origin(source))
        && source.username().is_empty()
        && source.password().is_none()
        // Enumerate the Workbench entry routes; never infer membership from a shared origin or a prefix.
        && matches!(source.path(), "/" | "/setup" | "/settings/devices" | "/settings/remote-access")
}

fn presentation_source_allowed(
    view: &Presentation,
    bundle: &Url,
    local: Option<&LoopbackOrigin>,
    committed_receipt: Option<u64>,
    label: &str,
    current: &Url,
) -> bool {
    if label != MAIN_WINDOW
        || view.staging
        || (view.pending_commit.is_some() && view.pending_commit != committed_receipt)
    {
        return false;
    }
    if view.account {
        return account_view(current, bundle).is_some();
    }
    match (&view.remote, &view.state.connection.committed) {
        (Some(origin), ClientTarget::Remote { canonical_origin, .. }) => {
            origin.origin().ascii_serialization() == *canonical_origin && current.origin() == origin.origin()
        }
        (None, ClientTarget::Local { .. }) => local.is_some_and(|origin| origin.matches_url_origin(current)),
        _ => false,
    }
}

/// Only native-generated guarded requests carry a correlation token. It is a
/// presentation receipt, never updater authority, and has no uncorrelated
/// fallback if it is stale or malformed.
fn presentation_destination(destination: &Url) -> Option<(&'static str, Option<String>, Option<&'static str>)> {
    let mut base = destination.clone();
    let mut token = None;
    let mut locale = None;
    for (key, value) in destination.query_pairs() {
        match key.as_ref() {
            "native_request" if token.is_none() && !value.is_empty() && value.len() <= 128 => {
                token = Some(value.into_owned())
            }
            "locale" if locale.is_none() => {
                locale = Some(match value.as_ref() {
                    "en" => "en",
                    "zh" => "zh",
                    _ => return None,
                })
            }
            _ => return None,
        }
    }
    base.set_query(None);
    let view = presentation_view(&base).or_else(|| (base.as_str() == crate::updater::OPEN_URL).then_some("updates"))?;
    if view == "claim" && token.is_some() {
        return None;
    }
    Some((view, token, locale))
}
fn account_url(bundle: &Url, view: &str, locale: Option<&str>) -> Option<Url> {
    let mut url = bundle.join("/account/index.html").ok()?;
    url.query_pairs_mut()
        .append_pair("view", view)
        .append_pair("locale", locale.unwrap_or_else(|| system_locale()));
    account_view(&url, bundle)?;
    Some(url)
}
fn system_locale() -> &'static str {
    if sys_locale::get_locales()
        .next()
        .is_some_and(|locale| locale.starts_with("zh"))
    {
        "zh"
    } else {
        "en"
    }
}

/// Native presentation requests share one source check in every shell assembly.
/// Returning true means the URL was consumed, never that an action was approved.
pub fn presentation_request(
    app: &AppHandle,
    label: &str,
    current: impl FnOnce() -> Option<Url>,
    destination: &Url,
) -> bool {
    if !enabled(app) || !is_main_view(app, label) {
        return false;
    }
    let Some((view, token, locale)) = presentation_destination(destination) else {
        return false;
    };
    // Wry's macOS URL getter has no value during the initial navigation.
    // Presentation can only originate from an already loaded document; do not
    // ask for its current URL when admitting bootstrap/ordinary navigation.
    let Some(client) = app.try_state::<Client>() else {
        return false;
    };
    // Capture before reading the document. A newer explicit action must not be
    // recaptured as authority after this source was inspected.
    let owner = client.coordinator.presentation_owner();
    let workbench = if view == "claim" {
        let queue = client.presentations.lock().expect("presentation intent");
        let views = client.view.views.lock().expect("views");
        let Some(origin) = views.workbench(label, queue.window) else {
            return true;
        };
        Some((queue.window, queue.document, queue.sequence, origin))
    } else {
        None
    };
    let Some(receiver) = current() else { return true };
    if active_webview(app).is_none_or(|view| view.label() != label) {
        return true;
    }
    if label.starts_with("main-account-") && !app.get_webview(label).is_some_and(|view| trusted_page(&client, &view)) {
        return true;
    }
    let committed_receipt = client.coordinator.committed_view_generation();
    let allowed = {
        let state = client.view.presentation.lock().expect("presentation");
        let local = client.view.local_origin.lock().expect("local origin");
        if view == "claim" {
            claim_source_allowed(&state, local.as_ref(), &receiver)
        } else {
            presentation_source_allowed(
                &state,
                &client.view.bundle,
                local.as_ref(),
                committed_receipt,
                MAIN_WINDOW,
                &receiver,
            )
        }
    };
    if allowed {
        if let Some((window, document, sequence, origin)) = workbench {
            let mut queue = client.presentations.lock().expect("presentation intent");
            if queue.sequence != sequence
                || queue.window != window
                || queue.document != document
                || queue.pending.is_some_and(|intent| intent.view == "claim")
                || client.coordinator.presentation_owner() != owner
                || client
                    .view
                    .views
                    .lock()
                    .expect("views")
                    .workbench(label, window)
                    .as_ref()
                    != Some(&origin)
            {
                return true;
            }
            queue.enqueue(view, owner, false, locale);
            queue.source = Some(PresentationSource {
                window,
                document,
                label: label.to_owned(),
                url: receiver.to_string(),
            });
            queue.claim_workbench = Some(origin);
            drop(queue);
            retire_stale_link(app);
            schedule_presentation(app);
            ensure_presentation_retry(app);
            return true;
        }
        let approval = if let Some(token) = token {
            let Some(approval) = client
                .presentations
                .lock()
                .expect("presentation intent")
                .guard_authority(&token, view, owner, &receiver)
            else {
                return true;
            };
            Some(approval)
        } else {
            None
        };
        let account_source = client.view.presentation.lock().expect("presentation").account;
        present_owned(
            app,
            view,
            owner,
            account_source && approval.is_none(),
            approval.as_ref(),
            locale,
            Some((label, &receiver)),
        );
    }
    true
}
/// Accept a presentation intent, not an updater action. Busy/rebinding windows
/// retain it until a view/lifecycle signal can admit navigation. A newer explicit
/// account action supersedes it; a local lifecycle generation change does not.
pub fn present(app: &AppHandle, view: &'static str) -> bool {
    let Some(client) = app.try_state::<Client>() else {
        return false;
    };
    present_owned(
        app,
        view,
        client.coordinator.presentation_owner(),
        false,
        None,
        None,
        None,
    )
}

fn present_owned(
    app: &AppHandle,
    view: &'static str,
    owner: u64,
    guarded: bool,
    approval: Option<&GuardAttempt>,
    locale: Option<&'static str>,
    receiver: Option<(&str, &Url)>,
) -> bool {
    let Some(client) = app.try_state::<Client>() else {
        return false;
    };
    // Only a stamped request from the Workbench can present this role.
    if view == "claim" {
        return false;
    }
    if account_url(&client.view.bundle, view, locale).is_none() {
        return false;
    }
    {
        let mut queue = client.presentations.lock().expect("presentation intent");
        if client.coordinator.presentation_owner() != owner
            || approval.is_some_and(|attempt| !queue.owns_guard_authority(attempt))
        {
            return false;
        }
        if let Some(approval) = approval {
            let Some((label, url)) = receiver else { return false };
            if !queue.approve(approval, locale, label, url) {
                return false;
            }
        } else {
            queue.enqueue(view, owner, guarded, locale);
            queue.source = receiver.map(|(label, url)| PresentationSource {
                window: queue.window,
                document: queue.document,
                label: label.into(),
                url: url.to_string(),
            });
        }
    }
    retire_stale_link(app);
    schedule_presentation(app);
    ensure_presentation_retry(app);
    true
}

/// Native navigation failure need not emit a page/view event. One bounded-rate
/// worker per client keeps accepted intents live without relying on that signal.
fn ensure_presentation_retry(app: &AppHandle) {
    let Some(client) = app.try_state::<Client>() else {
        return;
    };
    {
        let mut queue = client.presentations.lock().expect("presentation intent");
        if queue.retrying || queue.pending.is_none() {
            return;
        }
        queue.retrying = true;
    }
    let app = app.clone();
    tauri::async_runtime::spawn(async move {
        let mut delay = std::time::Duration::from_millis(100);
        loop {
            tokio::time::sleep(delay).await;
            let Some(client) = app.try_state::<Client>() else {
                return;
            };
            {
                let mut queue = client.presentations.lock().expect("presentation intent");
                if queue.current(client.coordinator.presentation_owner()).is_none() {
                    queue.retrying = false;
                    return;
                }
            }
            schedule_presentation(&app);
            delay = (delay * 2).min(std::time::Duration::from_secs(2));
        }
    });
}

fn schedule_presentation(app: &AppHandle) {
    let Some(client) = app.try_state::<Client>() else {
        return;
    };
    {
        let mut queue = client.presentations.lock().expect("presentation intent");
        if queue.scheduled || queue.pending.is_none() || queue.guard.is_some() {
            return;
        }
        queue.scheduled = true;
    }
    let handle = app.clone();
    if app.run_on_main_thread(move || apply_presentation(&handle)).is_err() {
        // Keep the accepted intent; a later page/view signal can retry it.
        client.presentations.lock().expect("presentation intent").scheduled = false;
    }
}

fn apply_presentation(app: &AppHandle) {
    retire_stale_link(app);
    let Some(client) = app.try_state::<Client>() else {
        return;
    };
    let intent = {
        let mut queue = client.presentations.lock().expect("presentation intent");
        queue.scheduled = false;
        let intent = queue.current(client.coordinator.presentation_owner());
        if queue.guard.is_some() {
            return;
        }
        intent
    };
    let Some(intent) = intent else { return };
    let native = &client.view;
    let Ok(windows) = native.windows.try_lock() else { return };
    if native.presentation.lock().expect("presentation").staging {
        return;
    }
    let Some(window) = active_webview(app) else {
        return;
    };
    let Some(url) = account_url(&native.bundle, intent.view, intent.locale) else {
        return;
    };
    let route = window.url().ok();
    if client.coordinator.presentation_epoch(intent.owner).is_err() {
        client
            .presentations
            .lock()
            .expect("presentation intent")
            .complete(intent.id);
        return;
    }
    let account_receiver = native.presentation.lock().expect("presentation").account;
    if account_receiver && !trusted_page(&client, &window) {
        return;
    }
    let remote_popup = intent.view == "updates" && !client.coordinator.state().connection.committed.is_local();
    if remote_popup {
        let Some(receiver) = route.as_ref() else { return };
        if !popup_source_allowed(&client, &window, receiver) {
            return;
        }
        let mut queue = client.presentations.lock().expect("presentation intent");
        if !queue.can_start(intent, window.label(), receiver) {
            return;
        }
        if queue.source.is_none() {
            queue.source = Some(PresentationSource {
                window: queue.window,
                document: queue.document,
                label: window.label().into(),
                url: receiver.to_string(),
            });
        }
    }
    // A remote Runtime is retained unchanged. An Account document still owns
    // recovery material and must approve even when its retained target is remote.
    if intent.guarded && (account_receiver || !remote_popup) {
        let Some(receiver) = route else { return };
        let committed_receipt = client.coordinator.committed_view_generation();
        let allowed = {
            let state = native.presentation.lock().expect("presentation");
            let local = native.local_origin.lock().expect("local origin");
            presentation_source_allowed(
                &state,
                &native.bundle,
                local.as_ref(),
                committed_receipt,
                MAIN_WINDOW,
                &receiver,
            )
        };
        if !allowed {
            return;
        }
        if account_receiver {
            // This exact admitted receiver owns any transient recovery material.
            // Bring it forward before asking; refusal has no separate wire result.
            let foreground = (|| -> Result<(), ClientError> {
                client.coordinator.presentation_epoch(intent.owner)?;
                let physical = window.window();
                physical.unminimize().map_err(failure)?;
                physical.show().map_err(failure)?;
                window.show().map_err(failure)?;
                physical.set_focus().map_err(failure)?;
                window.set_focus().map_err(failure)?;
                client.coordinator.presentation_epoch(intent.owner)?;
                Ok(())
            })();
            if foreground.is_err() {
                client
                    .presentations
                    .lock()
                    .expect("presentation intent")
                    .complete(intent.id);
                retire_stale_link(app);
                return;
            }
        }
        let attempt = client
            .presentations
            .lock()
            .expect("presentation intent")
            .begin_guard(intent, receiver.to_string());
        let Some(attempt) = attempt else { return };
        let script = guarded_presentation_script(&attempt);
        let handle = app.clone();
        let callback_attempt = attempt.clone();
        if window
            .eval_with_callback(script, move |result| {
                if let Some(client) = handle.try_state::<Client>() {
                    let mut queue = client.presentations.lock().expect("presentation intent");
                    queue.current(client.coordinator.presentation_owner());
                    queue.guard_result(&callback_attempt, &result);
                }
                schedule_presentation(&handle);
            })
            .is_err()
        {
            // The runtime did not accept the script. Execution errors delivered
            // as empty/null callbacks are different and must stay ambiguous.
            client
                .presentations
                .lock()
                .expect("presentation intent")
                .guard_result(&attempt, "\"not_dispatched\"");
        }
        return;
    }
    // Submission is not replacement admission. Keep one queued task, but let a
    // newer material-producing action retire it before it obtains the serializer.
    client.presentations.lock().expect("presentation intent").scheduled = true;
    let native = native.clone();
    let app = app.clone();
    drop(windows);
    tauri::async_runtime::spawn(async move {
        let windows = native.windows.lock().await;
        let client = app.state::<Client>();
        let result = async {
            let epoch = native.presentation_epoch(intent.owner)?;
            let receiver = active_webview(&app).ok_or_else(|| failure("receiver missing"))?;
            let current = receiver.url().map_err(failure)?;
            if intent.view == "claim" {
                let state = native.presentation.lock().expect("presentation");
                let local = native.local_origin.lock().expect("local origin");
                if !claim_source_allowed(&state, local.as_ref(), &current) {
                    return Err(ClientError::new("stale_generation"));
                }
            }
            if native.presentation.lock().expect("presentation").account && !trusted_page(&client, &receiver) {
                return Err(ClientError::new("stale_generation"));
            }
            if intent.view == "updates" && !client.coordinator.state().connection.committed.is_local() {
                if !popup_source_allowed(&client, &receiver, &current) {
                    return Err(ClientError::new("stale_generation"));
                }
                let presentation = {
                    let queue = client.presentations.lock().expect("presentation intent");
                    if !queue.can_start(intent, receiver.label(), &current) {
                        return Err(ClientError::new("stale_generation"));
                    }
                    PopupPresentation {
                        intent,
                        source: PresentationSource {
                            window: queue.window,
                            document: queue.document,
                            label: receiver.label().into(),
                            url: current.to_string(),
                        },
                    }
                };
                return if crate::update_popover::open(&app, &native.bundle, native.script(true, None), presentation) {
                    Ok(())
                } else {
                    Err(failure("update popup unavailable"))
                };
            }
            let (continuation, workbench) = {
                let mut queue = client.presentations.lock().expect("presentation intent");
                if !queue.can_start(intent, receiver.label(), &current) {
                    return Err(ClientError::new("stale_generation"));
                }
                let continuation = if queue.link.as_ref().is_some_and(|(id, _)| *id == intent.id) {
                    let links = app.state::<Mutex<crate::DeepLinks>>();
                    let links = links.lock().expect("deep links");
                    if !links.owns_local_return(&queue.link.as_ref().expect("link").1) {
                        queue.cancel();
                        return Err(ClientError::new("stale_generation"));
                    }
                    queue.link.take().map(|(_, ticket)| ticket)
                } else {
                    None
                };
                if intent.view == "sign-out" {
                    if !queue.source_matches(receiver.label(), &current) {
                        queue.complete(intent.id);
                        return Err(ClientError::new("stale_generation"));
                    }
                    let generation = client.coordinator.state().generation;
                    queue.logout = Some(LogoutReceipt {
                        result: DirectResult::new_sign_out_intent(generation),
                        owner: intent.owner,
                        generation,
                        window: queue.window,
                        document: queue.document + 1,
                        destination: url.clone(),
                    });
                }
                // Replacement admission, not the asynchronous completion of view disposal,
                // ends an old document's claim. A mere presentation event does not start one.
                let workbench = if intent.view == "claim" {
                    let origin = queue
                        .claim_workbench
                        .as_ref()
                        .ok_or_else(|| ClientError::new("stale_generation"))?;
                    if native
                        .views
                        .lock()
                        .expect("views")
                        .workbench(receiver.label(), queue.window)
                        .as_ref()
                        != Some(origin)
                    {
                        return Err(ClientError::new("stale_generation"));
                    }
                    Some(origin.clone())
                } else {
                    None
                };
                retire_claim_presentation(&app);
                native.presentation.lock().expect("presentation").staging = true;
                (continuation, workbench)
            };
            if let Some(ticket) = continuation {
                // The accepted local escape needs this serializer itself.
                // Staging now prevents a new finalization from issuing material
                // after this presentation has crossed its admission boundary.
                drop(windows);
                let result = client
                    .coordinator
                    .return_local_if_current(ticket.expected_generation, ticket.action_owner)
                    .await;
                let accepted = result
                    .as_ref()
                    .ok()
                    .copied()
                    .filter(|owner| client.coordinator.presentation_epoch(*owner).is_ok());
                app.state::<Mutex<crate::DeepLinks>>()
                    .lock()
                    .expect("deep links")
                    .finish_local_return(&ticket, accepted);
                result.map(|_| ())
            } else {
                let result = native.open_account(epoch, url, workbench).await;
                drop(windows);
                result
            }
        }
        .await;
        native.presentation.lock().expect("presentation").staging = false;
        let mut queue = client.presentations.lock().expect("presentation intent");
        queue.scheduled = false;
        if result.is_ok()
            || result.as_ref().is_err_and(|error| error.code == "stale_generation")
            || client.coordinator.presentation_epoch(intent.owner).is_err()
        {
            queue.complete(intent.id);
        } else {
            queue.logout = None;
        }
        drop(queue);
        retire_stale_link(&app);
        native.changed(&client.coordinator.state());
    });
}
// Deferred popup construction and showing share the existing window serializer.
// This does not grant command authority; popup IPC checks its actual receiver.
pub(crate) async fn popup_window_lock(app: &AppHandle) -> Option<tokio::sync::OwnedMutexGuard<()>> {
    let windows = app.try_state::<Client>()?.view.windows.clone();
    Some(windows.lock_owned().await)
}

fn popup_source_allowed(client: &Client, receiver: &Webview, current: &Url) -> bool {
    // trusted_page may ask the event loop for a URL. Never hold presentation or
    // queue locks across that getter; the event loop also needs those locks.
    let account = trusted_page(client, receiver);
    let state = client.view.presentation.lock().expect("presentation");
    if state.state.connection.committed.is_local() || (state.account && !account) {
        return false;
    }
    let local = client.view.local_origin.lock().expect("local origin");
    presentation_source_allowed(
        &state,
        &client.view.bundle,
        local.as_ref(),
        client.coordinator.committed_view_generation(),
        MAIN_WINDOW,
        current,
    )
}

/// Called with the window serializer held, immediately before a deferred action.
/// Completion may clear pending, but cannot renew this immutable source receipt.
pub(crate) fn popup_presentation_current(app: &AppHandle, presentation: &PopupPresentation) -> bool {
    let Some(client) = app.try_state::<Client>() else {
        return false;
    };
    if client
        .coordinator
        .presentation_epoch(presentation.intent.owner)
        .is_err()
    {
        return false;
    }
    {
        let queue = client.presentations.lock().expect("presentation intent");
        if !queue.popup_is_current(presentation) {
            return false;
        }
    }
    let Some(receiver) = active_webview(app) else {
        return false;
    };
    receiver.label() == presentation.source.label
        && receiver
            .url()
            .is_ok_and(|url| url.as_str() == presentation.source.url && popup_source_allowed(&client, &receiver, &url))
}

/// The callback reports actual guarded-event delivery, not merely native eval submission.
/// A compatible page cancels the event before it performs (or awaits) its own guarded
/// navigation. An older page that does not know the event leaves it unconsumed and
/// therefore receives no fallback URL; ambiguous execution remains parked.
/// Once an event is consumed, either approving or cancelling its unsaved-work guard
/// is terminal. Only a proven receiver mismatch can be retried safely.
fn guarded_presentation_script(attempt: &GuardAttempt) -> String {
    let receiver = serde_json::to_string(&attempt.receiver).expect("receiver");
    let view = serde_json::to_string(attempt.intent.view).expect("view");
    let token = serde_json::to_string(&attempt.token()).expect("token");
    format!(
        r#"(() => {{
        try {{
            if (window.self !== window.top || window.location.href !== {receiver}) return "not_dispatched";
            const event = new CustomEvent('avibe:desktop-present', {{ cancelable: true, detail: {{ view: {view}, presentationToken: {token} }} }});
            window.dispatchEvent(event);
            return event.defaultPrevented ? "dispatched" : "unconsumed";
        }} catch (_) {{ return "unknown"; }}
    }})()"#
    )
}

/// Called before legacy navigation. None keeps the legacy shell policy.
pub fn navigation(app: &AppHandle, label: &str, destination: &Url) -> Option<bool> {
    let client = app.try_state::<Client>()?;
    {
        let views = client.view.views.lock().expect("views");
        if let Some(account) = &views.account {
            if account.label == label {
                return Some(destination == &account.destination);
            }
        }
        if let Some(candidate) = views.candidate.as_ref().filter(|view| view.label == label) {
            return Some(candidate.allows_navigation(destination, &client.view.bundle));
        }
        if views.runtime.as_ref().is_none_or(|view| view.label != label) {
            return None;
        }
    }
    let state = client.view.presentation.lock().expect("presentation");
    if state.staging && destination.as_str() == "about:blank" {
        return Some(true);
    }
    if let Some(origin) = &state.remote {
        return Some(
            destination.origin() == origin.origin()
                && destination.username().is_empty()
                && destination.password().is_none(),
        );
    }
    None
}

impl NativeView {
    /// The update popup's state: the bundled page in its own window, guarded to that document.
    fn popover_script(&self, receiver: &Url) -> String {
        let state = self.presentation.lock().expect("presentation");
        let value = serde_json::to_value(&state.state).expect("client state");
        format!(
            "if (window.self === window.top && window.location.href === {}) {{window.dispatchEvent(new CustomEvent('{EVENT}', {{detail:{value}}}));}}",
            serde_json::to_string(receiver.as_str()).expect("receiver URL")
        )
    }

    async fn close_account(&self) -> Result<(), ClientError> {
        let account = self.views.lock().expect("views").account.take();
        let Some(mut account) = account else { return Ok(()) };
        account.claim.take(); // retire before awaiting native disposal
        let app = self.app.clone();
        let label = account.label.clone();
        let result = tokio::task::spawn_blocking(move || {
            app.get_webview(&label).map_or(Ok(()), |view| {
                crate::instance_store::destroy_completed(&view).map_err(failure)
            })
        })
        .await
        .map_err(failure)
        .and_then(|result| result);
        if result.is_err() {
            // Authority is already gone. Keep the exact old store as a
            // cleanup obligation; never recover its account admission.
            self.views.lock().expect("views").retired.push(RuntimeDocument {
                label: account.label,
                window: account.window,
                destination: account.destination,
                disposable: true,
                directory: None,
                owner: account.owner,
                document: account.document,
            });
        }
        result
    }

    async fn open_account(
        &self,
        epoch: Epoch,
        destination: Url,
        workbench: Option<(String, u64)>,
    ) -> Result<(), ClientError> {
        epoch.check()?;
        if account_view(&destination, &self.bundle) == Some("claim") {
            if workbench.is_none() || self.views.lock().expect("views").account.is_some() {
                return Err(ClientError::new("stale_generation"));
            }
        } else {
            self.close_account().await?;
        }
        epoch.check()?;
        let physical = self
            .app
            .state::<Client>()
            .presentations
            .lock()
            .expect("presentation intent")
            .window;
        let label = {
            let mut views = self.views.lock().expect("views");
            views.next += 1;
            let label = format!("main-account-{}", views.next);
            let returning = views
                .runtime
                .as_ref()
                .map(|runtime| (runtime.label.clone(), runtime.document));
            if workbench.is_some() && workbench != returning {
                return Err(ClientError::new("stale_generation"));
            }
            views.account = Some(AccountDocument {
                claim: None,
                claim_visibility: 0,
                sheet_open: SheetOpen::default(),
                label: label.clone(),
                window: physical,
                destination: destination.clone(),
                document: 0,
                admitted: false,
                loaded: false,
                shown: false,
                token: None,
                returning,
                owner: epoch.value,
            });
            label
        };
        let (loaded_tx, loaded_rx) = tokio::sync::oneshot::channel();
        *self.navigation.lock().expect("navigation") = Some(Navigation {
            label: label.clone(),
            epoch: epoch.clone(),
            destination: destination.clone(),
            completed: loaded_tx,
        });
        let (created_tx, created_rx) = tokio::sync::oneshot::channel();
        let app = self.app.clone();
        let url = destination.clone();
        let script = self.script(true, None);
        let checked = epoch.clone();
        let created_label = label.clone();
        let creation = self.app.run_on_main_thread(move || {
            let result: Result<Webview, ClientError> = (|| {
                checked.check()?;
                let window = app.get_window(MAIN_WINDOW).ok_or_else(|| failure("main missing"))?;
                let size = window.inner_size().map_err(failure)?.to_logical::<f64>(window.scale_factor().map_err(failure)?);
                let expected = url.clone();
                let builder = tauri::webview::WebviewBuilder::new(&created_label, tauri::WebviewUrl::External(url))
                    .incognito(true)
                    .auto_resize()
                    .initialization_script(script)
                    .initialization_script("if (window.self === window.top) Object.defineProperty(window, '__AVIBE_DESKTOP_DOCUMENT__', {value:crypto.randomUUID()});")
                    .on_new_window(|url, _| crate::handle_new_window_request(url))
                    .on_page_load(move |view, event| {
                        let Some(client) = view.app_handle().try_state::<Client>() else { return };
                        let owned = {
                            let mut views = client.view.views.lock().expect("views");
                            let Some(account) = views.account.as_mut().filter(|account| account.label == view.label()) else { return };
                            if event.event() == tauri::webview::PageLoadEvent::Started {
                                account.claim.take();
                                account.document += 1;
                                account.admitted = account.document == 1 && *event.url() == account.destination;
                                account.loaded = false;
                                account.token = None;
                            } else {
                                account.loaded = *event.url() == account.destination;
                            }
                            account.admitted
                        };
                        if event.event() == tauri::webview::PageLoadEvent::Started {
                            {
                                let mut queue = client.presentations.lock().expect("presentation intent");
                                let physical = queue.window;
                                queue.document_started(physical, event.url());
                            }
                            retire_stale_link(view.app_handle());
                        }
                        if event.event() == tauri::webview::PageLoadEvent::Finished && owned && *event.url() == expected {
                            let app = view.app_handle().clone();
                            let label = view.label().to_owned();
                            let destination = expected.clone();
                            let generation = checked.value;
                            let _ = view.eval_with_callback("window.__AVIBE_DESKTOP_DOCUMENT__", move |raw| {
                                let Ok(token) = serde_json::from_str::<String>(&raw) else { return };
                                let client = app.state::<Client>();
                                {
                                    let mut views = client.view.views.lock().expect("views");
                                    let Some(account) = views.account.as_mut().filter(|account| account.label == label && account.admitted && account.document == 1) else { return };
                                    account.token = Some(token);
                                }
                                navigation_loaded(&app, &label, generation, &destination);
                            });
                        }
                    });
                #[cfg(target_os = "macos")]
                let builder = builder.initialization_script(crate::macos_title_bar::inset_script(app.config()));
                let view = window.add_child(builder, tauri::LogicalPosition::new(0., 0.), size).map_err(failure)?;
                view.hide().map_err(failure)?;
                #[cfg(target_os = "macos")]
                crate::macos_new_context::install(&view);
                Ok(view)
            })();
            let _ = created_tx.send(result);
        }).map_err(failure);
        let result = async {
            creation?;
            let view = created_rx.await.map_err(failure)??;
            tokio::time::timeout(std::time::Duration::from_secs(30), loaded_rx)
                .await
                .map_err(|_| ClientError::new("instance_navigation_timeout"))?
                .map_err(failure)??;
            epoch.check()?;
            let admitted = {
                let views = self.views.lock().expect("views");
                views.account.as_ref().is_some_and(|account| {
                    account.label == label && account.admitted && (workbench.is_none() || views.return_matches(account))
                })
            };
            if !admitted {
                return Err(ClientError::new("stale_generation"));
            }
            if let Some(runtime) = runtime_webview(&self.app) {
                runtime.hide().map_err(failure)?;
            }
            view.show().map_err(failure)?;
            if let Some(account) = self
                .views
                .lock()
                .expect("views")
                .account
                .as_mut()
                .filter(|account| account.label == label)
            {
                account.shown = true;
            }
            view.set_focus().map_err(failure)?;
            let mut state = self.presentation.lock().expect("presentation");
            state.account = true;
            state.locale = destination
                .query_pairs()
                .find_map(|(key, value)| (key == "locale").then(|| value.into_owned()));
            epoch.check()
        }
        .await;
        if result.is_err() {
            {
                let mut navigation = self.navigation.lock().expect("navigation");
                if navigation.as_ref().is_some_and(|pending| pending.label == label) {
                    navigation.take();
                }
            }
            self.close_account().await?;
            if epoch.check().is_ok() {
                if let Some(runtime) = runtime_webview(&self.app) {
                    let _ = runtime.show();
                }
            }
        }
        result
    }

    fn script(&self, initial: bool, receiver: Option<&Url>) -> String {
        let account_token = self
            .views
            .lock()
            .expect("views")
            .account
            .as_ref()
            .filter(|account| account.admitted)
            .and_then(|account| account.token.clone());
        let state = self.presentation.lock().expect("presentation");
        // Initialization survives navigation for the lifetime of a WebView.
        // It can never contain account-only state, even for an account build.
        let account_receiver = receiver.filter(|url| {
            !initial && state.account && account_token.is_some() && account_view(url, &self.bundle).is_some()
        });
        let value = if account_receiver.is_some() {
            serde_json::to_value(&state.state).expect("client state")
        } else {
            state.state.runtime_projection()
        };
        let dispatch = format!("window.dispatchEvent(new CustomEvent('{EVENT}', {{detail:{value}}}));");
        if initial {
            format!("if (window.self === window.top) {{Object.defineProperty(window,'__AVIBE_DESKTOP_CLIENT_VERSION__',{{value:1}}); Object.defineProperty(window,'__AVIBE_DESKTOP_CLIENT_STATE__',{{value:Object.freeze({value})}}); {dispatch}}}")
        } else {
            // Native URL inspection precedes asynchronous eval. Check the actual
            // execution receiver too; a queued full event must not cross a
            // navigation back to Runtime, staging, bootstrap or another frame.
            let receiver_guard = account_receiver.map_or(String::new(), |url| {
                format!(
                    " && window.location.href === {} && window.__AVIBE_DESKTOP_DOCUMENT__ === {}",
                    serde_json::to_string(url.as_str()).expect("receiver URL"),
                    serde_json::to_string(&account_token).expect("document token")
                )
            });
            format!("if (window.self === window.top{receiver_guard}) {{{dispatch}}}")
        }
    }
    fn presentation_epoch(&self, owner: u64) -> Result<Epoch, ClientError> {
        self.app.state::<Client>().coordinator.presentation_epoch(owner)
    }
    fn lifecycle_retry(&self, owner: u64, epoch: &Epoch, result: &Result<(), ClientError>) -> bool {
        result.as_ref().is_err_and(|error| error.code == "stale_generation")
            && epoch.check().is_err()
            && self.presentation_epoch(owner).is_ok()
    }
    async fn retire_runtime(&self, mut role: RuntimeDocument) -> Result<(), ClientError> {
        if let Err(error) = crate::app_windows::close_runtime_completed(&self.app, &role.label, role.window).await {
            self.views.lock().expect("views").retired.push(role);
            return Err(failure(error));
        }
        let app = self.app.clone();
        let label = role.label.clone();
        let disposable = role.disposable;
        let physical = self
            .app
            .state::<Client>()
            .presentations
            .lock()
            .expect("presentation intent")
            .window;
        let destroyed = role.window != physical;
        let retired = tokio::task::spawn_blocking(move || {
            if destroyed {
                return Ok(());
            }
            if let Some(view) = app.get_webview(&label) {
                if disposable {
                    crate::instance_store::retire(&view)
                } else {
                    crate::instance_store::destroy_completed(&view)
                }
            } else {
                Ok(())
            }
        })
        .await
        .map_err(failure)
        .and_then(|result| result.map_err(failure));
        let removed = if destroyed || self.app.get_webview(&role.label).is_none() {
            let removed = role.directory.as_ref().map_or(Ok(()), |directory| {
                if directory.path().exists() {
                    std::fs::remove_dir_all(directory.path()).map_err(failure)
                } else {
                    Ok(())
                }
            });
            if removed.is_ok() {
                role.directory.take();
            }
            removed
        } else {
            Err(failure("retired view still registered"))
        };
        let result = retired.and(removed);
        if result.is_err() {
            self.views.lock().expect("views").retired.push(role);
        }
        result
    }
    async fn discard_candidate(&self) -> Result<(), ClientError> {
        let role = self.views.lock().expect("views").candidate.take();
        if let Some(role) = role {
            self.retire_runtime(role).await?;
        }
        self.presentation.lock().expect("presentation").pending_commit = None;
        Ok(())
    }
    async fn retire_current(&self) -> Result<Option<(String, u64)>, ClientError> {
        let parent = self
            .views
            .lock()
            .expect("views")
            .runtime
            .as_ref()
            .map(|role| (role.label.clone(), role.window));
        if let Some((label, window)) = parent {
            crate::app_windows::close_runtime_completed(&self.app, &label, window)
                .await
                .map_err(failure)?;
        }
        let role = self.views.lock().expect("views").runtime.take();
        // Capture identity at the same boundary that removes its authority.
        // A candidate load may have awaited while this document navigated.
        let document = role.as_ref().map(|role| (role.label.clone(), role.document));
        if let Some(role) = role {
            self.retire_runtime(role).await?;
        }
        Ok(document)
    }
    async fn build_candidate(
        &self,
        epoch: Epoch,
        destination: Url,
        disposable: bool,
        cookies: Vec<cookie::Cookie<'static>>,
    ) -> Result<Webview, ClientError> {
        epoch.check()?;
        self.discard_candidate().await?;
        let directory = if disposable {
            let root = self
                .app
                .path()
                .app_local_data_dir()
                .map_err(failure)?
                .join("instance-stores");
            std::fs::create_dir_all(&root).map_err(failure)?;
            Some(tempfile::tempdir_in(root).map_err(failure)?)
        } else {
            None
        };
        let path = directory.as_ref().map(|directory| directory.path().to_owned());
        let physical = self
            .app
            .state::<Client>()
            .presentations
            .lock()
            .expect("presentation intent")
            .window;
        let label = {
            let mut views = self.views.lock().expect("views");
            views.next += 1;
            let label = format!("main-runtime-{}", views.next);
            views.candidate = Some(RuntimeDocument {
                label: label.clone(),
                window: physical,
                destination: destination.clone(),
                disposable,
                directory,
                owner: self.app.state::<Client>().coordinator.presentation_owner(),
                document: 0,
            });
            label
        };
        let (tx, rx) = tokio::sync::oneshot::channel();
        let app = self.app.clone();
        let checked = epoch.clone();
        let created_label = label.clone();
        let created_destination = destination.clone();
        self.app
            .run_on_main_thread(move || {
                let result = checked.check().and_then(|_| {
                    crate::build_main_child(
                        &app,
                        &created_label,
                        disposable,
                        path,
                        checked.value,
                        &created_destination,
                    )
                    .ok_or_else(|| failure("create child"))
                });
                let _ = tx.send(result);
            })
            .map_err(failure)?;
        let window = rx.await.map_err(failure)??;
        let projected = window.clone();
        let checked = epoch.clone();
        tokio::task::spawn_blocking(move || crate::instance_store::project(&projected, &cookies, &checked))
            .await
            .map_err(failure)??;
        let (loaded, navigation) = tokio::sync::oneshot::channel();
        *self.navigation.lock().expect("navigation") = Some(Navigation {
            label,
            epoch: epoch.clone(),
            destination: destination.clone(),
            completed: loaded,
        });
        epoch.check()?;
        window.navigate(destination).map_err(failure)?;
        let result = tokio::time::timeout(std::time::Duration::from_secs(30), navigation)
            .await
            .map_err(|_| ClientError::new("instance_navigation_timeout"))?
            .map_err(failure)?;
        epoch.check()?;
        result?;
        Ok(window)
    }
    async fn show(&self, epoch: Epoch, view: &Webview, destination: &Url) -> Result<(), ClientError> {
        let app = self.app.clone();
        let bundle = self.bundle.clone();
        let destination = destination.clone();
        let view = view.clone();
        let (tx, rx) = tokio::sync::oneshot::channel();
        self.app
            .run_on_main_thread(move || {
                let result = (|| {
                    epoch.check()?;
                    if app.get_webview(view.label()).is_none() {
                        return Err(failure("retired"));
                    }
                    let actual = view.url().map_err(failure)?;
                    let matches = if destination == bundle || account_view(&destination, &bundle).is_some() {
                        actual == destination
                    } else {
                        actual.origin() == destination.origin()
                    };
                    if !matches {
                        return Err(ClientError::new("instance_navigation_mismatch"));
                    }
                    view.show().map_err(failure)?;
                    view.window().show().map_err(failure)?;
                    view.set_focus().map_err(failure)?;
                    epoch.check()
                })();
                let _ = tx.send(result);
            })
            .map_err(failure)?;
        rx.await.map_err(failure)?
    }
    async fn replace_local(&self, owner: u64, preserve_account: bool) -> Result<(), ClientError> {
        self.presentation_epoch(owner)?;
        let physical = self
            .app
            .state::<Client>()
            .presentations
            .lock()
            .expect("presentation intent")
            .window;
        // A trusted lifecycle replacement may transfer an existing return
        // receipt. Capture it before retirement (including lifecycle retries);
        // arbitrary navigation or a different account document cannot acquire it.
        let mut returning = if preserve_account {
            let views = self.views.lock().expect("views");
            views.account.as_ref().and_then(|account| {
                let runtime = views.runtime.as_ref()?;
                let receipt = (runtime.label.clone(), runtime.document);
                (account.admitted
                    && account_view(&account.destination, &self.bundle) != Some("claim")
                    && account.window == physical
                    && runtime.window == physical
                    && !runtime.disposable
                    && account.returning.as_ref() == Some(&receipt))
                .then(|| (account.label.clone(), account.document, account.owner, receipt))
            })
        } else {
            None
        };
        loop {
            let epoch = self.presentation_epoch(owner)?;
            let result = async {
                let url = self.local_url()?;
                let candidate = self.build_candidate(epoch.clone(), url.clone(), false, vec![]).await?;
                epoch.check()?;
                if let Some(retired) = self.retire_current().await? {
                    if returning.as_ref().is_some_and(|(_, _, _, receipt)| receipt != &retired) {
                        returning = None;
                    }
                }
                epoch.check()?;
                if !preserve_account {
                    self.close_account().await?;
                    self.show(epoch.clone(), &candidate, &url).await?;
                }
                epoch.check()?;
                let mut views = self.views.lock().expect("views");
                let runtime = views.candidate.take();
                if let (Some((label, document, account_owner, receipt)), Some(runtime)) = (&returning, &runtime) {
                    if let Some(account) = views.account.as_mut().filter(|account| {
                        account.admitted
                            && account.label == *label
                            && account.document == *document
                            && account.owner == *account_owner
                            && account.window == physical
                            && runtime.window == physical
                            && account.returning.as_ref() == Some(receipt)
                    }) {
                        account.returning = Some((runtime.label.clone(), runtime.document));
                    }
                }
                views.runtime = runtime;
                views.terminal = None;
                drop(views);
                let mut state = self.presentation.lock().expect("presentation");
                state.account = preserve_account;
                state.remote = None;
                state.pending_commit = None;
                Ok(())
            }
            .await;
            if result.is_err() {
                self.discard_candidate().await?;
            }
            if self.lifecycle_retry(owner, &epoch, &result) {
                continue;
            }
            return result;
        }
    }
    fn local_url(&self) -> Result<Url, ClientError> {
        if let Some(origin) = self.local_origin.lock().ok().and_then(|origin| origin.clone()) {
            return Ok(origin.navigation_url());
        }
        Ok(self.bundle.clone())
    }
    fn auxiliaries(&self) {
        if self.app.try_state::<Shell>().is_none() {
            return;
        }
        // Runtime monitor owns the single local control connection. Selection
        // only gates its user-facing output; it never stops or reopens it.
        crate::notifications::reconcile(&self.app);
        crate::pet::reconcile(&self.app);
        crate::refresh_native_controls(&self.app, None);
    }
}
impl NativeView {
    /// Tells the committed view of another computer's instance who this
    /// computer is, while its document is at a Workbench route. A candidate,
    /// a retired view, an account page, a Show page or any other document of
    /// the instance is told nothing. A page mounted before the commit hears
    /// it at the commit's publication; every later load, at its own.
    fn describe_device(&self, view: &Webview) {
        let remote = self.presentation.lock().expect("presentation").remote.clone();
        let disposable = self
            .views
            .lock()
            .expect("views")
            .runtime
            .as_ref()
            .is_some_and(|runtime| runtime.label == view.label() && runtime.disposable);
        let Ok(url) = view.url() else { return };
        if !crate::device_description::admits(remote.as_ref(), disposable && is_current_runtime(view), &url) {
            return;
        }
        if let Some(script) = crate::device_description::delivery(&self.app, &url) {
            let _ = view.eval(script);
        }
    }
}
#[async_trait]
impl ClientView for NativeView {
    /// The committed instance's WebView store, read back for exactly its origin.
    /// Called off the main thread; never for a candidate, an account page or a
    /// document on another origin.
    fn instance_cookies(&self, epoch: &Epoch, origin: &Url) -> Option<Vec<cookie::Cookie<'static>>> {
        epoch.check().ok()?;
        let view = runtime_webview(&self.app)?;
        if view.label() == MAIN_WINDOW || view.url().ok()?.origin() != origin.origin() {
            return None;
        }
        let cookies = view.cookies_for_url(origin.clone()).ok()?;
        epoch.check().ok()?;
        Some(cookies)
    }
    fn desktop_update(&self, action: ClientAction, epoch: Epoch) -> Result<(), ClientError> {
        let client = self
            .app
            .try_state::<Client>()
            .ok_or_else(|| ClientError::new("desktop_client_unavailable"))?;
        if !updater_caller(&self.app, &client.view.bundle, || {
            active_webview(&self.app).is_some_and(|window| trusted_page(&client, &window))
        }) {
            return Err(ClientError::new("untrusted_desktop_caller"));
        }
        epoch.check()?;
        crate::updater::client_action(&self.app, action, epoch.value).map_err(ClientError::new)
    }
    fn local_rebound(&self, epoch: Epoch) {
        let Some(client) = self.app.try_state::<Client>() else {
            return;
        };
        let view = client.view.clone();
        let coordinator = client.coordinator.clone();
        let owner = coordinator.presentation_owner();
        tauri::async_runtime::spawn(async move {
            let _windows = view.windows.lock().await;
            if epoch.check().is_err() || view.presentation_epoch(owner).is_err() {
                return;
            }
            if !coordinator.state().connection.committed.is_local() {
                return;
            }
            let preserve_account = view.presentation.lock().expect("presentation").account;
            let destination = match view.local_url() {
                Ok(url) => url,
                Err(_) => return,
            };
            let matches = runtime_webview(&view.app)
                .is_some_and(|runtime| runtime.url().is_ok_and(|url| url.origin() == destination.origin()));
            if !matches {
                if let Err(error) = view.replace_local(owner, preserve_account).await {
                    eprintln!("desktop client lifecycle restoration: {}", error.code);
                }
            }
            drop(_windows);
            view.changed(&coordinator.state());
        });
    }
    fn changed(&self, state: &ClientState) {
        self.presentation.lock().expect("presentation").state = state.clone();
        retire_stale_link(&self.app);
        if self.windows.try_lock().is_ok() {
            if let Some(runtime) = runtime_webview(&self.app) {
                // Runtime receives only the minimal projection even while retained.
                let _ = runtime.eval(self.script(false, None));
                self.describe_device(&runtime);
            }
            if let Some(account) = active_webview(&self.app) {
                let client = self.app.state::<Client>();
                if trusted_page(&client, &account) {
                    if let Ok(url) = account.url() {
                        let _ = account.eval(self.script(false, Some(&url)));
                    }
                }
            }
        }
        crate::update_popover::publish(&self.app, &self.bundle, |url| self.popover_script(url));
        self.auxiliaries();
        crate::apply_pending_deep_link(&self.app);
        schedule_presentation(&self.app);
    }
    fn committed(&self, epoch: Epoch) -> Result<(), ClientError> {
        // All fallible native work was awaited in commit. Publication only
        // transfers the already-shown handle; it cannot spawn a second finish.
        epoch.check()?;
        let mut views = self.views.lock().expect("views");
        if views.terminal != Some(epoch.value) {
            return Ok(());
        }
        let mut state = self.presentation.lock().expect("presentation");
        let candidate = views.candidate.take().ok_or_else(|| failure("candidate missing"))?;
        state.remote = Some(candidate.destination.clone());
        state.account = false;
        state.pending_commit = None;
        views.runtime = Some(candidate);
        views.terminal = None;
        Ok(())
    }
    fn terminal_commit(&self, epoch: &Epoch) -> bool {
        self.views.lock().expect("views").terminal == Some(epoch.value)
    }
    async fn abort_commit(&self, epoch: Epoch) -> Result<(), ClientError> {
        let _windows = self.windows.lock().await;
        let current = self
            .views
            .lock()
            .expect("views")
            .candidate
            .as_ref()
            .is_some_and(|candidate| candidate.owner <= epoch.value);
        if current {
            self.discard_candidate().await?;
        }
        if !self.views.lock().expect("views").retired.is_empty() {
            return Err(failure("retirement incomplete"));
        }
        Ok(())
    }
    fn invalidate(&self, _: u64) {
        if let Some(navigation) = self.navigation.lock().expect("navigation").take() {
            let _ = navigation.completed.send(Err(ClientError::new("stale_generation")));
        }
        crate::app_windows::close_all(&self.app);
        if self.app.try_state::<crate::notifications::Notifications>().is_some() {
            crate::notifications::clear_badge(&self.app);
        }
        crate::instance_passkeys::cancel(&self.app);
        crate::instance_downloads::invalidate(&self.app);
    }
    async fn commit(
        &self,
        epoch: Epoch,
        target: ClientTarget,
        cookies: Vec<cookie::Cookie<'static>>,
    ) -> Result<(), ClientError> {
        let _windows = self.windows.lock().await;
        epoch.check()?;
        let ClientTarget::Remote { canonical_origin, .. } = &target else {
            return Err(ClientError::new("invalid_action"));
        };
        let url: Url = canonical_origin.parse().map_err(failure)?;
        let result = async {
            let candidate = self.build_candidate(epoch.clone(), url.clone(), true, cookies).await?;
            epoch.check()?;
            // From this point onward the old role is terminal. Its cookies and
            // URL can never be used as a restoration snapshot.
            self.views.lock().expect("views").terminal = Some(epoch.value);
            self.retire_current().await?;
            epoch.check()?;
            self.close_account().await?;
            epoch.check()?;
            self.show(epoch.clone(), &candidate, &url).await?;
            epoch.check()?;
            self.presentation.lock().expect("presentation").pending_commit = Some(epoch.value);
            Ok(())
        }
        .await;
        if result.is_err() {
            self.discard_candidate().await?;
        }
        result
    }
    async fn restore(
        &self,
        epoch: Epoch,
        target: ClientTarget,
        destination: RestoreDestination,
    ) -> Result<(), ClientError> {
        let _windows = self.windows.lock().await;
        let owner = epoch.value;
        self.presentation_epoch(owner)?;
        self.discard_candidate().await?;
        if destination != RestoreDestination::Local {
            self.views.lock().expect("views").check_return()?;
        }
        if let Some(runtime) = runtime_webview(&self.app) {
            let same = runtime.url().is_ok_and(|url| match &target {
                ClientTarget::Local { .. } => self
                    .local_origin
                    .lock()
                    .expect("local origin")
                    .as_ref()
                    .is_some_and(|origin| origin.matches_url_origin(&url)),
                ClientTarget::Remote { canonical_origin, .. } => {
                    url.origin().ascii_serialization() == *canonical_origin
                }
            });
            if same {
                // This is the presentation boundary: retire the account and
                // its return receipt before replacing the retained document.
                // The windows lock serializes later Cancel/ReturnLocal restores.
                // If they supersede us before navigation, the epoch check keeps
                // the old route; afterward they restore the current Runtime.
                self.close_account().await?;
                if destination == RestoreDestination::Home {
                    let checked = self.presentation_epoch(owner)?;
                    let returning = runtime.clone();
                    let app = self.app.clone();
                    let (tx, rx) = tokio::sync::oneshot::channel();
                    self.app
                        .run_on_main_thread(move || {
                            let result = (|| {
                                let current = returning.url().map_err(failure)?;
                                let home = app
                                    .state::<Client>()
                                    .view
                                    .views
                                    .lock()
                                    .expect("views")
                                    .home_destination(&checked, returning.label(), &current)?;
                                returning.navigate(home).map_err(failure)
                            })();
                            let _ = tx.send(result);
                        })
                        .map_err(failure)?;
                    rx.await.map_err(failure)??;
                    self.presentation_epoch(owner)?;
                }
                let epoch = self.presentation_epoch(owner)?;
                let url = runtime.url().map_err(failure)?;
                self.show(epoch, &runtime, &url).await?;
                self.presentation.lock().expect("presentation").account = false;
                return Ok(());
            }
        }
        if target.is_local() {
            self.replace_local(owner, false).await
        } else {
            Err(ClientError::new("instance_session_missing"))
        }
    }
    async fn signed_out(&self, epoch: Epoch, _: ClientTarget, cleanup_failed: bool) -> Result<(), ClientError> {
        let _windows = self.windows.lock().await;
        let owner = epoch.value;
        // A newer escape may already have replaced every old role. Never erase
        // that successor while completing the earlier cleanup obligation.
        let roles = {
            let mut views = self.views.lock().expect("views");
            let mut roles = std::mem::take(&mut views.retired);
            if views.runtime.as_ref().is_some_and(|view| view.owner <= owner) {
                roles.extend(views.runtime.take());
            }
            if views.candidate.as_ref().is_some_and(|view| view.owner <= owner) {
                roles.extend(views.candidate.take());
            }
            views.terminal = None;
            roles
        };
        let mut retired = Ok(());
        for role in roles {
            if let Err(error) = self.retire_runtime(role).await {
                retired = Err(error);
            }
        }
        if self.presentation_epoch(owner).is_err() {
            return retired;
        }
        self.close_account().await?;
        {
            let mut state = self.presentation.lock().expect("presentation");
            state.account = false;
            state.remote = None;
            state.pending_commit = None;
        }
        if cleanup_failed || retired.is_err() {
            return retired;
        }
        self.replace_local(owner, false).await
    }
    async fn signout_failed(&self, epoch: Epoch) -> Result<(), ClientError> {
        let _windows = self.windows.lock().await;
        let owner = epoch.value;
        let epoch = self.presentation_epoch(owner)?;
        let locale = self.presentation.lock().expect("presentation").locale.clone();
        let url = account_url(&self.bundle, "sign-out", locale.as_deref()).ok_or_else(|| failure("account route"))?;
        self.app
            .state::<Client>()
            .presentations
            .lock()
            .expect("presentation intent")
            .logout = None;
        self.open_account(epoch, url, None).await
    }
}

/// Root of the already validated committed instance, without a stale page query/fragment.
fn workbench_home(current: &Url) -> Url {
    let mut home = current.clone();
    home.set_path("/");
    home.set_query(None);
    home.set_fragment(None);
    home
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sign_in_home_keeps_the_selected_origin_and_clears_the_old_route() {
        for (current, expected) in [
            (
                "http://127.0.0.1:4080/chat/session?draft=1#tail",
                "http://127.0.0.1:4080/",
            ),
            (
                "https://studio.example/settings/account?tab=old",
                "https://studio.example/",
            ),
        ] {
            assert_eq!(workbench_home(&current.parse().unwrap()).as_str(), expected);
        }
    }

    #[test]
    fn sign_in_home_retires_the_return_receipt_before_navigation_and_later_cancel() {
        use std::sync::atomic::{AtomicU64, Ordering};

        for superseded_before_navigation in [false, true] {
            let current: Url = "https://studio.example/chat/session?draft=1".parse().unwrap();
            let generation = Arc::new(AtomicU64::new(2));
            let epoch = Epoch::new(generation.clone(), 2);
            let mut views = Views {
                runtime: Some(RuntimeDocument {
                    label: "main-runtime-1".into(),
                    window: 1,
                    destination: current.clone(),
                    disposable: true,
                    directory: None,
                    owner: 1,
                    document: 4,
                }),
                account: Some(AccountDocument {
                    claim: None,
                    claim_visibility: 0,
                    sheet_open: SheetOpen::default(),
                    label: "main-account-2".into(),
                    window: 1,
                    destination: "tauri://localhost/account/index.html".parse().unwrap(),
                    document: 1,
                    admitted: true,
                    loaded: true,
                    shown: true,
                    token: None,
                    returning: Some(("main-runtime-1".into(), 4)),
                    owner: 2,
                }),
                ..Views::default()
            };
            assert!(views.check_return().is_ok());
            // The previous ordering navigated while this receipt was live.
            // The native navigation callback now explicitly rejects that order.
            assert!(views.home_destination(&epoch, "main-runtime-1", &current).is_err());
            views.account.take(); // close_account retires admission before awaiting disposal
            if superseded_before_navigation {
                generation.store(3, Ordering::SeqCst);
                assert!(views.home_destination(&epoch, "main-runtime-1", &current).is_err());
                assert_eq!(views.runtime.as_ref().unwrap().document, 4);
            } else {
                assert_eq!(
                    views
                        .home_destination(&epoch, "main-runtime-1", &current)
                        .unwrap()
                        .as_str(),
                    "https://studio.example/"
                );
                // Navigation has started; Cancel/ReturnLocal wins before the
                // old callback finishes. Cancel must not inspect the old receipt.
                views.runtime.as_mut().unwrap().document += 1;
                generation.store(3, Ordering::SeqCst);
                assert!(epoch.check().is_err());
            }
            assert!(views.check_return().is_ok());
            assert_eq!(views.displayed(), Some("main-runtime-1"));
            assert!(views.home_destination(&epoch, "main-runtime-1", &current).is_err());
        }
    }

    /// The browser's last page comes from the product catalogs, one language at a time.
    #[test]
    fn the_provider_callback_copy_is_the_catalogs_in_each_language() {
        let catalog = |source: &str| {
            serde_json::from_str::<serde_json::Value>(source).unwrap()["desktopBootstrap"]["providerCallback"].clone()
        };
        for (locale, source) in [
            (UiLocale::En, crate::EN_PRODUCT_CATALOG),
            (UiLocale::Zh, crate::ZH_PRODUCT_CATALOG),
        ] {
            let copy = provider_callback_copy(locale);
            let expected = catalog(source);
            assert_eq!(copy.done, expected["done"].as_str().unwrap());
            assert_eq!(copy.failed, expected["failed"].as_str().unwrap());
        }
        assert_ne!(
            provider_callback_copy(UiLocale::En).done,
            provider_callback_copy(UiLocale::Zh).done
        );
    }

    /// Showing or focusing main must target what covers the window. A shown
    /// account page reloads with `loaded == false`; the Runtime under it must
    /// not be revealed and made first responder.
    #[test]
    fn a_reloading_account_page_stays_the_displayed_view() {
        let url = Url::parse("tauri://localhost/account/index.html?view=account").unwrap();
        let views = |account: Option<(bool, bool)>| Views {
            runtime: Some(RuntimeDocument {
                label: "main-runtime-1".into(),
                window: 1,
                destination: Url::parse("https://studio.avibe.bot/").unwrap(),
                disposable: true,
                directory: None,
                owner: 1,
                document: 1,
            }),
            account: account.map(|(loaded, shown)| AccountDocument {
                claim: None,
                claim_visibility: 0,
                sheet_open: SheetOpen::default(),
                label: "main-account-2".into(),
                window: 1,
                destination: url.clone(),
                document: 2,
                admitted: false,
                loaded,
                shown,
                token: None,
                returning: None,
                owner: 1,
            }),
            ..Views::default()
        };
        // (account document (loaded, shown), displayed)
        for (account, displayed) in [
            (None, "main-runtime-1"),
            // A new document loading hidden, including one that replaces a
            // shown account page while the presentation still says account.
            (Some((false, false)), "main-runtime-1"),
            // Loaded but not yet shown by `open_account`.
            (Some((true, false)), "main-runtime-1"),
            (Some((true, true)), "main-account-2"),
            // Shown, then reloading: it still covers the Runtime.
            (Some((false, true)), "main-account-2"),
        ] {
            assert_eq!(views(account).displayed(), Some(displayed), "{account:?}");
        }
    }

    /// The popup only opens while main shows another computer, so the updater's
    /// own check must admit it there; otherwise Update, Check and both switches
    /// would all be refused from the popup. Drives the same window lookup
    /// `NativeView::desktop_update` uses; main's account-page test is passed in.
    #[test]
    fn the_update_popup_may_drive_the_updater_while_main_shows_another_computer() {
        use tauri::{WebviewBuilder, WebviewUrl, WebviewWindowBuilder, WindowBuilder};

        let app = tauri::test::mock_app();
        let bundle = Url::parse("tauri://localhost/").unwrap();
        let window = |label: &str, raw: &str| {
            WebviewWindowBuilder::new(&app, label, WebviewUrl::External(Url::parse(raw).unwrap()))
                .build()
                .unwrap()
        };
        let account_page = || {
            app.get_webview("main-runtime-1")
                .is_some_and(|window| window.url().is_ok_and(|url| account_view(&url, &bundle).is_some()))
        };
        // Main shows another computer's page and no popup is open: refused.
        let physical = WindowBuilder::new(&app, MAIN_WINDOW).build().unwrap();
        physical
            .add_child(
                WebviewBuilder::new(
                    "main-runtime-1",
                    WebviewUrl::External("https://studio.avibe.bot/".parse().unwrap()),
                ),
                tauri::LogicalPosition::new(0.0, 0.0),
                tauri::LogicalSize::new(880.0, 600.0),
            )
            .unwrap();
        assert!(!updater_caller(app.handle(), &bundle, account_page));
        // The popup on its own page: admitted.
        let popover = window(
            crate::update_popover::UPDATE_POPOVER,
            "tauri://localhost/account/index.html?view=update-popover",
        );
        assert!(updater_caller(app.handle(), &bundle, account_page));
        // Not once the popup shows anything else.
        popover
            .navigate(Url::parse("tauri://localhost/account/index.html?view=account").unwrap())
            .unwrap();
        assert!(!updater_caller(app.handle(), &bundle, account_page));
    }

    // A popup completes the presentation without replacing main. Its deferred
    // build/show must still lose authority on a newer material action, click,
    // document or physical window; pending alone cannot fence that interval.
    #[test]
    fn completed_popup_sources_retire_on_cancel_or_replacement() {
        for replacement in ["cancel", "presentation", "document", "window"] {
            let mut queue = PresentationQueue::default();
            let url: Url = "tauri://localhost/account/index.html?view=account".parse().unwrap();
            let physical = queue.window_created();
            queue.document_started(physical, &url);
            queue.enqueue("updates", 4, false, None);
            let intent = queue.current(4).unwrap();
            let presentation = PopupPresentation {
                intent,
                source: PresentationSource {
                    window: queue.window,
                    document: queue.document,
                    label: "main-account-2".into(),
                    url: url.to_string(),
                },
            };
            queue.complete(intent.id);
            assert!(queue.pending.is_none());
            assert!(queue.popup_is_current(&presentation));
            match replacement {
                "cancel" => queue.cancel(),
                "presentation" => queue.enqueue("account", 4, true, None),
                "document" => queue.document_started(physical, &url),
                "window" => {
                    queue.window_created();
                }
                _ => unreachable!(),
            }
            assert!(!queue.popup_is_current(&presentation), "{replacement}");
        }
    }

    #[test]
    fn a_local_bootstrap_candidate_admits_only_its_exact_bundled_destination() {
        let bundle: Url = "tauri://localhost/".parse().unwrap();
        let mut candidate = RuntimeDocument {
            label: "main-runtime-1".into(),
            window: 1,
            destination: bundle.clone(),
            disposable: false,
            directory: None,
            owner: 1,
            document: 0,
        };
        assert!(candidate.allows_navigation(&bundle, &bundle));
        assert!(candidate.allows_navigation(&"about:blank".parse().unwrap(), &bundle));
        for raw in [
            "tauri://localhost/account/index.html",
            "tauri://localhost/?other=1",
            "tauri://localhost/#other",
            "tauri://other/",
            "tauri://localhost:123/",
            "tauri://user@localhost/",
            "tauri://user:password@localhost/",
            "https://localhost/",
        ] {
            assert!(!candidate.allows_navigation(&raw.parse().unwrap(), &bundle), "{raw}");
        }
        candidate.disposable = true;
        assert!(!candidate.allows_navigation(&bundle, &bundle));
        candidate.destination = "https://remote.example.test/".parse().unwrap();
        assert!(!candidate.allows_navigation(&bundle, &bundle));
        assert!(candidate.allows_navigation(
            &"https://remote.example.test/path?query=1#fragment".parse().unwrap(),
            &bundle
        ));
        assert!(!candidate.allows_navigation(&"https://user:password@remote.example.test/".parse().unwrap(), &bundle));
        assert!(!candidate.allows_navigation(&"https://other.example.test/".parse().unwrap(), &bundle));
        candidate.disposable = false;
        candidate.destination = "tauri://localhost/account/index.html".parse().unwrap();
        assert!(!candidate.allows_navigation(&candidate.destination, &bundle));
        assert!(!candidate.allows_navigation(&bundle, &bundle));
    }

    #[test]
    fn a_link_approval_preserves_its_exact_continuation_in_both_callback_orders() {
        for callback_first in [false, true] {
            let mut links = crate::DeepLinks::default();
            links.receive_owned(["avibe://settings"], Some(4));
            let ticket = links.claim_local_return(12, 4).unwrap();
            let mut queue = PresentationQueue::default();
            let physical = queue.window_created();
            let receiver: Url = "tauri://localhost/account/index.html?view=account".parse().unwrap();
            queue.document_started(physical, &receiver);
            queue.enqueue("account", 4, true, None);
            let intent = queue.current(4).unwrap();
            queue.link = Some((intent.id, ticket));
            let attempt = queue.begin_guard(intent, receiver.to_string()).unwrap();
            if callback_first {
                queue.guard_result(&attempt, "\"dispatched\"");
                assert!(queue.pending.is_none());
                assert!(queue.owns_intent(intent.id));
                assert!(links.owns_local_return(&queue.link.as_ref().unwrap().1));
            }
            assert!(queue.approve(&attempt, Some("zh"), "main-account-2", &receiver));
            let approved = queue.current(4).unwrap();
            assert!(!approved.guarded);
            assert!(queue.can_start(approved, "main-account-2", &receiver));
            assert!(!queue.can_start(approved, "main-account-3", &receiver));
            assert_eq!(queue.link.as_ref().unwrap().0, approved.id);
            assert!(links.owns_local_return(&queue.link.as_ref().unwrap().1));
            queue.guard_result(&attempt, "\"dispatched\"");
            assert_eq!(queue.pending, Some(approved));
            assert!(!queue.approve(&attempt, None, "main-account-2", &receiver));
            // A new explicit material-producing action cancels even an already
            // approved but not-yet-started replacement.
            queue.cancel();
            assert!(!queue.can_start(approved, "main-account-2", &receiver));
            assert!(!queue.owns_intent(approved.id));
        }
    }

    #[test]
    fn a_consumed_or_unknown_link_guard_cannot_replay_and_retires_with_its_authority() {
        for callback in ["\"dispatched\"", "\"unknown\"", "\"unconsumed\"", "null"] {
            for retirement in ["presentation", "owner", "document", "window"] {
                let mut queue = PresentationQueue::default();
                let receiver: Url = "tauri://localhost/account/index.html?view=account".parse().unwrap();
                let physical = queue.window_created();
                queue.document_started(physical, &receiver);
                queue.enqueue("account", 4, true, None);
                let intent = queue.current(4).unwrap();
                let attempt = queue.begin_guard(intent, receiver.to_string()).unwrap();
                queue.guard_result(&attempt, callback);
                // Consumed leaves only dormant approval; unknown keeps the
                // original guard. Neither can schedule another guard.
                assert!(queue.pending.is_none() || queue.guard.is_some());
                assert!(queue.begin_guard(intent, receiver.to_string()).is_none());
                match retirement {
                    "presentation" => queue.enqueue("instances", 4, true, None),
                    "owner" => {
                        queue.current(5);
                    }
                    "document" => queue.document_started(physical, &receiver),
                    "window" => {
                        queue.window_created();
                    }
                    _ => unreachable!(),
                }
                assert!(!queue.owns_guard_authority(&attempt));
                assert!(!queue.approve(&attempt, None, "main-account-2", &receiver));
            }
        }
    }

    #[test]
    fn an_approved_receiver_cannot_cross_a_same_url_document_replacement() {
        let mut queue = PresentationQueue::default();
        let receiver: Url = "tauri://localhost/account/index.html?view=account".parse().unwrap();
        let physical = queue.window_created();
        queue.document_started(physical, &receiver);
        queue.enqueue("instances", 4, true, None);
        let intent = queue.current(4).unwrap();
        let attempt = queue.begin_guard(intent, receiver.to_string()).unwrap();
        assert!(queue.approve(&attempt, None, "main-account-2", &receiver));
        let approved = queue.current(4).unwrap();
        queue.document_started(physical, &receiver);
        assert!(!queue.can_start(approved, "main-account-2", &receiver));
    }

    // A footer/menu intent used to disappear when its deferred callback met a
    // busy window or a lifecycle rebind. Only actual admission may settle it.
    // A4: a repeated initial request is safe, but a new physical window or
    // same-URL reload must retire the unspent receipt permanently.
    #[test]
    fn logout_receipt_follows_exact_document_and_is_consumed_once() {
        for interruption in ["none", "reload", "window", "owner", "generation"] {
            let mut queue = PresentationQueue::default();
            let window = queue.window_created();
            let url: Url = "tauri://localhost/account/index.html?view=sign-out&locale=zh"
                .parse()
                .unwrap();
            let result = DirectResult::new_sign_out_intent(8);
            let DirectResult::SignOutIntent { ref intent_id, .. } = result else {
                unreachable!()
            };
            let id = intent_id.clone();
            queue.logout = Some(LogoutReceipt {
                result,
                owner: 7,
                generation: 8,
                window,
                document: 1,
                destination: url.clone(),
            });
            assert!(queue.logout_result(7, 8, &url).is_none());
            queue.document_started(window, &url);
            for _ in 0..2 {
                assert!(
                    matches!(queue.logout_result(7,8,&url),Some(DirectResult::SignOutIntent {intent_id,..}) if intent_id==id)
                );
            }
            let (owner, generation) = match interruption {
                "reload" => {
                    queue.document_started(window, &url);
                    (7, 8)
                }
                "window" => {
                    queue.window_created();
                    queue.document_started(window, &url);
                    (7, 8)
                }
                "owner" => (9, 9),
                "generation" => (7, 9),
                _ => (7, 8),
            };
            assert_eq!(
                queue.consume_logout(&id, owner, generation, &url),
                interruption == "none"
            );
            assert!(!queue.consume_logout(&id, 7, 8, &url));
            assert!(queue.logout_result(7, 8, &url).is_none());
        }
    }
    #[test]
    fn locale_grammar_does_not_relax_presentation_authority() {
        assert_eq!(
            presentation_destination(&"avibe://desktop/sign-out?locale=zh&native_request=1-2".parse().unwrap()),
            Some(("sign-out", Some("1-2".into()), Some("zh")))
        );
        for query in [
            "locale=zh&locale=en",
            "locale=fr",
            "locale=zh&action=sign_out",
            "native_request=1&native_request=2",
        ] {
            assert!(presentation_destination(&format!("avibe://desktop/sign-out?{query}").parse().unwrap()).is_none());
        }
    }
    #[test]
    fn presentation_intents_survive_retries_until_the_exact_navigation_is_admitted() {
        let mut queue = PresentationQueue::default();
        queue.enqueue("updates", 4, false, None);
        let first = queue.current(4).unwrap();
        // Lock contention, pending presentation and failed navigation leave the
        // same intent available. Lifecycle generation is deliberately not owner.
        for _ in 0..3 {
            assert_eq!(queue.current(4), Some(first));
        }
        queue.enqueue("account", 4, false, None);
        let newer = queue.current(4).unwrap();
        queue.complete(first.id);
        assert_eq!(queue.current(4), Some(newer));
        queue.complete(newer.id);
        assert!(queue.current(4).is_none());
    }

    #[test]
    fn a_new_explicit_action_supersedes_an_old_presentation_without_reviving_it() {
        let mut queue = PresentationQueue::default();
        queue.enqueue("updates", 4, false, None);
        let old = queue.current(4).unwrap();
        assert!(queue.current(5).is_none());
        queue.enqueue("updates", 5, false, None);
        let current = queue.current(5).unwrap();
        queue.complete(old.id);
        assert_eq!(queue.current(5), Some(current));
    }

    // Native eval acceptance is not event delivery. Unknown execution could
    // already have shown the unsaved-work prompt, so only an explicit
    // not-dispatched callback may retry; stale acknowledgements own nothing.
    #[test]
    fn guarded_presentation_requires_a_compatible_event_consumer() {
        let attempt = GuardAttempt {
            sequence: 1,
            intent: PresentationIntent {
                id: 1,
                view: "updates",
                owner: 4,
                guarded: true,
                locale: None,
            },
            receiver: "tauri://localhost/account/index.html?view=instances".into(),
        };
        let script = guarded_presentation_script(&attempt);
        assert!(script.contains("event.defaultPrevented ? \"dispatched\" : \"unconsumed\""));
        assert!(!script.contains("window.location.assign"));
    }

    #[test]
    fn guarded_presentation_retries_only_proven_non_dispatch_and_fences_callbacks() {
        let mut queue = PresentationQueue::default();
        queue.enqueue("updates", 4, true, None);
        let intent = queue.current(4).unwrap();
        let first = queue
            .begin_guard(intent, "https://fixture.avibe.bot/apps/1".into())
            .unwrap();
        for ambiguous in ["", "null", "not json", "\"unknown\"", "\"unconsumed\""] {
            queue.guard_result(&first, ambiguous);
            assert!(queue.begin_guard(intent, first.receiver.clone()).is_none());
            assert_eq!(queue.current(4), Some(intent));
        }
        queue.guard_result(&first, "\"not_dispatched\"");
        let retry = queue
            .begin_guard(intent, "https://fixture.avibe.bot/apps/2".into())
            .unwrap();
        queue.guard_result(&first, "\"dispatched\"");
        assert_eq!(queue.guard.as_ref(), Some(&retry));
        // Delivered means the page owns approval/cancellation, not permission
        // to bypass its guard. No further prompt is scheduled in either case.
        queue.guard_result(&retry, "\"dispatched\"");
        assert!(queue.current(4).is_none());
        assert!(queue.guard.is_none());

        queue.enqueue("updates", 4, true, None);
        let intent = queue.current(4).unwrap();
        let old = queue.begin_guard(intent, retry.receiver.clone()).unwrap();
        queue.enqueue("instances", 4, true, None);
        let successor = queue.current(4).unwrap();
        queue.guard_result(&old, "\"dispatched\"");
        assert_eq!(queue.current(4), Some(successor));
        queue.begin_guard(successor, retry.receiver).unwrap();
        assert!(queue.current(5).is_none());
        assert!(queue.guard.is_none());
    }

    // A callback is not the approved navigation: browser approval can arrive
    // before or after that callback. Both paths must retain the original owner
    // and intent instead of adopting whichever selection is current later.
    #[test]
    fn guarded_approval_keeps_its_receipt_across_callback_order_and_rejects_stale_handoffs() {
        for callback_first in [false, true] {
            let mut queue = PresentationQueue::default();
            let receiver = Url::parse("tauri://localhost/account/index.html?view=instances").unwrap();
            queue.enqueue("updates", 4, true, None);
            let intent = queue.current(4).unwrap();
            let attempt = queue.begin_guard(intent, receiver.to_string()).unwrap();
            let token = attempt.token();
            if callback_first {
                queue.guard_result(&attempt, "\"dispatched\"");
                assert!(queue.pending.is_none());
            }
            assert_eq!(
                queue.guard_authority(&token, "updates", 4, &receiver),
                Some(attempt.clone())
            );
            assert!(queue.guard_authority(&token, "updates", 5, &receiver).is_none());
            assert!(queue.guard_authority(&token, "account", 4, &receiver).is_none());
            assert!(queue
                .guard_authority(&token, "updates", 4, &Url::parse("https://fixture.avibe.bot/").unwrap())
                .is_none());
            // Approved navigation consumes the receipt in the same queue lock.
            queue.enqueue("updates", 4, false, None);
            let approved = queue.current(4).unwrap();
            queue.guard_result(&attempt, "\"dispatched\"");
            assert_eq!(queue.current(4), Some(approved));
            assert!(!queue.owns_guard_authority(&attempt));
            assert!(queue.guard_authority(&token, "updates", 4, &receiver).is_none());
        }
        let mut queue = PresentationQueue::default();
        let receiver = Url::parse("https://fixture.avibe.bot/apps/1").unwrap();
        queue.enqueue("updates", 4, true, None);
        let intent = queue.current(4).unwrap();
        let old = queue.begin_guard(intent, receiver.to_string()).unwrap();
        queue.guard_result(&old, "\"dispatched\"");
        queue.enqueue("instances", 4, true, None);
        assert!(queue.guard_authority(&old.token(), "updates", 4, &receiver).is_none());
        assert!(!queue.owns_guard_authority(&old));
    }

    #[test]
    fn a_same_url_reload_cannot_reuse_a_previous_guard_approval() {
        let mut queue = PresentationQueue::default();
        let receiver = Url::parse("http://127.0.0.1:1984/").unwrap();
        let window = queue.window_created();
        queue.document_started(window, &receiver);
        queue.enqueue("sign-out", 4, true, None);
        let intent = queue.current(4).unwrap();
        let attempt = queue.begin_guard(intent, receiver.to_string()).unwrap();
        queue.guard_result(&attempt, "\"dispatched\"");
        assert!(queue
            .guard_authority(&attempt.token(), "sign-out", 4, &receiver)
            .is_some());
        queue.document_started(window, &receiver);
        assert!(queue
            .guard_authority(&attempt.token(), "sign-out", 4, &receiver)
            .is_none());
    }

    #[test]
    fn native_presentation_tokens_have_no_malformed_or_uncorrelated_fallback() {
        let parse = |url| presentation_destination(&Url::parse(url).unwrap());
        assert_eq!(
            parse("avibe://desktop/claim?locale=zh"),
            Some(("claim", None, Some("zh")))
        );
        assert_eq!(parse("avibe://desktop/claim"), Some(("claim", None, None)));
        assert_eq!(parse("avibe://updates"), Some(("updates", None, None)));
        assert_eq!(
            parse("avibe://desktop/updates?native_request=1-2"),
            Some(("updates", Some("1-2".into()), None))
        );
        for url in [
            "avibe://desktop/claim?target=local",
            "avibe://desktop/claim?native_request=1-2",
            "avibe://desktop/claim?locale=de",
            "avibe://desktop/claim?locale=en&locale=zh",
            "avibe://desktop/claim?locale=zh#claim",
            "avibe://desktop/updates?native_request=",
            "avibe://desktop/updates?native_request=1-2&native_request=3-4",
            "avibe://desktop/updates?native_request=1-2&view=account",
            "avibe://desktop/unknown?native_request=1-2",
            "avibe://desktop/updates?native_request=1-2#fragment",
        ] {
            assert!(parse(url).is_none(), "{url}");
        }
    }

    // Queue-only tests cannot detect rejecting a valid source before enqueue.
    // Exercise the authority matrix without admission's window serializer:
    // committed origins survive contention; uncommitted candidates stay denied.
    #[test]
    fn presentation_sources_require_owned_documents_not_an_idle_window() {
        let local = LoopbackOrigin::parse("http://127.0.0.1:1984").unwrap();
        let mut view = Presentation {
            locale: None,
            state: ClientState::new("fixture".into(), "0.1.0".into(), serde_json::json!({})),
            account: false,
            staging: false,
            pending_commit: None,
            remote: None,
        };
        for raw_bundle in ["tauri://localhost", "http://tauri.localhost"] {
            let bundle = Url::parse(raw_bundle).unwrap();
            let allows = |view: &Presentation, receipt, label, url: &str| {
                presentation_source_allowed(view, &bundle, Some(&local), receipt, label, &Url::parse(url).unwrap())
            };
            assert!(allows(&view, None, MAIN_WINDOW, "http://127.0.0.1:1984/chat"));
            assert!(!allows(&view, None, "other", "http://127.0.0.1:1984/chat"));
            assert!(!allows(&view, None, MAIN_WINDOW, "http://127.0.0.1:1985/chat"));
            view.account = true;
            assert!(allows(
                &view,
                None,
                MAIN_WINDOW,
                bundle.join("/account/index.html?view=updates").unwrap().as_str()
            ));
            assert!(!allows(
                &view,
                None,
                MAIN_WINDOW,
                bundle.join("/index.html").unwrap().as_str()
            ));
            view.account = false;
        }
        let bundle = Url::parse("tauri://localhost").unwrap();
        let remote = Url::parse("https://fixture.avibe.bot/chat").unwrap();
        view.remote = Some(remote.clone());
        view.state.connection.committed = ClientTarget::Remote {
            name: "fixture".into(),
            instance_id: "fixture".into(),
            canonical_origin: "https://fixture.avibe.bot".into(),
        };
        let allows = |view: &Presentation, receipt| {
            presentation_source_allowed(view, &bundle, Some(&local), receipt, MAIN_WINDOW, &remote)
        };
        assert!(allows(&view, None));
        view.pending_commit = Some(7);
        assert!(!allows(&view, None));
        assert!(!allows(&view, Some(6)));
        assert!(allows(&view, Some(7)));
        view.staging = true;
        assert!(!allows(&view, Some(7)));
        view.staging = false;
        view.remote = Some(Url::parse("https://other.avibe.bot").unwrap());
        assert!(!allows(&view, Some(7)));
    }

    /// "Upgrade and try again" restarts this computer's Runtime under the
    /// account document; neither the shell losing the Runtime nor binding it
    /// again may navigate that window. Only this computer's Workbench in front
    /// hands the window to bootstrap.
    #[test]
    fn a_runtime_restart_under_the_account_document_does_not_navigate_it() {
        let mut p = Presentation {
            locale: None,
            state: ClientState::new("fixture".into(), "1".into(), serde_json::json!({})),
            account: false,
            staging: false,
            pending_commit: None,
            remote: None,
        };
        assert!(p.bootstrap_owns_window());
        p.account = true;
        assert!(!p.bootstrap_owns_window());
        p.account = false;
        p.remote = Some(Url::parse("https://other.avibe.bot").unwrap());
        assert!(!p.bootstrap_owns_window());
        p.remote = None;
        p.pending_commit = Some(3);
        assert!(!p.bootstrap_owns_window());
        p.pending_commit = None;
        p.staging = true;
        assert!(!p.bootstrap_owns_window());
    }

    #[test]
    fn claim_presentation_requires_a_committed_local_workbench() {
        let local = LoopbackOrigin::parse("http://127.0.0.1:1984").unwrap();
        let mut p = Presentation {
            locale: None,
            state: ClientState::new("fixture".into(), "1".into(), serde_json::json!({})),
            account: false,
            staging: false,
            pending_commit: None,
            remote: None,
        };
        let source = Url::parse("http://127.0.0.1:1984/settings/remote-access").unwrap();
        let allows = |p: &Presentation| claim_source_allowed(p, Some(&local), &source);
        assert!(allows(&p));
        assert!(!claim_source_allowed(&p, None, &source));
        for url in [
            "http://127.0.0.1:1985/",
            "https://fixture.avibe.bot/",
            "http://127.0.0.1:1984/show/fixture",
            "http://127.0.0.1:1984/api/status",
            "http://user@127.0.0.1:1984/",
        ] {
            assert!(!claim_source_allowed(&p, Some(&local), &Url::parse(url).unwrap()));
        }
        p.account = true;
        assert!(!allows(&p));
        p.account = false;
        p.staging = true;
        assert!(!allows(&p));
        p.staging = false;
        p.pending_commit = Some(7);
        assert!(!allows(&p));
        p.pending_commit = None;
        p.state.connection.status = ConnectionStatus::Switching;
        assert!(!allows(&p));
        p.state.connection.status = ConnectionStatus::Local;
        p.state.connection.pending = Some(p.state.connection.committed.clone());
        assert!(!allows(&p));
        p.state.connection.pending = None;
        p.state.connection.committed = ClientTarget::Remote {
            name: "fixture".into(),
            instance_id: "fixture".into(),
            canonical_origin: "https://fixture.avibe.bot".into(),
        };
        assert!(!allows(&p));
    }
    #[test]
    fn only_a_delivered_unbound_observation_allows_one_sheet_open() {
        let mut sheet = SheetOpen::default();
        assert!(!sheet.take(7, 1));
        for state in [
            "owner_observed",
            "legacy_wait",
            "legacy_publication_pending",
            "bound_owner_unconfirmed",
        ] {
            sheet.observed(7, 1, Some(&serde_json::json!({"state":state})));
            assert!(!sheet.take(7, 1));
        }
        let result = serde_json::json!({"state":"no_published_binding"});
        for (generation, visibility) in [(8, 1), (7, 2)] {
            sheet.observed(7, 1, Some(&result));
            assert!(!sheet.take(generation, visibility));
        }
        sheet.observed(7, 1, Some(&result));
        assert!(sheet.take(7, 1));
        sheet.observed(7, 1, Some(&result));
        assert!(!sheet.take(7, 1));
    }
    #[test]
    fn specific_visible_claim_role_rejects_hidden_reloaded_updater_and_runtime_receivers() {
        let bundle = Url::parse("tauri://localhost/").unwrap();
        let url = account_url(&bundle, "claim", Some("en")).unwrap();
        let mut role = AccountDocument {
            claim: None,
            claim_visibility: 0,
            sheet_open: SheetOpen::default(),
            label: "main-account-7".into(),
            window: 2,
            destination: url.clone(),
            document: 1,
            admitted: true,
            loaded: true,
            shown: true,
            token: Some("fixture-document".into()),
            returning: None,
            owner: 2,
        };
        assert!(role.claim_receipt("main-account-7", &bundle).is_none());
        role.returning = Some(("main-runtime-1".into(), 4));
        assert!(role.claim_receipt("main-account-7", &bundle).is_some());
        role.destination = account_url(&bundle, "account", Some("en")).unwrap();
        assert!(role.private_receipt("main-account-7", &bundle, true).is_some());
        assert!(role.claim_receipt("main-account-7", &bundle).is_none());
        role.destination = url.clone();
        for label in [
            "main",
            "main-runtime-7",
            "main-account-8",
            crate::update_popover::UPDATE_POPOVER,
        ] {
            assert!(role.claim_receipt(label, &bundle).is_none());
        }
        for view in ["updates", "sign-out", "setup-complete", "instances"] {
            role.destination = account_url(&bundle, view, Some("en")).unwrap();
            assert!(role.claim_receipt("main-account-7", &bundle).is_none());
        }
        role.destination = url;
        role.shown = false;
        assert!(role.claim_receipt("main-account-7", &bundle).is_none());
        role.shown = true;
        role.document = 2;
        assert!(role.claim_receipt("main-account-7", &bundle).is_none());
        role.document = 1;
        role.loaded = false;
        assert!(role.claim_receipt("main-account-7", &bundle).is_none());
        let dev = Url::parse("http://localhost:1420/").unwrap();
        role.destination = account_url(&dev, "claim", Some("en")).unwrap();
        role.loaded = true;
        assert!(role.claim_receipt("main-account-7", &dev).is_none());

        // The shell retains the Workbench, not a URL to navigate to on close.
        role.destination = account_url(&bundle, "claim", Some("en")).unwrap();
        let mut views = Views {
            runtime: Some(RuntimeDocument {
                label: "main-runtime-1".into(),
                window: 2,
                destination: Url::parse("http://127.0.0.1:1984/").unwrap(),
                disposable: false,
                directory: None,
                owner: 2,
                document: 4,
            }),
            ..Views::default()
        };
        assert_eq!(views.workbench("main-runtime-1", 2), role.returning);
        views.account = Some(role);
        assert!(views.workbench("main-runtime-1", 2).is_none()); // duplicate presentation cannot replace this sheet
        assert!(views.return_matches(views.account.as_ref().unwrap()));
        views.runtime.as_mut().unwrap().document += 1;
        assert!(!views.return_matches(views.account.as_ref().unwrap())); // same URL, different document
        views.account.take();
        assert!(views
            .account
            .as_ref()
            .and_then(|a| a.claim_receipt("main-account-7", &bundle))
            .is_none());
    }
    #[test]
    fn only_supported_static_bundle_enables_the_client() {
        assert!(compatible_asset(
            b"<meta name=\"avibe-desktop-client-version\" content=\"1\">"
        ));
        assert!(!compatible_asset(b"window.__AVIBE_DESKTOP_CLIENT_VERSION__ = 1"));
        assert!(!compatible_asset(
            b"<meta name=\"avibe-desktop-client-version\" content=\"2\">"
        ));
    }
}
