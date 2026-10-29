//! The desktop pet's native window: when it exists, where it sits, how it is
//! woken, and the narrow command surface its `/pet` page and the Workbench's
//! "Show in pet" use. The decisions that need no window live in
//! `avibe_runtime_host::pet`; see `docs/plans/2026-10-01-desktop-pet.md`.
//!
//! Lifecycle in one sentence: the window exists exactly when the pet is
//! enabled and the Workbench shows a Runtime, at that Runtime's origin, and
//! [`reconcile_now`] is the only code that creates or destroys it. The Shell's
//! `active_origin` is the one record of that Runtime; the pet keeps no copy
//! and is reconciled whenever that record is written.

use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex, MutexGuard};
use std::time::Duration;

use avibe_runtime_host::deep_link::parse_deep_link;
use avibe_runtime_host::pet::{
    area_for, clamp_anchor, default_anchor, frame_at, hovering, place_moved, rescale, upward_frame, valid_session_id,
    FrameState, FrameStep, HitRect, LayoutSnapshot, LogicalRect, PageKind, PetAnchor, PetBinding, PetFrame, PetIntent,
    PetLayout, PetReady, PetStore, ShortcutEvent, VoiceMode, VoiceShortcutState, WorkArea, DEFAULT_SHORTCUT,
    DEFAULT_TOGGLE_SHORTCUT,
};
use avibe_runtime_host::LoopbackOrigin;
use serde::{Deserialize, Serialize};
use tauri::menu::{CheckMenuItem, MenuItem};
use tauri::{
    AppHandle, Manager, Monitor, PhysicalPosition, PhysicalSize, WebviewUrl, WebviewWindow, WebviewWindowBuilder,
};
use tauri_plugin_global_shortcut::{GlobalShortcutExt, Modifiers, Shortcut, ShortcutState};
use url::Url;

use crate::pet_native::{primary_button_down, set_frame, start_drag};
use crate::{native_catalog_for_locales, Shell, DESKTOP_SHELL_MARKER, MAIN_WINDOW};

pub const PET_WINDOW: &str = "pet";
pub const MENU_ID: &str = "show-pet";
pub const SHORTCUT_SETTINGS_MENU_ID: &str = "pet-shortcut-settings";
const PET_PATH: &str = "/pet";
const SUMMON_EVENT: &str = "avibe:pet-summon";
const BOUND_EVENT: &str = "avibe:pet-bound";
const HOVER_EVENT: &str = "avibe:pet-hover";
const LAYOUT_EVENT: &str = "avibe:pet-layout";
pub const SETTINGS_EVENT: &str = "avibe:desktop-pet-settings";
/// A drag settles before its anchor is saved, as the main window's frame does.
const MOVE_SETTLE: Duration = Duration::from_millis(400);
/// How often a drag checks whether the button is still down.
const DRAG_POLL: Duration = Duration::from_millis(16);
/// Tells the page, before its scripts run, that this shell drags the pet
/// itself (`pet_drag`), so the page chooses the drag path the moment it is
/// pressed. Older shells have no marker and the page uses `start_dragging`.
pub const PET_DRAG_MARKER: &str =
    "if (window.self === window.top) Object.defineProperty(window, '__AVIBE_PET_DRAG__', { value: true });";
/// How long a dropped pet waits for its page to draw the chosen layout before
/// the shell restores the full window on its own.
const SETTLE_TIMEOUT: Duration = Duration::from_millis(1000);
const WINDOW_GONE: &str = "The desktop pet window is gone.";

/// Tells the Workbench, before its scripts run, that this shell can show a
/// session in the pet. An older shell has no `pet_bind`, so the page offers
/// "Show in pet" only when this is present.
pub const DESKTOP_PET_MARKER: &str =
    "if (window.self === window.top) Object.defineProperty(window, '__AVIBE_DESKTOP_PET__', { value: true });";
/// A newer shell capability than the original pet window contract. The
/// Workbench probes this before rendering settings controls.
pub const DESKTOP_PET_SETTINGS_MARKER: &str =
    "if (window.self === window.top) Object.defineProperty(window, '__AVIBE_DESKTOP_PET_SETTINGS__', { value: true });";

#[derive(Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct NativePetCatalog {
    toggle: String,
    shortcut_settings: String,
}

fn native_pet_catalog() -> NativePetCatalog {
    native_catalog_for_locales(sys_locale::get_locales()).pet
}

#[derive(Clone, Copy)]
struct Frame {
    expanded: bool,
    /// The native frame and the layout the page draws (`FrameState`).
    state: FrameState,
}

pub struct Pet {
    store: Mutex<PetStore>,
    /// The origin the current pet window was built for. The pet may load only
    /// this origin.
    window_origin: Mutex<Option<LoopbackOrigin>>,
    frame: Mutex<Frame>,
    /// The accelerator the OS refused. Settings renders this inline while the
    /// previously working shortcut remains registered.
    shortcut_error: Mutex<Option<PetShortcutError>>,
    registered_shortcuts: Mutex<Vec<String>>,
    voice_shortcut_state: Mutex<VoiceShortcutState>,
    toggle_shortcut_down: Mutex<bool>,
    move_generation: Arc<AtomicU64>,
    /// Advances with every drag and every new window, so a stale drag's steps
    /// change nothing.
    drag_generation: AtomicU64,
    hit_regions: Mutex<Vec<HitRect>>,
    poll_generation: AtomicU64,
    poll_wake: Arc<tokio::sync::Notify>,
}

impl Pet {
    pub fn new(path: PathBuf) -> Self {
        Self {
            store: Mutex::new(PetStore::load(path)),
            window_origin: Mutex::new(None),
            frame: Mutex::new(Frame {
                expanded: false,
                state: FrameState::new(PetFrame::default()),
            }),
            shortcut_error: Mutex::new(None),
            registered_shortcuts: Mutex::new(Vec::new()),
            voice_shortcut_state: Mutex::new(VoiceShortcutState::default()),
            toggle_shortcut_down: Mutex::new(false),
            move_generation: Arc::new(AtomicU64::new(0)),
            drag_generation: AtomicU64::new(0),
            hit_regions: Mutex::new(Vec::new()),
            poll_generation: AtomicU64::new(0),
            poll_wake: Arc::new(tokio::sync::Notify::new()),
        }
    }

    fn store(&self) -> MutexGuard<'_, PetStore> {
        self.store.lock().unwrap_or_else(|poisoned| poisoned.into_inner())
    }

    pub fn enabled(&self) -> bool {
        self.store().enabled()
    }

    fn set_enabled(&self, enabled: bool) -> Result<(), String> {
        self.store().set_enabled(enabled).map_err(|error| error.to_string())?;
        self.reset_voice_shortcut_state();
        if !enabled {
            // Unregistering voice means the old key-up event can no longer arrive.
            *lock(&self.voice_shortcut_state) = VoiceShortcutState::default();
        }
        Ok(())
    }

    fn toggle_pressed(&self, event: ShortcutState) -> bool {
        let mut down = lock(&self.toggle_shortcut_down);
        let pressed = event == ShortcutState::Pressed;
        let fire = pressed && !*down;
        *down = pressed;
        fire
    }

    fn reset_voice_shortcut_state(&self) -> bool {
        lock(&self.voice_shortcut_state).reset()
    }

    fn page_loading(&self) {
        lock(&self.frame).state.page_loading();
        self.store().page_loading();
        lock(&self.hit_regions).clear();
    }

    fn window_gone(&self) {
        self.store().window_gone();
        lock(&self.hit_regions).clear();
    }
}

fn lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex.lock().unwrap_or_else(|poisoned| poisoned.into_inner())
}

pub struct PetMenus {
    toggle: CheckMenuItem<tauri::Wry>,
}

#[derive(Clone, Copy, Debug, Deserialize, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum PetShortcutTarget {
    Voice,
    Toggle,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct PetShortcutError {
    pub target: PetShortcutTarget,
    pub shortcut: String,
}

#[derive(Clone, Debug, Serialize)]
pub struct PetSettingsState {
    pub enabled: bool,
    pub voice_shortcut: String,
    pub toggle_shortcut: String,
    pub voice_mode: VoiceMode,
    pub error: Option<PetShortcutError>,
}

/// The tray's pet switch and one link into the authoritative Shortcut Settings
/// page, placed next to Notifications.
pub fn tray_items(app: &AppHandle) -> tauri::Result<(CheckMenuItem<tauri::Wry>, MenuItem<tauri::Wry>)> {
    let catalog = native_pet_catalog();
    let enabled = app.state::<Pet>().enabled();
    let toggle = CheckMenuItem::with_id(app, MENU_ID, &catalog.toggle, true, enabled, None::<&str>)?;
    let shortcut_settings = MenuItem::with_id(
        app,
        SHORTCUT_SETTINGS_MENU_ID,
        &catalog.shortcut_settings,
        true,
        None::<&str>,
    )?;
    app.manage(PetMenus { toggle: toggle.clone() });
    Ok((toggle, shortcut_settings))
}

