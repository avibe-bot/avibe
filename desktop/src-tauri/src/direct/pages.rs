//! The page's `direct` capability, against real windows: what `bridge.rs`
//! words, delivered to the windows of another computer's instance, and the
//! two things such a page may ask of the shell.
//!
//! A window is known here from the moment its bridge script is made, with
//! the instance it was made for. Its page hears the truth about that one
//! window: "direct" only while the window has a session's route and that
//! session's client prefers a direct path.
use super::{choose, direct, lock, Direct};
use crate::direct::{
    bridge::{self, Ledger, Message, Method, Request},
    rendezvous::origin_of,
    Client, Handover, PathStatus,
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

/// What a session's client tells and does for its windows' pages.
struct Live {
    paths: watch::Receiver<PathStatus>,
    handover: Handover,
}

#[derive(Default)]
pub(super) struct Pages {
    windows: HashMap<String, Page>,
    sessions: HashMap<u64, Live>,
}

/// The bridge script for a window of another computer's instance that is
/// about to be created for the instance at `origin`. `None` where this
/// system or configuration can never route: the page then behaves as it
/// does in any browser.
pub(crate) fn script(app: &AppHandle, label: &str, origin: &url::Url) -> Option<String> {
    let state = direct(app).filter(|state| state.available)?;
    let origin = origin_of(origin.as_str())?;
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
    let script = bridge::script(&token, &app.package_info().version.to_string());
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
            handover: client.handover(),
        },
    );
    let app = app.clone();
    let state = state.clone();
    tauri::async_runtime::spawn(async move {
        // Ends with the client.
        while paths.changed().await.is_ok() {
            refresh(&app, &state, false);
        }
    });
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
    let Some((account, origin)) = lock(&state.pages)
        .windows
        .get(label)
        .map(|page| (page.account.clone(), page.origin.clone()))
    else {
        return;
    };
    let (routed, mode) = {
        let wiring = lock(&state.wiring);
        (wiring.routed_by(label), wiring.mode(&account, &origin))
    };
    let script = {
        let mut pages = lock(&state.pages);
        let Pages { windows, sessions } = &mut *pages;
        let Some(page) = windows.get_mut(label) else { return };
        // A route whose client is gone carries nothing.
        let routed = routed.and_then(|session| sessions.get(&session).map(|live| (session, *live.paths.borrow())));
        if !page.ledger.observe(routed) && !always {
            return;
        }
        bridge::delivery(
            &origin,
            &Message::State {
                status: page.ledger.status(),
                preference: bridge::preference(mode),
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
            let preference = bridge::preference(lock(&state.wiring).mode(&account, &origin));
            let reply = Message::Reply {
                id: request.id,
                result: preference.clone(),
                preference: Some(preference),
            };
            answer(app, label, &origin, reply);
        }
        Method::SetPreference(mode) => {
            choose(app, &state, &account, &origin, mode);
            let preference = bridge::preference(mode);
            let reply = Message::Reply {
                id: request.id,
                result: preference.clone(),
                preference: Some(preference),
            };
            answer(app, label, &origin, reply);
        }
        Method::Handover => {
            let routed = lock(&state.wiring).routed_by(label);
            let live =
                routed.and_then(|session| Some((session, lock(&state.pages).sessions.get(&session)?.handover.clone())));
            let (app, label) = (app.clone(), label.to_owned());
            tauri::async_runtime::spawn(async move {
                let result = match (live, origin.strip_prefix("https://")) {
                    (Some((session, handover)), Some(host)) => {
                        // The session's windows share one listener: its quiet
                        // public connections are this page's to close only
                        // while no other window has the route. The wiring is
                        // held for each claim, so none can join during one.
                        let sole = || {
                            let open = app.webviews();
                            let wiring = lock(&state.wiring);
                            wiring
                                .sole(session, &label, |window| open.contains_key(window))
                                .then_some(wiring)
                        };
                        bridge::handover(&handover.close_quiet_public(host, sole).await)
                    }
                    _ => bridge::handover(&crate::direct::HandoverResult::NotUsable),
                };
                let reply = Message::Reply {
                    id: request.id,
                    result,
                    preference: None,
                };
                answer(&app, &label, &origin, reply);
            });
        }
    }
}
