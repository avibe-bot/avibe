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
    defer, defer_kind, init, is_locked, protects_view, queue_if_locked, ready, show_lifecycle_busy, show_quit_question,
    sync_windows, EntryKind,
};

pub const MARKER: &str = if cfg!(target_os = "macos") {
    "if (window.self === window.top && location.protocol === 'http:' && ['127.0.0.1','[::1]'].includes(location.hostname) && !location.pathname.startsWith('/show/')) Object.defineProperty(window, '__AVIBE_DESKTOP_APP_LOCK__', {value:true});"
} else {
    ""
};

#[derive(Clone, Copy, Debug, Default, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub enum Policy {
    #[default]
    Off,
    Immediately,
    OneMinute,
    FiveMinutes,
}
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

/// The only clock input is continuous monotonic seconds (includes sleep).
/// No system time, user data or native authentication is accessed by this type.
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
}
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
        }
    }
    fn lock(&mut self) {
        if !self.locked {
            self.locked = true;
            self.auto_prompted = false;
            self.failed = false;
        }
    }
    pub fn leave(&mut self, now: f64) {
        if self.authenticating.is_none() && self.effective() {
            self.away_since.get_or_insert(now);
            self.tick(now);
        }
    }
    /// Sleep, screen lock and session switch are real departures even while
    /// an authentication panel happens to be open. Invalidate its late reply.
    pub fn system_leave(&mut self, now: f64) {
        self.epoch = self.epoch.wrapping_add(1);
        self.authenticating = None;
        self.leave(now);
    }
    pub fn block(&mut self, reason: u8, now: f64) {
        self.blocked |= reason;
        self.system_leave(now);
    }
    pub fn unblock(&mut self, reason: u8, active: bool, now: f64) {
        self.blocked &= !reason;
        self.tick(now);
        if active {
            self.arrive(now);
        }
    }
    pub fn tick(&mut self, now: f64) {
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
        if self.authenticating.is_none() && self.blocked == 0 {
            self.tick(now);
            self.away_since = None;
        }
    }
    pub fn should_prompt(&self) -> bool {
        self.blocked == 0 && self.locked && !self.auto_prompted && self.authenticating.is_none()
    }
    pub fn begin(&mut self, purpose: Authentication) -> Option<u64> {
        if self.blocked != 0 || self.authenticating.is_some() || !self.availability.available {
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
    /// A policy is committed only after its caller has persisted it. Failed
    /// writes are reported like failed verification, leaving the old policy.
    pub fn complete(&mut self, epoch: u64, outcome: Outcome, active: bool, now: f64) -> bool {
        if epoch != self.epoch {
            return false;
        }
        let Some(purpose) = self.authenticating.take() else {
            return false;
        };
        self.failed = outcome == Outcome::Failed;
        if outcome == Outcome::Success {
            if let Authentication::Change(policy) = purpose {
                self.policy = policy;
            }
            self.locked = false;
            self.away_since = None;
        }
        // LA's own focus bounce is ignored while authenticating. Only its
        // final real foreground state can start a new departure.
        if !active {
            self.leave(now);
        }
        true
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
mod tests {
    use super::*;
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
    fn only_effective_policies_lock_on_cold_start() {
        for policy in [Policy::Off, Policy::Immediately, Policy::OneMinute, Policy::FiveMinutes] {
            for enabled in [true, false] {
                let m = Machine::new(
                    policy,
                    Availability {
                        available: enabled,
                        ..available()
                    },
                );
                assert_eq!(m.locked, policy != Policy::Off && enabled);
            }
        }
    }
    #[test]
    fn every_departure_deadline_is_continuous_and_first_departure_wins() {
        for (policy, delay) in [
            (Policy::Immediately, 0.0),
            (Policy::OneMinute, 60.0),
            (Policy::FiveMinutes, 300.0),
        ] {
            let mut m = unlocked(policy);
            m.leave(10.0);
            if delay > 0.0 {
                m.leave(11.0);
                m.tick(10.0 + delay - 0.01);
                assert!(!m.locked);
            }
            m.tick(10.0 + delay);
            assert!(m.locked);
            m.arrive(10.0 + delay + 1000.0);
            assert!(m.locked);
        }
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
    fn only_successful_authentication_changes_settings() {
        for outcome in [
            Outcome::Success,
            Outcome::Cancelled,
            Outcome::Failed,
            Outcome::Unavailable,
        ] {
            let mut m = unlocked(Policy::FiveMinutes);
            let epoch = m.begin(Authentication::Change(Policy::Off)).unwrap();
            m.leave(0.0);
            m.arrive(1000.0);
            assert!(!m.locked);
            m.complete(epoch, outcome, true, 1000.0);
            assert_eq!(
                m.policy,
                if outcome == Outcome::Success {
                    Policy::Off
                } else {
                    Policy::FiveMinutes
                }
            );
        }
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
    #[test]
    fn system_departure_invalidates_an_inflight_success() {
        let mut m = unlocked(Policy::Immediately);
        let epoch = m.begin(Authentication::Change(Policy::Off)).unwrap();
        m.system_leave(10.0);
        assert!(!m.complete(epoch, Outcome::Success, true, 11.0));
        assert!(m.locked);
        assert_eq!(m.policy, Policy::Immediately);
    }
    #[test]
    fn missing_password_disables_but_preserves_the_preference() {
        let mut m = Machine::new(Policy::FiveMinutes, available());
        m.refresh(Availability {
            available: false,
            biometry: Biometry::None,
            biometry_state: BiometryState::Unavailable,
        });
        assert!(!m.locked);
        assert!(!m.effective());
        assert_eq!(m.policy, Policy::FiveMinutes);
        m.refresh(available());
        assert!(m.locked);
    }
    #[test]
    fn system_resume_only_ends_departure_when_the_app_is_really_in_front() {
        let mut m = unlocked(Policy::OneMinute);
        m.block(1, 10.0);
        m.block(8, 11.0);
        m.unblock(1, true, 20.0);
        assert!(m.begin(Authentication::Unlock).is_none());
        m.unblock(8, false, 30.0);
        m.tick(70.0);
        assert!(m.locked);
        m.arrive(71.0);
        assert!(m.should_prompt());
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
pub(crate) fn show_lifecycle_busy(_app: &tauri::AppHandle, _catalog: &crate::NativeTrayCatalog) -> bool {
    false
}

#[cfg(not(target_os = "macos"))]
pub(crate) enum EntryKind {
    Navigation,
    Content,
    Action,
}
#[cfg(not(target_os = "macos"))]
pub(crate) fn defer_kind(
    _app: &tauri::AppHandle,
    _kind: EntryKind,
    _action: impl FnOnce(&tauri::AppHandle) + Send + 'static,
) -> bool {
    false
}
