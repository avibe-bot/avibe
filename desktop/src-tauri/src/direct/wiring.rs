//! Which windows share which direct client, and when it starts and ends.
//! Plain data and decisions only: the shell (`shell.rs`) does what the
//! effects say, on the main thread and against real windows.
//!
//! One session is one direct client for one signed-in instance view: the
//! main window's instance view (its "runtime") and the app windows opened
//! from it. It is found by account, instance origin and that view's own
//! label, never by an installation ID, and it ends when that view does: on
//! sign-out, a switch of instance, or a change of the choice of path. A view
//! that is only between two documents (loading, reloading) keeps it.
//!
//! The rules it keeps:
//! - an instance whose choice is "Cloudflare tunnel only", or any instance
//!   while the app has a configured proxy, has no session and no route;
//! - while the system's own proxy settings rule the direct route out (see
//!   `system_proxy`), the same;
//! - where the system cannot route one host of one window (macOS before 14),
//!   there is no session at all;
//! - where a route can only be given when a window is created (Windows), a
//!   window gets one only for an instance that answered a rendezvous in an
//!   earlier session, and a window already open is never rebuilt. Until an
//!   instance has answered once, its session only asks (a "scout"): it
//!   routes nothing and ends with the first answer;
//! - where a route can be put on an open window (macOS 14 and later), an
//!   instance that answered before still gets its client before its view is
//!   created and its route before that view's first load. WebKit does not
//!   use a route written while it has a connection to that host: not for
//!   the next request, a reload or a new navigation, until every such
//!   connection has closed (`examples/direct_webview_probe.rs` measures
//!   it). So only a route that is there first carries the view, and only
//!   then are its tunnel connections the listener's to hand over. An
//!   instance that has not answered before is asked from its open view and
//!   routed there; that view keeps the tunnel while its connections live,
//!   and the windows opened from it later are routed as they are made.
//!
//! An instance whose choice is "the direct path and the relay only" has its
//! own rules on top (`Only`), which the three constants below it time:
//! - its windows are never without the session's route, and that route
//!   allows no failover: a window whose listener is gone or refuses shows a
//!   failure, never the tunnel. So its client and route are there before
//!   its view's first load, first open included; a view that has none is
//!   opened again with one;
//! - connecting goes through the tunnel once: the session's listener serves
//!   the tunnel until its first direct link is up (the view's sign-in, its
//!   first load and the rendezvous need it). Then the listener is made
//!   direct-only, its tunnel connections are closed, and its windows are
//!   reloaded once, onto the link;
//! - when the link goes, the listener stays direct-only for a while:
//!   requests wait and then fail. Then the session connects again as it did
//!   the first time, through the tunnel, and reloads its windows once the
//!   link is back;
//! - the choice is set back to automatic, and the instance opened again
//!   under it, when the tunnel reaches the instance and no link comes up,
//!   or when the link has had to be made again too often and the instance
//!   is then heard from. Never because nothing reaches the instance at
//!   all, and never by a timer alone: a computer that is offline keeps its
//!   choice.
use super::{
    choices::{Choices, Mode, Reverted},
    connect::Authority,
    RefreshOutcome,
};
use std::{net::SocketAddrV4, time::Duration};
use tokio::time::Instant;

/// How this system routes one window's requests for one host.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Routing {
    /// On a window that is already open (macOS 14 and later).
    Live,
    /// Only when the window is created (Windows).
    AtCreation,
    /// Not at all: every instance stays on the tunnel.
    Unsupported,
}

impl Routing {
    /// WebKit takes a proxy for one window's browsing data from macOS 14.
    pub fn on_macos(major: u64) -> Self {
        if major >= 14 {
            Self::Live
        } else {
            Self::Unsupported
        }
    }
}

/// A signed-in instance. Deliberately not `Debug`: neither ID is ever logged.
#[derive(Clone, PartialEq, Eq)]
pub struct Target {
    pub account: String,
    /// `https://host`, as the instance's documents serialize it.
    pub origin: String,
    pub instance: String,
}

impl Target {
    pub fn host(&self) -> Option<&str> {
        self.origin.strip_prefix("https://").filter(|host| !host.is_empty())
    }
}

/// The instance view the main window holds, as the shell publishes it.
#[derive(Clone, PartialEq, Eq)]
pub struct Published {
    pub target: Target,
    /// That view's label, new for every sign-in or switch.
    pub runtime: String,
    /// Whether the instance's own document is in the view now. It is not
    /// for a moment while the view loads or reloads.
    pub document: bool,
}

/// A client's listener, as native routing needs it: where it listens. What a
/// window presents to it is that window's own registration, which the shell
/// keeps (`pages.rs`) and this never sees.
#[derive(Clone, PartialEq, Eq)]
pub struct Material {
    pub proxy: SocketAddrV4,
    /// The route allows no failover: when the listener cannot be reached,
    /// or refuses, the window's request fails instead of going to the
    /// instance's address by itself. For "the direct path and the relay
    /// only".
    pub exclusive: bool,
}

