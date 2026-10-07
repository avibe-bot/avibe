//! The desktop pet's native window: when it exists, where it sits, how it is
//! woken, and the narrow command surface its `/pet` page and the Workbench's
//! "Show in pet" use. The decisions that need no window live in
//! `avibe_runtime_host::pet`; see `docs/plans/2026-10-01-desktop-pet.md`.
//!
//! Lifecycle in one sentence: the window exists exactly when the pet is
//! enabled and a Runtime is ready, and [`reconcile_now`] is the only code that
//! creates or destroys it.

use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex, MutexGuard};
use std::time::Duration;

use avibe_runtime_host::deep_link::parse_deep_link;
use avibe_runtime_host::pet::{
    anchor_from_window, choose_layout, clamp_anchor, default_anchor, shortcut_label, valid_session_id, window_origin,
    window_size, LogicalRect, PetAnchor, PetBinding, PetIntent, PetLayout, PetReady, PetStore, WorkArea, PET_SIZE,
    SHORTCUT_PRESETS,
};
use avibe_runtime_host::LoopbackOrigin;
use serde::{Deserialize, Serialize};
use tauri::menu::{CheckMenuItem, MenuItem, Submenu};
use tauri::{
    AppHandle, LogicalPosition, LogicalSize, Manager, Monitor, WebviewUrl, WebviewWindow, WebviewWindowBuilder,
};
use tauri_plugin_global_shortcut::GlobalShortcutExt;
use url::Url;

use crate::{native_catalog_for_locales, Shell, DESKTOP_SHELL_MARKER, MAIN_WINDOW};

pub const PET_WINDOW: &str = "pet";
pub const MENU_ID: &str = "show-pet";
pub const SHORTCUT_MENU_PREFIX: &str = "pet-shortcut:";
const PET_PATH: &str = "/pet";
const SUMMON_EVENT: &str = "avibe:pet-summon";
const BOUND_EVENT: &str = "avibe:pet-bound";
/// A drag settles before its anchor is saved, as the main window's frame does.
const MOVE_SETTLE: Duration = Duration::from_millis(400);

/// Tells the Workbench, before its scripts run, that this shell can show a
/// session in the pet. An older shell has no `pet_bind`, so the page offers
/// "Show in pet" only when this is present.
pub const DESKTOP_PET_MARKER: &str =
    "if (window.self === window.top) Object.defineProperty(window, '__AVIBE_DESKTOP_PET__', { value: true });";

#[derive(Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct NativePetCatalog {
    toggle: String,
    shortcut_menu: String,
    shortcut_unavailable: String,
}

fn native_pet_catalog() -> NativePetCatalog {
    native_catalog_for_locales(sys_locale::get_locales()).pet
}

#[derive(Clone, Copy)]
struct Frame {
    expanded: bool,
    layout: PetLayout,
}

pub struct Pet {
    store: Mutex<PetStore>,
    /// The Runtime the Workbench is showing. The pet may load only this origin.
    runtime: Mutex<Option<LoopbackOrigin>>,
    /// The origin the current pet window was built for.
    window_origin: Mutex<Option<LoopbackOrigin>>,
    frame: Mutex<Frame>,
    /// The accelerator the OS refused, shown in the tray until a registration succeeds.
    shortcut_error: Mutex<Option<String>>,
    move_generation: Arc<AtomicU64>,
}

impl Pet {
    pub fn new(path: PathBuf) -> Self {
        Self {
            store: Mutex::new(PetStore::load(path)),
            runtime: Mutex::new(None),
            window_origin: Mutex::new(None),
            frame: Mutex::new(Frame {
                expanded: false,
                layout: PetLayout::default(),
            }),
            shortcut_error: Mutex::new(None),
            move_generation: Arc::new(AtomicU64::new(0)),
        }
    }

    fn store(&self) -> MutexGuard<'_, PetStore> {
        self.store.lock().unwrap_or_else(|poisoned| poisoned.into_inner())
    }

    pub fn enabled(&self) -> bool {
        self.store().enabled()
    }
}

