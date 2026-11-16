//! This computer's update panel as a popup while the main window shows another
//! computer (ui/design: chrome/bottom-row/remote-popup). Everywhere else the
//! version opens a popover; here the shell draws the same panel in its own small
//! window over the main one, from the bundled account page. The remote page only
//! asks for it, so it never reaches this computer's installer.
use avibe_runtime_host::instance_client::protocol::ClientAction;
use tauri::webview::NewWindowResponse;
use tauri::{AppHandle, Manager, PhysicalPosition, WebviewUrl, WebviewWindow, WebviewWindowBuilder, WindowEvent};
use url::Url;

pub(crate) const UPDATE_POPOVER: &str = "update-popover";
const VIEW: &str = "update-popover";
/// The panel's 320 px plus room for its shadow. The page draws the panel at its
/// bottom-left; the rest is a transparent backdrop that closes the popup.
const WIDTH: f64 = 336.0;
const HEIGHT: f64 = 520.0;
/// The window's bottom-left corner, from the main window's bottom-left: aligned
/// with the sidebar's padding, just above its bottom row.
const LEFT: f64 = 8.0;
const BOTTOM: f64 = 40.0;

/// The one page this window may show.
pub(crate) fn page_url(bundle: &Url) -> Option<Url> {
    let mut url = bundle.join("/account/index.html").ok()?;
    url.set_query(Some(&format!("view={VIEW}")));
    Some(url)
}

pub(crate) fn is_page(window: &WebviewWindow, bundle: &Url) -> bool {
    window.label() == UPDATE_POPOVER && window.url().ok().is_some_and(|url| Some(url) == page_url(bundle))
}

/// What the popup may ask of the desktop client: its state and this computer's
/// updater, nothing about accounts or instances.
pub(crate) fn allows(action: &ClientAction) -> bool {
    matches!(action, ClientAction::GetState) || action.is_desktop_update()
}

pub(crate) fn navigation_allowed(url: &Url, bundle: &Url) -> bool {
    Some(url.clone()) == page_url(bundle)
}

/// Show the popup over the main window, or bring an open one forward.
pub(crate) fn open(app: &AppHandle, bundle: &Url, initialization: String) -> bool {
    if let Some(window) = app.get_webview_window(UPDATE_POPOVER) {
        return window.set_focus().is_ok();
    }
    let Some(main) = app.get_webview_window(crate::MAIN_WINDOW) else {
        return false;
    };
    let Some(url) = page_url(bundle) else {
        return false;
    };
    let (Ok(origin), Ok(size), Ok(scale)) = (main.inner_position(), main.inner_size(), main.scale_factor()) else {
        return false;
    };
    let builder = WebviewWindowBuilder::new(app, UPDATE_POPOVER, WebviewUrl::External(url))
        .initialization_script(initialization)
        .on_new_window(|_, _| NewWindowResponse::Deny)
        .title("Avibe")
        .inner_size(WIDTH, HEIGHT)
        .transparent(true)
        .decorations(false)
        .shadow(false)
        .resizable(false)
        .maximizable(false)
        .minimizable(false)
        .skip_taskbar(true)
        .visible(false)
        .focused(true);
    let Ok(builder) = builder.parent(&main) else {
        return false;
    };
    #[cfg(target_os = "macos")]
    let builder = builder.accept_first_mouse(true);
    let Ok(window) = builder.build() else {
        return false;
    };
    // Place it in physical pixels from the main window's content area, the same
    // on every platform; then show it, focused, as a click on the version does.
    let x = origin.x as f64 + LEFT * scale;
    let y = origin.y as f64 + size.height as f64 - (BOTTOM + HEIGHT) * scale;
    let _ = window.set_position(PhysicalPosition::new(x.round() as i32, y.round() as i32));
    let closing = window.clone();
    window.on_window_event(move |event| {
        // A click anywhere outside the popup moves focus away: it closes, like a popover.
        if let WindowEvent::Focused(false) = event {
            let _ = closing.close();
        }
    });
    window.show().is_ok() && window.set_focus().is_ok()
}

/// Deliver a state change to an open popup that still shows its page.
pub(crate) fn publish(app: &AppHandle, bundle: &Url, script: impl FnOnce(&Url) -> String) {
    let Some(window) = app.get_webview_window(UPDATE_POPOVER) else {
        return;
    };
    if let Ok(url) = window.url() {
        if is_page(&window, bundle) {
            let _ = window.eval(script(&url));
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_popup_shows_only_its_own_bundled_page() {
        let bundle = Url::parse("tauri://localhost/").unwrap();
        let page = page_url(&bundle).unwrap();
        assert_eq!(
            page.as_str(),
            "tauri://localhost/account/index.html?view=update-popover"
        );
        assert!(navigation_allowed(&page, &bundle));
        for other in [
            "tauri://localhost/account/index.html?view=updates",
            "tauri://localhost/account/index.html?view=account",
            "tauri://localhost/account/index.html?view=update-popover&x=1",
            "https://studio.avibe.bot/account/index.html?view=update-popover",
        ] {
            assert!(!navigation_allowed(&Url::parse(other).unwrap(), &bundle), "{other}");
        }
    }

    #[test]
    fn the_popup_asks_only_for_state_and_this_computers_updater() {
        for action in [
            ClientAction::GetState,
            ClientAction::UpdateCheck {},
            ClientAction::UpdateSetPrereleases { enabled: true },
            ClientAction::UpdateSetAutoUpdate { enabled: false },
            ClientAction::UpdateInstall {
                target_version: "3.2.8".into(),
            },
        ] {
            assert!(allows(&action), "{action:?}");
        }
        for action in [
            ClientAction::SignOut,
            ClientAction::ReturnLocal,
            ClientAction::RefreshInstances,
            ClientAction::SelectInstance {
                instance_id: "studio".into(),
            },
            ClientAction::RequestOtp {
                email: "max@example.test".into(),
            },
            ClientAction::Cancel,
        ] {
            assert!(!allows(&action), "{action:?}");
        }
    }
}