/// What the shell does next. Labels only.
#[derive(Debug, PartialEq, Eq)]
pub enum Effect {
    /// Start a client for the published instance view `runtime`, then `open`.
    Open { runtime: String },
    /// Put the session's route on this window.
    Install { session: u64, window: String },
    /// Answer the session's listener when this window is challenged by it.
    Answer { session: u64, window: String },
    /// Take the route off these windows, then drop the session's client.
    Retire { session: u64, detach: Vec<String> },
    /// Make the session's listener direct-only, closing the tunnel
    /// connections it carries (`true`), or let it serve the tunnel again
    /// (`false`), for the connections its windows open from now on.
    Strict { session: u64, only: bool },
    /// Reload these windows of the session, once: their connections are
    /// then made anew, on the session's direct link.
    Reload { session: u64, windows: Vec<String> },
    /// Ask the session's rendezvous now, not when it is next due.
    Ask { session: u64 },
    /// Open the instance of the view `runtime` again: a new view, with a
    /// new session's route from before its first load.
    Reopen { runtime: String },
    /// Put a route to nothing on this window, with no failover: an app
    /// window of the direct-only instance at `origin`, opened while that
    /// instance has no session. Its requests fail; they never take the
    /// tunnel. Only where a route is put on an open window.
    Block { window: String, origin: String },
    /// Set this instance's choice back to automatic (`Wiring::revert`),
    /// then open the instance of the view `runtime` again under it.
    Revert {
        account: String,
        origin: String,
        runtime: String,
    },
}

/// A session of an instance whose choice is "the direct path and the relay
/// only".
struct Only {
    phase: Phase,
    /// The core's count of failed direct-only connections at the last look.
    counted: Option<u64>,
    /// The core's count of path changes at the last look.
    sequence: Option<u64>,
    /// When direct-only connections failed while a link was reported.
    failed: Vec<Instant>,
}

enum Phase {
    /// The listener serves the tunnel, until a link is up.
    Connecting {
        /// Since when the tunnel is known to reach the instance with no
        /// link coming of it, and how often the instance has answered so.
        unlinked: Option<(Instant, u8)>,
    },
    /// A link is up and the listener is direct-only.
    Linked,
    /// The link went and the listener is still direct-only: requests wait,
    /// then fail, until `until`.
    Failing { until: Instant },
}

/// How long requests fail after a direct-only session's link went, before
/// it connects again through the tunnel. The core lets one request wait as
/// long for a link before it fails it.
const FAILING: Duration = Duration::from_secs(10);
/// A direct-only session that is connecting: how long the tunnel may reach
/// the instance with no link coming up, and how many of the instance's
/// answers may come to nothing, before the choice is set back.
const UNLINKED: (Duration, u8) = (Duration::from_secs(20), 2);
/// How often, within how long, a direct-only instance's link may have to be
/// made again: the third time in ten minutes arms the setting-back, which
/// is done when the instance is next heard from (it answers a rendezvous,
/// or a link comes up). Three drops for want of any network set nothing
/// back.
const AGAIN: (usize, Duration) = (3, Duration::from_secs(600));
/// How many direct-only connections may fail, within how long, while a link
/// is reported, before that link is taken for one that carries nothing.
const CARRYING: (usize, Duration) = (3, Duration::from_secs(60));
/// How long an instance that is being opened again may take to show its
/// new view before it is asked again. The sign-in it starts with and the
/// new view's creation come within this.
const REOPENING: Duration = Duration::from_secs(60);
/// How soon an instance is asked again after the request to open it again
/// failed outright: this long after the first failure, twice as long after
/// each further one, and never sooner than `REOPENING` allows later.
const REFUSED: Duration = Duration::from_secs(10);

/// A view whose instance is being opened again. It stays the one that is to
/// be replaced until another view is on show: whatever the choice has
/// become meanwhile, its route was made for another kind of choice.
struct Reopening {
    runtime: String,
    /// Not asked again before this.
    until: Instant,
    /// How often the request has failed outright, in a row.
    refused: u32,
}

struct Session {
    id: u64,
    target: Target,
    material: Material,
    /// The instance view this session belongs to, once it is published.
    runtime: Option<String>,
    windows: Vec<String>,
    routed: Vec<String>,
    /// Since when the view's document has not been the instance's.
    away: Option<Instant>,
    /// A rendezvous has been answered in this session.
    ready: bool,
    /// It only learns whether the instance answers; it routes no window.
    scout: bool,
    /// Started before its instance view was created, where a route can be
    /// put on an open window: every window of it is routed as it is made,
    /// before its first load, without waiting for an answer.
    early: bool,
    opened: Instant,
    /// The choice is "the direct path and the relay only".
    only: Option<Only>,
}

/// How long a session made for a window that is being created may wait for
/// that window to become the published instance view. The shell gives a new
/// instance view 30 seconds to load.
const UNPUBLISHED: Duration = Duration::from_secs(60);
/// How long a session's instance view may be without the instance's document
/// (loading, reloading) before the session ends. Sign-out, a switch and a
/// change of the choice do not wait for it.
const AWAY: Duration = Duration::from_secs(30);
/// After a session's listener ended, or none could be started, how long
/// before another is started for the same instance view.
const COOLING: Duration = Duration::from_secs(60);

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
/// Never sooner than this after a try, however little of a kept admission
/// is left.
const SOONEST: Duration = Duration::from_secs(1);

/// When one session asks its rendezvous next, from how each try ended.
#[derive(Default)]
pub struct Pace {
    /// Tries in a row without a fresh answer.
    failures: usize,
    /// The last try left the session with a direct link.
    linked: bool,
}

impl Pace {
    pub fn next(&mut self, outcome: &RefreshOutcome, now: Instant) -> Instant {
        let after = match outcome {
            RefreshOutcome::Renewed => {
                (self.failures, self.linked) = (0, true);
                RENEW
            }
            // The link is kept under the admission it had. The core says by
            // when to ask again so that it does not run out between two
            // tries: that bound wins over the ladder.
            RefreshOutcome::Retained { retry_before } => {
                (self.failures, self.linked) = (self.failures + 1, true);
                self.ladder().min(*retry_before).max(SOONEST)
            }
            // No link. One that was just lost is asked for again from the
            // ladder's first step, not from where its kept tries had got to.
            RefreshOutcome::Retired => {
                self.failures = if self.linked { 1 } else { self.failures + 1 };
                self.linked = false;
                self.ladder()
            }
        };
        now + after
    }