fn lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex.lock().unwrap_or_else(|poisoned| poisoned.into_inner())
}

pub struct PetMenus {
    toggle: CheckMenuItem<tauri::Wry>,
    shortcut: Submenu<tauri::Wry>,
    presets: Vec<CheckMenuItem<tauri::Wry>>,
    unavailable: MenuItem<tauri::Wry>,
    unavailable_present: AtomicBool,
}

/// The tray's pet switch and shortcut presets, placed next to Notifications.
pub fn tray_items(app: &AppHandle) -> tauri::Result<(CheckMenuItem<tauri::Wry>, Submenu<tauri::Wry>)> {
    let catalog = native_pet_catalog();
    let enabled = app.state::<Pet>().enabled();
    let toggle = CheckMenuItem::with_id(app, MENU_ID, &catalog.toggle, true, enabled, None::<&str>)?;
    let macos = cfg!(target_os = "macos");
    let presets = SHORTCUT_PRESETS
        .iter()
        .map(|preset| {
            CheckMenuItem::with_id(
                app,
                format!("{SHORTCUT_MENU_PREFIX}{preset}"),
                shortcut_label(preset, macos),
                true,
                false,
                None::<&str>,
            )
        })
        .collect::<tauri::Result<Vec<_>>>()?;
    let shortcut = Submenu::with_id(app, "pet-shortcut", &catalog.shortcut_menu, true)?;
    for preset in &presets {
        shortcut.append(preset)?;
    }
    let unavailable = MenuItem::with_id(app, "pet-shortcut-unavailable", "", false, None::<&str>)?;
    app.manage(PetMenus {
        toggle: toggle.clone(),
        shortcut: shortcut.clone(),
        presets,
        unavailable,
        unavailable_present: AtomicBool::new(false),
    });
    Ok((toggle, shortcut))
}

fn refresh_menu(app: &AppHandle) {
    let Some(menus) = app.try_state::<PetMenus>() else {
        return;
    };
    let pet = app.state::<Pet>();
    let (enabled, shortcut) = {
        let store = pet.store();
        (store.enabled(), store.shortcut().to_owned())
    };
    let error = lock(&pet.shortcut_error).clone();
    let update = || -> tauri::Result<()> {
        menus.toggle.set_checked(enabled)?;
        for (item, preset) in menus.presets.iter().zip(SHORTCUT_PRESETS) {
            item.set_checked(preset == shortcut)?;
        }
        if let Some(refused) = &error {
            let label = native_pet_catalog()
                .shortcut_unavailable
                .replace("{{shortcut}}", &shortcut_label(refused, cfg!(target_os = "macos")));
            menus.unavailable.set_text(label)?;
        }
        if error.is_some() != menus.unavailable_present.load(Ordering::SeqCst) {
            if error.is_some() {
                menus.shortcut.append(&menus.unavailable)?;
            } else {
                menus.shortcut.remove(&menus.unavailable)?;
            }
            menus.unavailable_present.store(error.is_some(), Ordering::SeqCst);
        }
        Ok(())
    };
    if update().is_err() {
        eprintln!("failed to refresh the desktop pet menu");
    }
}

/// Registers the summon shortcut while the pet is on, and nothing while it is
/// off. A shortcut the OS refuses is shown in the tray, never swallowed.
pub fn apply_shortcut(app: &AppHandle) {
    let pet = app.state::<Pet>();
    let (enabled, shortcut) = {
        let store = pet.store();
        (store.enabled(), store.shortcut().to_owned())
    };
    let shortcuts = app.global_shortcut();
    let _ = shortcuts.unregister_all();
    let error = if enabled {
        match shortcuts.register(shortcut.as_str()) {
            Ok(()) => None,
            Err(error) => {
                eprintln!("The desktop pet shortcut could not be registered: {error}");
                Some(shortcut)
            }
        }
    } else {
        None
    };
    *lock(&pet.shortcut_error) = error;
    refresh_menu(app);
}

