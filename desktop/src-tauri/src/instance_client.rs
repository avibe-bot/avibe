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
use tauri::{AppHandle, Manager, WebviewWindow};
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
}
impl PresentationQueue {
    fn enqueue(&mut self, view: &'static str, owner: u64, guarded: bool) {
        self.sequence += 1;
        self.guard = None;
        self.approval = None;
        self.pending = Some(PresentationIntent {
            id: self.sequence,
            view,
            owner,
            guarded,
        });
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
#[derive(Clone)]
struct PreviousView {
    remote: Option<Url>,
    route: Option<Url>,
    cookies: Vec<cookie::Cookie<'static>>,
}
struct Presentation {
    state: ClientState,
    previous: Option<PreviousView>,
    account: bool,
    disposable: bool,
    staging: bool,
    pending_commit: Option<u64>,
    local_owner: Option<u64>,
    remote: Option<Url>,
    route: Option<Url>,
}
struct NativeView {
    app: AppHandle,
    bundle: Url,
    local_origin: Arc<Mutex<Option<LoopbackOrigin>>>,
    presentation: Mutex<Presentation>,
    windows: Arc<tokio::sync::Mutex<()>>,
    navigation: Mutex<Option<Navigation>>,
    store_directory: Mutex<Option<tempfile::TempDir>>,
}
struct Navigation {
    epoch: Epoch,
    destination: Url,
    completed: tokio::sync::oneshot::Sender<Result<(), ClientError>>,
}
fn failure(_: impl std::fmt::Display) -> ClientError {
    ClientError::new("window_unavailable")
}

pub(crate) fn enabled(app: &AppHandle) -> bool {
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
pub(crate) fn download_epoch(app: &AppHandle, current: &Url, target: &Url) -> Option<Epoch> {
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
    caller_can_present(app, MAIN_WINDOW, current).then(|| client.coordinator.epoch())
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
            previous: None,
            account: false,
            disposable: false,
            staging: false,
            pending_commit: None,
            local_owner: None,
            remote: None,
            route: None,
        }),
        windows: Arc::new(tokio::sync::Mutex::new(())),
        navigation: Mutex::new(None),
        store_directory: Mutex::new(None),
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
    // The bundled account entry is already a trusted native document and does
    // not mount the Workbench's guarded presentation consumer. Keep the
    // unsaved-work handoff for Runtime documents, but let this known entry
    // navigate through the same owner-fenced queue without inventing a
    // Runtime fallback.
    let guarded = app
        .get_webview_window(MAIN_WINDOW)
        .and_then(|window| window.url().ok())
        .is_none_or(|current| {
            let state = client.view.presentation.lock().expect("presentation");
            !(state.account && account_view(&current, &client.view.bundle).is_some())
        });
    present_owned(app, view, client.coordinator.presentation_owner(), guarded, None)
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
pub fn page_loaded(app: &AppHandle) {
    if let Some(client) = app.try_state::<Client>() {
        client.view.changed(&client.coordinator.state());
    }
}
/// The per-window callback captures the generation at construction. A late
/// event from a retired window with the reused main label cannot complete a
/// successor's navigation, including a reconnect to the same instance.
pub(crate) fn navigation_loaded(app: &AppHandle, generation: u64, url: &Url) {
    let Some(client) = app.try_state::<Client>() else {
        return;
    };
    let mut pending = client.view.navigation.lock().expect("navigation");
    if pending.as_ref().is_some_and(|pending| {
        pending.epoch.value == generation
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
fn trusted_page(client: &Client, window: &WebviewWindow) -> bool {
    let owned = {
        let presentation = client.view.presentation.lock().expect("presentation");
        presentation.account && !presentation.staging && presentation.pending_commit.is_none()
    };
    window.label() == MAIN_WINDOW
        && owned
        && window
            .url()
            .is_ok_and(|url| account_view(&url, &client.view.bundle).is_some())
}
#[tauri::command]
pub async fn desktop_client_request(
    window: WebviewWindow,
    app: AppHandle,
    request: ClientRequest,
) -> Result<ClientReply, String> {
    let client = app.try_state::<Client>().ok_or("desktop_client_unavailable")?;
    if !trusted_page(&client, &window) {
        return Err("untrusted_desktop_caller".into());
    }
    let coordinator = client.coordinator.clone();
    Ok(coordinator.request(request).await)
}

/// Only the current main document can request enumerated presentation. This is
/// separate from navigation admission and never performs the presented action.
pub fn caller_can_present(app: &AppHandle, label: &str, current: &Url) -> bool {
    if label != MAIN_WINDOW {
        return false;
    }
    let Some(client) = app.try_state::<Client>() else {
        return false;
    };
    if client.view.windows.try_lock().is_err() {
        return false;
    }
    let view = client.view.presentation.lock().expect("presentation");
    if view.staging || view.pending_commit.is_some() {
        return false;
    }
    if view.account {
        return account_view(current, &client.view.bundle).is_some();
    }
    if let Some(origin) = &view.remote {
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
fn presentation_destination(destination: &Url) -> Option<(&'static str, Option<String>)> {
    let mut base = destination.clone();
    let token = if destination.query().is_some() {
        let pairs: Vec<_> = destination.query_pairs().collect();
        if pairs.len() != 1 || pairs[0].0 != "native_request" || pairs[0].1.is_empty() || pairs[0].1.len() > 128 {
            return None;
        }
        base.set_query(None);
        Some(pairs[0].1.to_string())
    } else {
        None
    };
    let view = presentation_view(&base).or_else(|| (base.as_str() == crate::updater::OPEN_URL).then_some("updates"))?;
    Some((view, token))
}

/// Native presentation requests share one source check in every shell assembly.
/// Returning true means the URL was consumed, never that an action was approved.
pub fn presentation_request(
    app: &AppHandle,
    label: &str,
    current: impl FnOnce() -> Option<Url>,
    destination: &Url,
) -> bool {
    if !enabled(app) || label != MAIN_WINDOW {
        return false;
    }
    let Some((view, token)) = presentation_destination(destination) else {
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
    let allowed = {
        let view = client.view.presentation.lock().expect("presentation");
        let local = client.view.local_origin.lock().expect("local origin");
        presentation_source_allowed(
            &view,
            &client.view.bundle,
            local.as_ref(),
            client.coordinator.committed_view_generation(),
            label,
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
        present_owned(app, view, owner, false, approval.as_ref());
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
    present_owned(app, view, client.coordinator.presentation_owner(), false, None)
}

fn present_owned(
    app: &AppHandle,
    view: &'static str,
    owner: u64,
    guarded: bool,
    approval: Option<&GuardAttempt>,
) -> bool {
    let Some(client) = app.try_state::<Client>() else {
        return false;
    };
    let Ok(mut url) = client.view.bundle.join("/account/index.html") else {
        return false;
    };
    url.set_query(Some(&format!("view={view}")));
    if account_view(&url, &client.view.bundle).is_none() {
        return false;
    }
    {
        let mut queue = client.presentations.lock().expect("presentation intent");
        if client.coordinator.presentation_owner() != owner
            || approval.is_some_and(|attempt| !queue.owns_guard_authority(attempt))
        {
            return false;
        }
        queue.enqueue(view, owner, guarded);
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
    let Some(window) = app.get_webview_window(MAIN_WINDOW) else {
        return;
    };
    let Ok(mut url) = native.bundle.join("/account/index.html") else {
        return;
    };
    url.set_query(Some(&format!("view={}", intent.view)));
    let route = window.url().ok();
    if client.coordinator.presentation_epoch(intent.owner).is_err() {
        client
            .presentations
            .lock()
            .expect("presentation intent")
            .complete(intent.id);
        return;
    }
    if intent.guarded {
        let Some(receiver) = route else { return };
        let allowed = {
            let state = native.presentation.lock().expect("presentation");
            let local = native.local_origin.lock().expect("local origin");
            presentation_source_allowed(
                &state,
                &native.bundle,
                local.as_ref(),
                client.coordinator.committed_view_generation(),
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
    let previous = {
        let mut state = native.presentation.lock().expect("presentation");
        if state.staging || state.pending_commit.is_some() {
            return;
        }
        let previous = (state.account, state.route.clone());
        if !state.account {
            state.route = route;
        }
        state.account = true;
        previous
    };
    if window.navigate(url).is_err() {
        let mut state = native.presentation.lock().expect("presentation");
        state.account = previous.0;
        state.route = previous.1;
        return;
    }
    client
        .presentations
        .lock()
        .expect("presentation intent")
        .complete(intent.id);
    drop(windows);
    // A newer presentation may have arrived during native admission.
    schedule_presentation(app);
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
    if label != MAIN_WINDOW {
        return None;
    }
    let state = client.view.presentation.lock().expect("presentation");
    if state.staging && destination.as_str() == "about:blank" {
        return Some(true);
    }
    if state.account {
        return Some(account_view(destination, &client.view.bundle).is_some());
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
    fn script(&self, initial: bool, receiver: Option<&Url>) -> String {
        let state = self.presentation.lock().expect("presentation");
        // Initialization survives navigation for the lifetime of a WebView.
        // It can never contain account-only state, even for an account build.
        let account_receiver =
            receiver.filter(|url| !initial && state.account && account_view(url, &self.bundle).is_some());
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
                    " && window.location.href === {}",
                    serde_json::to_string(url.as_str()).expect("receiver URL")
                )
            });
            format!("if (window.self === window.top{receiver_guard}) {{{dispatch}}}")
        }
    }
    async fn remember_current(&self, epoch: &Epoch) -> Result<(), ClientError> {
        let (remote, route) = {
            let state = self.presentation.lock().expect("presentation");
            if state.previous.is_some() {
                // An interrupted candidate never becomes the rollback owner.
                return epoch.check();
            }
            (state.remote.clone(), state.route.clone())
        };
        let cookies = if let Some(origin) = &remote {
            let window = self
                .app
                .get_webview_window(MAIN_WINDOW)
                .ok_or_else(|| failure("missing"))?;
            let origin = origin.clone();
            tokio::task::spawn_blocking(move || window.cookies_for_url(origin).map_err(failure))
                .await
                .map_err(failure)??
        } else {
            vec![]
        };
        epoch.check()?;
        self.presentation.lock().expect("presentation").previous = Some(PreviousView { remote, route, cookies });
        Ok(())
    }
    fn presentation_epoch(&self, owner: u64) -> Result<Epoch, ClientError> {
        self.app.state::<Client>().coordinator.presentation_epoch(owner)
    }
    fn lifecycle_retry(&self, owner: u64, epoch: &Epoch, result: &Result<(), ClientError>) -> bool {
        result.as_ref().is_err_and(|error| error.code == "stale_generation")
            && epoch.check().is_err()
            && self.presentation_epoch(owner).is_ok()
    }
    async fn restore_previous(&self, owner: u64, previous: &PreviousView, account: bool) -> Result<(), ClientError> {
        if previous.remote.is_none() && !account {
            return self.restore_local(owner, previous.route.clone()).await;
        }
        // The caller holds windows for the whole obligation, including show.
        // Keep the original material across repeated lifecycle invalidations;
        // a later user action replaces the owner and stops this continuation.
        loop {
            let epoch = self.presentation_epoch(owner)?;
            let result = async {
                self.retire_current().await?;
                epoch.check()?;
                let disposable = previous.remote.is_some();
                let url = if account {
                    self.bundle.join("account/index.html").map_err(failure)?
                } else {
                    previous
                        .route
                        .clone()
                        .or(previous.remote.clone())
                        .unwrap_or(self.local_url()?)
                };
                let committed = self.app.state::<Client>().coordinator.state();
                {
                    let mut state = self.presentation.lock().expect("presentation");
                    state.account = account;
                    state.pending_commit = None;
                    state.state = committed;
                    state.remote = previous.remote.clone();
                    state.disposable = disposable;
                    state.route = previous.route.clone();
                }
                self.build(epoch.clone(), url.clone(), disposable, previous.cookies.clone())
                    .await?;
                self.show(epoch.clone(), &url).await?;
                epoch.check()
            }
            .await;
            if self.lifecycle_retry(owner, &epoch, &result) {
                continue;
            }
            if result.is_ok() {
                self.presentation.lock().expect("presentation").previous = None;
            }
            return result;
        }
    }
    async fn restore_local(&self, owner: u64, route: Option<Url>) -> Result<(), ClientError> {
        self.presentation_epoch(owner)?;
        self.presentation.lock().expect("presentation").local_owner = Some(owner);
        loop {
            let epoch = self.presentation_epoch(owner)?;
            let result = async {
                self.retire_current().await?;
                epoch.check()?;
                let url = route
                    .clone()
                    .filter(|url| {
                        self.local_origin
                            .lock()
                            .ok()
                            .and_then(|origin| origin.clone())
                            .is_some_and(|origin| origin.matches_url_origin(url))
                    })
                    .unwrap_or(self.local_url()?);
                let committed = self.app.state::<Client>().coordinator.state();
                {
                    let mut state = self.presentation.lock().expect("presentation");
                    state.account = false;
                    state.pending_commit = None;
                    state.state = committed;
                    state.remote = None;
                    state.disposable = false;
                    state.route = None;
                }
                self.build(epoch.clone(), url.clone(), false, vec![]).await?;
                self.show(epoch.clone(), &url).await?;
                epoch.check()
            }
            .await;
            if self.lifecycle_retry(owner, &epoch, &result) {
                continue;
            }
            if result.is_ok() {
                self.presentation.lock().expect("presentation").previous = None;
            }
            return result;
        }
    }
    async fn finish_commit(&self, owner: u64, receipt: u64) -> Result<(), ClientError> {
        loop {
            let epoch = self.presentation_epoch(owner)?;
            let destination = {
                let state = self.presentation.lock().expect("presentation");
                if state.pending_commit != Some(receipt)
                    || self.app.state::<Client>().coordinator.committed_view_generation() != Some(receipt)
                {
                    return Err(ClientError::new("stale_generation"));
                }
                state.remote.clone().ok_or_else(|| failure("missing target"))?
            };
            let result = self.show(epoch.clone(), &destination).await;
            if self.lifecycle_retry(owner, &epoch, &result) {
                continue;
            }
            result?;
            let mut state = self.presentation.lock().expect("presentation");
            state.previous = None;
            state.pending_commit = None;
            return Ok(());
        }
    }
    async fn retire_current(&self) -> Result<(), ClientError> {
        let disposable = self.presentation.lock().expect("presentation").disposable;
        let retired = if let Some(window) = self.app.get_webview_window(MAIN_WINDOW) {
            tokio::task::spawn_blocking(move || {
                if disposable {
                    crate::instance_store::retire(&window)
                } else {
                    crate::instance_store::destroy_completed(&window)
                }
            })
            .await
            .map_err(failure)
            .and_then(|result| result.map_err(failure))
        } else {
            Ok(())
        };
        let removed = if let Some(directory) = self.store_directory.lock().expect("store directory").take() {
            directory.close().map_err(failure)
        } else {
            Ok(())
        };
        retired.and(removed)
    }
    async fn build(
        &self,
        epoch: Epoch,
        url: Url,
        disposable: bool,
        cookies: Vec<cookie::Cookie<'static>>,
    ) -> Result<(), ClientError> {
        let app = self.app.clone();
        let initial = url.clone();
        self.presentation.lock().expect("presentation").staging = true;
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
        *self.store_directory.lock().expect("store directory") = directory;
        let (tx, rx) = tokio::sync::oneshot::channel();
        let checked = epoch.clone();
        self.app
            .run_on_main_thread(move || {
                let result: Result<WebviewWindow, ClientError> = (|| {
                    checked.check()?;
                    let window = crate::build_main_window(&app, Some(initial), disposable, path)
                        .ok_or_else(|| failure("create"))?;
                    Ok(window)
                })();
                let _ = tx.send(result);
            })
            .map_err(failure)?;
        let window = rx.await.map_err(failure)??;
        self.presentation.lock().expect("presentation").staging = false;
        // Native cookie APIs can block on the UI thread. The new view starts at
        // the bundled page, with no instance request until projection completes.
        let projected = window.clone();
        let checked = epoch.clone();
        tokio::task::spawn_blocking(move || crate::instance_store::project(&projected, &cookies, &checked))
            .await
            .map_err(failure)??;
        let (loaded, navigation) = tokio::sync::oneshot::channel();
        *self.navigation.lock().expect("navigation") = Some(Navigation {
            epoch: epoch.clone(),
            destination: url.clone(),
            completed: loaded,
        });
        let (tx, rx) = tokio::sync::oneshot::channel();
        let checked = epoch.clone();
        self.app
            .run_on_main_thread(move || {
                let result: Result<(), ClientError> = (|| {
                    checked.check()?;
                    window.navigate(url).map_err(failure)?;
                    Ok(())
                })();
                let _ = tx.send(result);
            })
            .map_err(failure)?;
        rx.await.map_err(failure)??;
        let result = tokio::time::timeout(std::time::Duration::from_secs(30), navigation)
            .await
            .map_err(|_| ClientError::new("instance_navigation_timeout"))?
            .map_err(failure)?;
        epoch.check()?;
        result
    }
    async fn show(&self, epoch: Epoch, destination: &Url) -> Result<(), ClientError> {
        let app = self.app.clone();
        let bundle = self.bundle.clone();
        let destination = destination.clone();
        let (sender, receiver) = tokio::sync::oneshot::channel();
        self.app
            .run_on_main_thread(move || {
                let result = (|| {
                    epoch.check()?;
                    let window = app.get_webview_window(MAIN_WINDOW).ok_or_else(|| failure("missing"))?;
                    let actual = window.url().map_err(failure)?;
                    let matches = if destination == bundle || account_view(&destination, &bundle).is_some() {
                        actual == destination
                    } else {
                        actual.origin() == destination.origin()
                    };
                    if !matches {
                        return Err(ClientError::new("instance_navigation_mismatch"));
                    }
                    window.show().map_err(failure)?;
                    window.set_focus().map_err(failure)?;
                    if app.try_state::<Shell>().is_some() {
                        crate::refresh_native_controls(&app, None);
                    }
                    epoch.check()
                })();
                let _ = sender.send(result);
            })
            .map_err(failure)?;
        receiver.await.map_err(failure)?
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
        let window = self
            .app
            .get_webview_window(MAIN_WINDOW)
            .ok_or_else(|| ClientError::new("untrusted_desktop_caller"))?;
        if !trusted_page(&client, &window) {
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
            let committed = coordinator.committed_view_generation();
            let (previous, committed_candidate, local) = {
                let state = view.presentation.lock().expect("presentation");
                if state.pending_commit.is_some() && state.pending_commit == committed {
                    // Public commit won the lifecycle race after load completed.
                    (None, state.pending_commit, false)
                } else {
                    (
                        state.previous.clone(),
                        None,
                        state.local_owner == Some(owner) && !state.account && state.remote.is_none(),
                    )
                }
            };
            let result = if let Some(previous) = previous {
                view.restore_previous(owner, &previous, true).await
            } else if let Some(receipt) = committed_candidate {
                view.finish_commit(owner, receipt).await
            } else if local {
                // A completed local presentation can be invalidated just after
                // its terminal show. Reconcile only the current local intent;
                // an account page or committed remote is never displaced.
                let expected = view.local_url();
                let settled = expected.as_ref().is_ok_and(|expected| {
                    view.app.get_webview_window(MAIN_WINDOW).is_some_and(|window| {
                        window.is_visible().unwrap_or(false) && window.url().is_ok_and(|url| url == *expected)
                    })
                });
                if settled {
                    Ok(())
                } else {
                    view.restore_local(owner, None).await
                }
            } else {
                Ok(())
            };
            if let Err(error) = result {
                eprintln!("desktop client lifecycle restoration: {}", error.code);
            }
            drop(_windows);
            view.changed(&coordinator.state());
        });
    }
    fn changed(&self, state: &ClientState) {
        {
            let mut view = self.presentation.lock().expect("presentation");
            let staged = view.state.connection.clone();
            view.state = state.clone();
            if view.pending_commit.is_some() {
                // A fresh candidate is still hidden. Its document receives the
                // identity whose cookies it owns, never the previous target's
                // initial footer snapshot. Public commit follows load/readback.
                view.state.connection = staged;
                view.state.remote_auxiliary = RemoteAuxiliary::DisabledIncomplete;
            }
        }
        // During creation WKWebView may not have a URL yet. The serialized
        // owner publishes after completion; no eval can target that gap.
        if self.windows.try_lock().is_ok() {
            if let Some(window) = self.app.get_webview_window(MAIN_WINDOW) {
                if let Ok(url) = window.url() {
                    if caller_can_present(&self.app, MAIN_WINDOW, &url) {
                        let _ = window.eval(self.script(false, Some(&url)));
                    }
                }
            }
        }
        self.auxiliaries();
        // Finished may arrive while this owner still holds the window lock.
        // The owner's terminal publication is the deferred delivery handoff.
        crate::apply_pending_deep_link(&self.app);
        schedule_presentation(&self.app);
    }
    fn committed(&self, epoch: Epoch) {
        let Some(client) = self.app.try_state::<Client>() else {
            return;
        };
        let view = client.view.clone();
        let coordinator = client.coordinator.clone();
        tauri::async_runtime::spawn(async move {
            let _windows = view.windows.lock().await;
            let pending = view.presentation.lock().expect("presentation").pending_commit;
            if pending == Some(epoch.value) {
                if let Err(error) = view.finish_commit(epoch.value, epoch.value).await {
                    eprintln!("desktop client committed presentation: {}", error.code);
                }
            }
            drop(_windows);
            view.changed(&coordinator.state());
        });
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
        let ClientTarget::Remote {
            ref canonical_origin, ..
        } = target
        else {
            return Err(ClientError::new("invalid_action"));
        };
        let url: Url = canonical_origin.parse().map_err(failure)?;
        self.remember_current(&epoch).await?;
        let result = async {
            self.retire_current().await?;
            epoch.check()?;
            {
                let mut state = self.presentation.lock().expect("presentation");
                state.account = false;
                state.disposable = true;
                state.local_owner = None;
                state.remote = Some(url.clone());
                state.route = Some(url.clone());
                state.pending_commit = Some(epoch.value);
                state.state.connection = ConnectionState {
                    committed: target,
                    pending: None,
                    status: ConnectionStatus::RemoteConnected,
                };
                state.state.generation = epoch.value;
                state.state.remote_auxiliary = RemoteAuxiliary::DisabledIncomplete;
            }
            self.build(epoch.clone(), url, true, cookies).await
        }
        .await;
        if result.is_err() && epoch.check().is_ok() {
            let previous = self.presentation.lock().expect("presentation").previous.clone();
            if let Some(previous) = previous {
                self.restore_previous(epoch.value, &previous, true).await?;
            }
        }
        result
    }
    async fn restore(&self, epoch: Epoch, target: ClientTarget) -> Result<(), ClientError> {
        let _windows = self.windows.lock().await;
        let owner = epoch.value;
        self.presentation_epoch(owner)?;
        let previous = {
            let mut state = self.presentation.lock().expect("presentation");
            let matches_previous = state.previous.as_ref().is_some_and(|old| match &target {
                ClientTarget::Local { .. } => old.remote.is_none(),
                ClientTarget::Remote { canonical_origin, .. } => old
                    .remote
                    .as_ref()
                    .is_some_and(|origin| origin.origin().ascii_serialization() == *canonical_origin),
            });
            if matches_previous {
                state.previous.clone()
            } else {
                state.previous = None;
                None
            }
        };
        if let Some(previous) = previous {
            return self.restore_previous(owner, &previous, false).await;
        }
        let local = target.is_local();
        let route = {
            let state = self.presentation.lock().expect("presentation");
            state.route.clone()
        };
        if local {
            return self.restore_local(owner, route).await;
        }
        let url = route.ok_or_else(|| ClientError::new("instance_session_missing"))?;
        let epoch = self.presentation_epoch(owner)?;
        let app = self.app.clone();
        let (tx, rx) = tokio::sync::oneshot::channel();
        self.presentation.lock().expect("presentation").account = false;
        self.app
            .run_on_main_thread(move || {
                let result = epoch
                    .check()
                    .and_then(|_| app.get_webview_window(MAIN_WINDOW).ok_or_else(|| failure("missing")))
                    .and_then(|window| window.navigate(url).map_err(failure));
                let _ = tx.send(result);
            })
            .map_err(failure)?;
        rx.await.map_err(failure)?
    }
    async fn signed_out(&self, epoch: Epoch, _: ClientTarget) -> Result<(), ClientError> {
        let _windows = self.windows.lock().await;
        let owner = epoch.value;
        if self.presentation_epoch(owner).is_err() && !self.presentation.lock().expect("presentation").disposable {
            // A newer explicit local escape already retired the instance and
            // presented its replacement. Do not erase that new local window.
            return Ok(());
        }
        // Erase even when another local escape superseded navigation.
        let retired = self.retire_current().await;
        {
            let mut state = self.presentation.lock().expect("presentation");
            state.account = false;
            state.pending_commit = None;
            state.remote = None;
            state.disposable = false;
            state.route = None;
            state.previous = None;
        }
        // A newer explicit escape owns its own queued restore. Lifecycle-only
        // changes keep this owner and re-evaluate the local destination.
        let restored = if self.presentation_epoch(owner).is_ok() {
            match self.restore_local(owner, None).await {
                Err(error) if error.code == "stale_generation" && self.presentation_epoch(owner).is_err() => Ok(()),
                result => result,
            }
        } else {
            Ok(())
        };
        retired.and(restored)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    // A footer/menu intent used to disappear when its deferred callback met a
    // busy window or a lifecycle rebind. Only actual admission may settle it.
    #[test]
    fn presentation_intents_survive_retries_until_the_exact_navigation_is_admitted() {
        let mut queue = PresentationQueue::default();
        queue.enqueue("updates", 4, false);
        let first = queue.current(4).unwrap();
        // Lock contention, pending presentation and failed navigation leave the
        // same intent available. Lifecycle generation is deliberately not owner.
        for _ in 0..3 {
            assert_eq!(queue.current(4), Some(first));
        }
        queue.enqueue("account", 4, false);
        let newer = queue.current(4).unwrap();
        queue.complete(first.id);
        assert_eq!(queue.current(4), Some(newer));
        queue.complete(newer.id);
        assert!(queue.current(4).is_none());
    }

    #[test]
    fn a_new_explicit_action_supersedes_an_old_presentation_without_reviving_it() {
        let mut queue = PresentationQueue::default();
        queue.enqueue("updates", 4, false);
        let old = queue.current(4).unwrap();
        assert!(queue.current(5).is_none());
        queue.enqueue("updates", 5, false);
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
        queue.enqueue("updates", 4, true);
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

        queue.enqueue("updates", 4, true);
        let intent = queue.current(4).unwrap();
        let old = queue.begin_guard(intent, retry.receiver.clone()).unwrap();
        queue.enqueue("instances", 4, true);
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
            queue.enqueue("updates", 4, true);
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
            queue.enqueue("updates", 4, false);
            let approved = queue.current(4).unwrap();
            queue.guard_result(&attempt, "\"dispatched\"");
            assert_eq!(queue.current(4), Some(approved));
            assert!(!queue.owns_guard_authority(&attempt));
            assert!(queue.guard_authority(&token, "updates", 4, &receiver).is_none());
        }
        let mut queue = PresentationQueue::default();
        let receiver = Url::parse("https://fixture.avibe.bot/apps/1").unwrap();
        queue.enqueue("updates", 4, true);
        let intent = queue.current(4).unwrap();
        let old = queue.begin_guard(intent, receiver.to_string()).unwrap();
        queue.guard_result(&old, "\"dispatched\"");
        queue.enqueue("instances", 4, true);
        assert!(queue.guard_authority(&old.token(), "updates", 4, &receiver).is_none());
        assert!(!queue.owns_guard_authority(&old));
    }

    #[test]
    fn native_presentation_tokens_have_no_malformed_or_uncorrelated_fallback() {
        let parse = |url| presentation_destination(&Url::parse(url).unwrap());
        assert_eq!(parse("avibe://updates"), Some(("updates", None)));
        assert_eq!(
            parse("avibe://desktop/updates?native_request=1-2"),
            Some(("updates", Some("1-2".into())))
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
            state: ClientState::new("fixture".into(), "0.1.0".into(), serde_json::json!({})),
            previous: None,
            account: false,
            disposable: false,
            staging: false,
            pending_commit: None,
            local_owner: None,
            remote: None,
            route: None,
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
