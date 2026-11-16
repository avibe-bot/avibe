//! This computer's update panel as a popup while the main window shows another
//! computer (ui/design: chrome/remote-update-popup). Everywhere else the
//! version opens a popover; here the shell draws the same panel in its own small
//! window over the main one, from the bundled account page. The remote page only
//! asks for it, so it never reaches this computer's installer.
use avibe_runtime_host::instance_client::protocol::ClientAction;
use std::sync::Mutex;
use std::time::{Duration, Instant};
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

pub(crate) fn is_page<R: tauri::Runtime>(window: &WebviewWindow<R>, bundle: &Url) -> bool {
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

/// A popup being built or loading, so a second click waits for it instead of
/// building another or replacing it. Cleared once its page loads or it is gone.
struct Opening {
    since: Option<Instant>,
    /// The page state to rebuild with once a stuck window is actually destroyed.
    rebuild: Option<String>,
}
static OPENING: Mutex<Opening> = Mutex::new(Opening {
    since: None,
    rebuild: None,
});
/// How long a popup may take to build and load (a cold WebView2 can be slow)
/// before a click treats it as stuck and replaces it.
const LOAD_DEADLINE: Duration = Duration::from_secs(15);

fn opening() -> std::sync::MutexGuard<'static, Opening> {
    OPENING.lock().unwrap_or_else(|poisoned| poisoned.into_inner())
}

#[derive(Debug, PartialEq)]
enum Step {
    Focus,
    Wait,
    Replace,
    Build,
}

/// What a click on the version does, given the popup window (shown or not) and
/// how long one has been building or loading.
fn step(window: Option<bool>, loading_for: Option<Duration>) -> Step {
    let loading = loading_for.is_some_and(|elapsed| elapsed < LOAD_DEADLINE);
    match window {
        Some(true) => Step::Focus,
        _ if loading => Step::Wait,
        Some(false) => Step::Replace,
        None => Step::Build,
    }
}

/// The popup's page is transparent from its first paint: the account page's own
/// background would otherwise flash as a solid box over the remote sidebar.
/// WebKit runs it once the document element exists; WebView2 runs it before the
/// parser starts, so it waits for the root when there is none yet. The page sets
/// the same transparency itself as a fallback (AccountApp's UpdatePopover).
const TRANSPARENT: &str = "(()=>{const s=document.createElement('style');s.textContent='html,body{background:transparent!important}';const add=()=>(document.head||document.documentElement).appendChild(s);if(document.documentElement){add();}else{new MutationObserver((_,o)=>{if(document.documentElement){o.disconnect();add();}}).observe(document,{childList:true});}})();";

/// Show the popup over the main window, or bring an open one forward. Called from
/// navigation and menu handlers: the window is built off the handler's own thread,
/// since building one inside them deadlocks on Windows.
pub(crate) fn open(app: &AppHandle, bundle: &Url, initialization: String) -> bool {
    if app.get_webview_window(crate::MAIN_WINDOW).is_none() || page_url(bundle).is_none() {
        return false;
    }
    let window = app.get_webview_window(UPDATE_POPOVER);
    // Decide and record under the lock, act after releasing it: destroying a
    // window may run its Destroyed handler, which takes the lock too.
    let (next, initialization) = {
        let mut opening = opening();
        let next = step(
            window.as_ref().map(|window| window.is_visible().unwrap_or(false)),
            opening.since.map(|since| since.elapsed()),
        );
        if matches!(next, Step::Replace | Step::Build) {
            opening.since = Some(Instant::now());
        }
        if next == Step::Replace {
            // Built for the new window once the stuck one is gone.
            opening.rebuild = Some(initialization);
            (next, None)
        } else {
            (next, Some(initialization))
        }
    };
    match (next, window, initialization) {
        (Step::Focus, Some(window), _) => window.set_focus().is_ok(),
        // Never shown and past its deadline: its page is stuck. Destroying it
        // completes on the event loop; its Destroyed event builds the new one.
        (Step::Replace, Some(window), _) => {
            let _ = window.destroy();
            true
        }
        (Step::Build, _, Some(initialization)) => {
            spawn_build(app, bundle, initialization);
            true
        }
        _ => true,
    }
}