/// The tray switch.
pub fn toggle(app: &AppHandle) {
    let pet = app.state::<Pet>();
    let enabled = !pet.enabled();
    if pet.store().set_enabled(enabled).is_err() {
        eprintln!("The desktop pet preference could not be saved.");
    }
    apply_shortcut(app);
    if pet.enabled() {
        wake(app, PetIntent::Show);
    } else {
        reconcile(app);
    }
}

/// A shortcut preset from the tray.
pub fn choose_shortcut(app: &AppHandle, id: &str) {
    let Some(preset) = id
        .strip_prefix(SHORTCUT_MENU_PREFIX)
        .and_then(|choice| SHORTCUT_PRESETS.iter().find(|preset| **preset == choice))
    else {
        return;
    };
    app.state::<Pet>().store().set_shortcut(preset);
    apply_shortcut(app);
}

/// The Workbench is showing a ready Runtime at `origin`.
pub fn runtime_ready(app: &AppHandle, origin: LoopbackOrigin) {
    *lock(&app.state::<Pet>().runtime) = Some(origin);
    reconcile(app);
}

/// The Workbench no longer shows a Runtime the shell vouches for.
pub fn runtime_gone(app: &AppHandle) {
    if let Some(pet) = app.try_state::<Pet>() {
        *lock(&pet.runtime) = None;
        reconcile(app);
    }
}

pub fn reconcile(app: &AppHandle) {
    let handle = app.clone();
    let _ = app.run_on_main_thread(move || {
        reconcile_now(&handle);
    });
}

/// Makes the window exist exactly when the pet is enabled and a Runtime is
/// ready, at that Runtime's origin. The only creator and destroyer of `pet`.
fn reconcile_now(app: &AppHandle) -> Option<WebviewWindow> {
    let pet = app.state::<Pet>();
    let desired = if pet.enabled() {
        lock(&pet.runtime).clone()
    } else {
        None
    };
    let current = lock(&pet.window_origin).clone();
    if let Some(window) = app.get_webview_window(PET_WINDOW) {
        if desired.is_some() && desired == current {
            return Some(window);
        }
        let _ = window.destroy();
        *lock(&pet.window_origin) = None;
        pet.store().window_gone();
    }
    let origin = desired?;
    let window = build_window(app, &origin)?;
    *lock(&pet.window_origin) = Some(origin);
    Some(window)
}

fn build_window(app: &AppHandle, origin: &LoopbackOrigin) -> Option<WebviewWindow> {
    let pet = app.state::<Pet>();
    let mut url = origin.navigation_url();
    url.set_path(PET_PATH);
    let areas = work_areas(app, None);
    let saved = pet.store().anchor().cloned();
    let anchor = match (saved, areas.first()) {
        (Some(saved), _) => clamp_anchor(&saved, &areas),
        (None, Some(primary)) => default_anchor(primary),
        (None, None) => PetAnchor {
            x: 0.0,
            y: 0.0,
            monitor: None,
        },
    };
    let layout = PetLayout::default();
    *lock(&pet.frame) = Frame {
        expanded: false,
        layout,
    };
    let (width, height) = window_size(false);
    let (x, y) = window_origin((anchor.x, anchor.y), false, layout);
    pet.store().page_loading();
    let builder = WebviewWindowBuilder::new(app, PET_WINDOW, WebviewUrl::External(url))
        .initialization_script(DESKTOP_SHELL_MARKER)
        .on_new_window(|_, _| tauri::webview::NewWindowResponse::Deny)
        .title("Avibe")
        .inner_size(width, height)
        .position(x, y)
        .transparent(true)
        .decorations(false)
        .shadow(false)
        .resizable(false)
        .maximizable(false)
        .minimizable(false)
        .always_on_top(true)
        .visible_on_all_workspaces(true)
        .skip_taskbar(true)
        .focused(false);
    #[cfg(target_os = "macos")]
    let builder = builder.accept_first_mouse(true);
    match builder.build() {
        Ok(window) => Some(window),
        Err(error) => {
            eprintln!("The desktop pet window could not be created: {error}");
            pet.store().window_gone();
            None
        }
    }
}

