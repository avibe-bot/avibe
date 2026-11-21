//! Which windows share which direct client, and when it starts and ends.
//! Plain data and decisions only: the shell (`shell.rs`) does what the
//! effects say, on the main thread and against real windows.
//!
//! One session is one direct client for one signed-in instance view: the
//! main window's instance view (its "runtime") and the app windows opened
//! from it. It is found by account, instance origin and that view's own
//! label, never by an installation ID, and it ends when that view does: on
//! sign-out, a switch of instance, or a change of the choice of path.
//!
//! The rules it keeps:
//! - an instance whose choice is "Cloudflare tunnel only", or any instance
//!   while the app has a configured proxy, has no session and no route;
//! - where the system cannot route one host of one window (macOS before 14),
//!   there is no session at all;
//! - where a route can only be given when a window is created (Windows), a
//!   window gets one only for an instance that answered a rendezvous in an
//!   earlier session, and a window already open is never rebuilt. Until an
//!   instance has answered once, its session only asks (a "scout"): it
//!   routes nothing and ends with the first answer.
use super::choices::{Choices, Mode};
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
}

/// A client's listener, as native routing needs it. Deliberately not `Debug`.
/// It stays in memory: never in a URL, an argument, storage or a log.
#[derive(Clone, PartialEq, Eq)]
pub struct Material {
    pub proxy: SocketAddrV4,
    pub username: String,
    pub password: String,
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
}

struct Session {
    id: u64,
    target: Target,
    material: Material,
    /// The instance view this session belongs to, once it is published.
    runtime: Option<String>,
    windows: Vec<String>,
    routed: Vec<String>,
    /// A rendezvous has been answered in this session.
    ready: bool,
    /// It only learns whether the instance answers; it routes no window.
    scout: bool,
    opened: Instant,
}

/// How long a session made for a window that is being created may wait for
/// that window to become the published instance view. The shell gives a new
/// instance view 30 seconds to load.
const UNPUBLISHED: Duration = Duration::from_secs(60);
/// After a session's listener ended, or none could be started, how long
/// before another is started for the same instance view.
const COOLING: Duration = Duration::from_secs(60);

pub struct Wiring {
    routing: Routing,
    proxy_configured: bool,
    choices: Choices,
    sessions: Vec<Session>,
    next: u64,
    cooling: Option<(String, Instant)>,
}

impl Wiring {
    pub fn new(routing: Routing, proxy_configured: bool, choices: Choices) -> Self {
        Self {
            routing,
            proxy_configured,
            choices,
            sessions: Vec::new(),
            next: 0,
            cooling: None,
        }
    }

    pub fn mode(&self, account: &str, origin: &str) -> Mode {
        self.choices.mode(account, origin)
    }

    /// Keeps the choice for this account's instance. "Cloudflare tunnel only"
    /// ends its sessions at once; "automatic" starts one at the next
    /// `reconcile` (live routing) or the next window (routing at creation).
    pub fn choose(&mut self, account: &str, origin: &str, mode: Mode) -> Vec<Effect> {
        self.choices.set(account, origin, mode);
        if mode == Mode::Automatic {
            return Vec::new();
        }
        self.retire_where(|session| session.target.account == account && session.target.origin == origin)
    }

    /// Whether a session may exist for `target` at all.
    fn permitted(&self, target: &Target) -> bool {
        self.routing != Routing::Unsupported
            && !self.proxy_configured
            && target.host().is_some()
            && !target.account.is_empty()
            && !target.instance.is_empty()
            && self.choices.mode(&target.account, &target.origin) == Mode::Automatic
    }

    fn reached(&self, target: &Target) -> bool {
        self.choices.reached(&target.account, &target.origin)
    }

    /// Routing at creation: whether the instance view about to be created
    /// for `target` gets a route, so that a client must exist before it does.
    pub fn starts_at_creation(&self, target: &Target) -> bool {
        self.routing == Routing::AtCreation && self.permitted(target) && self.reached(target)
    }

    /// A client exists for `target`. `runtime` is the published instance view
    /// it was started for (`Effect::Open`), or `None` when its window is
    /// still to be created (`starts_at_creation`). The caller registers that
    /// window with `window_created`.
    pub fn open(&mut self, target: Target, material: Material, runtime: Option<&str>, now: Instant) -> u64 {
        self.next += 1;
        // Where a route is given only at creation, a session started for a
        // view that is already open can route nothing.
        let scout = self.routing == Routing::AtCreation && runtime.is_some();
        self.sessions.push(Session {
            id: self.next,
            target,
            material,
            runtime: runtime.map(str::to_owned),
            windows: runtime.map(str::to_owned).into_iter().collect(),
            routed: Vec::new(),
            ready: false,
            scout,
            opened: now,
        });
        self.next
    }

    /// The session of the published instance view of `account` at `origin`:
    /// the one an app window opened from that view shares.
    pub fn shared(&self, account: &str, origin: &str) -> Option<u64> {
        self.sessions
            .iter()
            .find(|session| {
                !session.scout
                    && session.runtime.is_some()
                    && session.target.account == account
                    && session.target.origin == origin
            })
            .map(|session| session.id)
    }

    /// A window that shows the session's instance now exists.
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
            Routing::Live if found.ready => {
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
        for session in &mut self.sessions {
            session.windows.retain(|window| open(window));
            session.routed.retain(|window| open(window));
        }
        let mut kept = Vec::new();
        for mut session in std::mem::take(&mut self.sessions) {
            // A scout has nothing left to learn once the instance answered.
            let allowed = self.permitted(&session.target) && (!session.scout || !self.reached(&session.target));
            let keep = match (&session.runtime, published) {
                (Some(runtime), Some(shown)) => allowed && shown.runtime == *runtime && shown.target == session.target,
                (Some(_), None) => false,
                (None, Some(shown)) if shown.target == session.target && session.windows.contains(&shown.runtime) => {
                    session.runtime = Some(shown.runtime.clone());
                    allowed
                }
                (None, _) => allowed && now.saturating_duration_since(session.opened) < UNPUBLISHED,
            };
            if keep {
                kept.push(session);
            } else {
                effects.push(Effect::Retire {
                    session: session.id,
                    detach: session.routed,
                });
            }
        }
        self.sessions = kept;
        if let Some(shown) = published {
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
            if wanted && !held && !cooling && self.permitted(&shown.target) {
                effects.push(Effect::Open {
                    runtime: shown.runtime.clone(),
                });
            }
        }
        effects
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
        ended
            .into_iter()
            .map(|session| Effect::Retire {
                session: session.id,
                detach: session.routed,
            })
            .collect()
    }

    /// The window a session asks its rendezvous through: its instance view,
    /// once that is published.
    pub fn asks_through(&self, session: u64) -> Option<&str> {
        self.sessions
            .iter()
            .find(|candidate| candidate.id == session)
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

    /// Whether any session exists, so the shell knows to keep looking for
    /// windows that closed.
    pub fn busy(&self) -> bool {
        !self.sessions.is_empty()
    }
}
