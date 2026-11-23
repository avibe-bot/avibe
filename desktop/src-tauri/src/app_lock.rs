//! Per-device App Lock. The state machine owns time and authentication; AppKit
//! owns presentation. There is no renderer-accessible unlock command.
use serde::{Deserialize, Serialize};

#[cfg(all(feature = "app-lock-e2e", not(debug_assertions)))]
compile_error!("app-lock-e2e authentication substitutes must never ship in release builds");

#[cfg(target_os = "macos")]
#[path = "app_lock/macos.rs"]
mod macos;
#[cfg(target_os = "macos")]
pub(crate) use macos::{
    defer, defer_kind, defer_with_replacement, init, is_locked, protects_view, queue_if_locked, ready,
    show_discard_question, show_lifecycle_busy, show_quit_question, sync_windows, voice_document_ready, voice_epoch,
};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum EntryKind {
    Navigation,
    #[cfg(any(target_os = "macos", test))]
    Content,
    Command,
    Action,
}

#[cfg(any(target_os = "macos", test))]
struct PendingEntry<T> {
    kind: EntryKind,
    action: T,
}

#[cfg(any(target_os = "macos", test))]
impl<T> PendingEntry<T> {
    /// Foreground-only requests add no work to an occupied slot. Content,
    /// navigation and explicit commands retain latest-wins semantics.
    fn offer(slot: &mut Option<Self>, kind: EntryKind, action: T) -> Result<Option<Self>, T> {
        if kind == EntryKind::Action && slot.is_some() {
            return Err(action);
        }
        Ok(slot.replace(Self { kind, action }))
    }
}

pub const MARKER: &str = if cfg!(target_os = "macos") {
    "if (window.self === window.top && location.protocol === 'http:' && ['127.0.0.1','[::1]'].includes(location.hostname) && !location.pathname.startsWith('/show/')) Object.defineProperty(window, '__AVIBE_DESKTOP_APP_LOCK__', {value:true});"
} else {
    ""
};

/// Read-only admission marker; native page-load sync supplies the current state.
pub const VOICE_MARKER: &str = if cfg!(target_os = "macos") {
    "if (window.self === window.top) window.__AVIBE_VOICE_LOCK__ = {revision:-1,locked:true};"
} else {
    ""
};

#[cfg(not(target_os = "macos"))]
pub(crate) fn voice_epoch(_app: &tauri::AppHandle) -> u64 {
    0
}
#[cfg(not(target_os = "macos"))]
pub(crate) fn voice_document_ready(_webview: &tauri::Webview) {}

#[derive(Clone, Copy, Debug, Default, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub enum Policy {
    #[default]
    Off,
    Immediately,
    OneMinute,
    FiveMinutes,
}
#[cfg(any(target_os = "macos", test))]
impl Policy {
    fn delay(self) -> Option<f64> {
        match self {
            Self::Off => None,
            Self::Immediately => Some(0.0),
            Self::OneMinute => Some(60.0),
            Self::FiveMinutes => Some(300.0),
        }
    }
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub enum Biometry {
    #[serde(rename = "touchID")]
    TouchId,
    #[serde(rename = "faceID")]
    FaceId,
    #[serde(rename = "opticID")]
    OpticId,
    None,
}
#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub enum BiometryState {
    Usable,
    Denied,
    NotEnrolled,
    Unavailable,
    Lockout,
}
#[derive(Clone, Copy, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct Availability {
    pub available: bool,
    pub biometry: Biometry,
    pub biometry_state: BiometryState,
}
#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct Status {
    pub policy: Policy,
    pub available: bool,
    pub biometry: Biometry,
    pub biometry_state: BiometryState,
}
#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub enum Outcome {
    Success,
    Cancelled,
    Failed,
    Unavailable,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Authentication {
    Unlock,
    Change(Policy),
}

#[cfg(any(target_os = "macos", test))]
const AUTH_RETURN_GRACE: f64 = 2.0;

#[cfg(any(target_os = "macos", test))]
#[derive(Clone, Copy, Debug)]
struct Completion {
    epoch: u64,
    purpose: Authentication,
    outcome: Outcome,
}