/// Every way to summon the pet. The hotkey listens; everything else shows.
pub fn wake(app: &AppHandle, intent: PetIntent) {
    let handle = app.clone();
    let _ = app.run_on_main_thread(move || {
        if !handle.state::<Pet>().enabled() {
            return;
        }
        let Some(window) = reconcile_now(&handle) else {
            return;
        };
        let _ = window.show();
        let _ = window.set_focus();
        let deliver = handle.state::<Pet>().store().summon(intent);
        if let Some(intent) = deliver {
            dispatch(&window, SUMMON_EVENT, &serde_json::json!({ "intent": intent }));
        }
    });
}

fn dispatch(window: &WebviewWindow, event: &str, detail: &serde_json::Value) {
    let script = format!(
        "window.dispatchEvent(new CustomEvent({}, {{ detail: {} }}));",
        serde_json::to_string(event).expect("event name JSON"),
        detail
    );
    let _ = window.eval(script);
}

fn announce_binding(app: &AppHandle, binding: &PetBinding) {
    let ready = app.state::<Pet>().store().page_is_ready();
    if let Some(window) = app.get_webview_window(PET_WINDOW).filter(|_| ready) {
        dispatch(
            &window,
            BOUND_EVENT,
            &serde_json::json!({ "session_id": binding.binding, "revision": binding.revision }),
        );
    }
}

/// The pet may only show its own route on the Runtime it was built for, so
/// it can never become a second Workbench.
pub fn navigation_allowed(app: &AppHandle, url: &Url) -> bool {
    app.try_state::<Pet>()
        .is_some_and(|pet| is_pet_page(lock(&pet.window_origin).as_ref(), url))
}

fn is_pet_page(origin: Option<&LoopbackOrigin>, url: &Url) -> bool {
    origin.is_some_and(|origin| origin.matches_url_origin(url)) && url.path() == PET_PATH
}

/// The pet window's own document is starting over: summons wait for it.
pub fn page_loading(app: &AppHandle) {
    if let Some(pet) = app.try_state::<Pet>() {
        pet.store().page_loading();
    }
}

/// An OS close request only hides the pet; the tray switch turns it off.
pub fn hide(app: &AppHandle) {
    if let Some(window) = app.get_webview_window(PET_WINDOW) {
        let _ = window.hide();
    }
}

/// Saves where the user dragged the pet once the drag settles.
pub fn window_moved(app: &AppHandle) {
    let Some(pet) = app.try_state::<Pet>() else {
        return;
    };
    let generation = pet.move_generation.clone();
    let observed = generation.fetch_add(1, Ordering::SeqCst) + 1;
    let app = app.clone();
    tauri::async_runtime::spawn(async move {
        tokio::time::sleep(MOVE_SETTLE).await;
        let handle = app.clone();
        let _ = app.run_on_main_thread(move || {
            if generation.load(Ordering::SeqCst) != observed {
                return;
            }
            let Some(window) = handle.get_webview_window(PET_WINDOW) else {
                return;
            };
            let Some(anchor) = current_anchor(&handle, &window) else {
                return;
            };
            let areas = work_areas(&handle, window.scale_factor().ok());
            let monitor = area_for(&areas, (anchor.0, anchor.1)).and_then(|area| area.name.clone());
            handle.state::<Pet>().store().set_anchor(PetAnchor {
                x: anchor.0.round(),
                y: anchor.1.round(),
                monitor,
            });
        });
    });
}

fn current_anchor(app: &AppHandle, window: &WebviewWindow) -> Option<(f64, f64)> {
    let scale = window.scale_factor().ok()?;
    let position = window.outer_position().ok()?.to_logical::<f64>(scale);
    let frame = *lock(&app.state::<Pet>().frame);
    Some(anchor_from_window(
        (position.x, position.y),
        frame.expanded,
        frame.layout,
    ))
}