fn spawn_build(app: &AppHandle, bundle: &Url, initialization: String) {
    let app = app.clone();
    let bundle = bundle.clone();
    tauri::async_runtime::spawn(async move {
        if !build(&app, &bundle, initialization) {
            opening().since = None;
        }
    });
}

/// Builds the popup, hidden until its page loads. False when nothing was built.
fn build(app: &AppHandle, bundle: &Url, initialization: String) -> bool {
    if app.get_webview_window(UPDATE_POPOVER).is_some() {
        return false;
    }
    let (Some(main), Some(url)) = (app.get_webview_window(crate::MAIN_WINDOW), page_url(bundle)) else {
        return false;
    };
    let (Ok(origin), Ok(size), Ok(scale)) = (main.inner_position(), main.inner_size(), main.scale_factor()) else {
        return false;
    };
    // The main window's content area in its own logical points, and the popup's
    // top-left from its bottom-left corner.
    let origin = origin.to_logical::<f64>(scale);
    let height = size.height as f64 / scale;
    let (x, y) = (origin.x + LEFT, origin.y + height - BOTTOM - HEIGHT);
    let builder = WebviewWindowBuilder::new(app, UPDATE_POPOVER, WebviewUrl::External(url))
        .initialization_script(TRANSPARENT)
        .initialization_script(initialization)
        // The panel's release-notes link opens in the system browser, as in the main window.
        .on_new_window(|url, _features| crate::handle_new_window_request(url))
        .title("Avibe")
        .inner_size(WIDTH, HEIGHT)
        .position(x, y)
        .transparent(true)
        .decorations(false)
        .shadow(false)
        .resizable(false)
        .maximizable(false)
        .minimizable(false)
        .skip_taskbar(true)
        // Shown once its page has loaded (lib.rs on_page_load), so nothing half-drawn appears.
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
    // macOS places windows in one space of points, which the logical position
    // above already is. Windows places them on one physical desktop, where the
    // popup's own scale may not be the main window's yet: use main's pixels.
    if !cfg!(target_os = "macos") {
        let physical = (x * scale, y * scale);
        let _ = window.set_position(PhysicalPosition::new(
            physical.0.round() as i32,
            physical.1.round() as i32,
        ));
    }
    let closing = window.clone();
    let rebuilding = (app.clone(), bundle.clone());
    window.on_window_event(move |event| match event {
        // A click anywhere outside the popup moves focus away: it closes, like a popover.
        WindowEvent::Focused(false) => {
            let _ = closing.close();
        }
        // Gone, so its label is free: build the replacement a click asked for, if any.
        WindowEvent::Destroyed => {
            let mut opening = opening();
            match opening.rebuild.take() {
                Some(initialization) => {
                    opening.since = Some(Instant::now());
                    spawn_build(&rebuilding.0, &rebuilding.1, initialization);
                }
                None => opening.since = None,
            }
        }
        _ => {}
    });
    true
}

/// The popup's page has loaded: show it, focused, as a click on the version does.
pub(crate) fn page_loaded(window: &WebviewWindow) {
    if window.label() == UPDATE_POPOVER {
        opening().since = None;
        let _ = window.show();
        let _ = window.set_focus();
    }
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

    /// A click while the popup is still building or loading waits for it rather
    /// than replacing it; only one stuck past its deadline is replaced.
    #[test]
    fn a_click_waits_for_a_loading_popup_and_replaces_only_a_stuck_one() {
        let early = Some(Duration::from_millis(300));
        let late = Some(LOAD_DEADLINE + Duration::from_secs(1));
        assert_eq!(step(None, None), Step::Build);
        assert_eq!(step(None, early), Step::Wait);
        assert_eq!(step(Some(false), early), Step::Wait);
        assert_eq!(step(Some(false), late), Step::Replace);
        assert_eq!(step(Some(false), None), Step::Replace);
        assert_eq!(step(None, late), Step::Build);
        assert_eq!(step(Some(true), None), Step::Focus);
        assert_eq!(step(Some(true), early), Step::Focus);
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
