//! This computer's update panel as a popup while the main window shows another
//! computer (ui/design: chrome/remote-update-popup). Everywhere else the
//! version opens a popover; here the shell draws the same panel in its own small
//! window over the main one, from the bundled account page. The remote page only
//! asks for it, so it never reaches this computer's installer.
use avibe_runtime_host::instance_client::protocol::ClientAction;
use std::sync::{
    atomic::{AtomicU64, Ordering},
    Arc, Mutex, OnceLock,
};
use std::time::{Duration, Instant};
use tauri::{
    AppHandle, Manager, PhysicalPosition, Webview, WebviewUrl, WebviewWindow, WebviewWindowBuilder, WindowEvent,
};

use crate::instance_client::PopupPresentation;
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

pub(crate) fn is_page<R: tauri::Runtime>(window: &Webview<R>, bundle: &Url) -> bool {
    window.label() == UPDATE_POPOVER
        && window.window().label() == UPDATE_POPOVER
        && window.url().ok().is_some_and(|url| Some(url) == page_url(bundle))
}

/// What the popup may ask of the desktop client: its state and this computer's
/// updater, nothing about accounts or instances.
pub(crate) fn allows(action: &ClientAction) -> bool {
    matches!(action, ClientAction::GetState) || action.is_desktop_update()
}

pub(crate) fn navigation_allowed(url: &Url, bundle: &Url) -> bool {
    Some(url.clone()) == page_url(bundle)
}

#[derive(Clone)]
struct Request {
    initialization: String,
    presentation: PopupPresentation,
}

// A native-only resource attached to this Webview's own resource table. Tauri
// Webview equality compares labels; the Arc distinguishes a same-label rebuild.
// The document counter also fences a GetState suspended across an exact-URL reload.
struct Popup {
    resource: OnceLock<tauri::ResourceId>,
    document: AtomicU64,
}
impl tauri::Resource for Popup {}
impl Popup {
    fn new() -> Arc<Self> {
        Arc::new(Self {
            resource: OnceLock::new(),
            document: AtomicU64::new(0),
        })
    }
    fn bind<R: tauri::Runtime>(self: &Arc<Self>, view: &Webview<R>) {
        self.resource
            .get_or_init(|| view.resources_table().add_arc(self.clone()));
    }
    fn owns<R: tauri::Runtime>(self: &Arc<Self>, view: &Webview<R>) -> bool {
        self.resource.get().is_some_and(|rid| {
            view.resources_table()
                .get::<Self>(*rid)
                .is_ok_and(|stored| Arc::ptr_eq(&stored, self))
        })
    }
}

pub(crate) struct Receipt {
    popup: Arc<Popup>,
    document: u64,
}

/// The existing single opening/rebuild slot also owns the exact popup receiver.
struct Opening {
    since: Option<Instant>,
    rebuild: Option<Request>,
    current: Option<Arc<Popup>>,
    request: Option<Request>,
}
impl Opening {
    fn request(&self, popup: &Arc<Popup>) -> Option<Request> {
        self.owns(popup).then(|| self.request.clone()).flatten()
    }
    fn owns(&self, popup: &Arc<Popup>) -> bool {
        self.current.as_ref().is_some_and(|current| Arc::ptr_eq(current, popup))
    }
}
static OPENING: Mutex<Opening> = Mutex::new(Opening {
    since: None,
    rebuild: None,
    current: None,
    request: None,
});

/// Admission uses the invoking Webview, never the existence of another popup.
pub(crate) fn admit<R: tauri::Runtime>(view: &Webview<R>, bundle: &Url, action: &ClientAction) -> Option<Receipt> {
    if !allows(action) || !is_page(view, bundle) {
        return None;
    }
    let popup = opening().current.clone()?;
    let document = popup.document.load(Ordering::SeqCst);
    let receipt = Receipt { popup, document };
    still_current(view, bundle, &receipt).then_some(receipt)
}