fn refresh_menu(app: &AppHandle) {
    let Some(menus) = app.try_state::<PetMenus>() else {
        return;
    };
    let pet = app.state::<Pet>();
    let enabled = pet.enabled();
    let update = || -> tauri::Result<()> { menus.toggle.set_checked(enabled) };
    if update().is_err() {
        eprintln!("failed to refresh the desktop pet menu");
    }
}

fn settings_state(pet: &Pet) -> PetSettingsState {
    let store = pet.store();
    PetSettingsState {
        enabled: store.enabled(),
        voice_shortcut: store.shortcut().to_owned(),
        toggle_shortcut: store.toggle_shortcut().to_owned(),
        voice_mode: store.voice_mode(),
        error: lock(&pet.shortcut_error).clone(),
    }
}

fn dispatch_settings(app: &AppHandle, state: &PetSettingsState) {
    let Some(window) = app.get_webview_window(MAIN_WINDOW).filter(crate::pet::is_workbench) else {
        return;
    };
    dispatch(
        &window,
        SETTINGS_EVENT,
        &serde_json::to_value(state).expect("pet settings are serializable"),
    );
}

fn shortcut_matches(left: &str, right: &str) -> bool {
    match (left.parse::<Shortcut>(), right.parse::<Shortcut>()) {
        (Ok(left), Ok(right)) => left == right,
        _ => left.eq_ignore_ascii_case(right),
    }
}

fn parse_shortcut(shortcut: &str) -> Result<Shortcut, String> {
    shortcut.parse::<Shortcut>().map_err(|error| error.to_string())
}

fn validate_shortcut(shortcut: &str, other: &str) -> Result<(), String> {
    let candidate = parse_shortcut(shortcut)?;
    if !candidate
        .mods
        .intersects(Modifiers::CONTROL | Modifiers::ALT | Modifiers::SUPER | Modifiers::META)
    {
        return Err("A shortcut requires Control, Alt, or Command.".to_owned());
    }
    if candidate == parse_shortcut(other)? {
        return Err("Desktop Mini shortcuts must be different.".to_owned());
    }
    for reserved in ["Alt+KeyZ", "Alt+KeyX"] {
        if candidate == parse_shortcut(reserved).expect("reserved shortcuts are valid") {
            return Err("This shortcut is already used by Avibe.".to_owned());
        }
    }
    Ok(())
}

fn replace_registered_shortcuts<F, U>(
    current: &[String],
    desired: &[String],
    mut register: F,
    mut unregister: U,
) -> Result<Vec<String>, (String, String)>
where
    F: FnMut(&str) -> Result<(), String>,
    U: FnMut(&str) -> Result<(), String>,
{
    let mut added = Vec::new();
    let mut removed = Vec::new();
    let rollback = |added: &[String], removed: &[String], register: &mut F, unregister: &mut U| {
        for shortcut in added.iter().rev() {
            let _ = unregister(shortcut);
        }
        for shortcut in removed {
            let _ = register(shortcut);
        }
    };
    for shortcut in desired {
        if current.iter().any(|registered| shortcut_matches(registered, shortcut)) {
            continue;
        }
        if let Err(error) = register(shortcut) {
            rollback(&added, &removed, &mut register, &mut unregister);
            return Err((shortcut.clone(), error));
        }
        added.push(shortcut.clone());
    }
    for shortcut in current {
        if desired.iter().any(|candidate| shortcut_matches(candidate, shortcut)) {
            continue;
        }
        if let Err(error) = unregister(shortcut) {
            rollback(&added, &removed, &mut register, &mut unregister);
            return Err((shortcut.clone(), error));
        }
        removed.push(shortcut.clone());
    }
    Ok(desired.to_vec())
}

fn desired_shortcuts(state: &PetSettingsState) -> Vec<(PetShortcutTarget, String)> {
    let mut desired = vec![(PetShortcutTarget::Toggle, state.toggle_shortcut.clone())];
    if state.enabled {
        desired.push((PetShortcutTarget::Voice, state.voice_shortcut.clone()));
    }
    desired
}

/// Reconcile a registration set, preserving each failure's actual accelerator.
/// Startup may recover the unchanged show/hide binding independently; edits are
/// transactional and must never retain a partially accepted candidate.
fn update_registered_shortcuts<F, U>(
    current: &mut Vec<String>,
    desired: &[(PetShortcutTarget, String)],
    recover_toggle: bool,
    mut register: F,
    mut unregister: U,
) -> Result<(), PetShortcutError>
where
    F: FnMut(&str) -> Result<(), String>,
    U: FnMut(&str) -> Result<(), String>,
{
    let values = desired.iter().map(|(_, shortcut)| shortcut.clone()).collect::<Vec<_>>();
    match replace_registered_shortcuts(current, &values, &mut register, &mut unregister) {
        Ok(next) => {
            *current = next;
            Ok(())
        }
        Err((shortcut, reason)) => {
            let target = desired
                .iter()
                .find(|(_, value)| shortcut_matches(value, &shortcut))
                .map(|(target, _)| *target)
                .unwrap_or(PetShortcutTarget::Voice);
            eprintln!("Desktop Mini shortcut registration refused: {shortcut}: {reason}");
            if recover_toggle && target == PetShortcutTarget::Voice {
                if let Some((_, toggle)) = desired.iter().find(|(target, _)| *target == PetShortcutTarget::Toggle) {
                    if !current.iter().any(|value| shortcut_matches(value, toggle)) && register(toggle).is_ok() {
                        current.push(toggle.clone());
                    }
                }
            }
            Err(PetShortcutError { target, shortcut })
        }
    }
}

fn apply_desired_shortcuts(
    app: &AppHandle,
    desired: &[(PetShortcutTarget, String)],
    recover_toggle: bool,
) -> Result<(), PetShortcutError> {
    let pet = app.state::<Pet>();
    let shortcuts = app.global_shortcut();
    let mut current = lock(&pet.registered_shortcuts);
    update_registered_shortcuts(
        &mut current,
        desired,
        recover_toggle,
        |shortcut| shortcuts.register(shortcut).map_err(|error| error.to_string()),
        |shortcut| shortcuts.unregister(shortcut).map_err(|error| error.to_string()),
    )
}

/// Registers show/hide while Mini is off, and adds voice only while Mini is
/// on. A refused replacement leaves the previous registration active.
pub fn apply_shortcuts(app: &AppHandle) {
    let pet = app.state::<Pet>();
    let state = settings_state(&pet);
    *lock(&pet.shortcut_error) = None;
    if let Err(error) = apply_desired_shortcuts(app, &desired_shortcuts(&state), true) {
        eprintln!("A Desktop Mini shortcut could not be registered: {}", error.shortcut);
        *lock(&pet.shortcut_error) = Some(error);
    }
    refresh_menu(app);
    dispatch_settings(app, &settings_state(&pet));
}

fn set_enabled(app: &AppHandle, enabled: bool) -> Result<(), String> {
    let pet = app.state::<Pet>();
    pet.set_enabled(enabled)?;
    apply_shortcuts(app);
    if pet.enabled() {
        wake(app, PetIntent::Show);
    } else {
        reconcile(app);
    }
    Ok(())
}

/// The tray switch and the show/hide global shortcut share this writer.
pub fn toggle(app: &AppHandle) {
    if let Err(error) = set_enabled(app, !app.state::<Pet>().enabled()) {
        eprintln!("The desktop pet preference could not be saved: {error}");
    }
}

pub fn handle_toggle_shortcut(app: &AppHandle, event: ShortcutState) {
    if app.state::<Pet>().toggle_pressed(event) {
        toggle(app);
    }
}

pub fn handle_voice_shortcut(app: &AppHandle, event: ShortcutState) {
    let pet = app.state::<Pet>();
    let mode = pet.store().voice_mode();
    let event = match event {
        ShortcutState::Pressed => ShortcutEvent::Pressed,
        ShortcutState::Released => ShortcutEvent::Released,
    };
    let intent = { lock(&pet.voice_shortcut_state).event(mode, event) };
    if let Some(intent) = intent {
        wake(app, intent);
    }
}

pub fn handle_shortcut(app: &AppHandle, shortcut: &Shortcut, event: ShortcutState) {
    let pet = app.state::<Pet>();
    let (voice, toggle) = {
        let store = pet.store();
        (store.shortcut().to_owned(), store.toggle_shortcut().to_owned())
    };
    if parse_shortcut(&toggle).is_ok_and(|candidate| candidate == *shortcut) {
        handle_toggle_shortcut(app, event);
    } else if parse_shortcut(&voice).is_ok_and(|candidate| candidate == *shortcut) {
        handle_voice_shortcut(app, event);
    }
}

/// Brings the window in line with the switch and the Runtime the Workbench
/// shows. The Shell calls this whenever it records or clears that Runtime.
pub fn reconcile(app: &AppHandle) {
    let handle = app.clone();
    let _ = app.run_on_main_thread(move || {
        reconcile_now(&handle);
    });
}

