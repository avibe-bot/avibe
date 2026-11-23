//! Brings the app back after a step it handed to the system browser.
//!
//! Where the Desktop App cannot run a Protected Vault passkey step itself, the
//! Workbench opens that step in the user's browser. The Workbench learns when
//! the step has finished there and then asks for its own window to come
//! forward. The decision of *which* steps qualify stays in the page
//! (`ui/src/lib/vaultHandoffReturn.ts`); this command only shows and focuses the
//! main window. It takes no arguments, does nothing while the window is already
//! in front, and is granted only to the main window's loopback Workbench pages
//! (`capabilities/main-window-focus.json` and the `is_workbench` check below).
//! A same-origin Show Page inside the Workbench can still reach it through its
//! parent, as it can the `window-drag` commands. The shell therefore does not
//! rest "once per finished step" on page code: it activates the window at most
//! once per [`MIN_INTERVAL`], however often it is asked.
//!
//! The platforms may refuse to activate an app from the background (macOS 14+
//! treats activation as a request; Windows has foreground-lock rules). When the
//! window is still not in front shortly afterwards, the shell falls back to the
//! platform's attention request: one Dock bounce on macOS, a taskbar flash on
//! Windows.

use std::sync::Mutex;
use std::time::{Duration, Instant};

use tauri::{UserAttentionType, Webview};

/// Tells the Workbench that this shell answers `focus_main_window`. Older
/// shells lack it, so the page never calls a command they do not have.
/// Top-level document only, like every other shell marker.
pub(crate) const DESKTOP_FOCUS_MARKER: &str =
    "if (window.self === window.top) Object.defineProperty(window, '__AVIBE_DESKTOP_FOCUS__', { value: true });";

/// Activations closer together than this are dropped. A user completes a
/// handed-off step in the browser at most every so often; a page asking more
/// often is misbehaving, and the shell, not the page, is what holds that line.
const MIN_INTERVAL: Duration = Duration::from_secs(60);

/// How long the platform gets to complete an activation before the shell asks
/// for attention instead.
const ACTIVATION_SETTLE: Duration = Duration::from_millis(700);

static LAST_ACTIVATION: Mutex<Option<Instant>> = Mutex::new(None);

#[derive(Debug, PartialEq, Eq)]
enum Plan {
    /// The user is already in the app: nothing to do.
    AlreadyInFront,
    /// An activation was just made; this call is dropped.
    TooSoon,
    Activate,
}

fn plan(focused: bool, last: Option<Instant>, now: Instant) -> Plan {
    if focused {
        Plan::AlreadyInFront
    } else if last.is_some_and(|last| now.saturating_duration_since(last) < MIN_INTERVAL) {
        Plan::TooSoon
    } else {
        Plan::Activate
    }
}

#[tauri::command]
pub fn focus_main_window(webview: Webview) -> Result<(), String> {
    if !crate::pet::is_workbench(&webview) {
        return Err("This command is only available to the Avibe Workbench.".to_owned());
    }
    let window = webview.window();
    let focused = window.is_focused().unwrap_or(false);
    {
        let mut last = LAST_ACTIVATION
            .lock()
            .map_err(|_| "activation state unavailable".to_owned())?;
        let now = Instant::now();
        if plan(focused, *last, now) != Plan::Activate {
            return Ok(());
        }
        *last = Some(now);
    }
    let _ = window.unminimize();
    let _ = window.show();
    let _ = window.set_focus();
    std::thread::spawn(move || {
        std::thread::sleep(ACTIVATION_SETTLE);
        if !window.is_focused().unwrap_or(true) {
            let _ = window.request_user_attention(Some(UserAttentionType::Informational));
        }
    });
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_window_already_in_front_is_left_alone() {
        let now = Instant::now();
        assert_eq!(plan(true, None, now), Plan::AlreadyInFront);
        assert_eq!(
            plan(true, Some(now - Duration::from_secs(60)), now),
            Plan::AlreadyInFront
        );
    }

    #[test]
    fn a_background_window_is_activated_once_per_interval() {
        let now = Instant::now();
        assert_eq!(plan(false, None, now), Plan::Activate);
        assert_eq!(plan(false, Some(now - Duration::from_secs(1)), now), Plan::TooSoon);
        assert_eq!(plan(false, Some(now - Duration::from_secs(59)), now), Plan::TooSoon);
        assert_eq!(plan(false, Some(now - MIN_INTERVAL), now), Plan::Activate);
    }
}
