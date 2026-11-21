//! Desktop direct in the running app: does what `wiring.rs` decides, against
//! real windows, and keeps each session's client asking its rendezvous.
//!
//! Nothing here is reachable from a page: there is no command, no message
//! handler and no bridge. The choice of path is made in the app's own menus.
//! Logs name what happened and nothing else: no host, address, ID or count
//! that could identify an instance.
use super::{
    choices::{Choices, Mode},
    rendezvous::{origin_of, Request},
    wiring::{Effect, Material, Published, Routing, Target, Wiring},
    Client,
};
use crate::instance_client;
use avibe_runtime_host::instance_client::protocol::ClientTarget;
use std::{
    collections::HashMap,
    sync::{Arc, Mutex, MutexGuard},
    time::{Duration, SystemTime},
};
use tauri::{
    menu::{CheckMenuItem, Submenu},
    webview::{PermissionKind, PermissionResponse},
    AppHandle, Manager, Runtime,
};
use tokio::{
    sync::{oneshot, Notify},
    time::Instant,
};

pub(crate) const MENU_AUTOMATIC: &str = "direct-connection-automatic";
pub(crate) const MENU_TUNNEL: &str = "direct-connection-tunnel";

/// A rendezvous answer lasts five minutes; it is asked again at four.
const RENEW: Duration = Duration::from_secs(240);
/// After an unanswered rendezvous: sooner at first (the page may still be
/// loading), then rarely, so an instance that offers no direct path is asked
/// about four times an hour.
const RETRY: [Duration; 5] = [
    Duration::from_secs(5),
    Duration::from_secs(15),
    Duration::from_secs(60),
    Duration::from_secs(300),
    Duration::from_secs(900),
];
/// How often a session looks at the clock, to notice that the computer slept.
const GLANCE: Duration = Duration::from_secs(30);
/// How often open windows are looked at while any session exists.
const LOOK: Duration = Duration::from_secs(5);

/// The 「连接方式」 submenu: this computer's choice of path for the instance
/// on show (ui/design: desktop/connection-choice/menu-*). Its three strings
/// are the Devices page's own, read by key.
pub(crate) struct ConnectionMenu<R: Runtime> {
    pub submenu: Submenu<R>,
    pub automatic: CheckMenuItem<R>,
    pub tunnel: CheckMenuItem<R>,
}

impl<R: Runtime> ConnectionMenu<R> {
    /// Greyed, with "automatic" ticked, until `show` says otherwise.
    pub(crate) fn build(app: &AppHandle<R>) -> tauri::Result<Option<Self>> {
        let text = |key: &str| crate::product_text(&format!("devices.connection.{key}"));
        let (Some(title), Some(automatic), Some(tunnel)) = (text("title"), text("automatic"), text("tunnelOnly"))
        else {
            return Ok(None);
        };
        let automatic = CheckMenuItem::with_id(app, MENU_AUTOMATIC, automatic, true, true, None::<&str>)?;
        let tunnel = CheckMenuItem::with_id(app, MENU_TUNNEL, tunnel, true, false, None::<&str>)?;
        let submenu = Submenu::with_id_and_items(app, "direct-connection", title, false, &[&automatic, &tunnel])?;
        Ok(Some(Self {
            submenu,
            automatic,
            tunnel,
        }))
    }

    /// The choice for the instance on show; `None`, and greyed, while this
    /// computer's own instance is. The system ticks whatever was clicked, so
    /// both ticks are always set from the choice.
    pub(crate) fn show(&self, chosen: Option<Mode>) {
        let _ = self.submenu.set_enabled(chosen.is_some());
        let _ = self.automatic.set_checked(chosen != Some(Mode::Tunnel));
        let _ = self.tunnel.set_checked(chosen == Some(Mode::Tunnel));
    }
}

pub(crate) struct Direct {
    wiring: Mutex<Wiring>,
    /// Ends a session's task, which then drops its client.
    stops: Mutex<HashMap<u64, oneshot::Sender<()>>>,
    /// The instance view the main window held at the last look.
    shown: Mutex<Option<Published>>,
    wake: Notify,
    menu: Mutex<Option<ConnectionMenu<tauri::Wry>>>,
    /// Whether this system and configuration can route at all.
    available: bool,
}

fn lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex.lock().unwrap_or_else(|poisoned| poisoned.into_inner())
}