/// Each display's work area in logical points, the primary display first.
fn work_areas(app: &AppHandle, window_scale: Option<f64>) -> Vec<WorkArea> {
    let primary = app
        .primary_monitor()
        .ok()
        .flatten()
        .and_then(|monitor| monitor.name().cloned());
    let mut areas: Vec<WorkArea> = app
        .available_monitors()
        .unwrap_or_default()
        .iter()
        .map(|monitor| logical_work_area(monitor, window_scale))
        .collect();
    areas.sort_by_key(|area| area.name != primary);
    areas
}

fn logical_work_area(monitor: &Monitor, window_scale: Option<f64>) -> WorkArea {
    // macOS reports each display in its own pixels; Windows and Linux report
    // one physical desktop, which the window's scale turns into its points.
    let scale = if cfg!(target_os = "macos") {
        monitor.scale_factor()
    } else {
        window_scale.unwrap_or_else(|| monitor.scale_factor())
    };
    let area = monitor.work_area();
    WorkArea {
        name: monitor.name().cloned(),
        area: LogicalRect {
            x: f64::from(area.position.x) / scale,
            y: f64::from(area.position.y) / scale,
            width: f64::from(area.size.width) / scale,
            height: f64::from(area.size.height) / scale,
        },
    }
}

fn area_for(areas: &[WorkArea], anchor: (f64, f64)) -> Option<&WorkArea> {
    let center = (anchor.0 + PET_SIZE / 2.0, anchor.1 + PET_SIZE / 2.0);
    areas
        .iter()
        .find(|area| {
            center.0 >= area.area.x
                && center.0 < area.area.x + area.area.width
                && center.1 >= area.area.y
                && center.1 < area.area.y + area.area.height
        })
        .or_else(|| areas.first())
}

const UNAVAILABLE: &str = "This command is only available to the Avibe desktop pet.";

/// The second layer behind the `pet` capability: the caller is the pet window
/// showing its own route on the Runtime it was built for.
fn ensure_pet_page(window: &WebviewWindow) -> Result<(), String> {
    let pet = window.state::<Pet>();
    let url = window.url().map_err(|_| UNAVAILABLE.to_owned())?;
    if window.label() == PET_WINDOW && is_pet_page(lock(&pet.window_origin).as_ref(), &url) {
        Ok(())
    } else {
        Err(UNAVAILABLE.to_owned())
    }
}

/// The Workbench in `main`, on the Runtime the shell navigated it to.
fn is_workbench(window: &WebviewWindow) -> bool {
    let Some(shell) = window.try_state::<Shell>() else {
        return false;
    };
    let active = lock(&shell.active_origin).clone();
    window.label() == MAIN_WINDOW
        && window
            .url()
            .is_ok_and(|url| active.is_some_and(|origin| origin.matches_url_origin(&url)))
}

/// Called once the `/pet` page has its listeners installed.
#[tauri::command]
pub fn pet_ready(window: WebviewWindow) -> Result<PetReady, String> {
    ensure_pet_page(&window)?;
    let pet = window.state::<Pet>();
    // A new document starts collapsed, so the frame does too.
    let expanded = lock(&pet.frame).expanded;
    if expanded {
        let _ = set_expanded(&window, false);
    }
    let ready = pet.store().page_ready();
    Ok(ready)
}

/// Grows the window to fit the panel, or shrinks it to the pet, keeping the
/// pet where it is. The answer is the one placement the page renders from.
#[tauri::command]
pub fn pet_set_expanded(window: WebviewWindow, expanded: bool) -> Result<PetLayout, String> {
    ensure_pet_page(&window)?;
    set_expanded(&window, expanded)
}

fn set_expanded(window: &WebviewWindow, expanded: bool) -> Result<PetLayout, String> {
    let app = window.app_handle();
    let anchor = current_anchor(app, window).ok_or_else(|| "The pet window has no position.".to_owned())?;
    let pet = app.state::<Pet>();
    let previous = *lock(&pet.frame);
    let layout = if expanded {
        let areas = work_areas(app, window.scale_factor().ok());
        match area_for(&areas, anchor) {
            Some(area) => choose_layout(anchor, area.area),
            None => PetLayout::default(),
        }
    } else {
        previous.layout
    };
    *lock(&pet.frame) = Frame { expanded, layout };
    let (width, height) = window_size(expanded);
    let (x, y) = window_origin(anchor, expanded, layout);
    window
        .set_size(LogicalSize::new(width, height))
        .and_then(|()| window.set_position(LogicalPosition::new(x, y)))
        .map_err(|_| "The pet window could not be resized.".to_owned())?;
    Ok(layout)
}