    fn ladder(&self) -> Duration {
        RETRY[self.failures.clamp(1, RETRY.len()) - 1]
    }
}

/// One account's instance, as the things kept per instance name it.
fn key(account: &str, origin: &str) -> String {
    format!("{account} {origin}")
}

/// An app window that shows another computer's instance, whether or not a
/// session exists for it right now.
struct AppWindow {
    window: String,
    account: String,
    origin: String,
}

pub struct Wiring {
    routing: Routing,
    proxy_configured: bool,
    system_proxy_rules_out: bool,
    choices: Choices,
    /// How many choices of path this run of the app has made.
    chosen: u64,
    sessions: Vec<Session>,
    /// Every open app window of a remote instance, so a session that starts
    /// after a window was opened still finds it.
    app_windows: Vec<AppWindow>,
    next: u64,
    cooling: Option<(String, Instant)>,
    /// When a direct-only instance's link had to be made again, by account
    /// and origin.
    again: Vec<(String, Instant)>,
    /// The instances whose link was made again once too often: their
    /// choice is set back when they are next heard from.
    armed: Vec<String>,
    /// The instance view that is being opened again.
    reopening: Option<Reopening>,
    /// The view that an opening-again brought, until a session holds it.
    /// One that comes without a session is not opened again a second time:
    /// the tunnel has just carried its sign-in, and no route could be made.
    reopened: Option<String>,
}

impl Wiring {
    pub fn new(routing: Routing, proxy_configured: bool, choices: Choices) -> Self {
        Self {
            routing,
            proxy_configured,
            system_proxy_rules_out: false,
            choices,
            chosen: 0,
            sessions: Vec::new(),
            app_windows: Vec::new(),
            next: 0,
            cooling: None,
            again: Vec::new(),
            armed: Vec::new(),
            reopening: None,
            reopened: None,
        }
    }

    pub fn mode(&self, account: &str, origin: &str) -> Mode {
        self.choices.mode(account, origin)
    }

    /// The choice, with the app's own setting-back of it while that stands
    /// and how many choices had been made when it is read: what puts two
    /// readings of it in order, whichever is told first.
    pub fn choice(&self, account: &str, origin: &str) -> (Mode, Option<Reverted>, u64) {
        (
            self.mode(account, origin),
            self.choices.reverted(account, origin),
            self.chosen,
        )
    }

    /// Keeps the choice someone made for this account's instance.
    /// "Cloudflare tunnel only" ends its sessions at once; "automatic"
    /// starts one at the next `reconcile` (live routing) or the next window
    /// (routing at creation).
    ///
    /// A choice to or from "the direct path and the relay only" ends the
    /// instance's sessions too, whichever way: its windows were made for
    /// the other kind of route. The caller then opens the instance again
    /// (`reopens`).
    pub fn choose(&mut self, account: &str, origin: &str, mode: Mode) -> Vec<Effect> {
        let before = self.mode(account, origin);
        self.choices.set(account, origin, mode);
        self.chosen += 1;
        self.forget_again(account, origin);
        if mode != Mode::Tunnel && !Self::reopens(before, mode) {
            return Vec::new();
        }
        self.retire_where(|session| session.target.account == account && session.target.origin == origin)
    }

    /// Whether a change of the choice needs the instance opened again: one
    /// to or from "the direct path and the relay only". A window's route is
    /// of one kind for as long as its connections live (the module's
    /// notes), so only a new view takes the other.
    pub fn reopens(before: Mode, after: Mode) -> bool {
        before != after && (before == Mode::Direct || after == Mode::Direct)
    }

    /// The app sets this instance's choice back to automatic itself
    /// (`Effect::Revert`), and its sessions end. The caller opens the
    /// instance again.
    pub fn revert(&mut self, account: &str, origin: &str, reverted: Reverted) -> Vec<Effect> {
        self.choices.revert(account, origin, reverted);
        self.chosen += 1;
        self.forget_again(account, origin);
        self.retire_where(|session| session.target.account == account && session.target.origin == origin)
    }

    /// A choice was made or set back: what was counted against the old one
    /// is forgotten.
    fn forget_again(&mut self, account: &str, origin: &str) {
        let instance = key(account, origin);
        self.again.retain(|(noted, _)| *noted != instance);
        self.armed.retain(|noted| *noted != instance);
    }

    /// What the system's own proxy settings say now. They rule the direct
    /// route out where it would change how other hosts are reached or could
    /// not reach the instance at all: on Windows any system proxy (explicit,
    /// PAC or auto-detect), because the route replaces it for the whole
    /// window; on macOS automatic configuration (PAC or auto-detect) or a
    /// TLS-to-proxy endpoint. While they do, sessions end at the next
    /// `reconcile` and none starts.
    pub fn system_proxy(&mut self, rules_out: bool) {
        self.system_proxy_rules_out = rules_out;
    }

    /// Whether a session may exist for `target` at all.
    fn permitted(&self, target: &Target) -> bool {
        !self.system_proxy_rules_out && self.chosen(target)
    }

    /// `permitted`, but for the system's proxy settings, which may change
    /// without the app hearing of it.
    fn chosen(&self, target: &Target) -> bool {
        self.routing != Routing::Unsupported
            && !self.proxy_configured
            // Only a managed address has a direct path: a custom domain keeps the tunnel.
            && target.host().is_some_and(|host| Authority::managed(host).is_some())
            && !target.account.is_empty()
            && !target.instance.is_empty()
            && self.choices.mode(&target.account, &target.origin) != Mode::Tunnel
    }