fn direct(app: &AppHandle) -> Option<Arc<Direct>> {
    app.try_state::<Arc<Direct>>().map(|state| state.inner().clone())
}

fn routing() -> Routing {
    if cfg!(windows) {
        Routing::AtCreation
    } else if cfg!(target_os = "macos") {
        Routing::on_macos(macos_major())
    } else {
        Routing::Unsupported
    }
}

#[cfg(target_os = "macos")]
fn macos_major() -> u64 {
    crate::macos_show_share::os_version().0
}

#[cfg(not(target_os = "macos"))]
fn macos_major() -> u64 {
    0
}

/// Starts desktop direct for this run of the app. Until an instance view is
/// published nothing runs: no timer, no socket, no request.
pub(crate) fn init(app: &AppHandle) {
    let choices = app
        .path()
        .app_local_data_dir()
        .map(|directory| Choices::load(directory.join("direct-paths.json")))
        .unwrap_or_else(|_| Choices::unsaved());
    let proxy_configured = app
        .config()
        .app
        .windows
        .iter()
        .find(|window| window.label == crate::MAIN_WINDOW)
        .is_some_and(|window| window.proxy_url.is_some());
    let routing = routing();
    let state = Arc::new(Direct {
        wiring: Mutex::new(Wiring::new(routing, proxy_configured, choices)),
        stops: Mutex::new(HashMap::new()),
        shown: Mutex::new(None),
        wake: Notify::new(),
        menu: Mutex::new(None),
        available: routing != Routing::Unsupported && !proxy_configured,
    });
    app.manage(state.clone());
    let app = app.clone();
    tauri::async_runtime::spawn(async move {
        loop {
            if lock(&state.wiring).busy() {
                tokio::select! {
                    _ = state.wake.notified() => {}
                    _ = tokio::time::sleep(LOOK) => {}
                }
            } else {
                state.wake.notified().await;
            }
            look(&app, &state).await;
        }
    });
}

/// Something that decides sessions may have changed: the shown instance, a
/// sign-in or sign-out, a window that closed. Cheap, and safe on any thread.
pub(crate) fn poke(app: &AppHandle) {
    if let Some(state) = direct(app) {
        state.wake.notify_one();
    }
}

/// The instance view the main window holds: a signed-in instance of another
/// computer whose own document is in that view.
fn published(app: &AppHandle) -> Option<Published> {
    let client = app.try_state::<instance_client::Client>()?;
    let state = client.coordinator.state();
    let ClientTarget::Remote {
        instance_id,
        canonical_origin,
        ..
    } = state.connection.committed
    else {
        return None;
    };
    let account = state.account.identity?.id;
    let view = instance_client::runtime_webview(app)?;
    if view.label() == crate::MAIN_WINDOW || origin_of(view.url().ok()?.as_str())? != canonical_origin {
        return None;
    }
    Some(Published {
        target: Target {
            account,
            origin: canonical_origin,
            instance: instance_id,
        },
        runtime: view.label().to_owned(),
    })
}

/// Whether the system's own proxy settings rule out a direct route to
/// `host` now (`Wiring::system_proxy` has the rule; the direct core reads
/// the settings, as it does for its own public leg). They can change at any
/// time, so they are read at every look and before every window that would
/// be routed, never remembered. Settings that cannot be read rule it out.
async fn system_proxy_rules_out(host: &str) -> bool {
    !Client::system_proxy_allows_route(host).await
}

/// One look at the app, off the main thread: reads a window's URL.
async fn look(app: &AppHandle, state: &Arc<Direct>) {
    let shown = published(app);
    *lock(&state.shown) = shown.clone();
    if let Some(host) = shown.as_ref().and_then(|shown| shown.target.host()) {
        let rules_out = system_proxy_rules_out(host).await;
        lock(&state.wiring).system_proxy(rules_out);
    }
    let effects = lock(&state.wiring).reconcile(
        shown.as_ref(),
        |window| app.get_webview(window).is_some(),
        Instant::now(),
    );
    for effect in effects {
        perform(app, state, effect).await;
    }
    refresh_menu(app, state);
}

