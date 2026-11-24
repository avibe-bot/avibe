//! The registrations of one session's routed windows.
//!
//! Every routed window of a session (its instance view and the app windows
//! opened from it) presents a credential of its own to the session's
//! listener, so the listener knows which window a connection belongs to. A
//! page's hand-over then closes its own window's quiet connections and no
//! other's.
//!
//! A registration is one window's, for that window's life. It is revoked
//! when the window is no longer routed (it closed), when a new window takes
//! its label, and when the session ends. What was started with a revoked
//! registration (a hand-over still waiting, a late teardown) does nothing:
//! the core holds it to the registration it was given, not to the label.
use super::{Handover, WindowRegistration};
use std::collections::HashMap;

pub struct Registered {
    handover: Handover,
    windows: HashMap<String, WindowRegistration>,
}

impl Registered {
    pub fn new(handover: Handover) -> Self {
        Self {
            handover,
            windows: HashMap::new(),
        }
    }

    /// A window with this label has just been routed. It is a new window,
    /// whatever was known under its label: an app window's label is used
    /// again once its window has closed, and what that window had is revoked
    /// before this one gets its own. `None` when the listener has ended or
    /// will take no more windows: the window then has no route.
    pub fn register(&mut self, window: &str) -> Option<WindowRegistration> {
        if let Some(earlier) = self.windows.remove(window) {
            self.handover.revoke_window(&earlier);
        }
        let registration = self.handover.window_credential(window)?;
        self.windows.insert(window.to_owned(), registration.clone());
        Some(registration)
    }

    /// The registration of the window that has this label now.
    pub fn of(&self, window: &str) -> Option<&WindowRegistration> {
        self.windows.get(window)
    }

    /// Revokes the registration of every window that is no longer `routed`:
    /// its connections are closed, and its credential opens no new one.
    pub fn keep(&mut self, routed: impl Fn(&str) -> bool) {
        let handover = &self.handover;
        self.windows.retain(|window, registration| {
            let kept = routed(window);
            if !kept {
                handover.revoke_window(registration);
            }
            kept
        });
    }

    /// How many tunnel connections the listener carries for these windows
    /// now, busy or quiet.
    pub fn public_legs(&self) -> usize {
        self.windows
            .values()
            .filter_map(|registration| self.handover.window_census(registration))
            .map(|census| census.quiet + census.busy)
            .sum()
    }

    /// The session is ending: every registration is revoked.
    pub fn revoke_all(&mut self) {
        self.keep(|_| false);
    }

    /// What a page's hand-over needs: the session's handle and the page's own
    /// window's registration, as it is when the page asks. Both are cloned
    /// out, so nothing is held while the hand-over waits, and a window that
    /// takes the label meanwhile is not the one it closes for.
    pub fn handing_over(&self, window: &str) -> Option<(Handover, WindowRegistration)> {
        Some((self.handover.clone(), self.windows.get(window)?.clone()))
    }
}