/// The shared capability contract: only a confirmed missing passcode disables
/// protection. Unknown/transient failures preserve the previous value, initially
/// failing closed. `None` is a successful capability query.
#[cfg(any(target_os = "macos", test))]
fn password_available(error: Option<isize>, previous: Option<bool>) -> bool {
    match error {
        None => true,
        Some(-5) => false,
        Some(_) => previous.unwrap_or(true),
    }
}

#[cfg(any(target_os = "macos", test))]
#[derive(Clone, Copy, Debug)]
struct ReturnWindow {
    ended_at: f64,
    reply: Option<Completion>,
}

/// The only clock input is continuous monotonic seconds (includes sleep).
/// A/C/V/W/Q/B/P transitions are specified in app-lock-desktop-event-table.md.
/// Native code supplies observations; it never owns a second timing rule.
#[cfg(any(target_os = "macos", test))]
pub struct Machine {
    pub policy: Policy,
    pub availability: Availability,
    pub locked: bool,
    pub authenticating: Option<Authentication>,
    pub failed: bool,
    away_since: Option<f64>,
    auto_prompted: bool,
    epoch: u64,
    blocked: u8,
    candidate_leave: Option<f64>,
    returning: Option<ReturnWindow>,
    completion: Option<Completion>,
}
#[cfg(any(target_os = "macos", test))]
impl Machine {
    pub fn new(policy: Policy, availability: Availability) -> Self {
        Self {
            policy,
            availability,
            locked: policy != Policy::Off && availability.available,
            authenticating: None,
            failed: false,
            away_since: None,
            auto_prompted: false,
            epoch: 0,
            blocked: 0,
            candidate_leave: None,
            returning: None,
            completion: None,
        }
    }
    pub fn effective(&self) -> bool {
        self.policy != Policy::Off && self.availability.available
    }
    pub fn status(&self) -> Status {
        Status {
            policy: self.policy,
            available: self.availability.available,
            biometry: self.availability.biometry,
            biometry_state: self.availability.biometry_state,
        }
    }
    pub fn refresh(&mut self, availability: Availability) {
        let was_effective = self.effective();
        self.availability = availability;
        if !self.effective() {
            self.locked = false;
            self.away_since = None;
        } else if !was_effective {
            self.lock();
            self.auto_prompted = false;
        }
    }
    fn lock(&mut self) {
        if !self.locked {
            self.locked = true;
            self.failed = false;
        }
        // Locking is not a visit. Only an actual return resets the prompt latch.
    }
    fn confirm_departure(&mut self, now: f64) {
        let since = self.candidate_leave.take().map_or(now, |candidate| candidate.min(now));
        if self.effective() {
            self.away_since = Some(self.away_since.map_or(since, |old| old.min(since)));
        }
    }
    pub fn leave(&mut self, now: f64) {
        if self.authenticating.is_some() || self.returning.is_some() {
            self.candidate_leave.get_or_insert(now);
        } else {
            // A resolved reply cannot authorize a write after a real departure.
            if self.completion.take().is_some() {
                self.epoch = self.epoch.wrapping_add(1);
            }
            self.confirm_departure(now);
        }
        self.tick(now);
    }
    /// E23/E40: positive evidence of departure closes the focus transition now.
    pub fn system_leave(&mut self, now: f64) {
        self.epoch = self.epoch.wrapping_add(1);
        self.authenticating = None;
        self.returning = None;
        self.completion = None;
        self.confirm_departure(now);
        self.tick(now);
    }
    /// E19: an internal sheet invalidates authentication, never creates absence.
    /// Retain a candidate until foreground or the original return deadline.
    fn cancel_for_lifecycle(&mut self, now: f64) {
        let had_attempt = self.authenticating.take().is_some() || self.returning.is_some() || self.completion.is_some();
        self.epoch = self.epoch.wrapping_add(1);
        self.completion = None;
        if let Some(window) = &mut self.returning {
            window.reply = None;
        } else if self.candidate_leave.is_some() {
            self.returning = Some(ReturnWindow {
                ended_at: now,
                reply: None,
            });
        }
        if had_attempt {
            self.auto_prompted = true;
        }
        self.tick(now);
    }
    pub fn block(&mut self, reason: u8, now: f64) {
        self.blocked |= reason;
        self.system_leave(now);
    }
    /// E39: only the app-activation coordinator supplies these public facts.
    pub fn reconcile(&mut self, awake_on_console: bool) {
        if awake_on_console {
            self.blocked = 0;
        }
    }
    pub fn needs_tick(&self) -> bool {
        self.returning.is_some() || (self.effective() && self.away_since.is_some()) || self.completion.is_some()
    }
    pub fn tick(&mut self, now: f64) {
        if self
            .returning
            .is_some_and(|window| now - window.ended_at >= AUTH_RETURN_GRACE)
        {
            let window = self.returning.take().expect("expired return window");
            if self.candidate_leave.is_some() {
                self.confirm_departure(now);
            }
            self.completion = window.reply.map(|mut reply| {
                reply.outcome = match reply.purpose {
                    Authentication::Change(_) => Outcome::Failed,
                    Authentication::Unlock => Outcome::Cancelled,
                };
                reply
            });
        }
        if self.effective()
            && self
                .away_since
                .zip(self.policy.delay())
                .is_some_and(|(since, delay)| now - since >= delay)
        {
            self.lock();
        }
    }
    pub fn arrive(&mut self, now: f64) {
        // Expiry and Due precede foreground consumption, even with delayed ticks.
        self.tick(now);
        if self.away_since.take().is_some() {
            self.auto_prompted =
                self.authenticating.is_some() || self.returning.is_some_and(|window| window.reply.is_some());
        }
        self.candidate_leave = None;
        if let Some(window) = self.returning.take() {
            self.completion = window.reply;
        }
        // B is reconciled separately; isActive at reply time is not that proof.
    }
    pub fn should_prompt(&self) -> bool {
        self.blocked == 0 && self.locked && !self.auto_prompted && !self.busy()
    }
    fn busy(&self) -> bool {
        self.authenticating.is_some() || self.returning.is_some() || self.completion.is_some()
    }
    /// Callers gate automatic attempts with should_prompt. Explicit Unlock may
    /// always recover stale suppression hints through a fresh successful LA.
    pub fn begin(&mut self, purpose: Authentication) -> Option<u64> {
        if self.busy() || !self.availability.available {
            return None;
        }
        if purpose == Authentication::Unlock && !self.locked {
            return None;
        }
        self.epoch = self.epoch.wrapping_add(1);
        self.authenticating = Some(purpose);
        self.auto_prompted = true;
        self.failed = false;
        Some(self.epoch)
    }
    pub fn reply(&mut self, epoch: u64, outcome: Outcome, active: bool, replied_at: f64, now: f64) {
        if epoch != self.epoch {
            return;
        }
        let Some(purpose) = self.authenticating.take() else {
            return;
        };
        self.returning = Some(ReturnWindow {
            ended_at: replied_at,
            reply: Some(Completion {
                epoch,
                purpose,
                outcome,
            }),
        });
        // The reply timestamp establishes TTL only; it is never a departure.
        self.tick(now);
        if active {
            self.arrive(now);
        }
    }
    fn take_completion(&mut self) -> Option<Completion> {
        self.completion.take()
    }
    fn commit(&mut self, completion: Completion, outcome: Outcome) -> bool {
        if completion.epoch != self.epoch {
            return false;
        }
        self.failed = outcome == Outcome::Failed;
        if outcome == Outcome::Success {
            if let Authentication::Change(policy) = completion.purpose {
                self.policy = policy;
            }
            self.locked = false;
            self.away_since = None;
            self.blocked = 0;
            self.auto_prompted = true;
        }
        // A failed completion drained after real arrival must not erase that
        // arrival's Fresh latch; any next attempt is a new LA, never old Success.
        true
    }
    #[cfg(test)]
    fn complete(&mut self, epoch: u64, outcome: Outcome, active: bool, now: f64) -> bool {
        self.reply(epoch, outcome, active, now, now);
        let Some(completion) = self.take_completion() else {
            return false;
        };
        self.commit(completion, completion.outcome)
    }
}

