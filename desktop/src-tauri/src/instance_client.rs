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
    if let Some(window) = app.get_webview_window(MAIN_WINDOW) {
        if window.url().is_ok_and(|url| caller_can_present(app, MAIN_WINDOW, &url)) {
            let view = serde_json::to_string(view).expect("view");
            let _ = window.eval(format!("if(window.dispatchEvent(new CustomEvent('avibe:desktop-present',{{cancelable:true,detail:{{view:{view}}}}}))) window.location.assign('avibe://desktop/'+{view});"));
            return;
        }
    }
    present(app, view);
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
    window.label() == MAIN_WINDOW
        && client.view.presentation.lock().expect("presentation").account
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
/// Native presentation requests share one caller check in every shell assembly.
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
    let view = presentation_view(destination);
    let updates = destination.as_str() == crate::updater::OPEN_URL;
    if view.is_none() && !updates {
        return false;
    }
    // Wry's macOS URL getter has no value during the initial navigation.
    // Presentation can only originate from an already loaded document; do not
    // ask for its current URL when admitting bootstrap/ordinary navigation.
    let allowed = current().is_some_and(|url| caller_can_present(app, label, &url));
    if let Some(view) = view {
        if allowed {
            present(app, view);
        }
        return true;
    }
    if updates {
        if allowed {
            crate::updater::trusted_decision(app.clone());
        }
        return true;
    }
    false
}
pub fn present(app: &AppHandle, view: &'static str) {
    let Some(client) = app.try_state::<Client>() else {
        return;
    };
    let native = client.view.clone();
    let generation = client.coordinator.state().generation;
    let app = app.clone();
    let _ = app.clone().run_on_main_thread(move || {
        if !epoch_matches(&app, generation) {
            return;
        }
        let Ok(_windows) = native.windows.try_lock() else {
            return;
        };
        let Some(window) = app.get_webview_window(MAIN_WINDOW) else {
            return;
        };
        let Ok(mut url) = native.bundle.join("/account/index.html") else {
            return;
        };
        url.set_query(Some(&format!("view={view}")));
        {
            let mut state = native.presentation.lock().expect("presentation");
            if state.staging || state.pending_commit.is_some() {
                return;
            }
            if !state.account {
                state.route = window.url().ok();
            }
            state.account = true;
        }
        if window.navigate(url).is_err() {
            native.presentation.lock().expect("presentation").account = false;
        }
    });
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
