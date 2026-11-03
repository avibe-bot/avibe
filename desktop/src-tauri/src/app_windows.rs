//! Native windows for standalone Avibe app pages.
//!
//! The Workbench sends a structured target across the bridge. This module owns
//! the allowlist, URL construction, deterministic labels, and native window
//! lifecycle so the page can never choose an arbitrary destination.

use avibe_runtime_host::pet::valid_session_id;
use avibe_runtime_host::LoopbackOrigin;
use tauri::{AppHandle, Manager, WebviewUrl, WebviewWindow, WebviewWindowBuilder};
use url::Url;

// Serialize bridge requests so rapid clicks cannot race the label lookup/build.
pub(crate) static LAUNCH_LOCK: tokio::sync::Mutex<()> = tokio::sync::Mutex::const_new(());

const WINDOW_PREFIX: &str = "avibe-app-";
const NEW_WINDOW_MARKER: &str = "--new-";

pub(crate) fn is_app_window(label: &str) -> bool {
    label.starts_with(WINDOW_PREFIX)
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) enum AppWindowTarget {
    Builtin(&'static str),
    ShowPage(String),
}

impl AppWindowTarget {
    fn base_label(&self) -> String {
        match self {
            Self::Builtin(app_id) => format!("{WINDOW_PREFIX}{app_id}"),
            Self::ShowPage(session_id) => format!("{WINDOW_PREFIX}show-{}", hex_encode(session_id.as_bytes())),
        }
    }
}

/// Parses the only targets the native-window command is allowed to open.
pub(crate) fn parse_target(app_id: &str, session_id: Option<&str>) -> Result<AppWindowTarget, String> {
    match app_id {
        "files" if session_id.is_none() => Ok(AppWindowTarget::Builtin("files")),
        "terminal" if session_id.is_none() => Ok(AppWindowTarget::Builtin("terminal")),
        "editor" if session_id.is_none() => Ok(AppWindowTarget::Builtin("editor")),
        "files" | "terminal" | "editor" => Err("Built-in app windows do not accept a session id.".to_owned()),
        "showpage" => match session_id {
            Some(session_id) if valid_session_id(session_id) => Ok(AppWindowTarget::ShowPage(session_id.to_owned())),
            Some(_) => Err("The Show Page session id is invalid.".to_owned()),
            None => Err("A Show Page session id is required.".to_owned()),
        },
        _ => Err("This app does not have a standalone native window.".to_owned()),
    }
}

/// Constructs a same-origin standalone page from a validated target.
pub(crate) fn target_url(origin: &LoopbackOrigin, target: &AppWindowTarget) -> Url {
    let mut url = origin.navigation_url();
    match target {
        AppWindowTarget::Builtin(app_id) => {
            url.set_path(&format!("/apps/{app_id}"));
            url.set_query(Some(if *app_id == "terminal" {
                "standalone=1&ephemeral=1"
            } else {
                "standalone=1"
            }));
        }
        AppWindowTarget::ShowPage(session_id) => {
            url.set_path(&format!("/show/{session_id}/"));
            url.set_query(None);
        }
    }
    url
}

pub(crate) fn open(
    app: &AppHandle,
    origin: &LoopbackOrigin,
    target: &AppWindowTarget,
    title: Option<&str>,
    new_window: bool,
) -> Result<(), String> {
    let base_label = target.base_label();
    if !new_window {
        if let Some(window) = find_target_window(app, &base_label) {
            return focus(&window);
        }
    }

    let label = if new_window {
        next_label(app, &base_label)
    } else {
        base_label
    };
    let title = title.filter(|title| !title.trim().is_empty()).unwrap_or("Avibe");
    let url = target_url(origin, target);
    let window = WebviewWindowBuilder::new(app, &label, WebviewUrl::External(url))
        .title(title)
        .inner_size(default_size(target).0, default_size(target).1)
        .min_inner_size(default_size(target).0.min(720.0), default_size(target).1.min(480.0))
        .center()
        .resizable(true)
        .decorations(true)
        .disable_drag_drop_handler()
        .visible(false)
        .focused(false)
        .on_new_window(|url, _features| crate::handle_new_window_request(url))
        .build()
        .map_err(|error| format!("Failed to create the native app window: {error}"))?;
    // Origin changes can run while the OS builds a webview. Do not leave a
    // window behind if close_all ran before this build registered its label.
    let current = app
        .state::<crate::Shell>()
        .active_origin
        .lock()
        .ok()
        .and_then(|active| active.clone());
    if current.as_ref() != Some(origin) {
        let _ = window.destroy();
        return Err("The desktop Runtime changed while the app window was opening.".to_owned());
    }
    focus(&window)
}

pub(crate) fn close_all(app: &AppHandle) {
    let windows: Vec<WebviewWindow> = app
        .webview_windows()
        .into_iter()
        .filter_map(|(label, window)| is_app_window(&label).then_some(window))
        .collect();
    for window in windows {
        let _ = window.destroy();
    }
}

fn find_target_window(app: &AppHandle, base_label: &str) -> Option<WebviewWindow> {
    let mut labels: Vec<String> = app
        .webview_windows()
        .into_keys()
        .filter(|label| is_target_label(label, base_label))
        .collect();
    labels.sort_by_key(|label| (label != base_label, label.clone()));
    labels.into_iter().find_map(|label| app.get_webview_window(&label))
}

fn next_label(app: &AppHandle, base_label: &str) -> String {
    let mut index = 1;
    loop {
        let label = format!("{base_label}{NEW_WINDOW_MARKER}{index}");
        if app.get_webview_window(&label).is_none() {
            return label;
        }
        index += 1;
    }
}

fn is_target_label(label: &str, base_label: &str) -> bool {
    if label == base_label {
        return true;
    }
    label
        .strip_prefix(base_label)
        .and_then(|suffix| suffix.strip_prefix(NEW_WINDOW_MARKER))
        .is_some_and(|suffix| !suffix.is_empty() && suffix.bytes().all(|byte| byte.is_ascii_digit()))
}

fn focus(window: &WebviewWindow) -> Result<(), String> {
    window
        .unminimize()
        .and_then(|_| window.show())
        .and_then(|_| window.set_focus())
        .map_err(|error| format!("Failed to focus the native app window: {error}"))
}

fn default_size(target: &AppWindowTarget) -> (f64, f64) {
    match target {
        AppWindowTarget::Builtin("files") => (920.0, 600.0),
        AppWindowTarget::Builtin("terminal") => (820.0, 540.0),
        AppWindowTarget::Builtin("editor") => (1000.0, 640.0),
        AppWindowTarget::ShowPage(_) => (1040.0, 720.0),
        AppWindowTarget::Builtin(_) => (920.0, 600.0),
    }
}

fn hex_encode(bytes: &[u8]) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut encoded = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        encoded.push(HEX[(byte >> 4) as usize] as char);
        encoded.push(HEX[(byte & 0x0f) as usize] as char);
    }
    encoded
}