#[derive(Serialize)]
pub struct BindResult {
    #[serde(flatten)]
    binding: PetBinding,
    /// Whether the pet will show the session now. False while the pet is off.
    shown: bool,
}

/// Binds the pet to a session: the pet's switcher, and the Workbench's
/// "Show in pet", which also brings the pet forward.
#[tauri::command]
pub fn pet_bind(window: WebviewWindow, session_id: String) -> Result<BindResult, String> {
    let from_workbench = is_workbench(&window);
    if !from_workbench {
        ensure_pet_page(&window)?;
    }
    if !valid_session_id(&session_id) {
        return Err("Not a session id.".to_owned());
    }
    let app = window.app_handle().clone();
    let pet = app.state::<Pet>();
    let binding = pet.store().bind(&session_id);
    announce_binding(&app, &binding);
    let shown = pet.enabled();
    if from_workbench && shown {
        // Off the command's own thread: creating a window inside an IPC
        // callback deadlocks on Windows.
        let wake_app = app.clone();
        tauri::async_runtime::spawn(async move { wake(&wake_app, PetIntent::Show) });
    }
    Ok(BindResult { binding, shown })
}

/// Clears the binding only if it is still `session_id`.
#[tauri::command]
pub fn pet_unbind(window: WebviewWindow, session_id: String) -> Result<PetBinding, String> {
    ensure_pet_page(&window)?;
    let app = window.app_handle();
    let pet = app.state::<Pet>();
    let before = pet.store().binding().revision;
    let binding = pet.store().unbind(&session_id);
    if binding.revision != before {
        announce_binding(app, &binding);
    }
    Ok(binding)
}

/// Opens a deep link in `main`, exactly as a clicked `avibe://` link would.
#[tauri::command]
pub fn pet_open(window: WebviewWindow, link: String) -> Result<(), String> {
    ensure_pet_page(&window)?;
    if parse_deep_link(&link).is_none() {
        return Err("Not an Avibe link.".to_owned());
    }
    let app = window.app_handle().clone();
    tauri::async_runtime::spawn(async move {
        let handle = app.clone();
        let _ = app.run_on_main_thread(move || crate::receive_native_deep_link(&handle, [link]));
    });
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_pet_navigates_only_to_its_own_route_on_its_own_runtime() {
        let origin = LoopbackOrigin::parse("http://127.0.0.1:5123").unwrap();
        let allowed = |raw: &str| is_pet_page(Some(&origin), &Url::parse(raw).unwrap());
        assert!(allowed("http://127.0.0.1:5123/pet"));
        assert!(allowed("http://127.0.0.1:5123/pet?x=1"));
        assert!(!allowed("http://127.0.0.1:5123/"));
        assert!(!allowed("http://127.0.0.1:5123/chat/abc"));
        assert!(!allowed("http://127.0.0.1:5124/pet"));
        assert!(!allowed("http://localhost:5123/pet"));
        assert!(!is_pet_page(None, &Url::parse("http://127.0.0.1:5123/pet").unwrap()));
    }

    #[test]
    fn the_pet_catalog_is_complete_in_every_locale() {
        for locale in ["en-US", "zh-CN"] {
            let catalog = native_catalog_for_locales([locale.to_owned()]).pet;
            assert!(!catalog.toggle.is_empty());
            assert!(!catalog.shortcut_menu.is_empty());
            assert!(catalog.shortcut_unavailable.contains("{{shortcut}}"));
        }
    }

    #[test]
    fn the_workbench_marker_is_top_level_only() {
        assert!(DESKTOP_PET_MARKER.starts_with("if (window.self === window.top)"));
        assert!(DESKTOP_PET_MARKER.contains("'__AVIBE_DESKTOP_PET__'"));
    }
}
