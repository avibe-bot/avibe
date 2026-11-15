//! Native-owned account presentation and disposable instance window lifecycle.
use async_trait::async_trait;
use avibe_runtime_host::{
    instance_client::{
        account::Passkeys,
        coordinator::{ClientView, Coordinator},
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
    source: Option<(u64, u64, String)>,
    logout: Option<LogoutReceipt>,
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
}
struct Presentation {
    state: ClientState,
    account: bool,
    staging: bool,
    pending_commit: Option<u64>,
    remote: Option<Url>,
    locale: Option<String>,
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
struct RuntimeDocument {
    label: String,
    window: u64,
    destination: Url,
    disposable: bool,
    directory: Option<tempfile::TempDir>,
    owner: u64,
    document: u64,
}
struct AccountDocument {
    label: String,
    window: u64,
    destination: Url,
    document: u64,
    admitted: bool,
    loaded: bool,
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
    (!state.account && state.remote.is_none() && !state.staging && state.pending_commit.is_none()).then_some(guard)
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
        Arc::new(HttpTransport::new()?),
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
    let coordinator = Arc::new(Coordinator::new(
        config.state,
        config.record,
        config.store,
        transport,
        credentials,
        passkeys,
        view.clone(),
    ));
    app.manage(Client {
        coordinator: coordinator.clone(),
        view,
        presentations: Mutex::new(PresentationQueue::default()),
    });
    coordinator
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
/// Per-physical-window callbacks cannot revive a retired document with the reused main label.
pub fn window_created(app: &AppHandle) -> Option<u64> {
    let client = app.try_state::<Client>()?;
    let window = client
        .presentations
        .lock()
        .expect("presentation intent")
        .window_created();
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
    let current = {
        let mut views = client.view.views.lock().expect("views");
        if let Some(runtime) = views.runtime.as_mut().filter(|runtime| runtime.label == label) {
            runtime.document += 1;
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

#[tauri::command]
pub async fn desktop_client_request(
    window: Webview,
    app: AppHandle,
    request: ClientRequest,
) -> Result<ClientReply, String> {
    let client = app.try_state::<Client>().ok_or("desktop_client_unavailable")?;
    // The update popup is its own window, limited to its page and to this computer's updater.
    let trusted = if window.label() == crate::update_popover::UPDATE_POPOVER {
        crate::update_popover::is_page(&window, &client.view.bundle) && crate::update_popover::allows(&request.action)
    } else {
        trusted_page(&client, &window)
    };
    if !trusted {
        return Err("untrusted_desktop_caller".into());
    }
    let coordinator = client.coordinator.clone();
    let receiver = window.url().map_err(|_| "untrusted_desktop_caller")?;
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
    let Some(receiver) = current() else { return true };
    if active_webview(app).is_none_or(|view| view.label() != label) {
        return true;
    }
    if label.starts_with("main-account-") && !app.get_webview(label).is_some_and(|view| trusted_page(&client, &view)) {
        return true;
    }
    let committed_receipt = client.coordinator.committed_view_generation();
    let allowed = {
        let view = client.view.presentation.lock().expect("presentation");
        let local = client.view.local_origin.lock().expect("local origin");
        presentation_source_allowed(
            &view,
            &client.view.bundle,
            local.as_ref(),
            committed_receipt,
            MAIN_WINDOW,
            &receiver,
        )
    };
    if allowed {
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
        present_owned(app, view, owner, false, approval.as_ref(), locale, Some(&receiver));
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
    receiver: Option<&Url>,
) -> bool {
    let Some(client) = app.try_state::<Client>() else {
        return false;
    };
    // While the main window shows another computer, this computer's update panel
    // opens as a popup over it instead of replacing the page (ui/design:
    // chrome/remote-update-popup). Nothing navigates, so no guard is needed.
    if view == "updates" && !local_selected(app) {
        return crate::update_popover::open(app, &client.view.bundle, client.view.script(true, None));
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
        queue.enqueue(view, owner, guarded, locale);
        queue.source = receiver.map(|url| (queue.window, queue.document, url.to_string()));
    }
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
    // Decide the bundled-account exemption only after taking the physical
    // window serializer and reading the actual receiver. A request queued
    // during restore must not inherit either the old account route or a later
    // Runtime route's policy.
    let native_account_entry = route.as_ref().is_some_and(|current| {
        let state = native.presentation.lock().expect("presentation");
        state.account && account_view(current, &native.bundle).is_some()
    });
    if intent.guarded && !native_account_entry {
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
    {
        let mut queue = client.presentations.lock().expect("presentation intent");
        if intent.view == "sign-out" {
            let matched = queue.source.as_ref().is_some_and(|(window, document, receiver)| {
                *window == queue.window
                    && *document == queue.document
                    && route.as_ref().is_some_and(|url| url.as_str() == receiver)
            });
            // Native menu entry while already bundled bypasses the Workbench guard;
            // it is itself the explicit source-bound native intent.
            if matched || (intent.guarded && native_account_entry) {
                let generation = client.coordinator.state().generation;
                queue.logout = Some(LogoutReceipt {
                    result: DirectResult::new_sign_out_intent(generation),
                    owner: intent.owner,
                    generation,
                    window: queue.window,
                    document: queue.document + 1,
                    destination: url.clone(),
                });
            } else {
                queue.complete(intent.id);
                return;
            }
        }
    }
    native.presentation.lock().expect("presentation").staging = true;
    let native = native.clone();
    let app = app.clone();
    drop(windows);
    tauri::async_runtime::spawn(async move {
        let _windows = native.windows.lock().await;
        let result = async {
            let epoch = native.presentation_epoch(intent.owner)?;
            native.open_account(epoch, url).await
        }
        .await;
        native.presentation.lock().expect("presentation").staging = false;
        let client = app.state::<Client>();
        let mut queue = client.presentations.lock().expect("presentation intent");
        if result.is_ok() || client.coordinator.presentation_epoch(intent.owner).is_err() {
            queue.complete(intent.id);
        } else {
            queue.logout = None;
        }
        drop(queue);
        drop(_windows);
        native.changed(&client.coordinator.state());
    });
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
            return Some(
                destination.as_str() == "about:blank"
                    || (destination.origin() == candidate.destination.origin()
                        && destination.username().is_empty()
                        && destination.password().is_none()),
            );
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
        let Some(account) = account else { return Ok(()) };
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

    async fn open_account(&self, epoch: Epoch, destination: Url) -> Result<(), ClientError> {
        epoch.check()?;
        self.close_account().await?;
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
            views.account = Some(AccountDocument {
                label: label.clone(),
                window: physical,
                destination: destination.clone(),
                document: 0,
                admitted: false,
                loaded: false,
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
                            let mut queue = client.presentations.lock().expect("presentation intent");
                            let physical = queue.window;
                            queue.document_started(physical, event.url());
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
                let builder = builder.initialization_script(crate::macos_title_bar::inset_script());
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
            if !self
                .views
                .lock()
                .expect("views")
                .account
                .as_ref()
                .is_some_and(|account| account.label == label && account.admitted)
            {
                return Err(ClientError::new("stale_generation"));
            }
            if let Some(runtime) = runtime_webview(&self.app) {
                runtime.hide().map_err(failure)?;
            }
            view.show().map_err(failure)?;
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
    async fn retire_current(&self) -> Result<(), ClientError> {
        let role = self.views.lock().expect("views").runtime.take();
        if let Some(role) = role {
            self.retire_runtime(role).await?;
        }
        Ok(())
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
        self.app
            .run_on_main_thread(move || {
                let result = checked.check().and_then(|_| {
                    crate::build_main_child(&app, &created_label, disposable, path, checked.value)
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
        loop {
            let epoch = self.presentation_epoch(owner)?;
            let result = async {
                let url = self.local_url()?;
                let candidate = self.build_candidate(epoch.clone(), url.clone(), false, vec![]).await?;
                epoch.check()?;
                self.retire_current().await?;
                epoch.check()?;
                if !preserve_account {
                    self.close_account().await?;
                    self.show(epoch.clone(), &candidate, &url).await?;
                }
                epoch.check()?;
                let mut views = self.views.lock().expect("views");
                views.runtime = views.candidate.take();
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
        crate::pet::reconcile(&self.app);
        crate::refresh_native_controls(&self.app, None);
    }
}
#[async_trait]
impl ClientView for NativeView {
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
        if self.windows.try_lock().is_ok() {
            if let Some(runtime) = runtime_webview(&self.app) {
                // Runtime receives only the minimal projection even while retained.
                let _ = runtime.eval(self.script(false, None));
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
    async fn restore(&self, epoch: Epoch, target: ClientTarget, preserve_document: bool) -> Result<(), ClientError> {
        let _windows = self.windows.lock().await;
        let owner = epoch.value;
        self.presentation_epoch(owner)?;
        self.discard_candidate().await?;
        if preserve_document {
            let views = self.views.lock().expect("views");
            if let Some(account) = &views.account {
                if !views.runtime.as_ref().is_some_and(|runtime| {
                    account.returning.as_ref() == Some(&(runtime.label.clone(), runtime.document))
                }) {
                    return Err(ClientError::new("instance_session_missing"));
                }
            }
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
                self.close_account().await?;
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
        self.open_account(epoch, url).await
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The popup only opens while main shows another computer, so the updater's
    /// own check must admit it there; otherwise Update, Check and both switches
    /// would all be refused from the popup. Drives the same window lookup
    /// `NativeView::desktop_update` uses; main's account-page test is passed in.
    #[test]
    fn the_update_popup_may_drive_the_updater_while_main_shows_another_computer() {
        use tauri::{WebviewUrl, WebviewWindowBuilder};

        let app = tauri::test::mock_app();
        let bundle = Url::parse("tauri://localhost/").unwrap();
        let window = |label: &str, raw: &str| {
            WebviewWindowBuilder::new(&app, label, WebviewUrl::External(Url::parse(raw).unwrap()))
                .build()
                .unwrap()
        };
        let account_page = || {
            app.get_webview(MAIN_WINDOW)
                .is_some_and(|window| window.url().is_ok_and(|url| account_view(&url, &bundle).is_some()))
        };
        // Main shows another computer's page and no popup is open: refused.
        window(MAIN_WINDOW, "https://studio.avibe.bot/");
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
        assert_eq!(parse("avibe://updates"), Some(("updates", None, None)));
        assert_eq!(
            parse("avibe://desktop/updates?native_request=1-2"),
            Some(("updates", Some("1-2".into()), None))
        );
        for url in [
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