#[cfg(test)]
mod tests {
    use super::*;

    fn origin() -> LoopbackOrigin {
        LoopbackOrigin::parse("http://127.0.0.1:5123").unwrap()
    }

    #[test]
    fn only_standalone_targets_are_accepted() {
        assert_eq!(parse_target("files", None), Ok(AppWindowTarget::Builtin("files")));
        assert_eq!(parse_target("terminal", None), Ok(AppWindowTarget::Builtin("terminal")));
        assert_eq!(parse_target("editor", None), Ok(AppWindowTarget::Builtin("editor")));
        assert_eq!(
            parse_target("showpage", Some("session-1")),
            Ok(AppWindowTarget::ShowPage("session-1".into()))
        );
        assert!(parse_target("library", None).is_err());
        assert!(parse_target("preview", None).is_err());
        assert!(parse_target("showpage", None).is_err());
        assert!(parse_target("files", Some("session-1")).is_err());
    }

    #[test]
    fn session_validation_rejects_path_injection() {
        assert!(parse_target("showpage", Some("session/other")).is_err());
        assert!(parse_target("showpage", Some("session?query")).is_err());
        assert!(parse_target("showpage", Some("session-1")).is_ok());
    }

    #[test]
    fn urls_are_built_from_the_validated_origin() {
        let origin = origin();
        assert_eq!(
            target_url(&origin, &AppWindowTarget::Builtin("files")).as_str(),
            "http://127.0.0.1:5123/apps/files?standalone=1"
        );
        assert_eq!(
            target_url(&origin, &AppWindowTarget::ShowPage("session-1".into())).as_str(),
            "http://127.0.0.1:5123/show/session-1/"
        );
        assert_eq!(
            target_url(&origin, &AppWindowTarget::Builtin("terminal")).as_str(),
            "http://127.0.0.1:5123/apps/terminal?standalone=1&ephemeral=1"
        );
    }

    #[test]
    fn origin_and_exit_cleanup_selects_all_app_windows_and_never_main_or_pet() {
        let mut labels = vec![crate::MAIN_WINDOW.to_owned(), crate::pet::PET_WINDOW.to_owned()];
        let mut expected = Vec::new();
        for target in [
            AppWindowTarget::Builtin("files"),
            AppWindowTarget::Builtin("terminal"),
            AppWindowTarget::Builtin("editor"),
            AppWindowTarget::ShowPage("session-1".into()),
        ] {
            let label = target.base_label();
            expected.extend([label.clone(), format!("{label}{NEW_WINDOW_MARKER}1")]);
        }
        labels.extend(expected.clone());
        labels.extend([
            "settings".to_owned(),
            "avibe-app".to_owned(),
            "other-app-files".to_owned(),
        ]);
        let selected: Vec<_> = labels.into_iter().filter(|label| is_app_window(label)).collect();
        assert_eq!(selected, expected);
    }

    #[test]
    fn labels_are_collision_safe_and_new_windows_are_distinct() {
        let first = AppWindowTarget::ShowPage("foo".into()).base_label();
        let second = AppWindowTarget::ShowPage("foo-1".into()).base_label();
        assert_ne!(first, second);
        assert!(is_target_label("avibe-app-files", "avibe-app-files"));
        assert!(is_target_label("avibe-app-files--new-2", "avibe-app-files"));
        assert!(!is_target_label("avibe-app-files--new-x", "avibe-app-files"));
        assert!(!is_target_label("avibe-app-files-1", "avibe-app-files"));
    }
}