    /// Whether `target`'s choice is "the direct path and the relay only".
    pub fn only(&self, target: &Target) -> bool {
        self.choices.mode(&target.account, &target.origin) == Mode::Direct
    }

    fn reached(&self, target: &Target) -> bool {
        self.choices.reached(&target.account, &target.origin)
    }

    /// Whether a route is given to a window only when it is created.
    pub fn routes_at_creation(&self) -> bool {
        self.routing == Routing::AtCreation
    }

    /// Whether the instance view about to be created for `target` gets a
    /// route before its first load, so that a client must exist before it
    /// does: wherever a window can be routed, for an instance that answered
    /// before, and always for "the direct path and the relay only".
    pub fn starts_at_creation(&self, target: &Target) -> bool {
        self.permitted(target) && (self.reached(target) || self.only(target))
    }

    /// A client exists for `target`. `runtime` is the published instance view
    /// it was started for (`Effect::Open`), or `None` when its window is
    /// still to be created (`starts_at_creation`). The caller registers that
    /// window with `window_created`.
    pub fn open(&mut self, target: Target, material: Material, runtime: Option<&str>, now: Instant) -> u64 {
        self.next += 1;
        // A direct-only session starts by connecting, with a listener that
        // serves the tunnel as any does.
        let only = self.only(&target).then(|| Only {
            phase: Phase::Connecting { unlinked: None },
            counted: None,
            sequence: None,
            failed: Vec::new(),
        });
        // Where a route is given only at creation, a session started for a
        // view that is already open can route nothing.
        let scout = self.routing == Routing::AtCreation && runtime.is_some();
        let early = self.routing == Routing::Live && runtime.is_none();
        let mut windows: Vec<String> = runtime.map(str::to_owned).into_iter().collect();
        // Where a route can be put on an open window, the app windows that
        // are already open for this instance belong to the session too: one
        // that replaces an ended session, or starts after they were opened,
        // routes them with its first answer. (They are closed with their
        // instance view, so any that are open are this view's.)
        if self.routing == Routing::Live && runtime.is_some() {
            windows.extend(
                self.app_windows
                    .iter()
                    .filter(|open| open.account == target.account && open.origin == target.origin)
                    .map(|open| open.window.clone()),
            );
        }
        self.sessions.push(Session {
            id: self.next,
            target,
            material,
            runtime: runtime.map(str::to_owned),
            windows,
            routed: Vec::new(),
            away: None,
            ready: false,
            scout,
            early,
            opened: now,
            only,
        });
        self.next
    }

    /// The session of the published instance view of `account` at `origin`:
    /// the one an app window opened from that view shares. A session that
    /// was started before its view was created counts from the start: its
    /// view can be on show for a moment before a look has seen it there,
    /// and an app window opened in that moment is routed like any other.
    ///
    /// The session whose view is published comes first. Without one, of the
    /// sessions that were started early it is the newest that still has its
    /// window: an older one whose view was replaced, or was never made, is
    /// about to end, and a window that joined it would lose its route.
    pub fn shared(&self, account: &str, origin: &str) -> Option<u64> {
        let of_instance =
            |session: &&Session| !session.scout && session.target.account == account && session.target.origin == origin;
        let published = self
            .sessions
            .iter()
            .filter(of_instance)
            .find(|session| session.runtime.is_some());
        let early = || {
            self.sessions
                .iter()
                .filter(of_instance)
                .filter(|session| session.early && !session.windows.is_empty())
                .max_by_key(|session| session.id)
        };
        published.or_else(early).map(|session| session.id)
    }

    /// An app window that shows the instance of `account` at `origin` now
    /// exists. It is remembered until it closes, with or without a session.
    ///
    /// The window is a new one, whatever its label: an app window's label
    /// is used again once its window has closed, so anything still known
    /// under it belonged to a window that is gone, and is forgotten first.
    ///
    /// Then it joins a session. Where a route can be put on an open window,
    /// the one its instance has right now, whenever that started: also one
    /// that started while the window was being made. Where a route is given
    /// only at creation, the session it was `created_for`, if any: only that
    /// one's listener is in the window's route.
    pub fn app_window_created(
        &mut self,
        window: &str,
        account: &str,
        origin: &str,
        created_for: Option<u64>,
    ) -> Vec<Effect> {
        self.app_windows.retain(|open| open.window != window);
        for session in &mut self.sessions {
            session.windows.retain(|label| label != window);
            session.routed.retain(|label| label != window);
        }
        self.app_windows.push(AppWindow {
            window: window.to_owned(),
            account: account.to_owned(),
            origin: origin.to_owned(),
        });
        let session = match self.routing {
            // Only a session that may still exist: one the system's proxy
            // settings have just ruled out ends at the next look, and a
            // window must not get its route for the moment in between.
            Routing::Live => self.shared(account, origin).filter(|shared| {
                self.sessions
                    .iter()
                    .any(|session| session.id == *shared && self.permitted(&session.target))
            }),
            Routing::AtCreation => created_for,
            Routing::Unsupported => None,
        };
        if let Some(session) = session {
            return self.window_created(session, window);
        }
        // A direct-only instance's window is never left without a route:
        // with no session to join it gets one to nothing, and fails until
        // its instance is opened again, which closes it. (Where the route
        // is given at creation, it was: `blocks`.)
        if self.routing == Routing::Live && self.blocks(account, origin) {
            return vec![Effect::Block {
                window: window.to_owned(),
                origin: origin.to_owned(),
            }];
        }
        Vec::new()
    }

    /// Whether an app window of this account's instance that has no session
    /// to join must get a route to nothing: the instance's choice is "the
    /// direct path and the relay only". Without one the window would load
    /// through the tunnel, which that choice forbids once and for all
    /// windows.
    pub fn blocks(&self, account: &str, origin: &str) -> bool {
        self.choices.mode(account, origin) == Mode::Direct
    }