/// Routes one window. It only hands the work to the main thread, in order,
/// so a window routed here is routed before anything asked of it later.
fn route_window(app: &AppHandle, state: &Arc<Direct>, effect: Effect) -> Option<Effect> {
    match effect {
        Effect::Install { session, window } => {
            if let Some((host, material)) = route(state, session) {
                native::install(app, window, host, material);
            }
            None
        }
        Effect::Answer { session, window } => {
            if let Some((_, material)) = route(state, session) {
                native::answer(app, window, material);
            }
            None
        }
        other => Some(other),
    }
}

async fn perform(app: &AppHandle, state: &Arc<Direct>, effect: Effect) {
    let Some(effect) = route_window(app, state, effect) else {
        return;
    };
    match effect {
        Effect::Open { runtime } => open(app, state, &runtime).await,
        Effect::Install { .. } | Effect::Answer { .. } => {}
        Effect::Retire { session, detach } => {
            // The route comes off every window before the client that owns
            // its listener is dropped. Taking it off is not what ends the
            // session's connections: a connection that is alive keeps its
            // proxy, and a store may hold on to an emptied route. Dropping
            // the client is: it closes the listener and everything through
            // it, and both routes then fail over to the tunnel.
            for window in detach {
                native::detach(app, window).await;
            }
            if let Some(stop) = lock(&state.stops).remove(&session) {
                let _ = stop.send(());
            }
            eprintln!("desktop direct: a session ended");
        }
    }
}

fn route(state: &Arc<Direct>, session: u64) -> Option<(String, Material)> {
    let wiring = lock(&state.wiring);
    let (host, material) = wiring.route(session)?;
    Some((host.to_owned(), material.clone()))
}

fn material(client: &Client) -> Option<Material> {
    let route = client.route()?;
    Some(Material {
        proxy: route.proxy,
        username: route.username.to_owned(),
        password: route.password.to_owned(),
    })
}

/// Starts a client for the instance view `runtime`, if it is still the one
/// that was looked at.
async fn open(app: &AppHandle, state: &Arc<Direct>, runtime: &str) {
    let Some(shown) = lock(&state.shown).clone().filter(|shown| shown.runtime == runtime) else {
        return;
    };
    let client = match shown.target.host() {
        Some(host) => Client::new(host, shown.target.instance.clone()).await,
        None => None,
    };
    let Some((client, material)) = client.and_then(|client| material(&client).map(|material| (client, material)))
    else {
        // Not an instance the direct path serves (a custom domain), or no
        // loopback port: ask no more for this view for a while.
        lock(&state.wiring).declined(runtime, Instant::now());
        return;
    };
    let session = lock(&state.wiring).open(shown.target, material, Some(runtime), Instant::now());
    start(app, state, session, client);
}

fn start(app: &AppHandle, state: &Arc<Direct>, session: u64, client: Client) {
    let (stop, stopped) = oneshot::channel();
    lock(&state.stops).insert(session, stop);
    eprintln!("desktop direct: a session started");
    tauri::async_runtime::spawn(run(app.clone(), state.clone(), session, client, stopped));
}

/// One session's client, for as long as the session lasts: it asks the
/// rendezvous through the session's instance view, again before the answer
/// runs out, and sooner after the computer slept.
async fn run(app: AppHandle, state: Arc<Direct>, session: u64, mut client: Client, mut stopped: oneshot::Receiver<()>) {
    let mut lifetime = client.listener_lifetime();
    let mut failures = 0usize;
    let mut due = Instant::now();
    let mut glanced = (Instant::now(), SystemTime::now());
    let ended = loop {
        tokio::select! {
            biased;
            _ = &mut stopped => break false,
            changed = lifetime.changed() => {
                if changed.is_err() {
                    break true;
                }
                continue;
            }
            _ = tokio::time::sleep_until(due.min(Instant::now() + GLANCE)) => {}
        }
        // The timer does not run while the computer sleeps, and the answer
        // runs out by the instance's clock: after a sleep, ask at once.
        let slept = glanced.1.elapsed().unwrap_or_default() > glanced.0.elapsed() + GLANCE;
        glanced = (Instant::now(), SystemTime::now());
        if !slept && Instant::now() < due {
            continue;
        }
        let through = {
            let wiring = lock(&state.wiring);
            wiring
                .asks_through(session)
                .map(str::to_owned)
                .zip(wiring.origin(session).map(str::to_owned))
        };
        let Some((window, origin)) = through else {
            // Its window is not the published instance view yet.
            due = Instant::now() + Duration::from_secs(1);
            continue;
        };
        let asking = client.refresh(|endpoint| ask(app.clone(), state.clone(), session, window, origin, endpoint));
        let answered = tokio::select! {
            biased;
            _ = &mut stopped => break false,
            answered = asking => answered,
        };
        let effects = lock(&state.wiring).probed(session, answered);
        for effect in effects {
            perform(&app, &state, effect).await;
        }
        due = Instant::now()
            + if answered {
                failures = 0;
                RENEW
            } else {
                failures += 1;
                RETRY[(failures - 1).min(RETRY.len() - 1)]
            };
    };
    if ended {
        // The listener is gone for good: take its route off before anything else.
        let effects = lock(&state.wiring).listener_ended(session, Instant::now());
        for effect in effects {
            perform(&app, &state, effect).await;
        }
        state.wake.notify_one();
    }
    lock(&state.stops).remove(&session);
    drop(client);
}