pub(crate) fn still_current<R: tauri::Runtime>(view: &Webview<R>, bundle: &Url, receipt: &Receipt) -> bool {
    is_page(view, bundle)
        && receipt.popup.owns(view)
        && receipt.popup.document.load(Ordering::SeqCst) == receipt.document
        && opening().owns(&receipt.popup)
}

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
pub(crate) fn open(app: &AppHandle, bundle: &Url, initialization: String, presentation: PopupPresentation) -> bool {
    if app.get_window(crate::MAIN_WINDOW).is_none()
        || page_url(bundle).is_none()
        || !crate::instance_client::popup_presentation_current(app, &presentation)
    {
        return false;
    }
    let window = app.get_webview_window(UPDATE_POPOVER);
    // Never call a window getter under OPENING: the event loop may need it too.
    let visible = window.as_ref().map(|window| window.is_visible().unwrap_or(false));
    let request = Request {
        initialization,
        presentation,
    };
    let (next, popup) = {
        let mut opening = opening();
        let next = step(visible, opening.since.map(|since| since.elapsed()));
        match next {
            Step::Build => {
                opening.since = Some(Instant::now());
                opening.current = Some(Popup::new());
                opening.request = Some(request);
            }
            Step::Replace => {
                opening.since = Some(Instant::now());
                opening.rebuild = Some(request);
            }
            Step::Wait | Step::Focus => {
                // A second explicitly admitted click can use the same loading
                // slot. It supplies its own immutable source receipt; delayed
                // work never captures fresh authority from the main document.
                if opening.current.is_none() {
                    return false;
                }
                opening.request = Some(request);
            }
        }
        (next, opening.current.clone())
    };
    match (next, window, popup) {
        (Step::Focus, Some(window), Some(popup)) => {
            popup.owns(window.as_ref()) && is_page(window.as_ref(), bundle) && window.set_focus().is_ok()
        }
        (Step::Replace, Some(window), _) => {
            let _ = window.destroy();
            true
        }
        (Step::Build, _, Some(popup)) => {
            spawn_build(app, bundle, popup);
            true
        }
        _ => true,
    }
}

fn retire(app: &AppHandle, bundle: &Url, popup: &Arc<Popup>) {
    let replacement = {
        let mut opening = opening();
        if !opening.owns(popup) {
            return;
        }
        opening.request = opening.rebuild.take();
        opening.current = opening.request.as_ref().map(|_| Popup::new());
        opening.since = opening.current.as_ref().map(|_| Instant::now());
        opening.current.clone()
    };
    if let Some(replacement) = replacement {
        spawn_build(app, bundle, replacement);
    }
}

fn spawn_build(app: &AppHandle, bundle: &Url, popup: Arc<Popup>) {
    let app = app.clone();
    let bundle = bundle.clone();
    tauri::async_runtime::spawn(async move {
        let Some(_windows) = crate::instance_client::popup_window_lock(&app).await else {
            return;
        };
        let Some(request) = opening().request(&popup) else {
            return;
        };
        if !crate::instance_client::popup_presentation_current(&app, &request.presentation)
            || !build(&app, &bundle, popup.clone(), request.initialization)
        {
            retire(&app, &bundle, &popup);
        }
    });
}

/// Builds the popup, hidden until its page loads. False when nothing was built.
fn build(app: &AppHandle, bundle: &Url, popup: Arc<Popup>, initialization: String) -> bool {
    if app.get_webview_window(UPDATE_POPOVER).is_some() {
        return false;
    }
    let (Some(main), Some(url)) = (app.get_window(crate::MAIN_WINDOW), page_url(bundle)) else {
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
    let loading = popup.clone();
    let loading_bundle = bundle.clone();
    let builder = WebviewWindowBuilder::new(app, UPDATE_POPOVER, WebviewUrl::External(url))
        .on_page_load(move |window, payload| {
            if !opening().owns(&loading) {
                return;
            }
            loading.bind(window.as_ref());
            match payload.event() {
                tauri::webview::PageLoadEvent::Started => {
                    loading.document.fetch_add(1, Ordering::SeqCst);
                }
                tauri::webview::PageLoadEvent::Finished => {
                    if navigation_allowed(payload.url(), &loading_bundle) {
                        page_loaded(window, loading_bundle.clone(), loading.clone());
                    }
                }
            }
        })
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
        // The build-owned callback shows only this receiver after its page loads.
        .visible(false)
        .focused(true);
    // Match WebviewWindowBuilder::parent semantics for a physical A5 Window.
    // Windows uses an owner, not a WS_CHILD parent relationship.
    #[cfg(target_os = "windows")]
    let builder = match main.hwnd() {
        Ok(parent) => builder.owner_raw(parent),
        Err(_) => return false,
    };
    #[cfg(target_os = "macos")]
    let builder = match main.ns_window() {
        Ok(parent) => builder.parent_raw(parent),
        Err(_) => return false,
    };
    #[cfg(any(
        target_os = "linux",
        target_os = "dragonfly",
        target_os = "freebsd",
        target_os = "netbsd",
        target_os = "openbsd"
    ))]
    let builder = match main.gtk_window() {
        Ok(parent) => builder.transient_for_raw(&parent),
        Err(_) => return false,
    };
    #[cfg(target_os = "macos")]
    let builder = builder.accept_first_mouse(true);
    let Ok(window) = builder.build() else {
        return false;
    };
    popup.bind(window.as_ref());
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
        WindowEvent::Destroyed => retire(&rebuilding.0, &rebuilding.1, &popup),
        _ => {}
    });
    true
}