    /// A window that shows the session's instance now exists: its instance
    /// view, whose label is never used twice.
    pub fn window_created(&mut self, session: u64, window: &str) -> Vec<Effect> {
        let routing = self.routing;
        let Some(found) = self.sessions.iter_mut().find(|candidate| candidate.id == session) else {
            return Vec::new();
        };
        if found.scout || found.windows.iter().any(|label| label == window) {
            return Vec::new();
        }
        found.windows.push(window.to_owned());
        match routing {
            // Its route was given when it was created; only the listener's
            // challenge is left to answer.
            Routing::AtCreation => {
                found.routed.push(window.to_owned());
                vec![Effect::Answer {
                    session,
                    window: window.to_owned(),
                }]
            }
            // After the first answer, or at once for a session that was
            // started so that its windows are routed before they load.
            Routing::Live if found.ready || found.early => {
                found.routed.push(window.to_owned());
                vec![Effect::Install {
                    session,
                    window: window.to_owned(),
                }]
            }
            _ => Vec::new(),
        }
    }

    /// Brings the sessions in line with the instance view the main window
    /// holds now (`published`) and the windows that are still open.
    pub fn reconcile(
        &mut self,
        published: Option<&Published>,
        open: impl Fn(&str) -> bool,
        now: Instant,
    ) -> Vec<Effect> {
        let mut effects = Vec::new();
        self.app_windows.retain(|app| open(&app.window));
        for session in &mut self.sessions {
            session.windows.retain(|window| open(window));
            session.routed.retain(|window| open(window));
        }
        let mut kept = Vec::new();
        for mut session in std::mem::take(&mut self.sessions) {
            // A scout has nothing left to learn once the instance answered.
            let allowed = self.permitted(&session.target) && (!session.scout || !self.reached(&session.target));
            let keep = match (&session.runtime, published) {
                (Some(runtime), Some(shown)) => {
                    let same = allowed && shown.runtime == *runtime && shown.target == session.target;
                    if same && !shown.document {
                        // The same view, between two of its documents.
                        let since = *session.away.get_or_insert(now);
                        now.saturating_duration_since(since) < AWAY
                    } else {
                        session.away = None;
                        same
                    }
                }
                (Some(_), None) => false,
                (None, Some(shown))
                    if shown.document && shown.target == session.target && session.windows.contains(&shown.runtime) =>
                {
                    session.runtime = Some(shown.runtime.clone());
                    allowed
                }
                (None, _) => allowed && now.saturating_duration_since(session.opened) < UNPUBLISHED,
            };
            if keep {
                kept.push(session);
            } else {
                effects.push(self.retired(session));
            }
        }
        self.sessions = kept;
        effects.extend(self.only_in_time(now));
        if self
            .reopening
            .as_ref()
            .is_some_and(|replaced| published.is_none_or(|shown| shown.runtime != replaced.runtime))
        {
            // The view that was being replaced is gone, and the one on show
            // now, if any, is what replaced it.
            self.reopening = None;
            self.reopened = published.map(|shown| shown.runtime.clone());
        }
        // A direct-only instance's view is never left without a session's
        // route, whatever document is in it: it is opened again with one.
        // Where no window of it can be routed now, the choice is set back.
        if let Some(shown) = published.filter(|shown| self.only(&shown.target)) {
            let held = self.sessions.iter().any(|session| {
                session.target == shown.target
                    && (session.runtime.as_deref() == Some(shown.runtime.as_str())
                        || session.windows.contains(&shown.runtime))
            });
            if held && self.reopened.as_deref() == Some(shown.runtime.as_str()) {
                self.reopened = None;
            }
            if !held && !self.is_reopening(now) {
                let came_without = self.reopened.as_deref() == Some(shown.runtime.as_str());
                let effect = if self.permitted(&shown.target) && !came_without {
                    // Opening it again is making its link again.
                    self.made_again(&shown.target, now);
                    self.replacing(&shown.runtime, now);
                    Effect::Reopen {
                        runtime: shown.runtime.clone(),
                    }
                } else {
                    self.set_back(&shown.target, &shown.runtime, now)
                };
                effects.push(effect);
            }
            return effects;
        }
        // A view that was to be replaced and is still the one on show: the
        // request to open its instance again failed, or brought no view.
        // It is asked again, whatever the choice is now. A view that left
        // "the direct path and the relay only" keeps a route that serves
        // nothing and allows no failover, so nothing else would ever bring
        // it back; and it gets no session of its own (below) either way.
        if let Some(shown) = published {
            let due = self
                .reopening
                .as_ref()
                .is_some_and(|replaced| replaced.runtime == shown.runtime && now >= replaced.until);
            if due {
                self.replacing(&shown.runtime, now);
                effects.push(Effect::Reopen {
                    runtime: shown.runtime.clone(),
                });
                return effects;
            }
        }
        if let Some(shown) = published.filter(|shown| shown.document) {
            let held = self
                .sessions
                .iter()
                .any(|session| session.runtime.as_deref() == Some(shown.runtime.as_str()));
            let cooling = self
                .cooling
                .as_ref()
                .is_some_and(|(runtime, until)| *runtime == shown.runtime && now < *until);
            let wanted = match self.routing {
                Routing::Live => true,
                // Only to learn whether the instance answers: its windows
                // were created without a route and keep the tunnel.
                Routing::AtCreation => !self.reached(&shown.target),
                Routing::Unsupported => false,
            };
            // A view that is about to be replaced gets no session of its own.
            if wanted && !held && !cooling && !self.is_reopening(now) && self.permitted(&shown.target) {
                effects.push(Effect::Open {
                    runtime: shown.runtime.clone(),
                });
            }
        }
        effects
    }