/// Asks the rendezvous through `window`, which must still be the session's
/// instance view when the request starts and when its answer arrives.
async fn ask(
    app: AppHandle,
    state: Arc<Direct>,
    session: u64,
    window: String,
    origin: String,
    endpoint: String,
) -> Option<String> {
    let request = Request::new(&origin, &endpoint)?;
    let (answer, answered) = oneshot::channel();
    let handle = app.clone();
    app.run_on_main_thread(move || {
        let Some(view) = handle.get_webview(&window) else {
            return;
        };
        let current = {
            let handle = handle.clone();
            move || {
                lock(&state.wiring).asks_through(session) == Some(window.as_str())
                    && instance_client::runtime_webview(&handle).is_some_and(|view| view.label() == window)
            }
        };
        native::ask(&view, request, current, move |body| {
            let _ = answer.send(body);
        });
    })
    .ok()?;
    answered.await.ok().flatten()
}

/// What a Windows window that shows another computer's instance answers a
/// permission request: no to notifications, and nothing of its own to any
/// other, which is then asked as it always was.
///
/// With that permission a page registers its push worker, and a worker that
/// controls the instance's documents has every rendezvous refused: the
/// window would never leave the tunnel. Nothing that works is lost: such a
/// window is InPrivate, so a push subscription made in it could not outlive
/// it, and the app turns system notifications off while another computer's
/// instance is on show. WKWebView has no `PushManager`, so macOS needs no
/// answer (`examples/direct_webview_probe.rs` holds it to that).
pub(crate) fn remote_instance_permission(kind: PermissionKind) -> PermissionResponse {
    match kind {
        PermissionKind::Notifications => PermissionResponse::Deny,
        _ => PermissionResponse::Default,
    }
}

/// A route for a window that is about to be created, where a route can only
/// be given then.
pub(crate) struct AtCreation {
    session: u64,
    /// The whole of the window's browser arguments, where its route is given
    /// by them.
    pub arguments: Option<String>,
}

/// The main window's instance view for `destination` is about to be created,
/// on the main thread. Where a route can only be given at creation, and this
/// account's instance has answered a rendezvous before, this starts its
/// client and returns the route; otherwise `None`, and the view is created
/// as it always was.
pub(crate) fn instance_view_route(app: &AppHandle, destination: &url::Url) -> Option<AtCreation> {
    let state = direct(app)?;
    let target = target_at(app, destination)?;
    let host = target.host()?.to_owned();
    // The system's settings are read, and the listener is bound, here and
    // now, so its port is in the arguments. Never from a thread that is
    // running the async runtime.
    if tokio::runtime::Handle::try_current().is_ok() {
        return None;
    }
    {
        let rules_out = tauri::async_runtime::block_on(system_proxy_rules_out(&host));
        let mut wiring = lock(&state.wiring);
        wiring.system_proxy(rules_out);
        if !wiring.starts_at_creation(&target) {
            return None;
        }
    }
    let client = tauri::async_runtime::block_on(Client::new(&host, target.instance.clone()))?;
    let material = material(&client)?;
    let arguments = native::arguments(&host, &material)?;
    let session = lock(&state.wiring).open(target, material, None, Instant::now());
    start(app, &state, session, client);
    state.wake.notify_one();
    Some(AtCreation {
        session,
        arguments: Some(arguments),
    })
}

