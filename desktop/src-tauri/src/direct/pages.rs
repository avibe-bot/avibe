//! The page's `direct` capability, against real windows: what `bridge.rs`
//! words, delivered to the windows of another computer's instance, and the
//! two things such a page may ask of the shell.
//!
//! A window is known here from the moment its bridge script is made, with
//! the instance it was made for. Its page hears the truth about that one
//! window: "direct" only while the window has a session's route and that
//! session's client prefers a direct path.
//!
//! A routed window has a registration of its own with its session's
//! listener (`registered.rs`), kept here: its credential is what the window
//! presents, and its page's hand-over closes that window's connections only.
use super::{choose, direct, lock, perform, Direct};
use crate::direct::{
    bridge::{self, Ledger, Message, Method, Request, Told},
    registered::Registered,
    rendezvous::origin_of,
    Client, Handover, HandoverResult, PathStatus, PreferredPath, WindowRegistration,
};
use crate::instance_client;
use std::{
    collections::HashMap,
    sync::Arc,
    time::{Duration, Instant},
};
use tauri::{AppHandle, Manager, Webview};
use tokio::sync::watch;

struct Page {
    /// Only this window's bridge script knows it.
    token: String,
    account: String,
    origin: String,
    ledger: Ledger,
    /// When its bridge script was made: just before the window was.
    made: Instant,
}

/// How long a window may take to exist after its bridge script was made,
/// before what is kept for it is dropped as a window that never was.
const UNMADE: Duration = Duration::from_secs(60);

/// What a session's client tells and does for its windows and their pages.
struct Live {
    paths: watch::Receiver<PathStatus>,
    registered: Registered,
    /// The session's own handle on its listener, for what is asked of the
    /// whole session and not of one window.
    handover: Handover,
}

#[derive(Default)]
pub(super) struct Pages {
    windows: HashMap<String, Page>,
    sessions: HashMap<u64, Live>,
}

/// The bridge script for a window of another computer's instance that is
/// about to be created for the instance at `origin`. `None` where a window
/// of that instance can never be routed (this system or configuration, or
/// an address the direct path does not serve): its page then is exactly as
/// it is in any browser.
pub(crate) fn script(app: &AppHandle, label: &str, origin: &url::Url) -> Option<String> {
    let state = direct(app)?;
    let origin = origin_of(origin.as_str())?;
    if !lock(&state.wiring).routable(&origin) {
        return None;
    }
    let account = app
        .try_state::<instance_client::Client>()?
        .coordinator
        .state()
        .account
        .identity?
        .id;
    let mut random = [0u8; 16];
    getrandom::fill(&mut random).ok()?;
    let token: String = random.iter().map(|byte| format!("{byte:02x}")).collect();
    let script = bridge::script(&token, &app.package_info().version.to_string(), cfg!(windows));
    // A label that is used again names a new window: nothing of the old
    // one's, its token least of all, is kept.
    lock(&state.pages).windows.insert(
        label.to_owned(),
        Page {
            token,
            account,
            origin,
            ledger: Ledger::default(),
            made: Instant::now(),
        },
    );
    Some(script)
}

/// A session's client has started.
pub(super) fn session_started(app: &AppHandle, state: &Arc<Direct>, session: u64, client: &Client) {
    let mut paths = client.path_changes();
    lock(&state.pages).sessions.insert(
        session,
        Live {
            paths: paths.clone(),
            registered: Registered::new(client.handover()),
            handover: client.handover(),
        },
    );
    let app = app.clone();
    let state = state.clone();
    tauri::async_runtime::spawn(async move {
        // Ends with the client. The wiring hears every change too: for a
        // direct-only session, a link that came up or went, and the
        // connections the listener failed for want of one.
        loop {
            let status = *paths.borrow_and_update();
            let linked = matches!(status.preferred, PreferredPath::Direct(_));
            let effects = lock(&state.wiring).path(
                session,
                linked,
                status.sequence,
                status.strict_failures,
                tokio::time::Instant::now(),
            );
            for effect in effects {
                perform(&app, &state, effect).await;
            }
            refresh(&app, &state, false);
            state.wake.notify_one();
            if paths.changed().await.is_err() {
                break;
            }
        }
    });
}

/// Makes the session's listener direct-only (`only`) or lets it serve the
/// tunnel again. Turning it on closes every tunnel connection the listener
/// carries, the busy ones too, and refuses the ones still being made: a
/// window keeps its connections across a reload, so one left open would go
/// on carrying its page with no new connection for the listener to refuse.
/// Returns how many it closed.
pub(super) fn direct_only(state: &Arc<Direct>, session: u64, only: bool) -> usize {
    let Some(handover) = lock(&state.pages)
        .sessions
        .get(&session)
        .map(|live| live.handover.clone())
    else {
        return 0;
    };
    handover.set_direct_only(only);
    if only {
        handover.close_public()
    } else {
        0
    }
}

/// How many tunnel connections the session's listener still carries for
/// its windows; `None` when its client is gone.
pub(super) fn public_legs(state: &Arc<Direct>, session: u64) -> Option<usize> {
    let pages = lock(&state.pages);
    let live = pages.sessions.get(&session)?;
    Some(live.registered.public_legs())
}

/// A window of the session has just been routed: its own registration with
/// the session's listener, whose credential the window presents. `None`
/// when the session's client is gone or its listener takes no more windows;
/// the window then gets no route.
pub(super) fn register(state: &Arc<Direct>, session: u64, window: &str) -> Option<WindowRegistration> {
    lock(&state.pages)
        .sessions
        .get_mut(&session)?
        .registered
        .register(window)
}