    /// What time alone changes for the direct-only sessions: one whose
    /// requests have failed for long enough connects again, and one that
    /// has been connecting for too long, with the tunnel reaching the
    /// instance, has its choice set back.
    fn only_in_time(&mut self, now: Instant) -> Vec<Effect> {
        let mut effects = Vec::new();
        let due: Vec<(u64, bool)> = self
            .sessions
            .iter()
            .filter_map(|session| match session.only.as_ref()?.phase {
                Phase::Failing { until } if now >= until => Some((session.id, true)),
                Phase::Connecting {
                    unlinked: Some((since, _)),
                } if now.saturating_duration_since(since) >= UNLINKED.0 => Some((session.id, false)),
                _ => None,
            })
            .collect();
        for (id, failing) in due {
            let Some(target) = self.target_of(id) else { continue };
            if failing {
                // Connecting again is making the link again.
                self.made_again(&target, now);
                if let Some(only) = self.only_mut(id) {
                    only.phase = Phase::Connecting { unlinked: None };
                }
                effects.push(Effect::Strict {
                    session: id,
                    only: false,
                });
                effects.push(Effect::Ask { session: id });
            } else if let Some(runtime) = self.view_of(id) {
                // The instance answered in this very connecting, and no
                // link has come since: it is heard from, and not linked.
                effects.push(self.set_back(&target, &runtime, now));
            }
        }
        effects
    }

    /// When time alone next changes something for a direct-only session
    /// (`only_in_time`), so the shell looks then and not only at its usual
    /// pace.
    pub fn deadline(&self) -> Option<Instant> {
        self.sessions
            .iter()
            .filter_map(|session| match session.only.as_ref()?.phase {
                Phase::Failing { until } => Some(until),
                Phase::Connecting {
                    unlinked: Some((since, _)),
                } => Some(since + UNLINKED.0),
                _ => None,
            })
            .min()
    }

    /// The session's client says whether a direct link is up (`linked`),
    /// how often its path has changed so far (`sequence`) and how many
    /// direct-only connections it has failed so far (`failures`); both
    /// counts only ever grow. Nothing follows for a session that is not
    /// direct-only.
    ///
    /// The client is looked at when it says something changed, and several
    /// changes may have happened by then. So every failure since the last
    /// look counts, not one a look; and failures count against a link only
    /// when the path did not change between the two looks: a link that
    /// went and came back unseen failed them for want of a link, which is
    /// no fault of the link that is there now. For the same reason a change
    /// of the path, seen or not, starts the count again: three failures
    /// are three on one path.
    pub fn path(&mut self, session: u64, linked: bool, sequence: u64, failures: u64, now: Instant) -> Vec<Effect> {
        let Some(found) = self.sessions.iter_mut().find(|candidate| candidate.id == session) else {
            return Vec::new();
        };
        let Some(only) = found.only.as_mut() else {
            return Vec::new();
        };
        // The first count is where this session starts counting from.
        let fresh = failures.saturating_sub(only.counted.unwrap_or(failures));
        only.counted = Some(failures);
        let steady = only.sequence.replace(sequence) == Some(sequence);
        if !steady {
            // Another path, or the same one after something was closed on
            // it: what failed before is no evidence about it.
            only.failed.clear();
        }
        let (connecting, failing) = match only.phase {
            Phase::Connecting { .. } => (true, false),
            Phase::Failing { .. } => (false, true),
            Phase::Linked => (false, false),
        };
        if connecting {
            if !linked {
                return Vec::new();
            }
            // A link that had to be made once too often: the instance is
            // heard from, and its choice is set back.
            let target = found.target.clone();
            if self.armed.contains(&key(&target.account, &target.origin)) {
                return match self.view_of(session) {
                    Some(runtime) => vec![self.set_back(&target, &runtime, now)],
                    None => Vec::new(),
                };
            }
            let Some(only) = self.only_mut(session) else {
                return Vec::new();
            };
            // Connected: from here on nothing of this session's uses the
            // tunnel. Its windows are reloaded onto the link.
            only.phase = Phase::Linked;
            only.failed.clear();
            let windows = self
                .sessions
                .iter()
                .find(|candidate| candidate.id == session)
                .map(|found| found.routed.clone())
                .unwrap_or_default();
            return vec![
                Effect::Strict { session, only: true },
                Effect::Reload { session, windows },
            ];
        }
        if failing {
            // Back while requests were still failing: they go on the link
            // again by themselves, and nothing was let onto the tunnel.
            if linked {
                only.phase = Phase::Linked;
            }
            return Vec::new();
        }
        if !linked {
            // The link went. The listener stays direct-only: requests wait
            // for a link, and fail without one.
            only.phase = Phase::Failing { until: now + FAILING };
            return Vec::new();
        }
        if fresh == 0 || !steady {
            return Vec::new();
        }
        // A link is reported, and connections fail on it all the same: it
        // carries nothing, and asking again would only renew it. A new
        // client makes a new one.
        let fresh = usize::try_from(fresh).unwrap_or(CARRYING.0).min(CARRYING.0);
        only.failed.resize(only.failed.len() + fresh, now);
        only.failed.retain(|at| now.saturating_duration_since(*at) < CARRYING.1);
        if only.failed.len() < CARRYING.0 {
            return Vec::new();
        }
        only.failed.clear();
        let target = found.target.clone();
        let Some(runtime) = self.view_of(session) else {
            return Vec::new();
        };
        self.made_again(&target, now);
        self.replacing(&runtime, now);
        let mut effects = self.retire_where(|candidate| candidate.id == session);
        effects.push(Effect::Reopen { runtime });
        effects
    }