/// What the window must become: `destroy` the existing one, then `build` one
/// for an origin.
#[derive(Debug, PartialEq, Eq)]
struct WindowChange {
    destroy: bool,
    build: Option<LoopbackOrigin>,
}

/// The window shows `/pet` from the Workbench's Runtime while the pet is on,
/// and does not exist otherwise. `existing` is `None` without a window, else
/// the origin that window was built for.
fn window_change(
    enabled: bool,
    workbench: Option<&LoopbackOrigin>,
    existing: Option<Option<&LoopbackOrigin>>,
) -> WindowChange {
    let desired = workbench.filter(|_| enabled);
    match existing {
        Some(built_for) if desired.is_some() && built_for == desired => WindowChange {
            destroy: false,
            build: None,
        },
        _ => WindowChange {
            destroy: existing.is_some(),
            build: desired.cloned(),
        },
    }
}

/// The only creator and destroyer of `pet`; see [`window_change`].
fn reconcile_now(app: &AppHandle) -> Option<WebviewWindow> {
    let (pet, shell) = (app.try_state::<Pet>()?, app.try_state::<Shell>()?);
    let workbench = lock(&shell.active_origin).clone();
    let built_for = lock(&pet.window_origin).clone();
    let window = app.get_webview_window(PET_WINDOW);
    let change = window_change(
        pet.enabled(),
        workbench.as_ref(),
        window.as_ref().map(|_| built_for.as_ref()),
    );
    match window {
        Some(window) if !change.destroy => return Some(window),
        Some(window) => {
            pet.poll_generation.fetch_add(1, Ordering::SeqCst);
            pet.poll_wake.notify_one();
            let _ = window.destroy();
            *lock(&pet.window_origin) = None;
            pet.window_gone();
        }
        None => {}
    }
    let origin = change.build?;
    // The navigation hook also judges the window's first load, so the origin
    // it may load is recorded before the webview starts loading it.
    *lock(&pet.window_origin) = Some(origin.clone());
    let window = build_window(app, &origin);
    if window.is_none() {
        *lock(&pet.window_origin) = None;
    }
    window
}

fn build_window(app: &AppHandle, origin: &LoopbackOrigin) -> Option<WebviewWindow> {
    let pet = app.state::<Pet>();
    let mut url = origin.navigation_url();
    url.set_path(PET_PATH);
    let scale = anchor_scale(app, None);
    let areas = work_areas(app, scale);
    let saved = pet.store().anchor().cloned();
    let anchor = initial_anchor(saved.as_ref(), &areas);
    let at = (anchor.x, anchor.y);
    let unit = area_for(&areas, at).map_or(1.0, |area| area.unit);
    let frame = frame_at(at, &areas);
    pet.drag_generation.fetch_add(1, Ordering::SeqCst);
    {
        let mut current = lock(&pet.frame);
        current.expanded = false;
        current.state.rebuild(frame);
    }
    let (width, height) = frame.size();
    let (x, y) = frame.origin(at, unit);
    pet.page_loading();
    let builder = WebviewWindowBuilder::new(app, PET_WINDOW, WebviewUrl::External(url))
        .initialization_script(DESKTOP_SHELL_MARKER)
        .initialization_script(PET_DRAG_MARKER)
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
        Ok(window) => {
            // Off macOS the builder's points use a scale this code does not
            // choose, so the window is put on the saved physical spot.
            if !cfg!(target_os = "macos") {
                let (x, y) = rescale((x, y), scale, 1.0);
                let _ = window.set_position(PhysicalPosition::new(x.round() as i32, y.round() as i32));
            }
            let _ = window.set_ignore_cursor_events(true);
            start_cursor_poll(app);
            Some(window)
        }
        Err(error) => {
            eprintln!("The desktop pet window could not be created: {error}");
            pet.window_gone();
            None
        }
    }
}

/// Both restored and first-use positions must remain reachable on short displays.
fn initial_anchor(saved: Option<&PetAnchor>, areas: &[WorkArea]) -> PetAnchor {
    match (saved, areas.first()) {
        (Some(saved), _) => clamp_anchor(saved, areas),
        (None, Some(primary)) => clamp_anchor(&default_anchor(primary), areas),
        (None, None) => PetAnchor {
            x: 0.0,
            y: 0.0,
            monitor: None,
        },
    }
}

/// Every way to summon the pet. The hotkey listens; everything else shows.
pub fn wake(app: &AppHandle, intent: PetIntent) {
    let handle = app.clone();
    let _ = app.run_on_main_thread(move || {
        let pet = handle.state::<Pet>();
        if !pet.enabled() {
            return;
        }
        let Some(window) = reconcile_now(&handle) else {
            // The Workbench has no Runtime yet; the summon waits for the
            // window that Runtime will bring.
            if intent != PetIntent::Release {
                pet.store().summon_before_window(intent);
            }
            return;
        };
        if intent != PetIntent::Release {
            let _ = window.show();
            pet.poll_wake.notify_one();
            let _ = window.set_focus();
        }
        let deliver = pet.store().summon(intent);
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
        pet.page_loading();
    }
}

/// An OS close request only hides the pet; the tray switch turns it off.
pub fn hide(app: &AppHandle) {
    if let Some(window) = app.get_webview_window(PET_WINDOW) {
        let _ = window.hide();
    }
}

/// Once a move settles: saves where the pet is and, unless the shell's own
/// drag is placing it, gives it the window for that spot (the platform moved
/// it after a display change, or an older page dragged it natively).
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
            follow_move(&handle, &window);
        });
    });
}

/// Puts the window where `place_moved` says for its spot now, through
/// `FrameState::follow`: the same layout applies at once, another one waits
/// for the page to draw it.
fn follow_move(app: &AppHandle, window: &WebviewWindow) {
    let pet = app.state::<Pet>();
    let current = {
        let frame = lock(&pet.frame);
        if frame.state.busy() {
            return;
        }
        frame.state.current()
    };
    let (Some(at), Ok(window_scale)) = (window_anchor(window, current), window.scale_factor()) else {
        return;
    };
    let scale = anchor_scale(app, Some(window_scale));
    let page_layouts = lock(&pet.frame).state.draws_layouts();
    let (anchor, next) = place_moved(
        rescale(at, window_scale, scale),
        &work_areas(app, scale),
        saved_monitor(app),
        page_layouts,
    );
    pet.store().set_anchor(PetAnchor {
        x: anchor.x.round(),
        y: anchor.y.round(),
        monitor: anchor.monitor.clone(),
    });
    let held = rescale((anchor.x, anchor.y), scale, window_scale);
    // A window this shell just placed comes back here: sub-point differences
    // are its own rounding, not a move.
    let close = |a: f64, b: f64| (a - b).abs() < 0.5;
    let moved = !close(held.0, at.0) || !close(held.1, at.1);
    let next = if close(next.height, current.height) {
        PetFrame {
            height: current.height,
            ..next
        }
    } else {
        next
    };
    let step = lock(&pet.frame).state.follow(next, held, moved);
    perform(app, window, step);
}

/// Carries out a `FrameStep`: moves the window now, or publishes the layout
/// to the document showing now, whichever document started the change. A
/// published frame applies on that revision's `pet_settle`, or on its own
/// after `SETTLE_TIMEOUT`.
fn perform(app: &AppHandle, window: &WebviewWindow, step: FrameStep) {
    match step {
        FrameStep::Keep => {}
        FrameStep::Apply { frame, anchor } => set_frame(window, frame.origin(anchor, 1.0), frame.size()),
        FrameStep::Publish { snapshot, publication } => {
            // An older page ignores the event; one still loading may already listen.
            dispatch(window, LAYOUT_EVENT, &serde_json::json!(snapshot));
            // The timer is this publication's: once the frame is published
            // again (the page reloaded), it no longer releases anything.
            let app = app.clone();
            tauri::async_runtime::spawn(async move {
                tokio::time::sleep(SETTLE_TIMEOUT).await;
                let _ = on_main_thread(&app, move |app| {
                    let Some(window) = app.get_webview_window(PET_WINDOW) else {
                        return;
                    };
                    let step = lock(&app.state::<Pet>().frame).state.expire(publication);
                    perform(app, &window, step);
                })
                .await;
            });
        }
    }
}

/// The display `pet.json` last placed the pet on.
fn saved_monitor(app: &AppHandle) -> Option<String> {
    app.state::<Pet>()
        .store()
        .anchor()
        .and_then(|anchor| anchor.monitor.clone())
}

/// The anchor of `window` drawn as `frame`, in the window's logical points.
fn window_anchor(window: &WebviewWindow, frame: PetFrame) -> Option<(f64, f64)> {
    let scale = window.scale_factor().ok()?;
    let position = window.outer_position().ok()?.to_logical::<f64>(scale);
    Some(frame.anchor((position.x, position.y), 1.0))
}