#[cfg(not(target_os = "macos"))]
pub(crate) fn defer(_app: &tauri::AppHandle, _action: impl FnOnce(&tauri::AppHandle) + Send + 'static) -> bool {
    false
}
#[cfg(not(target_os = "macos"))]
pub(crate) fn is_locked(_app: &tauri::AppHandle) -> bool {
    false
}
#[cfg(not(target_os = "macos"))]
pub(crate) fn sync_windows(_app: &tauri::AppHandle) {}

#[cfg(test)]
#[path = "app_lock/event_tests.rs"]
mod event_tests;

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn e28_e30_pending_actions_only_fill_empty_slots() {
        use EntryKind::{Action, Command, Content, Navigation};
        // Consume the surviving payload for every previous/incoming pairing,
        // including Action -> Action, not just the kind used for lock copy.
        for (previous, expected) in [
            (None, ["new", "new", "new", "new"]),
            (Some(Action), ["old", "new", "new", "new"]),
            (Some(Content), ["old", "new", "new", "new"]),
            (Some(Navigation), ["old", "new", "new", "new"]),
            (Some(Command), ["old", "new", "new", "new"]),
        ] {
            for (incoming, expected_payload) in [Action, Content, Navigation, Command].into_iter().zip(expected) {
                let mut slot = previous.map(|kind| PendingEntry { kind, action: "old" });
                let result = PendingEntry::offer(&mut slot, incoming, "new");
                let accepted = result.is_ok();
                assert_eq!(accepted, expected_payload == "new", "{previous:?} -> {incoming:?}");
                match result {
                    Ok(replaced) => assert_eq!(replaced.map(|entry| entry.action), previous.map(|_| "old")),
                    Err(rejected) => assert_eq!(rejected, "new"),
                }
                let delivered = slot.take().unwrap();
                assert_eq!(delivered.kind, if accepted { incoming } else { previous.unwrap() });
                assert_eq!(delivered.action, expected_payload);
                assert!(slot.is_none());
            }
        }
    }
    fn available() -> Availability {
        Availability {
            available: true,
            biometry: Biometry::TouchId,
            biometry_state: BiometryState::Usable,
        }
    }
    fn unlocked(policy: Policy) -> Machine {
        let mut m = Machine::new(policy, available());
        if m.locked {
            let epoch = m.begin(Authentication::Unlock).unwrap();
            m.complete(epoch, Outcome::Success, true, 0.0);
        }
        m
    }
    #[test]
    fn short_departures_reset_the_clock_on_return() {
        let mut m = unlocked(Policy::OneMinute);
        m.leave(0.0);
        m.arrive(59.0);
        m.tick(61.0);
        assert!(!m.locked);
        m.leave(100.0);
        m.arrive(161.0);
        assert!(m.locked);
    }
    #[test]
    fn cancelling_never_reprompts_without_an_explicit_action() {
        let mut m = Machine::new(Policy::Immediately, available());
        assert!(m.should_prompt());
        let epoch = m.begin(Authentication::Unlock).unwrap();
        assert!(m.begin(Authentication::Unlock).is_none());
        m.leave(1.0);
        m.arrive(2.0);
        m.complete(epoch, Outcome::Cancelled, true, 3.0);
        for now in [4.0, 100.0, 1000.0] {
            m.arrive(now);
            assert!(m.locked);
            assert!(!m.should_prompt());
        }
        let epoch = m.begin(Authentication::Unlock).unwrap();
        m.complete(epoch, Outcome::Success, true, 1001.0);
        assert!(!m.locked);
    }
    fn finish(m: &mut Machine) -> Outcome {
        let completion = m.take_completion().expect("settled result");
        assert!(m.commit(completion, completion.outcome));
        completion.outcome
    }
    #[test]
    fn bounded_return_accepts_both_focus_orderings_without_relocking() {
        for purpose in [Authentication::Unlock, Authentication::Change(Policy::Immediately)] {
            for reply_first in [false, true] {
                let mut m = if purpose == Authentication::Unlock {
                    Machine::new(Policy::Immediately, available())
                } else {
                    unlocked(Policy::Off)
                };
                let epoch = m.begin(purpose).unwrap();
                m.leave(10.0);
                if reply_first {
                    m.reply(epoch, Outcome::Success, false, 11.0, 11.0);
                    assert!(m.take_completion().is_none());
                    m.arrive(12.99);
                } else {
                    m.arrive(11.0);
                    m.reply(epoch, Outcome::Success, true, 12.0, 12.0);
                }
                assert_eq!(finish(&mut m), Outcome::Success);
                m.tick(100.0);
                assert!(!m.locked);
                assert_eq!(m.policy, Policy::Immediately);
                m.leave(101.0);
                assert!(m.locked);
            }
        }
    }
    #[test]
    fn dispatch_delay_cannot_renew_a_successful_reply() {
        for purpose in [Authentication::Unlock, Authentication::Change(Policy::Off)] {
            let mut m = Machine::new(Policy::Immediately, available());
            let epoch = m.begin(purpose).unwrap();
            m.leave(1.0);
            m.arrive(2.0);
            m.reply(epoch, Outcome::Success, true, 2.0, 60.0);
            assert_ne!(finish(&mut m), Outcome::Success);
            assert!(m.locked);
            assert_eq!(m.policy, Policy::Immediately);
        }
    }
    #[test]
    fn native_copy_is_present_in_both_shared_catalogs() {
        for catalog in [crate::EN_PRODUCT_CATALOG, crate::ZH_PRODUCT_CATALOG] {
            let values: serde_json::Value = serde_json::from_str(catalog).unwrap();
            for key in [
                "lockedTitle",
                "unlockTouchID",
                "unlockPassword",
                "verifying",
                "verificationFailed",
                "macNotEnrolled",
                "macLockout",
                "macUnavailable",
                "restoreAllWindows",
                "pendingContent",
                "changeReason",
                "unlockReason",
            ] {
                assert!(
                    values["appLock"]["lock"][key].as_str().is_some_and(|s| !s.is_empty()),
                    "missing {key}"
                );
            }
        }
    }
}