/// The signed-in instance whose document `destination` is, by its origin.
fn target_at(app: &AppHandle, destination: &url::Url) -> Option<Target> {
    let origin = origin_of(destination.as_str())?;
    let client = app.try_state::<instance_client::Client>()?;
    let state = client.coordinator.state();
    let account = state.account.identity?.id;
    let listed = state
        .catalog
        .instances
        .iter()
        .find(|instance| origin_of(&instance.url).as_deref() == Some(origin.as_str()))
        .map(|instance| instance.id.clone());
    let committed = match state.connection.committed {
        ClientTarget::Remote {
            instance_id,
            canonical_origin,
            ..
        } if canonical_origin == origin => Some(instance_id),
        _ => None,
    };
    Some(Target {
        account,
        origin,
        instance: listed.or(committed)?,
    })
}

/// An app window is about to be opened from the instance view at `origin`:
/// the session it shares, and its route where that is given at creation.
pub(crate) async fn app_window_route(app: &AppHandle, origin: &url::Url) -> Option<AtCreation> {
    let state = direct(app)?;
    let origin = origin_of(origin.as_str())?;
    let rules_out = system_proxy_rules_out(origin.strip_prefix("https://")?).await;
    lock(&state.wiring).system_proxy(rules_out);
    if rules_out {
        // The session ends at the next look; this window gets no route.
        state.wake.notify_one();
        return None;
    }
    let account = app
        .try_state::<instance_client::Client>()?
        .coordinator
        .state()
        .account
        .identity?
        .id;
    let wiring = lock(&state.wiring);
    let session = wiring.shared(&account, &origin)?;
    let (host, material) = wiring.route(session)?;
    Some(AtCreation {
        session,
        arguments: native::arguments(host, material),
    })
}

/// The window `route` was made for now exists. Called before the window is
/// sent to its instance, so its route is in place for its first request.
pub(crate) fn window_created(app: &AppHandle, window: &str, route: AtCreation) {
    let Some(state) = direct(app) else { return };
    let effects = lock(&state.wiring).window_created(route.session, window);
    for effect in effects {
        // A new window is only ever routed; nothing else follows from it.
        let _ = route_window(app, &state, effect);
    }
}

/// The app's 「连接方式」 submenu, for its tray and application menus. `None`
/// where this system or configuration can never route: there is nothing to
/// choose.
pub(crate) fn menu(app: &AppHandle) -> tauri::Result<Option<Submenu<tauri::Wry>>> {
    let Some(state) = direct(app).filter(|state| state.available) else {
        return Ok(None);
    };
    let Some(menu) = ConnectionMenu::build(app)? else {
        return Ok(None);
    };
    let submenu = menu.submenu.clone();
    *lock(&state.menu) = Some(menu);
    Ok(Some(submenu))
}

fn refresh_menu(app: &AppHandle, state: &Arc<Direct>) {
    let chosen = lock(&state.shown)
        .as_ref()
        .map(|shown| lock(&state.wiring).mode(&shown.target.account, &shown.target.origin));
    let state = state.clone();
    let _ = app.run_on_main_thread(move || {
        if let Some(menu) = lock(&state.menu).as_ref() {
            menu.show(chosen);
        }
    });
}

/// A click in the submenu. `true` when the event was this menu's.
pub(crate) fn menu_event(app: &AppHandle, id: &str) -> bool {
    let mode = match id {
        MENU_AUTOMATIC => Mode::Automatic,
        MENU_TUNNEL => Mode::Tunnel,
        _ => return false,
    };
    let Some(state) = direct(app) else { return true };
    let effects = match lock(&state.shown).as_ref() {
        Some(shown) => lock(&state.wiring).choose(&shown.target.account, &shown.target.origin, mode),
        None => Vec::new(),
    };
    refresh_menu(app, &state);
    let app = app.clone();
    tauri::async_runtime::spawn(async move {
        for effect in effects {
            perform(&app, &state, effect).await;
        }
        // "Automatic" starts a session for the instance on show, where a
        // route can be put on an open window.
        state.wake.notify_one();
    });
    true
}

#[cfg(target_os = "macos")]
#[path = "shell_macos.rs"]
mod native;
#[cfg(windows)]
#[path = "shell_windows.rs"]
mod native;
#[cfg(not(any(target_os = "macos", windows)))]
#[path = "shell_unsupported.rs"]
mod native;