/// A Finished event carries its build identity, not a fresh lookup by label.
fn page_loaded(window: WebviewWindow, bundle: Url, popup: Arc<Popup>) {
    let receipt = Receipt {
        document: popup.document.load(Ordering::SeqCst),
        popup,
    };
    tauri::async_runtime::spawn(async move {
        let app = window.app_handle();
        let Some(_windows) = crate::instance_client::popup_window_lock(app).await else {
            return;
        };
        if !still_current(window.as_ref(), &bundle, &receipt) {
            return;
        }
        let Some(request) = opening().request(&receipt.popup) else {
            return;
        };
        if !crate::instance_client::popup_presentation_current(app, &request.presentation) {
            let _ = window.destroy();
            return;
        }
        opening().since = None;
        let _ = window.show();
        let _ = window.set_focus();
    });
}

/// Deliver a state change to an open popup that still shows its page.
pub(crate) fn publish(app: &AppHandle, bundle: &Url, script: impl FnOnce(&Url) -> String) {
    let Some(window) = app.get_webview_window(UPDATE_POPOVER) else {
        return;
    };
    if let Ok(url) = window.url() {
        if is_page(window.as_ref(), bundle) {
            let _ = window.eval(script(&url));
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    // The old exact-URL check cannot distinguish two native Webviews reusing
    // the label. Exercise real Tauri resource tables plus the production receipt
    // pre/postchecks; real close/rebuild/GetState execution belongs to the probe.
    #[test]
    fn popup_receipts_bind_the_actual_webview_and_document() {
        let first_app = tauri::test::mock_app();
        let next_app = tauri::test::mock_app();
        let bundle: Url = "tauri://localhost/".parse().unwrap();
        let window = |app: &tauri::App<tauri::test::MockRuntime>, label: &str, url: Url| {
            WebviewWindowBuilder::new(app, label, WebviewUrl::External(url))
                .build()
                .unwrap()
        };
        let first = window(&first_app, UPDATE_POPOVER, page_url(&bundle).unwrap());
        let next = window(&next_app, UPDATE_POPOVER, page_url(&bundle).unwrap());
        // Tauri equality alone would admit the successor.
        assert!(first.as_ref() == next.as_ref());
        let original = Popup::new();
        original.bind(first.as_ref());
        opening().current = Some(original.clone());
        let receipt = admit(first.as_ref(), &bundle, &ClientAction::GetState).unwrap();
        assert!(still_current(first.as_ref(), &bundle, &receipt));
        assert!(!still_current(next.as_ref(), &bundle, &receipt));
        assert!(admit(next.as_ref(), &bundle, &ClientAction::GetState).is_none());
        for action in [
            ClientAction::SignOut {
                intent_id: Some("foreign".into()),
            },
            ClientAction::Cancel,
        ] {
            assert!(admit(first.as_ref(), &bundle, &action).is_none());
        }
        for (label, url) in [
            ("main-account-1", page_url(&bundle).unwrap()),
            ("main-runtime-1", "https://remote.example.test/".parse().unwrap()),
            ("main-runtime-candidate", page_url(&bundle).unwrap()),
        ] {
            let foreign = window(&first_app, label, url);
            assert!(admit(foreign.as_ref(), &bundle, &ClientAction::GetState).is_none());
        }
        original.document.fetch_add(1, Ordering::SeqCst);
        assert!(!still_current(first.as_ref(), &bundle, &receipt));
        let fresh = admit(first.as_ref(), &bundle, &ClientAction::GetState).unwrap();
        assert!(still_current(first.as_ref(), &bundle, &fresh));
        let successor = Popup::new();
        successor.bind(next.as_ref());
        opening().current = Some(successor);
        assert!(!still_current(first.as_ref(), &bundle, &fresh));
        assert!(admit(first.as_ref(), &bundle, &ClientAction::GetState).is_none());
        let next_receipt = admit(next.as_ref(), &bundle, &ClientAction::GetState).unwrap();
        assert!(still_current(next.as_ref(), &bundle, &next_receipt));
        opening().current = None;
        assert!(!still_current(next.as_ref(), &bundle, &next_receipt));
    }

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
            ClientAction::SignOut { intent_id: None },
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