/// The scale `pet.json`'s anchor is stored in. macOS places every window in
/// one space of points, which the window's own scale reaches. Windows and Linux
/// place windows on one physical desktop, and the pet window's scale changes
/// as it crosses displays, so the anchor uses the primary display's scale both
/// when it is saved and when it is restored.
fn anchor_scale(app: &AppHandle, window_scale: Option<f64>) -> f64 {
    let primary = || {
        app.primary_monitor()
            .ok()
            .flatten()
            .map_or(1.0, |monitor| monitor.scale_factor())
    };
    match window_scale {
        Some(scale) if cfg!(target_os = "macos") => scale,
        _ => primary(),
    }
}

/// Each display's work area in points, the primary display first. Off macOS,
/// `scale` turns the one physical desktop into points.
fn work_areas(app: &AppHandle, scale: f64) -> Vec<WorkArea> {
    let primary = app
        .primary_monitor()
        .ok()
        .flatten()
        .and_then(|monitor| monitor.name().cloned());
    let mut areas: Vec<WorkArea> = app
        .available_monitors()
        .unwrap_or_default()
        .iter()
        .map(|monitor| logical_work_area(monitor, scale))
        .collect();
    areas.sort_by_key(|area| area.name != primary);
    areas
}

fn logical_work_area(monitor: &Monitor, scale: f64) -> WorkArea {
    // macOS reports each display in its own pixels and shares one space of
    // points; off macOS a display's points are its own scale over `scale`.
    let (scale, unit) = if cfg!(target_os = "macos") {
        (monitor.scale_factor(), 1.0)
    } else {
        (scale, monitor.scale_factor() / scale)
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
        unit,
    }
}

const UNAVAILABLE: &str = "This command is only available to the Avibe desktop pet.";

/// The second layer behind the `pet` capability: the caller is the pet window
/// showing its own route on the Runtime it was built for.
fn ensure_pet_page(window: &WebviewWindow) -> Result<(), String> {
    let pet = window.state::<Pet>();
    let url = window.url().map_err(|_| UNAVAILABLE.to_owned())?;
    if is_pet_caller(window.label(), lock(&pet.window_origin).as_ref(), &url) {
        Ok(())
    } else {
        Err(UNAVAILABLE.to_owned())
    }
}

fn is_pet_caller(label: &str, built_for: Option<&LoopbackOrigin>, url: &Url) -> bool {
    label == PET_WINDOW && is_pet_page(built_for, url)
}

/// The Workbench in `main`, on the Runtime the shell navigated it to.
pub(crate) fn is_workbench(window: &WebviewWindow) -> bool {
    let Some(shell) = window.try_state::<Shell>() else {
        return false;
    };
    let active = lock(&shell.active_origin).clone();
    window
        .url()
        .is_ok_and(|url| is_workbench_caller(window.label(), active.as_ref(), &url))
}

fn is_workbench_caller(label: &str, active: Option<&LoopbackOrigin>, url: &Url) -> bool {
    label == MAIN_WINDOW && active.is_some_and(|origin| origin.matches_url_origin(url))
}

fn ensure_workbench_page(window: &WebviewWindow) -> Result<(), String> {
    if is_workbench(window) {
        Ok(())
    } else {
        Err("This command is only available to the Avibe Workbench.".to_owned())
    }
}

#[tauri::command]
pub fn pet_settings(window: WebviewWindow) -> Result<PetSettingsState, String> {
    ensure_workbench_page(&window)?;
    Ok(settings_state(&window.state::<Pet>()))
}

#[tauri::command]
pub fn pet_set_enabled(window: WebviewWindow, enabled: bool) -> Result<PetSettingsState, String> {
    ensure_workbench_page(&window)?;
    set_enabled(window.app_handle(), enabled)?;
    Ok(settings_state(&window.state::<Pet>()))
}

#[tauri::command]
pub fn pet_set_shortcut(
    window: WebviewWindow,
    target: PetShortcutTarget,
    shortcut: String,
) -> Result<PetSettingsState, String> {
    ensure_workbench_page(&window)?;
    let app = window.app_handle();
    let pet = app.state::<Pet>();
    let before = settings_state(&pet);
    let other = match target {
        PetShortcutTarget::Voice => before.toggle_shortcut.as_str(),
        PetShortcutTarget::Toggle => before.voice_shortcut.as_str(),
    };
    validate_shortcut(&shortcut, other)?;
    parse_shortcut(&shortcut)?;

    let mut candidate = before.clone();
    match target {
        PetShortcutTarget::Voice => candidate.voice_shortcut = shortcut.clone(),
        PetShortcutTarget::Toggle => candidate.toggle_shortcut = shortcut.clone(),
    }
    save_shortcuts(app, &before, &candidate)
}

#[derive(Debug)]
enum ShortcutSaveError {
    Registration(PetShortcutError),
    Persistence(std::io::Error),
}

/// One transaction for both edits and reset. The complete pair is registered
/// before persistence, allowing even swapped shortcuts to return to defaults.
fn commit_shortcuts<F, U>(
    store: &mut PetStore,
    current: &mut Vec<String>,
    before: &PetSettingsState,
    candidate: &PetSettingsState,
    mut register: F,
    mut unregister: U,
) -> Result<(), ShortcutSaveError>
where
    F: FnMut(&str) -> Result<(), String>,
    U: FnMut(&str) -> Result<(), String>,
{
    let previous_registration = current.clone();
    let mut probe = candidate.clone();
    probe.enabled = candidate.enabled || !shortcut_matches(&before.voice_shortcut, &candidate.voice_shortcut);
    update_registered_shortcuts(
        current,
        &desired_shortcuts(&probe),
        false,
        &mut register,
        &mut unregister,
    )
    .map_err(ShortcutSaveError::Registration)?;
    let save = if let Err(error) = update_registered_shortcuts(
        current,
        &desired_shortcuts(candidate),
        false,
        &mut register,
        &mut unregister,
    ) {
        Err(ShortcutSaveError::Registration(error))
    } else {
        store
            .try_set_shortcuts(
                &candidate.voice_shortcut,
                &candidate.toggle_shortcut,
                candidate.voice_mode,
            )
            .map_err(ShortcutSaveError::Persistence)
    };
    if save.is_err() {
        // Restore exactly what was live, not a desired binding refused at startup.
        match replace_registered_shortcuts(current, &previous_registration, register, unregister) {
            Ok(restored) => *current = restored,
            Err((shortcut, reason)) => eprintln!("Desktop Mini shortcut rollback failed for {shortcut}: {reason}"),
        }
    }
    save
}

fn save_shortcuts(
    app: &AppHandle,
    before: &PetSettingsState,
    candidate: &PetSettingsState,
) -> Result<PetSettingsState, String> {
    let pet = app.state::<Pet>();
    let shortcuts = app.global_shortcut();
    let result = commit_shortcuts(
        &mut pet.store(),
        &mut lock(&pet.registered_shortcuts),
        before,
        candidate,
        |shortcut| shortcuts.register(shortcut).map_err(|error| error.to_string()),
        |shortcut| shortcuts.unregister(shortcut).map_err(|error| error.to_string()),
    );
    match result {
        Ok(()) => {
            let was_active = pet.reset_voice_shortcut_state();
            // Removed accelerators will never deliver their release event.
            if !shortcut_matches(&before.voice_shortcut, &candidate.voice_shortcut) {
                *lock(&pet.voice_shortcut_state) = VoiceShortcutState::default();
            }
            if !shortcut_matches(&before.toggle_shortcut, &candidate.toggle_shortcut) {
                *lock(&pet.toggle_shortcut_down) = false;
            }
            if was_active {
                wake(app, PetIntent::Release);
            }
            *lock(&pet.shortcut_error) = None;
        }
        Err(ShortcutSaveError::Registration(error)) => *lock(&pet.shortcut_error) = Some(error),
        Err(ShortcutSaveError::Persistence(error)) => {
            return Err(format!("The Desktop Mini shortcuts could not be saved: {error}"))
        }
    }
    refresh_menu(app);
    let state = settings_state(&pet);
    dispatch_settings(app, &state);
    Ok(state)
}

#[tauri::command]
pub fn pet_reset_shortcuts(window: WebviewWindow) -> Result<PetSettingsState, String> {
    ensure_workbench_page(&window)?;
    let app = window.app_handle();
    let before = settings_state(&app.state::<Pet>());
    let mut candidate = before.clone();
    candidate.voice_shortcut = DEFAULT_SHORTCUT.to_owned();
    candidate.toggle_shortcut = DEFAULT_TOGGLE_SHORTCUT.to_owned();
    candidate.voice_mode = VoiceMode::Hold;
    save_shortcuts(app, &before, &candidate)
}