    /// The instance answered a rendezvous of the session through the
    /// tunnel, and no link came of it: it offers no direct path now, or
    /// none that reaches it from here. For a direct-only session that is
    /// connecting, the second such answer sets the choice back. A request
    /// that reached nothing is not this: nothing is learned from it.
    pub fn unlinked(&mut self, session: u64, now: Instant) -> Vec<Effect> {
        let Some(found) = self.sessions.iter_mut().find(|candidate| candidate.id == session) else {
            return Vec::new();
        };
        let Some(Only {
            phase: Phase::Connecting { unlinked },
            ..
        }) = found.only.as_mut()
        else {
            return Vec::new();
        };
        let (_, answers) = unlinked.get_or_insert((now, 0));
        *answers += 1;
        let answers = *answers;
        let target = found.target.clone();
        // One answer is enough for a link that was made once too often.
        if answers < UNLINKED.1 && !self.armed.contains(&key(&target.account, &target.origin)) {
            return Vec::new();
        }
        match self.view_of(session) {
            Some(runtime) => vec![self.set_back(&target, &runtime, now)],
            None => Vec::new(),
        }
    }

    /// The instance of the view `runtime` is being opened again from now:
    /// until its new view is the one on show, or for as long as that may
    /// take, the old view gets no session and is not opened again.
    pub fn reopening(&mut self, runtime: &str, now: Instant) {
        self.replacing(runtime, now);
    }

    /// The request to open the instance of the view `runtime` again failed
    /// outright (no network, or its sign-in was refused): it is asked again
    /// sooner than a request that is still under way would be, and later
    /// each time it fails again. For a view of "the direct path and the
    /// relay only" nothing changes: its opening-again is counted, and keeps
    /// its pace.
    pub fn reopen_refused(&mut self, runtime: &str, target: &Target, now: Instant) {
        if self.only(target) {
            return;
        }
        let Some(replaced) = self.reopening.as_mut().filter(|replaced| replaced.runtime == runtime) else {
            return;
        };
        replaced.refused += 1;
        let wait = REFUSED
            .checked_mul(1 << (replaced.refused - 1).min(8))
            .map_or(REOPENING, |wait| wait.min(REOPENING));
        replaced.until = replaced.until.min(now + wait);
    }

    /// The view `runtime` is to be replaced from now. A view that already
    /// was keeps its count of refused requests.
    fn replacing(&mut self, runtime: &str, now: Instant) {
        let refused = self
            .reopening
            .as_ref()
            .filter(|replaced| replaced.runtime == runtime)
            .map_or(0, |replaced| replaced.refused);
        self.reopening = Some(Reopening {
            runtime: runtime.to_owned(),
            until: now + REOPENING,
            refused,
        });
    }

    fn is_reopening(&self, now: Instant) -> bool {
        self.reopening.as_ref().is_some_and(|replaced| now < replaced.until)
    }

    /// Notes that this instance's link has to be made again now. Once too
    /// often (`AGAIN`), its choice is set back when it is next heard from.
    fn made_again(&mut self, target: &Target, now: Instant) {
        let instance = key(&target.account, &target.origin);
        self.again
            .retain(|(_, at)| now.saturating_duration_since(*at) < AGAIN.1);
        self.again.push((instance.clone(), now));
        if self.again.iter().filter(|(noted, _)| *noted == instance).count() >= AGAIN.0
            && !self.armed.contains(&instance)
        {
            self.armed.push(instance);
        }
    }

    fn set_back(&mut self, target: &Target, runtime: &str, now: Instant) -> Effect {
        self.replacing(runtime, now);
        Effect::Revert {
            account: target.account.clone(),
            origin: target.origin.clone(),
            runtime: runtime.to_owned(),
        }
    }

    fn target_of(&self, session: u64) -> Option<Target> {
        self.sessions
            .iter()
            .find(|candidate| candidate.id == session)
            .map(|found| found.target.clone())
    }

    fn only_mut(&mut self, session: u64) -> Option<&mut Only> {
        self.sessions
            .iter_mut()
            .find(|candidate| candidate.id == session)?
            .only
            .as_mut()
    }

    /// The session's instance view: the one it is published as, or the
    /// first window it was given, which is that view before it is shown.
    fn view_of(&self, session: u64) -> Option<String> {
        let found = self.sessions.iter().find(|candidate| candidate.id == session)?;
        found.runtime.clone().or_else(|| found.windows.first().cloned())
    }

    /// The end of one session, as an effect. A direct-only session's route
    /// is not taken off a window that is still open where that would let
    /// the window onto the tunnel (live routing: the route is all that
    /// stands between it and the instance's address). Its client is
    /// dropped, the route then points at nothing and allows no failover,
    /// and the window's requests fail until it is opened again.
    fn retired(&self, session: Session) -> Effect {
        let keeps_route = session.only.is_some() && self.routing == Routing::Live;
        Effect::Retire {
            session: session.id,
            detach: if keeps_route { Vec::new() } else { session.routed },
        }
    }

    /// Whether a session may still start for the published view with nothing
    /// changing in the app: when the pause after a listener's end is over,
    /// or once the system's proxy settings allow it. The shell keeps looking
    /// while this holds, and stops when it does not.
    pub fn awaits(&self, published: Option<&Published>) -> bool {
        let Some(shown) = published else { return false };
        let held = self
            .sessions
            .iter()
            .any(|session| session.runtime.as_deref() == Some(shown.runtime.as_str()));
        // A direct-only instance's view always gets a session, or has its
        // choice set back, at the next look.
        if self.only(&shown.target) {
            return !held;
        }
        let wanted = match self.routing {
            Routing::Live => true,
            Routing::AtCreation => !self.reached(&shown.target),
            Routing::Unsupported => false,
        };
        wanted && !held && self.chosen(&shown.target)
    }

