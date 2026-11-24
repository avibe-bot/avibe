//! Desktop direct in the running app: does what `wiring.rs` decides, against
//! real windows, and keeps each session's client asking its rendezvous.
//!
//! A page of another computer's instance reaches this only through its
//! window's `direct` bridge (`bridge.rs`, `pages.rs`): it is told its
//! window's path, and may ask for a hand-over or change the choice of path.
//! There is no command and no message handler for it.
//! Logs name what happened and nothing else: no host, address, ID or count
//! that could identify an instance.
use super::{
    choices::{Choices, Mode, Reason, Reverted},
    rendezvous::{origin_of, Reply, Request},
    wiring::{Effect, Material, Pace, Published, Routing, Target, Wiring},
    Client, RefreshOutcome,
};
use crate::instance_client;
use avibe_runtime_host::instance_client::protocol::{ClientAction, ClientRequest, ClientTarget};
use std::{
    collections::HashMap,
    sync::{Arc, Mutex, MutexGuard},
    time::{Duration, SystemTime, UNIX_EPOCH},
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
pub(crate) const MENU_DIRECT: &str = "direct-connection-direct";
const MENU_CONNECTION: &str = "direct-connection";

/// How often a session looks at the clock, to notice that the computer slept.
const GLANCE: Duration = Duration::from_secs(30);
/// How often open windows are looked at while any session exists.
const LOOK: Duration = Duration::from_secs(5);
/// How often the app is looked at while another computer's instance is on
/// show without a session that could still start by itself: after the pause
/// that follows a listener's end, or once the system's proxy settings allow.
const WAIT: Duration = Duration::from_secs(30);

/// The 「连接方式」 submenu: this computer's choice of path for the instance
/// on show (ui/design: desktop/connection-choice/menu-*). Its strings are
/// the Devices page's own, read by key.
pub(crate) struct ConnectionMenu<R: Runtime> {
    pub submenu: Submenu<R>,
    pub automatic: CheckMenuItem<R>,
    pub tunnel: CheckMenuItem<R>,
    /// "Direct and relay only", where the product catalog has its string.
    /// It is in the submenu only while the instance on show could be routed
    /// (`ConnectionState::direct`).
    pub direct: Option<CheckMenuItem<R>>,
    /// Whether `direct` is in the submenu now.
    listed: Mutex<bool>,
}

/// The submenu as words: what every system builds it from. `None` when the
/// product catalog lacks its title or one of its first two choices.
struct ConnectionRows {
    title: String,
    /// The choices in the frame's order, each with its item's ID. The third,
    /// "direct and relay only", is there when the catalog has its string.
    choices: Vec<(&'static str, Mode, String)>,
}

impl ConnectionRows {
    fn read() -> Option<Self> {
        let text = |key: &str| crate::product_text(&format!("devices.connection.{key}"));
        let mut choices = vec![
            (MENU_AUTOMATIC, Mode::Automatic, text("automatic")?),
            (MENU_TUNNEL, Mode::Tunnel, text("tunnelOnly")?),
        ];
        choices.extend(text("directOnly").map(|text| (MENU_DIRECT, Mode::Direct, text)));
        Some(Self {
            title: text("title")?,
            choices,
        })
    }
}

/// What the submenu shows for a choice: whether it can be used, which of
/// its items is ticked, and whether "direct and relay only" is offered.
#[derive(Debug, PartialEq, Eq)]
struct ConnectionState {
    enabled: bool,
    ticked: Mode,
    /// The instance on show could be reached on a path that is not the
    /// tunnel: this system routes, and its address is one the direct path
    /// serves. Otherwise the third item is absent, not greyed.
    direct: bool,
}

impl ConnectionState {
    /// The choice for the instance on show, and whether it could be routed;
    /// `None`, and greyed with "automatic" ticked and no third item, while
    /// this computer's own instance is.
    fn of(chosen: Option<(Mode, bool)>) -> Self {
        Self {
            enabled: chosen.is_some(),
            ticked: chosen.map(|(mode, _)| mode).unwrap_or_default(),
            direct: chosen.is_some_and(|(_, routable)| routable),
        }
    }
}

/// This computer's choice of path for the instance on show, and whether
/// that instance could be routed: each account's instance has its own, and
/// this computer's own instance has none.
fn chosen(shown: Option<&Published>, wiring: &Wiring) -> Option<(Mode, bool)> {
    shown.map(|shown| {
        (
            wiring.mode(&shown.target.account, &shown.target.origin),
            wiring.routable(&shown.target.origin),
        )
    })
}

impl<R: Runtime> ConnectionMenu<R> {
    /// As for this computer's own instance until `show` says otherwise.
    pub(crate) fn build(app: &AppHandle<R>) -> tauri::Result<Option<Self>> {
        let Some(rows) = ConnectionRows::read() else {
            return Ok(None);
        };
        let state = ConnectionState::of(None);
        let item = |(id, mode, text): &(&str, Mode, String)| {
            CheckMenuItem::with_id(app, *id, text, true, state.ticked == *mode, None::<&str>)
        };
        let (automatic, tunnel) = (item(&rows.choices[0])?, item(&rows.choices[1])?);
        let direct = rows.choices.get(2).map(item).transpose()?;
        let submenu =
            Submenu::with_id_and_items(app, MENU_CONNECTION, rows.title, state.enabled, &[&automatic, &tunnel])?;
        Ok(Some(Self {
            submenu,
            automatic,
            tunnel,
            direct,
            listed: Mutex::new(false),
        }))
    }

    /// The system ticks whatever was clicked, so every tick is always set
    /// from the choice.
    pub(crate) fn show(&self, chosen: Option<(Mode, bool)>) {
        let state = ConnectionState::of(chosen);
        let _ = self.submenu.set_enabled(state.enabled);
        let _ = self.automatic.set_checked(state.ticked == Mode::Automatic);
        let _ = self.tunnel.set_checked(state.ticked == Mode::Tunnel);
        let Some(direct) = &self.direct else { return };
        let _ = direct.set_checked(state.ticked == Mode::Direct);
        let mut listed = lock(&self.listed);
        if state.direct != *listed {
            let changed = if state.direct {
                self.submenu.append(direct)
            } else {
                self.submenu.remove(direct)
            };
            if changed.is_ok() {
                *listed = state.direct;
            }
        }
    }
}

pub(crate) struct Direct {
    wiring: Mutex<Wiring>,
    /// Ends a session's task, which then drops its client.
    stops: Mutex<HashMap<u64, oneshot::Sender<()>>>,
    /// Has a session ask its rendezvous now.
    asks: Mutex<HashMap<u64, Arc<Notify>>>,
    /// Where an instance that is being opened again was, by account and
    /// origin: its new view goes back there once.
    returns: Mutex<Vec<Return>>,
    /// The instance view the main window held at the last look.
    shown: Mutex<Option<Published>>,
    wake: Notify,
    menu: Mutex<Option<ConnectionMenu<tauri::Wry>>>,
    /// The windows whose pages are told their path, and what tells them.
    /// Taken before `wiring` where both are held, never after it.
    pages: Mutex<pages::Pages>,
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
        asks: Mutex::new(HashMap::new()),
        returns: Mutex::new(Vec::new()),
        shown: Mutex::new(None),
        wake: Notify::new(),
        menu: Mutex::new(None),
        pages: Mutex::default(),
        available: routing != Routing::Unsupported && !proxy_configured,
    });
    app.manage(state.clone());
    let app = app.clone();
    tauri::async_runtime::spawn(async move {
        loop {
            // Nothing runs while no instance of another computer is on show
            // or none could get a session: only a poke starts a look then.
            let every = {
                let shown = lock(&state.shown);
                let wiring = lock(&state.wiring);
                // Sooner when a direct-only session has something due.
                let until = wiring
                    .deadline()
                    .map(|at| at.saturating_duration_since(Instant::now()) + Duration::from_millis(50));
                if wiring.busy() {
                    Some(until.map_or(LOOK, |until| until.min(LOOK)))
                } else if wiring.awaits(shown.as_ref()) {
                    Some(WAIT)
                } else {
                    None
                }
            };
            match every {
                Some(every) => tokio::select! {
                    _ = state.wake.notified() => {}
                    _ = tokio::time::sleep(every) => {}
                },
                None => state.wake.notified().await,
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

/// The instance view the main window holds: the view of a signed-in
/// instance of another computer, and whether that instance's own document
/// is in it right now (it is not for a moment while the view loads).
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
    if view.label() == crate::MAIN_WINDOW {
        return None;
    }
    let document = view
        .url()
        .ok()
        .and_then(|url| origin_of(url.as_str()))
        .is_some_and(|origin| origin == canonical_origin);
    Some(Published {
        target: Target {
            account,
            origin: canonical_origin,
            instance: instance_id,
        },
        runtime: view.label().to_owned(),
        document,
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
    // What was kept for a window that has closed went with it.
    native::forget_closed(|window| app.get_webview(window).is_some());
    if let Some(shown) = &shown {
        go_back(app, state, shown);
    }
    pages::refresh(app, state, false);
    refresh_menu(app, state);
}

/// Routes one window. It only hands the work to the main thread, in order,
/// so a window routed here is routed before anything asked of it later.
///
/// The session may end between this call and the main thread's turn. Taking
/// a session's route off is main-thread work too, queued when the session
/// ends, so the work handed over here asks `still` there, at its turn: if
/// the session has ended it does nothing, and if it has not, the taking-off
/// that may follow is queued behind it.
fn route_window(app: &AppHandle, state: &Arc<Direct>, effect: Effect) -> Option<Effect> {
    let still = |session: u64, window: &str| {
        let (state, window) = (state.clone(), window.to_owned());
        move || lock(&state.wiring).routes(session, &window)
    };
    match effect {
        // Each window is registered with the session's listener as it is
        // routed, and presents that registration's credential and no other.
        // A window the listener would not register gets no route.
        Effect::Install { session, window } => {
            if let Some(((host, material), registration)) =
                route(state, session).zip(pages::register(state, session, &window))
            {
                let still = still(session, &window);
                native::install(app, window, host, material, registration, still);
            }
            None
        }
        Effect::Answer { session, window } => {
            if let Some(((_, material), registration)) =
                route(state, session).zip(pages::register(state, session, &window))
            {
                let still = still(session, &window);
                native::answer(app, window, material, registration, still);
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
        Effect::Strict { session, only } => {
            let closed = pages::direct_only(state, session, only);
            if only {
                eprintln!("desktop direct: a session is direct-only; tunnel connections closed: {}", closed.min(99));
            } else {
                eprintln!("desktop direct: a direct-only session connects again");
            }
        }
        Effect::Reload { session, windows } => reload(app, state, session, windows),
        Effect::Ask { session } => {
            if let Some(ask) = lock(&state.asks).get(&session) {
                ask.notify_one();
            }
        }
        Effect::Reopen { runtime } => reopen(app, state, &runtime),
        Effect::Revert {
            account,
            origin,
            runtime,
        } => revert(app, state, &account, &origin, &runtime).await,
    }
}

/// Reloads the windows of a direct-only session, once, now that its link is
/// up and its listener's tunnel connections are closed: every connection
/// their pages make from here is the listener's to put on the link.
///
/// A window keeps connections across a reload, so afterwards the listener
/// is asked what it still carries on the tunnel for them. Anything but
/// nothing is closed again, and said: it would be the tunnel under a choice
/// that forbids it.
fn reload(app: &AppHandle, state: &Arc<Direct>, session: u64, windows: Vec<String>) {
    let handle = app.clone();
    let _ = app.run_on_main_thread(move || {
        for window in &windows {
            if let Some(view) = handle.get_webview(window) {
                let _ = view.reload();
            }
        }
    });
    let state = state.clone();
    tauri::async_runtime::spawn(async move {
        tokio::time::sleep(Duration::from_secs(3)).await;
        if pages::public_legs(&state, session).is_some_and(|legs| legs > 0) {
            let still = lock(&state.wiring).is_direct_only(session);
            if still {
                pages::direct_only(&state, session, true);
                eprintln!("desktop direct: a direct-only session carried tunnel connections after its reload; closed");
            }
        }
    });
}

/// Where an instance that is being opened again was.
struct Return {
    account: String,
    origin: String,
    /// The view that is being replaced: the new one has another label.
    from: String,
    url: url::Url,
    asked: Instant,
}

/// How long a return is kept for a view that has not come.
const RETURNING: Duration = Duration::from_secs(120);

/// Opens the instance of the view `runtime` again, if it is still the one
/// on show: the app signs in to the instance afresh and replaces the view
/// with a new one, which gets its session's route from before its first
/// load. The view on show stays until the new one is ready.
///
/// The new view starts at the instance's first page; it is sent back to
/// where the old one was once it shows (`go_back`). What was typed and not
/// sent in the old view is lost.
fn reopen(app: &AppHandle, state: &Arc<Direct>, runtime: &str) {
    let Some(shown) = lock(&state.shown).clone().filter(|shown| shown.runtime == runtime) else {
        return;
    };
    let Some(client) = app.try_state::<instance_client::Client>() else {
        return;
    };
    lock(&state.wiring).reopening(runtime, Instant::now());
    let at = app
        .get_webview(runtime)
        .and_then(|view| view.url().ok())
        .filter(|url| origin_of(url.as_str()).as_deref() == Some(shown.target.origin.as_str()));
    {
        let mut returns = lock(&state.returns);
        returns.retain(|kept| kept.account != shown.target.account || kept.origin != shown.target.origin);
        if let Some(url) = at {
            returns.push(Return {
                account: shown.target.account.clone(),
                origin: shown.target.origin.clone(),
                from: runtime.to_owned(),
                url,
                asked: Instant::now(),
            });
        }
    }
    let coordinator = client.coordinator.clone();
    let instance_id = shown.target.instance.clone();
    let (app, state) = (app.clone(), state.clone());
    tauri::async_runtime::spawn(async move {
        // As the app selects the instance it last showed when it starts.
        let reply = coordinator
            .request(ClientRequest {
                version: 1,
                request_id: "direct-reopen".into(),
                expected_generation: coordinator.state().generation,
                action: ClientAction::SelectInstance { instance_id },
            })
            .await;
        if reply.error.is_some() {
            eprintln!("desktop direct: an instance could not be opened again");
        }
        poke(&app);
        drop(state);
    });
}

/// Sends the new view of an instance that was opened again back to where
/// its old view was, once, when the instance's document is in it.
fn go_back(app: &AppHandle, state: &Arc<Direct>, shown: &Published) {
    let found = {
        let mut returns = lock(&state.returns);
        returns.retain(|kept| kept.asked.elapsed() < RETURNING);
        let at = returns.iter().position(|kept| {
            shown.document
                && kept.account == shown.target.account
                && kept.origin == shown.target.origin
                && kept.from != shown.runtime
        });
        at.map(|at| returns.swap_remove(at))
    };
    let Some(found) = found else { return };
    let (handle, window) = (app.clone(), shown.runtime.clone());
    let _ = app.run_on_main_thread(move || {
        let Some(view) = handle.get_webview(&window) else { return };
        // Only from the instance's own first page: a view that has already
        // gone somewhere else is left where it is.
        let at_start = view.url().is_ok_and(|url| {
            origin_of(url.as_str()) == origin_of(found.url.as_str()) && url.path() == "/" && url != found.url
        });
        if at_start {
            let _ = view.navigate(found.url);
        }
    });
}

/// The app sets the choice of the instance of `account` at `origin` back to
/// automatic itself: it could not be reached on a direct path or the relay
/// while the tunnel reached it. Every page of the instance hears it, and
/// the instance is opened again under the automatic choice.
async fn revert(app: &AppHandle, state: &Arc<Direct>, account: &str, origin: &str, runtime: &str) {
    let at = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |since| u64::try_from(since.as_millis()).unwrap_or(u64::MAX));
    let effects = {
        let mut wiring = lock(&state.wiring);
        // Only a choice that is still "direct and relay only": someone may
        // have chosen otherwise meanwhile.
        if wiring.mode(account, origin) != Mode::Direct {
            return;
        }
        wiring.revert(
            account,
            origin,
            Reverted {
                at,
                reason: Reason::Unreachable,
                from: Mode::Direct,
            },
        )
    };
    eprintln!("desktop direct: a direct-only choice was set back to automatic");
    for effect in effects {
        Box::pin(perform(app, state, effect)).await;
    }
    pages::refresh(app, state, true);
    refresh_menu(app, state);
    reopen(app, state, runtime);
}

fn route(state: &Arc<Direct>, session: u64) -> Option<(String, Material)> {
    let wiring = lock(&state.wiring);
    let (host, material) = wiring.route(session)?;
    Some((host.to_owned(), material.clone()))
}

/// Where the client's listener is. What each window presents to it is that
/// window's own registration (`pages::register`).
fn material(client: &Client, exclusive: bool) -> Option<Material> {
    Some(Material {
        proxy: client.route()?.proxy,
        exclusive,
    })
}

/// Starts a client for the instance view `runtime`, if it is still the one
/// that was looked at.
async fn open(app: &AppHandle, state: &Arc<Direct>, runtime: &str) {
    let Some(shown) = lock(&state.shown).clone().filter(|shown| shown.runtime == runtime) else {
        return;
    };
    // A direct-only instance never gets a session for a view that is
    // already open: the choice changed since this was decided, and the
    // next look opens the instance again.
    if lock(&state.wiring).only(&shown.target) {
        return;
    }
    let client = match shown.target.host() {
        Some(host) => Client::new(host, shown.target.instance.clone()).await,
        None => None,
    };
    let Some((client, material)) =
        client.and_then(|client| material(&client, false).map(|material| (client, material)))
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
    let ask = Arc::new(Notify::new());
    lock(&state.asks).insert(session, ask.clone());
    pages::session_started(app, state, session, &client);
    eprintln!("desktop direct: a session started");
    tauri::async_runtime::spawn(run(app.clone(), state.clone(), session, client, stopped, ask));
}

/// What a rendezvous request says about the tunnel, whatever came of it:
/// whether the instance itself answered it. A request that found no page
/// of the instance, failed on the way, or was not the selection's any more
/// says nothing.
fn instance_answered(reply: &Reply) -> bool {
    matches!(reply, Reply::Ready(_) | Reply::Refused(_) | Reply::Controlled)
}

/// One session's client, for as long as the session lasts: it asks the
/// rendezvous through the session's instance view, again before the answer
/// runs out, and sooner after the computer slept.
async fn run(
    app: AppHandle,
    state: Arc<Direct>,
    session: u64,
    mut client: Client,
    mut stopped: oneshot::Receiver<()>,
    ask_now: Arc<Notify>,
) {
    let mut lifetime = client.listener_lifetime();
    let mut pace = Pace::default();
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
            // A direct-only session that connects again asks at once.
            _ = ask_now.notified() => due = Instant::now(),
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
        // The core hears how the request ended in the adapter's own words:
        // it keeps a link through a page that was not ready or a request
        // that failed, and retires it on a refusal or a changed selection.
        let answered = Arc::new(Mutex::new(false));
        let asking = client.refresh_reply(|endpoint| {
            let (app, state, answered) = (app.clone(), state.clone(), answered.clone());
            async move {
                let reply = ask(app, state, session, window, origin, endpoint).await;
                *lock(&answered) = instance_answered(&reply);
                reply
            }
        });
        let outcome = tokio::select! {
            biased;
            _ = &mut stopped => break false,
            outcome = asking => outcome,
        };
        let effects = {
            let mut wiring = lock(&state.wiring);
            let mut effects = wiring.probed(session, outcome == RefreshOutcome::Renewed);
            // The tunnel carried the request to the instance and back, and
            // no link came of it.
            if outcome == RefreshOutcome::Retired && *lock(&answered) {
                effects.extend(wiring.unlinked(session, Instant::now()));
            }
            effects
        };
        for effect in effects {
            perform(&app, &state, effect).await;
        }
        pages::refresh(&app, &state, false);
        due = pace.next(&outcome, Instant::now());
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
    lock(&state.asks).remove(&session);
    // Every window's registration is revoked, then the listener goes with
    // the client.
    pages::session_ending(&state, session);
    drop(client);
    pages::session_ended(&app, &state, session);
}

/// Asks the rendezvous through `window`, and says how it ended in the
/// adapter's own words. `window` must still be the session's instance view
/// when the request starts and when its answer arrives: if the session
/// ended meanwhile, or its view is another one or is gone, the answer is
/// "the selection changed", whatever the instance said. A view that is only
/// between two documents is still the selection; the adapter then finds no
/// document of the instance and says "not ready".
async fn ask(
    app: AppHandle,
    state: Arc<Direct>,
    session: u64,
    window: String,
    origin: String,
    endpoint: String,
) -> Reply {
    let Some(request) = Request::new(&origin, &endpoint) else {
        return Reply::Failed;
    };
    let (answer, answered) = oneshot::channel();
    let handle = app.clone();
    let sent = app.run_on_main_thread(move || {
        let Some(view) = handle.get_webview(&window) else {
            let _ = answer.send(Reply::SelectionChanged);
            return;
        };
        let current = {
            let handle = handle.clone();
            move || {
                lock(&state.wiring).selects(session, &window)
                    && instance_client::runtime_webview(&handle).is_some_and(|view| view.label() == window)
            }
        };
        native::ask(&view, request, current, move |reply| {
            let _ = answer.send(reply);
        });
    });
    match sent {
        // The adapter could not be reached at all: nothing was learned.
        Ok(()) => answered.await.unwrap_or(Reply::Failed),
        Err(_) => Reply::Failed,
    }
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

/// The browser arguments `wry` gives every WebView2 by default
/// (`wry` 0.57.0, `src/webview2/mod.rs`): no "mini menu" and no SmartScreen,
/// and media that may start without a gesture (its `autoplay`, which is on
/// and which Tauri leaves on). Giving a window any argument of ours replaces
/// all of them, so a routed window is given them back; a routed and an
/// unrouted window then differ in the route alone.
pub(crate) const WRY_DEFAULT_ARGUMENTS: &str =
    "--disable-features=msWebOOUI,msPdfOOUI,msSmartScreenProtection --autoplay-policy=no-user-gesture-required";

/// The whole of a routed window's browser arguments.
#[cfg_attr(not(windows), allow(dead_code))]
pub(crate) fn window_arguments(route_argument: &str) -> String {
    format!("{WRY_DEFAULT_ARGUMENTS} {route_argument}")
}

/// The session a window that is about to be created is routed to, so that
/// its route is in place before its first load.
pub(crate) struct AtCreation {
    session: u64,
    /// The whole of the window's browser arguments, where its route is given
    /// by them.
    pub arguments: Option<String>,
}

/// The main window's instance view for `destination` is about to be created,
/// on the main thread. When this account's instance has answered a
/// rendezvous before, or its choice is "the direct path and the relay
/// only", this starts its client and returns the route, which
/// `window_created` puts in place before the view's first load; otherwise
/// `None`, and the view is created as it always was. (A direct-only
/// instance whose view is made without a route is opened again, or has its
/// choice set back, at the next look: `Wiring::reconcile`.)
pub(crate) fn instance_view_route(app: &AppHandle, destination: &url::Url) -> Option<AtCreation> {
    let state = direct(app).filter(|state| state.available)?;
    let target = target_at(app, destination)?;
    let host = target.host()?.to_owned();
    // The system's settings are read, and the listener is bound, here and
    // now, so the route is there before the view asks for anything. Never
    // from a thread that is running the async runtime.
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
    let exclusive = lock(&state.wiring).only(&target);
    let client = tauri::async_runtime::block_on(Client::new(&host, target.instance.clone()))?;
    let material = material(&client, exclusive)?;
    // Where the route is the window's arguments, there is none without them.
    let arguments = native::arguments(&host, &material);
    if arguments.is_none() && lock(&state.wiring).routes_at_creation() {
        return None;
    }
    let session = lock(&state.wiring).open(target, material, None, Instant::now());
    start(app, &state, session, client);
    state.wake.notify_one();
    Some(AtCreation { session, arguments })
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
    let account = app
        .try_state::<instance_client::Client>()?
        .coordinator
        .state()
        .account
        .identity?
        .id;
    let host = {
        let wiring = lock(&state.wiring);
        let session = wiring.shared(&account, &origin)?;
        wiring.route(session)?.0.to_owned()
    };
    // The system's settings may have changed since the last look.
    let rules_out = system_proxy_rules_out(&host).await;
    let mut wiring = lock(&state.wiring);
    wiring.system_proxy(rules_out);
    if rules_out {
        // The session ends at the next look; this window gets no route.
        state.wake.notify_one();
        return None;
    }
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
    route_windows(app, &state, effects);
}

/// An app window opened from the instance view at `origin` now exists, with
/// the `route` it was created for if it had one. Called once for every app
/// window of another computer's instance, before the window is sent to its
/// instance. The wiring remembers it with or without a session and decides
/// which session it joins (`Wiring::app_window_created`).
pub(crate) fn app_window_created(app: &AppHandle, window: &str, origin: &url::Url, route: Option<AtCreation>) {
    let Some(state) = direct(app) else { return };
    let account = app
        .try_state::<instance_client::Client>()
        .and_then(|client| client.coordinator.state().account.identity)
        .map(|identity| identity.id);
    let (Some(account), Some(origin)) = (account, origin_of(origin.as_str())) else {
        return;
    };
    let created_for = route.map(|route| route.session);
    let effects = lock(&state.wiring).app_window_created(window, &account, &origin, created_for);
    route_windows(app, &state, effects);
}

/// A new window is only ever routed; nothing else follows from it.
fn route_windows(app: &AppHandle, state: &Arc<Direct>, effects: Vec<Effect>) {
    for effect in effects {
        let _ = route_window(app, state, effect);
    }
    pages::refresh(app, state, false);
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
    let chosen = chosen(lock(&state.shown).as_ref(), &lock(&state.wiring));
    let state = state.clone();
    let _ = app.run_on_main_thread(move || {
        if let Some(menu) = lock(&state.menu).as_ref() {
            menu.show(chosen);
        }
    });
}

/// Undo the system's optimistic checkmark when admission deferred the click.
/// This only renders the saved choice; it never chooses a path or starts work.
pub(crate) fn restore_deferred_menu(app: &AppHandle) {
    if let Some(state) = direct(app) {
        refresh_menu(app, &state);
    }
}

/// A click in the submenu. `true` when the event was this menu's.
pub(crate) fn menu_event(app: &AppHandle, id: &str) -> bool {
    let mode = match id {
        MENU_AUTOMATIC => Mode::Automatic,
        MENU_TUNNEL => Mode::Tunnel,
        MENU_DIRECT => Mode::Direct,
        _ => return false,
    };
    let Some(state) = direct(app) else { return true };
    let shown = lock(&state.shown).as_ref().map(|shown| shown.target.clone());
    match shown {
        Some(target) => {
            choose(app, &state, &target.account, &target.origin, mode);
        }
        // Nothing to choose for; the ticks go back to what they were.
        None => refresh_menu(app, &state),
    }
    true
}

#[cfg(all(target_os = "macos", feature = "app-lock-e2e"))]
#[path = "../../tests/direct/app_lock_menu.rs"]
pub(crate) mod app_lock_menu;

/// The choice of path for the instance of `account` at `origin` is now
/// `mode`: from the menu, or from a page of that instance. Between
/// "automatic" and "Cloudflare tunnel only" nothing reloads. A choice to or
/// from "the direct path and the relay only" opens the instance again, if
/// it is the one on show: its windows were made for the other kind of
/// route. Returns the choice's place among this run's choices
/// (`Wiring::choice`).
fn choose(app: &AppHandle, state: &Arc<Direct>, account: &str, origin: &str, mode: Mode) -> u64 {
    let (effects, order, reopens) = {
        let mut wiring = lock(&state.wiring);
        let before = wiring.mode(account, origin);
        let effects = wiring.choose(account, origin, mode);
        (effects, wiring.choice(account, origin).2, Wiring::reopens(before, mode))
    };
    refresh_menu(app, state);
    let on_show = lock(&state.shown)
        .as_ref()
        .filter(|shown| reopens && shown.target.account == account && shown.target.origin == origin)
        .map(|shown| shown.runtime.clone());
    // Noted before anything else looks: the view on show is about to be
    // replaced, and gets no session of its own meanwhile.
    if let Some(runtime) = &on_show {
        lock(&state.wiring).reopening(runtime, Instant::now());
    }
    let (app, state) = (app.clone(), state.clone());
    tauri::async_runtime::spawn(async move {
        for effect in effects {
            perform(&app, &state, effect).await;
        }
        // Every page of the instance hears the choice, and its menu shows it.
        pages::refresh(&app, &state, true);
        if let Some(runtime) = on_show {
            reopen(&app, &state, &runtime);
        }
        // "Automatic" starts a session for the instance on show, where a
        // route can be put on an open window.
        state.wake.notify_one();
    });
    order
}

#[path = "pages.rs"]
mod pages;
pub(crate) use pages::{loaded as page_loaded, request as page_request, script as page_script};

#[cfg(target_os = "macos")]
#[path = "shell_macos.rs"]
mod native;
#[cfg(windows)]
#[path = "shell_windows.rs"]
mod native;
#[cfg(not(any(target_os = "macos", windows)))]
#[path = "shell_unsupported.rs"]
mod native;

#[cfg(test)]
#[path = "../../tests/direct/menu_model.rs"]
mod menu_model_tests;