#[tauri::command]
pub fn pet_set_voice_mode(window: WebviewWindow, mode: VoiceMode) -> Result<PetSettingsState, String> {
    ensure_workbench_page(&window)?;
    let app = window.app_handle();
    let pet = app.state::<Pet>();
    pet.store()
        .try_set_voice_mode(mode)
        .map_err(|error| format!("The Desktop Mini voice mode could not be saved: {error}"))?;
    if pet.reset_voice_shortcut_state() {
        wake(app, PetIntent::Release);
    }
    let state = settings_state(&pet);
    dispatch_settings(app, &state);
    Ok(state)
}

#[tauri::command]
pub fn pet_reset_voice_shortcut(window: WebviewWindow) -> Result<(), String> {
    ensure_pet_page(&window)?;
    window.state::<Pet>().reset_voice_shortcut_state();
    Ok(())
}

#[derive(Serialize)]
pub struct ReadyResult {
    #[serde(flatten)]
    ready: PetReady,
    /// The layout to draw and its revision (absent from older shells), the
    /// same snapshot the layout event carries.
    layout: PetLayout,
    layout_revision: u64,
}

/// Called once the `/pet` page has its listeners installed. `layouts` says
/// the page draws the top layout; a page that predates it (a shell can adopt
/// an older running Runtime) gets the upward window at the same spot.
#[tauri::command]
pub fn pet_ready(window: WebviewWindow, layouts: Option<bool>) -> Result<ReadyResult, String> {
    ensure_pet_page(&window)?;
    let pet = window.state::<Pet>();
    let kind = if layouts == Some(true) {
        PageKind::Layouts
    } else {
        PageKind::Legacy
    };
    page_ready(window.app_handle(), &window, kind);
    // A new document starts collapsed, so the frame does too.
    let expanded = lock(&pet.frame).expanded;
    if expanded {
        let _ = set_expanded(&window, false);
    }
    let ready = pet.store().page_ready();
    // A summon that waited for this page brings the window forward now; the
    // window was built without focus.
    if ready.summon_pending.is_some() {
        let _ = window.show();
        pet.poll_wake.notify_one();
        let _ = window.set_focus();
    }
    let snapshot = lock(&pet.frame).state.snapshot();
    Ok(ReadyResult {
        ready,
        layout: snapshot.layout,
        layout_revision: snapshot.revision,
    })
}

/// Records the page state without moving or resizing the fixed mini window.
#[tauri::command]
pub fn pet_set_expanded(window: WebviewWindow, expanded: bool) -> Result<PetLayout, String> {
    ensure_pet_page(&window)?;
    set_expanded(&window, expanded)
}

fn set_expanded(window: &WebviewWindow, expanded: bool) -> Result<PetLayout, String> {
    let pet = window.state::<Pet>();
    let mut frame = lock(&pet.frame);
    frame.expanded = expanded;
    // Older pages read the layout here; current ones take it only from the
    // revisioned snapshot.
    Ok(frame.state.snapshot().layout)
}

/// Drags the pet with the pointer (decision 2026-10-23) and returns once the
/// drop has been placed.
///
/// The window shrinks to the strip around Vibey and the dock for the drag, so
/// Vibey can reach the top of the work area (macOS keeps a window below the
/// menu bar), and both layouts draw Vibey at the same place in it. On release
/// the shell keeps the pet whole on a display, chooses the frame there and
/// saves the anchor. A page that draws layouts gets the drop's revisioned
/// snapshot through the layout event, and the full window comes back on that
/// revision's `pet_settle` (or after `SETTLE_TIMEOUT`); the reply carries no
/// layout, so the page has one source for it.
#[tauri::command]
pub async fn pet_drag(window: WebviewWindow) -> Result<(), String> {
    ensure_pet_page(&window)?;
    let app = window.app_handle().clone();
    let generation = on_main_thread(&app, {
        let window = window.clone();
        move |app| begin_drag(app, &window)
    })
    .await??;
    wait_for_release(&app, generation).await;
    let placed = on_main_thread(&app, {
        let window = window.clone();
        move |app| end_drag(app, &window, generation)
    })
    .await;
    // The one way a drag ends without a placement: whatever failed, it goes
    // back to the frame and anchor it started from (`FrameState::abort_drag`).
    if !matches!(placed, Ok(Ok(()))) {
        let _ = on_main_thread(&app, move |app| abandon_drag(app, generation)).await;
    }
    placed?
}

fn abandon_drag(app: &AppHandle, generation: u64) {
    let pet = app.state::<Pet>();
    if pet.drag_generation.load(Ordering::SeqCst) != generation {
        return;
    }
    let step = lock(&pet.frame).state.abort_drag();
    if let Some(window) = app.get_webview_window(PET_WINDOW) {
        perform(app, &window, step);
    }
}

/// The document said what it draws: the machine decides what that changes
/// for the window, given where the pet is now and the upward window there.
fn page_ready(app: &AppHandle, window: &WebviewWindow, kind: PageKind) {
    let pet = app.state::<Pet>();
    let current = lock(&pet.frame).state.current();
    let (Some(at), Ok(window_scale)) = (window_anchor(window, current), window.scale_factor()) else {
        lock(&pet.frame)
            .state
            .page_ready(kind, (0.0, 0.0), |_| PetFrame::default());
        return;
    };
    let scale = anchor_scale(app, Some(window_scale));
    let areas = work_areas(app, scale);
    let upward = |anchor: (f64, f64)| {
        let saved = rescale(anchor, window_scale, scale);
        area_for(&areas, saved).map_or_else(PetFrame::default, |area| upward_frame(saved, area))
    };
    let step = lock(&pet.frame).state.page_ready(kind, at, upward);
    perform(app, window, step);
}

/// The page drew the layout of `revision`. That revision's waiting frame (if
/// it is still the waiting one) comes back now; any other acknowledgement
/// changes nothing. Answers with the snapshot the page should now draw.
#[tauri::command]
pub fn pet_settle(window: WebviewWindow, revision: u64) -> Result<LayoutSnapshot, String> {
    ensure_pet_page(&window)?;
    let app = window.app_handle();
    let pet = window.state::<Pet>();
    let (step, snapshot) = {
        let mut frame = lock(&pet.frame);
        let step = frame.state.ack(revision);
        (step, frame.state.snapshot())
    };
    perform(app, &window, step);
    Ok(snapshot)
}

async fn on_main_thread<T: Send + 'static>(
    app: &AppHandle,
    work: impl FnOnce(&AppHandle) -> T + Send + 'static,
) -> Result<T, String> {
    let (send, receive) = tokio::sync::oneshot::channel();
    let handle = app.clone();
    app.run_on_main_thread(move || {
        let _ = send.send(work(&handle));
    })
    .map_err(|_| WINDOW_GONE.to_owned())?;
    receive.await.map_err(|_| WINDOW_GONE.to_owned())
}

fn begin_drag(app: &AppHandle, window: &WebviewWindow) -> Result<u64, String> {
    let pet = app.state::<Pet>();
    let mut frame = lock(&pet.frame);
    let anchor = window_anchor(window, frame.state.current()).ok_or(WINDOW_GONE)?;
    let Some(strip) = frame.state.begin_drag(anchor) else {
        // Placing it already, or its page has not said it draws the top
        // layout (it drags the window natively then).
        return Err("The desktop pet cannot be dragged by the shell now.".to_owned());
    };
    set_frame(window, strip.origin(anchor, 1.0), strip.size());
    let generation = pet.drag_generation.fetch_add(1, Ordering::SeqCst) + 1;
    drop(frame);
    if let Err(error) = start_drag(window) {
        // No drag will end: the drag finaliser puts the window back.
        let step = lock(&pet.frame).state.abort_drag();
        perform(app, window, step);
        return Err(error);
    }
    Ok(generation)
}

/// Until the primary button is up. Where the shell cannot read the button, a
/// drag is over once the window has stopped moving for `MOVE_SETTLE`.
async fn wait_for_release(app: &AppHandle, generation: u64) {
    let pet = app.state::<Pet>();
    let moves = pet.move_generation.clone();
    let mut last_move = (moves.load(Ordering::SeqCst), tokio::time::Instant::now());
    loop {
        tokio::time::sleep(DRAG_POLL).await;
        if pet.drag_generation.load(Ordering::SeqCst) != generation {
            return;
        }
        match on_main_thread(app, |_| primary_button_down()).await {
            Err(_) | Ok(Some(false)) => return,
            Ok(Some(true)) => {}
            Ok(None) => {
                let moved = moves.load(Ordering::SeqCst);
                if moved != last_move.0 {
                    last_move = (moved, tokio::time::Instant::now());
                } else if last_move.1.elapsed() >= MOVE_SETTLE {
                    return;
                }
            }
        }
    }
}