    /// A rendezvous of the session was answered and its direct link is up
    /// (`true`), or was not. The first answer makes the instance one that
    /// "has answered before", and puts the route on the session's windows
    /// where that can be done live; a scout ends with it. A later failure
    /// changes nothing here: the listener keeps serving the tunnel path, so
    /// the route stays.
    pub fn probed(&mut self, session: u64, answered: bool) -> Vec<Effect> {
        let routing = self.routing;
        let Some(found) = self.sessions.iter_mut().find(|candidate| candidate.id == session) else {
            return Vec::new();
        };
        if !answered {
            return Vec::new();
        }
        found.ready = true;
        self.choices.remember(&found.target.account, &found.target.origin);
        if found.scout {
            return self.retire_where(|candidate| candidate.id == session);
        }
        if routing != Routing::Live {
            return Vec::new();
        }
        let fresh: Vec<String> = found
            .windows
            .iter()
            .filter(|window| !found.routed.contains(window))
            .cloned()
            .collect();
        found.routed.extend(fresh.iter().cloned());
        fresh
            .into_iter()
            .map(|window| Effect::Install { session, window })
            .collect()
    }

    /// The session's listener ended for good. Its route comes off; another
    /// session starts for the same instance view only after a pause (live
    /// routing) or with the next window (routing at creation).
    pub fn listener_ended(&mut self, session: u64, now: Instant) -> Vec<Effect> {
        if let Some(runtime) = self
            .sessions
            .iter()
            .find(|candidate| candidate.id == session)
            .and_then(|found| found.runtime.clone())
        {
            self.cooling = Some((runtime, now + COOLING));
        }
        self.retire_where(|candidate| candidate.id == session)
    }

    /// No client could be started for the instance view `runtime` (not an
    /// instance the direct path serves, or no loopback port): none is tried
    /// again for it for a while.
    pub fn declined(&mut self, runtime: &str, now: Instant) {
        self.cooling = Some((runtime.to_owned(), now + COOLING));
    }

    fn retire_where(&mut self, ends: impl Fn(&Session) -> bool) -> Vec<Effect> {
        let (ended, kept): (Vec<_>, Vec<_>) = std::mem::take(&mut self.sessions)
            .into_iter()
            .partition(|session| ends(session));
        self.sessions = kept;
        ended.into_iter().map(|session| self.retired(session)).collect()
    }

    /// Whether `window` is still the session's instance view: the selection
    /// a rendezvous through it was started for. A view that is between two
    /// documents is still the selection; a session that ended, or whose
    /// view is another one, is not.
    pub fn selects(&self, session: u64, window: &str) -> bool {
        self.sessions
            .iter()
            .any(|candidate| candidate.id == session && candidate.runtime.as_deref() == Some(window))
    }

    /// Whether `window` is, at this moment, a window the session routes.
    /// Asked on the main thread just before a route or a challenge answer is
    /// put on the window: one that was queued for a session that has ended
    /// meanwhile is then dropped, instead of landing after the session's
    /// route was taken off.
    pub fn routes(&self, session: u64, window: &str) -> bool {
        self.sessions
            .iter()
            .any(|candidate| candidate.id == session && candidate.routed.iter().any(|label| label == window))
    }

    /// Whether no session routes `window` at this moment. Asked on the main
    /// thread just before a route to nothing is put on it (`Effect::Block`):
    /// a window that a session has taken meanwhile keeps that session's
    /// route.
    pub fn unrouted(&self, window: &str) -> bool {
        !self
            .sessions
            .iter()
            .any(|candidate| candidate.routed.iter().any(|label| label == window))
    }

    /// The window a session asks its rendezvous through: its instance view,
    /// once that is published, while the instance's document is in it.
    pub fn asks_through(&self, session: u64) -> Option<&str> {
        self.sessions
            .iter()
            .find(|candidate| candidate.id == session && candidate.away.is_none())
            .and_then(|found| found.runtime.as_deref())
    }

    /// What routing a window to the session's listener takes.
    pub fn route(&self, session: u64) -> Option<(&str, &Material)> {
        let found = self.sessions.iter().find(|candidate| candidate.id == session)?;
        Some((found.target.host()?, &found.material))
    }

    pub fn origin(&self, session: u64) -> Option<&str> {
        self.sessions
            .iter()
            .find(|candidate| candidate.id == session)
            .map(|found| found.target.origin.as_str())
    }

    /// Whether a window of the instance at `origin` could ever be routed:
    /// this system and configuration can route, and the address is one the
    /// direct path serves. Whatever the choice of path is now.
    pub fn routable(&self, origin: &str) -> bool {
        self.routing != Routing::Unsupported
            && !self.proxy_configured
            && origin
                .strip_prefix("https://")
                .is_some_and(|host| Authority::managed(host).is_some())
    }

    /// The session whose route is on `window`, if one is.
    pub fn routed_by(&self, window: &str) -> Option<u64> {
        self.sessions
            .iter()
            .find(|session| session.routed.iter().any(|label| label == window))
            .map(|session| session.id)
    }

    /// Whether the session is direct-only and has its listener so now: a
    /// link is up, or went a moment ago.
    pub fn is_direct_only(&self, session: u64) -> bool {
        self.sessions
            .iter()
            .find(|candidate| candidate.id == session)
            .and_then(|found| found.only.as_ref())
            .is_some_and(|only| !matches!(only.phase, Phase::Connecting { .. }))
    }

    /// Whether any session exists, so the shell knows to keep looking for
    /// windows that closed.
    pub fn busy(&self) -> bool {
        !self.sessions.is_empty()
    }
}