#[cfg(not(target_os = "macos"))]
#[tauri::command]
pub(crate) fn app_lock_status() -> Result<Status, String> {
    Err("App Lock is unavailable on this platform.".into())
}
#[cfg(not(target_os = "macos"))]
#[tauri::command]
pub(crate) fn app_lock_set_policy(_policy: Policy) -> Result<serde_json::Value, String> {
    Err("App Lock is unavailable on this platform.".into())
}

#[cfg(target_os = "macos")]
#[tauri::command]
pub(crate) async fn app_lock_status(window: tauri::Webview, app: tauri::AppHandle) -> Result<Status, String> {
    macos::app_lock_status(window, app).await
}
#[cfg(target_os = "macos")]
#[tauri::command]
pub(crate) async fn app_lock_set_policy(
    window: tauri::Webview,
    app: tauri::AppHandle,
    policy: Policy,
) -> Result<serde_json::Value, String> {
    macos::app_lock_set_policy(window, app, policy).await
}

#[cfg(not(target_os = "macos"))]
pub(crate) fn ready(_app: &tauri::AppHandle) {}
#[cfg(not(target_os = "macos"))]
pub(crate) fn queue_if_locked(
    _app: &tauri::AppHandle,
    _action: impl FnOnce(&tauri::AppHandle) + Send + 'static,
) -> bool {
    false
}

#[cfg(all(target_os = "macos", feature = "app-lock-e2e"))]
pub fn run_e2e() {
    macos::e2e::run();
}

#[cfg(not(target_os = "macos"))]
pub(crate) fn show_quit_question(
    _app: &tauri::AppHandle,
    _catalog: &crate::NativeTrayCatalog,
    _callback: impl FnOnce(tauri_plugin_dialog::MessageDialogResult) + Send + 'static,
) -> bool {
    false
}

#[cfg(not(target_os = "macos"))]
pub(crate) fn show_discard_question(
    _app: &tauri::AppHandle,
    _parent: &tauri::WebviewWindow,
    _callback: impl FnOnce(bool) + Send + 'static,
) -> bool {
    false
}

#[cfg(not(target_os = "macos"))]
pub(crate) fn show_lifecycle_busy(_app: &tauri::AppHandle, _catalog: &crate::NativeTrayCatalog) -> bool {
    false
}

#[cfg(not(target_os = "macos"))]
pub(crate) fn defer_kind(
    _app: &tauri::AppHandle,
    _kind: EntryKind,
    _action: impl FnOnce(&tauri::AppHandle) + Send + 'static,
) -> bool {
    false
}