fn end_drag(app: &AppHandle, window: &WebviewWindow, generation: u64) -> Result<(), String> {
    let pet = app.state::<Pet>();
    if pet.drag_generation.load(Ordering::SeqCst) != generation {
        return Err(WINDOW_GONE.to_owned());
    }
    let strip = lock(&pet.frame).state.current();
    let (Some(dropped), Ok(window_scale)) = (window_anchor(window, strip), window.scale_factor()) else {
        return Err(WINDOW_GONE.to_owned());
    };
    let scale = anchor_scale(app, Some(window_scale));
    let saved = rescale(dropped, window_scale, scale);
    // The document may have reloaded during the drag: the machine knows what
    // the one showing now draws (or that it is still loading).
    let page_layouts = lock(&pet.frame).state.draws_layouts();
    // A drag never saves where it is, so the saved display is the one the pet
    // was on before it: a drop off every display goes back there.
    let (anchor, next) = place_moved(saved, &work_areas(app, scale), saved_monitor(app), page_layouts);
    let at = (anchor.x, anchor.y);
    pet.store().set_anchor(PetAnchor {
        x: anchor.x.round(),
        y: anchor.y.round(),
        monitor: anchor.monitor,
    });
    let held = rescale(at, scale, window_scale);
    if held != dropped {
        set_frame(window, strip.origin(held, 1.0), strip.size());
    }
    let step = lock(&pet.frame).state.end_drag(next, held);
    perform(app, window, step);
    Ok(())
}

#[tauri::command]
pub fn pet_hit_regions(window: WebviewWindow, rects: Vec<HitRect>) -> Result<(), String> {
    ensure_pet_page(&window)?;
    if rects.len() > 4096 || rects.iter().any(|rect| !rect.valid()) {
        return Err("Invalid pet hit regions.".to_owned());
    }
    *lock(&window.state::<Pet>().hit_regions) = rects;
    Ok(())
}

#[tauri::command]
pub fn pet_set_dismissed(window: WebviewWindow, session_id: String, ids: Vec<String>) -> Result<(), String> {
    ensure_pet_page(&window)?;
    window
        .state::<Pet>()
        .store()
        .set_dismissed_ids(&session_id, ids)
        .map_err(|_| "The dismissed pet cards could not be saved.".to_owned())
}

fn cursor_scale(app: &AppHandle, window_scale: f64) -> f64 {
    if cfg!(target_os = "macos") {
        app.primary_monitor()
            .ok()
            .flatten()
            .map_or(window_scale, |monitor| monitor.scale_factor())
    } else {
        window_scale
    }
}

#[derive(Clone, Copy, Debug)]
struct WindowCursor {
    x: f64,
    y: f64,
    in_window: bool,
}

/// Converts the cursor and window geometry to the same logical screen space.
/// On macOS the global cursor uses the primary display's scale, while tao
/// reports the window position and size at the window's current display scale.
fn cursor_in_window(
    cursor: PhysicalPosition<f64>,
    cursor_scale: f64,
    origin: PhysicalPosition<i32>,
    window_scale: f64,
    size: PhysicalSize<u32>,
) -> WindowCursor {
    let x = cursor.x / cursor_scale - f64::from(origin.x) / window_scale;
    let y = cursor.y / cursor_scale - f64::from(origin.y) / window_scale;
    let in_window =
        x >= 0.0 && y >= 0.0 && x < f64::from(size.width) / window_scale && y < f64::from(size.height) / window_scale;
    WindowCursor { x, y, in_window }
}