/// The session is ending and its client is about to be dropped: what its
/// windows presented opens nothing from here on.
pub(super) fn session_ending(state: &Arc<Direct>, session: u64) {
    if let Some(live) = lock(&state.pages).sessions.get_mut(&session) {
        live.registered.revoke_all();
    }
}

/// A session's client is gone.
pub(super) fn session_ended(app: &AppHandle, state: &Arc<Direct>, session: u64) {
    lock(&state.pages).sessions.remove(&session);
    refresh(app, state, false);
}

/// Tells every window's page what changed for its window; with `all`, the
/// choice of path too, which is not the window's own.
pub(super) fn refresh(app: &AppHandle, state: &Arc<Direct>, all: bool) {
    let labels: Vec<String> = {
        let mut pages = lock(&state.pages);
        pages
            .windows
            .retain(|label, page| app.get_webview(label).is_some() || page.made.elapsed() < UNMADE);
        // A window that is no longer routed by its session (it closed) is
        // no longer registered with that session's listener.
        let wiring = lock(&state.wiring);
        for (session, live) in &mut pages.sessions {
            live.registered.keep(|window| wiring.routes(*session, window));
        }
        drop(wiring);
        pages.windows.keys().cloned().collect()
    };
    for label in labels {
        push(app, state, &label, all);
    }
}

/// A document has started or finished loading in `view`. Its page reads
/// its window's state at once, and may have read it before this arrives.
pub(crate) fn loaded(view: &Webview) {
    let app = view.app_handle();
    let Some(state) = direct(app) else { return };
    if !lock(&state.pages).windows.contains_key(view.label()) {
        return;
    }
    push(app, &state, view.label(), true);
}

fn push(app: &AppHandle, state: &Arc<Direct>, label: &str, always: bool) {
    let script = {
        // Held from the look at the wiring to the ledger's entry: several
        // threads refresh, and what each saw is entered in the order seen.
        let mut pages = lock(&state.pages);
        let Pages { windows, sessions } = &mut *pages;
        let Some(page) = windows.get_mut(label) else { return };
        let (routed, (mode, reverted, order)) = {
            let wiring = lock(&state.wiring);
            (wiring.routed_by(label), wiring.choice(&page.account, &page.origin))
        };
        // A route whose client is gone, or that the window was never given
        // (its registration was refused), carries nothing.
        let routed = routed.and_then(|session| {
            let live = sessions.get(&session)?;
            live.registered.of(label)?;
            Some((session, *live.paths.borrow()))
        });
        if !page.ledger.observe(routed) && !always {
            return;
        }
        bridge::delivery(
            &page.origin,
            &Message::State {
                status: page.ledger.status(),
                choice: Told::new(mode, reverted, order),
            },
        )
    };
    if let Some(view) = app.get_webview(label) {
        let _ = view.eval(script);
    }
}

fn answer(app: &AppHandle, label: &str, origin: &str, message: Message) {
    if let Some(view) = app.get_webview(label) {
        let _ = view.eval(bridge::delivery(origin, &message));
    }
}

/// Whether `view` was asked to follow `avibe-app://direct`. Whatever such a
/// link is, it is never followed; it is acted on only when it carries the
/// window's token and the window shows the instance it was made for.
pub(crate) fn request(view: &Webview, url: &url::Url) -> bool {
    if !bridge::is_request(url) {
        return false;
    }
    act(view, url);
    true
}

fn act(view: &Webview, url: &url::Url) {
    let app = view.app_handle();
    let label = view.label();
    let Some(state) = direct(app) else { return };
    let Some((token, account, origin)) = lock(&state.pages)
        .windows
        .get(label)
        .map(|page| (page.token.clone(), page.account.clone(), page.origin.clone()))
    else {
        return;
    };
    let Some(request) = Request::parse(url, &token) else {
        return;
    };
    let shows = view.url().ok().and_then(|url| origin_of(url.as_str()));
    if shows.as_deref() != Some(origin.as_str()) {
        return;
    }
    match request.method {
        Method::Preference => {
            let (mode, reverted, order) = lock(&state.wiring).choice(&account, &origin);
            let choice = Told::new(mode, reverted, order);
            let reply = Message::Reply {
                id: request.id,
                result: choice.preference.clone(),
                choice: Some(choice),
            };
            answer(app, label, &origin, reply);
        }
        Method::SetPreference(mode) => {
            // A choice someone makes stands as made: nothing is set back.
            let choice = Told::new(mode, None, choose(app, &state, &account, &origin, mode));
            let reply = Message::Reply {
                id: request.id,
                result: choice.preference.clone(),
                choice: Some(choice),
            };
            answer(app, label, &origin, reply);
        }
        Method::Handover => {
            // The page's own window's registration, as it is now: what is
            // closed is that window's quiet connections and no other's, and
            // a window that takes this label later is not touched.
            // (The wiring is let go before the pages are taken.)
            let session = lock(&state.wiring).routed_by(label);
            let own = session.and_then(|session| {
                lock(&state.pages)
                    .sessions
                    .get(&session)?
                    .registered
                    .handing_over(label)
            });
            let (app, label) = (app.clone(), label.to_owned());
            tauri::async_runtime::spawn(async move {
                let result = match (own, origin.strip_prefix("https://")) {
                    (Some((handover, registration)), Some(host)) => {
                        handover.close_quiet_public_for_window(host, &registration).await
                    }
                    _ => HandoverResult::NotUsable,
                };
                let reply = Message::Reply {
                    id: request.id,
                    result: bridge::handover(&result),
                    choice: None,
                };
                answer(&app, &label, &origin, reply);
            });
        }
    }
}