/// One poller per native window. All window calls and cursor-event toggles
/// happen on the main thread; a hidden window waits for an explicit wake.
fn start_cursor_poll(app: &AppHandle) {
    let pet = app.state::<Pet>();
    let generation = pet.poll_generation.fetch_add(1, Ordering::SeqCst) + 1;
    let wake = pet.poll_wake.clone();
    let app = app.clone();
    tauri::async_runtime::spawn(async move {
        let mut ignoring = true;
        // Hover the page cannot see itself: a webview in a window that is
        // not key gets no hover on macOS. Reported only when it changes.
        let mut hovered = false;
        loop {
            let handle = app.clone();
            let (send, receive) = tokio::sync::oneshot::channel();
            if app
                .run_on_main_thread(move || {
                    let pet = handle.state::<Pet>();
                    if pet.poll_generation.load(Ordering::SeqCst) != generation {
                        return;
                    }
                    let Some(window) = handle.get_webview_window(PET_WINDOW) else {
                        return;
                    };
                    if !window.is_visible().unwrap_or(false) {
                        if hovered {
                            dispatch(&window, HOVER_EVENT, &serde_json::json!({ "hover": false }));
                        }
                        let _ = send.send((None, ignoring, false));
                        return;
                    }
                    let (Ok(cursor), Ok(origin), Ok(window_scale), Ok(size)) = (
                        handle.cursor_position(),
                        window.outer_position(),
                        window.scale_factor(),
                        window.inner_size(),
                    ) else {
                        let _ = send.send((Some(100), ignoring, hovered));
                        return;
                    };
                    let pointer =
                        cursor_in_window(cursor, cursor_scale(&handle, window_scale), origin, window_scale, size);
                    let x = pointer.x;
                    let y = pointer.y;
                    let in_window = pointer.in_window;
                    let (next, hover) = {
                        let regions = lock(&pet.hit_regions);
                        (
                            !regions.iter().any(|rect| rect.contains(x, y)),
                            in_window && hovering(&regions, x, y),
                        )
                    };
                    if next != ignoring && window.set_ignore_cursor_events(next).is_ok() {
                        ignoring = next;
                    }
                    if hover != hovered {
                        dispatch(&window, HOVER_EVENT, &serde_json::json!({ "hover": hover }));
                    }
                    let _ = send.send((Some(if in_window { 16 } else { 100 }), ignoring, hover));
                })
                .is_err()
            {
                break;
            }
            let Ok((delay, applied, hover)) = receive.await else {
                break;
            };
            ignoring = applied;
            hovered = hover;
            if let Some(delay) = delay {
                tokio::time::sleep(Duration::from_millis(delay)).await;
            } else {
                wake.notified().await;
            }
        }
    });
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
    if link != crate::updater::OPEN_URL && parse_deep_link(&link).is_none() {
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
    use avibe_runtime_host::pet::{settle_frame, DRAG_HEIGHT, WINDOW_HEIGHT};
    use std::cell::RefCell;

    #[test]
    fn mixed_scale_cursor_hit_testing_uses_the_primary_scale_for_global_cursor() {
        // The primary display is 2x and the pet's secondary display is 1x.
        // tao's global cursor is in primary-display physical pixels, while
        // the window position and size use the window display's pixels.
        let cursor = PhysicalPosition::new(3400.0, 240.0);
        let origin = PhysicalPosition::new(1512, 100);
        let size = PhysicalSize::new(460, 600);

        let fixed = cursor_in_window(cursor, 2.0, origin, 1.0, size);
        assert_eq!((fixed.x, fixed.y), (188.0, 20.0));
        assert!(fixed.in_window);
        assert!(HitRect {
            x: 150.0,
            y: 0.0,
            width: 100.0,
            height: 100.0,
            hover: false,
        }
        .contains(fixed.x, fixed.y));

        // Using the window scale for both values reproduces the lost-input
        // bug: the same physical cursor is incorrectly placed outside.
        let broken = cursor_in_window(cursor, 1.0, origin, 1.0, size);
        assert!(!broken.in_window);
        assert!(!HitRect {
            x: 150.0,
            y: 0.0,
            width: 100.0,
            height: 100.0,
            hover: false,
        }
        .contains(broken.x, broken.y));
    }

    #[test]
    fn window_build_placement_keeps_the_image_and_dock_reachable_on_small_displays() {
        let area = |name: &str, x, y, width, height, unit| WorkArea {
            name: Some(name.into()),
            area: LogicalRect { x, y, width, height },
            unit,
        };
        let detached = PetAnchor {
            x: 9000.0,
            y: 9000.0,
            monitor: Some("detached".into()),
        };
        let primary_125 = area("primary", 0.0, 0.0, 1366.0 / 1.25, 718.0 / 1.25, 1.0);
        let side_150 = area("side", -1200.0, -180.0, 1200.0, 660.0, 1.5 / 1.25);
        let cases = [
            (
                "detached display",
                Some(detached.clone()),
                vec![area("primary", 0.0, 0.0, 800.0, 550.0, 1.0)],
                1.0,
                "primary",
            ),
            (
                "125% primary",
                Some(detached.clone()),
                vec![primary_125.clone()],
                1.25,
                "primary",
            ),
            (
                "small laptop with full-size external connected",
                Some(PetAnchor {
                    x: 100.0,
                    y: 500.0,
                    monitor: Some("primary".into()),
                }),
                vec![
                    primary_125.clone(),
                    area("external", 1366.0 / 1.25, 0.0, 1920.0, 1080.0, 1.2),
                ],
                1.25,
                "primary",
            ),
            (
                "mixed DPI with negative origin",
                Some(PetAnchor {
                    monitor: Some("side".into()),
                    ..detached.clone()
                }),
                vec![primary_125, side_150],
                1.25,
                "side",
            ),
            (
                "negative primary",
                Some(detached),
                vec![area("primary", -800.0, -550.0, 800.0, 550.0, 1.0)],
                1.0,
                "primary",
            ),
            (
                "first use on narrow display",
                None,
                vec![area("primary", 0.0, 0.0, 200.0, 500.0, 1.0)],
                1.0,
                "primary",
            ),
        ];
        for (case, saved, areas, primary_scale, expected_display) in cases {
            // This is build_window's actual anchor selection and coordinate
            // conversion, including the target monitor's own logical scale.
            let anchor = initial_anchor(saved.as_ref(), &areas);
            let target = area_for(&areas, (anchor.x, anchor.y)).unwrap();
            assert_eq!(target.name.as_deref(), Some(expected_display), "{case}");
            let at = (anchor.x, anchor.y);
            let frame = settle_frame(at, target);
            let origin = frame.origin(at, target.unit);
            let (dx, dy) = frame.pet_offset();
            let image = (origin.0 + dx * target.unit, origin.1 + dy * target.unit);
            let physical_image = rescale(image, primary_scale, 1.0);
            let physical_unit = target.unit * primary_scale;
            let physical_area = LogicalRect {
                x: target.area.x * primary_scale,
                y: target.area.y * primary_scale,
                width: target.area.width * primary_scale,
                height: target.area.height * primary_scale,
            };
            // CSS: 115px image; dock top 103px, height 40px, centred and
            // narrower than the image. Both rects must fit in physical pixels.
            assert!(physical_image.0 >= physical_area.x - 1e-9, "{case}");
            assert!(physical_image.1 >= physical_area.y - 1e-9, "{case}");
            assert!(
                physical_image.0 + 115.0 * physical_unit <= physical_area.x + physical_area.width + 1e-9,
                "{case}"
            );
            assert!(
                physical_image.1 + 143.0 * physical_unit <= physical_area.y + physical_area.height + 1e-9,
                "{case}"
            );
            // The window reaches from Vibey toward its column no further than
            // the work area, above or below, whichever way the column opens.
            let window = target.window_rect(at, frame);
            assert!(window.y >= target.area.y - 1e-9, "{case}");
            assert!(
                window.y + window.height <= target.area.y + target.area.height + 1e-9,
                "{case}"
            );
            assert!((DRAG_HEIGHT..=WINDOW_HEIGHT).contains(&frame.height), "{case}");
            assert_eq!(frame.size().0, 460.0);
        }
        assert_eq!(
            initial_anchor(None, &[]),
            PetAnchor {
                x: 0.0,
                y: 0.0,
                monitor: None
            }
        );
    }

    #[test]
    fn document_and_window_lifetimes_release_input_regions_and_fence_summons() {
        let directory = tempfile::tempdir().unwrap();
        let pet = Pet::new(directory.path().join("pet.json"));
        let seed = || {
            pet.store().page_ready();
            lock(&pet.hit_regions).push(HitRect {
                x: 10.0,
                y: 20.0,
                width: 100.0,
                height: 50.0,
                hover: false,
            });
        };
        seed();
        pet.page_loading();
        assert!(lock(&pet.hit_regions).is_empty());
        assert!(!pet.store().page_is_ready());
        assert_eq!(pet.store().summon(PetIntent::Show), None);
        // A reload/build keeps a queued summon until the replacement is ready.
        pet.page_loading();
        assert_eq!(pet.store().page_ready().summon_pending.unwrap().intent, PetIntent::Show);

        seed();
        pet.store().page_loading();
        assert_eq!(pet.store().summon(PetIntent::Listen), None);
        pet.window_gone();
        assert!(lock(&pet.hit_regions).is_empty());
        assert!(!pet.store().page_is_ready());
        assert!(pet.store().page_ready().summon_pending.is_none());
        // A fresh document may register its own regions after the old ones go.
        seed();
        assert!(lock(&pet.hit_regions)[0].contains(50.0, 40.0));
    }

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
            assert!(!catalog.shortcut_settings.is_empty());
        }
    }

    fn origin(raw: &str) -> LoopbackOrigin {
        LoopbackOrigin::parse(raw).unwrap()
    }

    #[test]
    fn the_window_follows_the_switch_and_the_runtime_the_workbench_shows() {
        let (a, b) = (origin("http://127.0.0.1:5123"), origin("http://127.0.0.1:6174"));
        let change = |destroy, build: Option<&LoopbackOrigin>| WindowChange {
            destroy,
            build: build.cloned(),
        };
        // The Workbench reaches a Runtime: the pet appears there.
        assert_eq!(window_change(true, Some(&a), None), change(false, Some(&a)));
        assert_eq!(window_change(true, Some(&a), Some(Some(&a))), change(false, None));
        // The Runtime moves: the old window goes and one is built for the new origin.
        assert_eq!(window_change(true, Some(&b), Some(Some(&a))), change(true, Some(&b)));
        // The Workbench no longer shows a Runtime (stopped, lost, or `main`
        // recreated): the pet goes with it.
        assert_eq!(window_change(true, None, Some(Some(&a))), change(true, None));
        assert_eq!(window_change(true, None, None), change(false, None));
        // The switch is off: no window, whatever the Workbench shows.
        assert_eq!(window_change(false, Some(&a), Some(Some(&a))), change(true, None));
        assert_eq!(window_change(false, Some(&a), None), change(false, None));
    }

    #[test]
    fn pet_commands_answer_only_the_pet_page_and_the_workbench() {
        let (a, b) = (origin("http://127.0.0.1:5123"), origin("http://127.0.0.1:6174"));
        let url = |raw: &str| Url::parse(raw).unwrap();
        assert!(is_pet_caller(PET_WINDOW, Some(&a), &url("http://127.0.0.1:5123/pet")));
        assert!(!is_pet_caller(MAIN_WINDOW, Some(&a), &url("http://127.0.0.1:5123/pet")));
        assert!(!is_pet_caller(
            PET_WINDOW,
            Some(&a),
            &url("http://127.0.0.1:5123/chat/abc")
        ));
        assert!(!is_pet_caller(PET_WINDOW, Some(&b), &url("http://127.0.0.1:5123/pet")));
        assert!(!is_pet_caller(PET_WINDOW, None, &url("http://127.0.0.1:5123/pet")));

        assert!(is_workbench_caller(
            MAIN_WINDOW,
            Some(&a),
            &url("http://127.0.0.1:5123/chat/abc")
        ));
        assert!(!is_workbench_caller(
            PET_WINDOW,
            Some(&a),
            &url("http://127.0.0.1:5123/pet")
        ));
        assert!(!is_workbench_caller(
            MAIN_WINDOW,
            Some(&b),
            &url("http://127.0.0.1:5123/")
        ));
        assert!(!is_workbench_caller(MAIN_WINDOW, None, &url("http://127.0.0.1:5123/")));
        assert!(!is_workbench_caller(MAIN_WINDOW, Some(&a), &url("tauri://localhost/")));
    }

    struct TestPetDirectory(PathBuf);
    impl TestPetDirectory {
        fn new() -> Self {
            static NEXT: AtomicU64 = AtomicU64::new(0);
            let path = std::env::temp_dir().join(format!(
                "avibe-pet-settings-{}-{}",
                std::process::id(),
                NEXT.fetch_add(1, Ordering::SeqCst)
            ));
            std::fs::create_dir(&path).unwrap();
            Self(path)
        }
        fn file(&self) -> PathBuf {
            self.0.join("pet.json")
        }
    }
    impl Drop for TestPetDirectory {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    #[test]
    fn show_hide_changes_once_per_physical_press_even_across_settings_writes() {
        let directory = TestPetDirectory::new();
        let pet = Pet::new(directory.file());
        for expected in [true, false, true] {
            assert!(pet.toggle_pressed(ShortcutState::Pressed));
            pet.set_enabled(!pet.enabled()).unwrap();
            assert_eq!(pet.enabled(), expected);
            for _ in 0..5 {
                assert!(!pet.toggle_pressed(ShortcutState::Pressed));
                pet.reset_voice_shortcut_state();
                assert_eq!(pet.enabled(), expected);
            }
            assert!(!pet.toggle_pressed(ShortcutState::Released));
        }
        assert_eq!(PetStore::load(directory.file()).enabled(), pet.enabled());
    }

    #[test]
    fn hiding_while_voice_is_held_does_not_leave_a_latch_after_showing_again() {
        let directory = TestPetDirectory::new();
        let pet = Pet::new(directory.file());
        pet.set_enabled(true).unwrap();
        assert_eq!(
            lock(&pet.voice_shortcut_state).event(VoiceMode::Hold, ShortcutEvent::Pressed),
            Some(PetIntent::Listen)
        );
        pet.set_enabled(false).unwrap();
        pet.set_enabled(true).unwrap();
        assert_eq!(
            lock(&pet.voice_shortcut_state).event(VoiceMode::Hold, ShortcutEvent::Pressed),
            Some(PetIntent::Listen)
        );
    }

    #[test]
    fn startup_voice_refusal_retains_toggle_and_names_the_failed_voice() {
        let mut current = Vec::new();
        let desired = vec![
            (PetShortcutTarget::Toggle, DEFAULT_TOGGLE_SHORTCUT.to_owned()),
            (PetShortcutTarget::Voice, DEFAULT_SHORTCUT.to_owned()),
        ];
        let registered = RefCell::new(Vec::<String>::new());
        let result = update_registered_shortcuts(
            &mut current,
            &desired,
            true,
            |shortcut| {
                if shortcut == DEFAULT_SHORTCUT {
                    return Err("OS refused voice".to_owned());
                }
                registered.borrow_mut().push(shortcut.to_owned());
                Ok(())
            },
            |shortcut| {
                registered.borrow_mut().retain(|value| value != shortcut);
                Ok(())
            },
        );
        assert_eq!(
            result,
            Err(PetShortcutError {
                target: PetShortcutTarget::Voice,
                shortcut: DEFAULT_SHORTCUT.to_owned()
            })
        );
        assert_eq!(current, vec![DEFAULT_TOGGLE_SHORTCUT]);
        assert_eq!(*registered.borrow(), current);
    }

    #[test]
    fn reset_swapped_shortcuts_commits_the_pair_and_preserves_other_preferences() {
        for enabled in [true, false] {
            let directory = TestPetDirectory::new();
            let pet = Pet::new(directory.file());
            pet.set_enabled(enabled).unwrap();
            pet.store().bind("session-kept");
            pet.store()
                .try_set_shortcuts(DEFAULT_TOGGLE_SHORTCUT, DEFAULT_SHORTCUT, VoiceMode::Toggle)
                .unwrap();
            let before = settings_state(&pet);
            let mut candidate = before.clone();
            candidate.voice_shortcut = DEFAULT_SHORTCUT.to_owned();
            candidate.toggle_shortcut = DEFAULT_TOGGLE_SHORTCUT.to_owned();
            candidate.voice_mode = VoiceMode::Hold;
            let mut current = desired_shortcuts(&before)
                .into_iter()
                .map(|(_, value)| value)
                .collect::<Vec<_>>();
            let registered = RefCell::new(current.clone());
            commit_shortcuts(
                &mut pet.store(),
                &mut current,
                &before,
                &candidate,
                |shortcut| {
                    assert!(
                        !registered
                            .borrow()
                            .iter()
                            .any(|value| shortcut_matches(value, shortcut)),
                        "must not re-register an owned key"
                    );
                    registered.borrow_mut().push(shortcut.to_owned());
                    Ok(())
                },
                |shortcut| {
                    registered.borrow_mut().retain(|value| value != shortcut);
                    Ok(())
                },
            )
            .unwrap();
            let restored = PetStore::load(directory.file());
            assert_eq!(restored.shortcut(), DEFAULT_SHORTCUT);
            assert_eq!(restored.toggle_shortcut(), DEFAULT_TOGGLE_SHORTCUT);
            assert_eq!(restored.voice_mode(), VoiceMode::Hold);
            assert_eq!(restored.enabled(), enabled);
            assert_eq!(restored.binding().binding.as_deref(), Some("session-kept"));
            assert_eq!(
                current,
                desired_shortcuts(&candidate)
                    .into_iter()
                    .map(|(_, value)| value)
                    .collect::<Vec<_>>()
            );
            let mut actual = registered.borrow().clone();
            actual.sort();
            current.sort();
            assert_eq!(actual, current);
        }
    }

    #[test]
    fn refused_edits_and_failed_persistence_keep_the_saved_and_live_pair() {
        for enabled in [true, false] {
            for disk_failure in [true, false] {
                let directory = TestPetDirectory::new();
                let pet = Pet::new(directory.file());
                pet.set_enabled(enabled).unwrap();
                let before = settings_state(&pet);
                let mut candidate = before.clone();
                candidate.voice_shortcut = "Control+Alt+KeyQ".to_owned();
                let saved = std::fs::read(directory.file()).unwrap();
                if disk_failure {
                    std::fs::create_dir(directory.file().with_extension("tmp")).unwrap();
                }
                let mut current = desired_shortcuts(&before)
                    .into_iter()
                    .map(|(_, value)| value)
                    .collect::<Vec<_>>();
                let original = current.clone();
                let registered = RefCell::new(current.clone());
                let result = commit_shortcuts(
                    &mut pet.store(),
                    &mut current,
                    &before,
                    &candidate,
                    |shortcut| {
                        if !disk_failure && shortcut == candidate.voice_shortcut {
                            return Err("refused".to_owned());
                        }
                        registered.borrow_mut().push(shortcut.to_owned());
                        Ok(())
                    },
                    |shortcut| {
                        registered.borrow_mut().retain(|value| value != shortcut);
                        Ok(())
                    },
                );
                assert!(result.is_err());
                assert_eq!(pet.store().shortcut(), before.voice_shortcut);
                assert_eq!(std::fs::read(directory.file()).unwrap(), saved);
                assert_eq!(current, original);
                assert_eq!(*registered.borrow(), original);
            }
        }
    }

    #[test]
    fn native_shortcut_validation_requires_a_modifier_and_distinct_bindings() {
        for invalid in ["Space", "Shift+Space", "Alt+KeyZ", "Alt+KeyX", "Control+Alt+KeyM"] {
            assert!(
                validate_shortcut(invalid, DEFAULT_TOGGLE_SHORTCUT).is_err(),
                "{invalid}"
            );
        }
        assert!(validate_shortcut(DEFAULT_SHORTCUT, DEFAULT_TOGGLE_SHORTCUT).is_ok());
    }

    #[test]
    fn disabled_pets_keep_only_the_show_hide_shortcut_registered() {
        let state = PetSettingsState {
            enabled: false,
            voice_shortcut: "Control+Alt+Space".to_owned(),
            toggle_shortcut: "Control+Alt+KeyM".to_owned(),
            voice_mode: VoiceMode::Hold,
            error: None,
        };
        assert_eq!(
            desired_shortcuts(&state),
            vec![(PetShortcutTarget::Toggle, "Control+Alt+KeyM".to_owned())]
        );
    }

    #[test]
    fn replacing_a_shortcut_rolls_back_when_registration_is_refused() {
        let current = vec!["Control+Alt+KeyM".to_owned(), "Control+Alt+Space".to_owned()];
        let desired = vec!["Control+Alt+KeyM".to_owned(), "Control+Alt+KeyK".to_owned()];
        let registered = RefCell::new(current.clone());
        let result = replace_registered_shortcuts(
            &current,
            &desired,
            |shortcut| {
                if shortcut == "Control+Alt+KeyK" {
                    return Err("refused".to_owned());
                }
                registered.borrow_mut().push(shortcut.to_owned());
                Ok(())
            },
            |shortcut| {
                registered.borrow_mut().retain(|value| value != shortcut);
                Ok(())
            },
        );
        assert_eq!(result, Err(("Control+Alt+KeyK".to_owned(), "refused".to_owned())));
        assert_eq!(*registered.borrow(), current);
    }

    #[test]
    fn replacing_a_shortcut_registers_before_removing_the_old_binding() {
        let current = vec!["Control+Alt+KeyM".to_owned(), "Control+Alt+Space".to_owned()];
        let desired = vec!["Control+Alt+KeyM".to_owned(), "Control+Alt+KeyK".to_owned()];
        let registered = RefCell::new(current.clone());
        let result = replace_registered_shortcuts(
            &current,
            &desired,
            |shortcut| {
                registered.borrow_mut().push(shortcut.to_owned());
                Ok(())
            },
            |shortcut| {
                registered.borrow_mut().retain(|value| value != shortcut);
                Ok(())
            },
        );
        assert_eq!(result, Ok(desired.clone()));
        assert_eq!(*registered.borrow(), vec!["Control+Alt+KeyM", "Control+Alt+KeyK"]);
    }

    #[test]
    fn the_page_listens_for_the_events_and_marker_the_shell_sends() {
        let ui = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../ui/src");
        let read = |path: &str| std::fs::read_to_string(ui.join(path)).unwrap();
        let bridge = read("pet/petBridge.ts");
        for event in [SUMMON_EVENT, BOUND_EVENT] {
            assert!(
                bridge.contains(&format!("'{event}'")),
                "the pet page must listen for {event}"
            );
        }
        assert!(DESKTOP_PET_MARKER.starts_with("if (window.self === window.top)"));
        assert!(DESKTOP_PET_MARKER.contains("'__AVIBE_DESKTOP_PET__'"));
        assert!(DESKTOP_PET_SETTINGS_MARKER.contains("'__AVIBE_DESKTOP_PET_SETTINGS__'"));
        assert!(read("lib/desktopShell.ts").contains("window.__AVIBE_DESKTOP_PET__ === true"));
        assert!(read("lib/desktopShell.ts").contains("window.__AVIBE_DESKTOP_PET_SETTINGS__ === true"));
    }
}
