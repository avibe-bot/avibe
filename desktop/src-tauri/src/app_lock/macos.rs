//! AppKit presentation and LocalAuthentication. All Cocoa state stays on the
//! main thread; the managed machine is the entry gate shared by every caller.
use super::*;
use block2::RcBlock;
use objc2::rc::{Retained, Weak};
use objc2::runtime::{AnyObject, Bool};
use objc2::{define_class, msg_send, sel, ClassType, DefinedClass, MainThreadMarker, MainThreadOnly};
use objc2_app_kit::*;
use objc2_foundation::{
    NSAttributedString, NSData, NSDistributedNotificationCenter, NSError, NSNotification, NSNotificationCenter,
    NSObjectProtocol, NSPoint, NSRect, NSSize, NSString,
};
use objc2_local_authentication::{LABiometryType, LAContext, LAPolicy};
use std::cell::{Cell, RefCell};
use std::path::PathBuf;
use std::ptr::NonNull;
use std::sync::Mutex;
use tauri::{AppHandle, Manager, Webview};

type PendingCallback = Box<dyn FnOnce(&AppHandle) + Send>;
struct PendingAction {
    execute: PendingCallback,
    replaced: Option<PendingCallback>,
}
type Entry = PendingEntry<PendingAction>;
type Observer = (
    Retained<NSNotificationCenter>,
    Retained<objc2::runtime::ProtocolObject<dyn NSObjectProtocol>>,
);
type Reply = tokio::sync::oneshot::Sender<serde_json::Value>;
struct Owner {
    machine: Mutex<Machine>,
    pending: Mutex<Option<Entry>>,
    path: PathBuf,
    dirty: std::sync::atomic::AtomicBool,
}
struct Cover {
    window: Weak<NSWindow>,
    view: Retained<LockView>,
    hidden: Vec<Weak<NSView>>,
    responder: Option<Weak<NSResponder>>,
    title: Retained<NSString>,
}
#[derive(Default)]
struct Presentation {
    app: Option<AppHandle>,
    covers: Vec<Cover>,
    hidden_windows: Vec<Weak<NSWindow>>,
    observers: Vec<Observer>,
    context: Option<Retained<LAContext>>,
    reply: Option<Reply>,
    quit_window: Option<Weak<NSWindow>>,
    quit_alert: Option<Retained<NSAlert>>,
}
thread_local! {
    static PRESENTATION: RefCell<Presentation> = RefCell::new(Presentation::default());
    static LAST_AVAILABLE: Cell<Option<bool>> = const { Cell::new(None) };
    static PAINTING: Cell<bool> = const { Cell::new(false) };
    // Kept outside the presentation borrow: hiding a view synchronously calls
    // Vault's lifetime checks, which need to recognize this exact temporary hide.
    static HIDDEN: RefCell<Vec<Weak<NSView>>> = const { RefCell::new(Vec::new()) };
}
fn machine(app: &AppHandle) -> tauri::State<'_, Owner> {
    app.state::<Owner>()
}
fn now() -> f64 {
    #[cfg(feature = "app-lock-e2e")]
    if let Some(time) = e2e::time() {
        return time;
    }
    #[repr(C)]
    struct Timebase {
        numer: u32,
        denom: u32,
    }
    extern "C" {
        fn mach_continuous_time() -> u64;
        fn mach_timebase_info(info: *mut Timebase) -> i32;
    }
    static SCALE: std::sync::OnceLock<f64> = std::sync::OnceLock::new();
    let scale = *SCALE.get_or_init(|| {
        let mut info = Timebase { numer: 0, denom: 0 };
        // SAFETY: Darwin writes this two-u32 structure. The continuous clock
        // includes suspended time, unlike std::time::Instant on macOS.
        assert_eq!(unsafe { mach_timebase_info(&mut info) }, 0);
        f64::from(info.numer) / f64::from(info.denom) / 1_000_000_000.0
    });
    unsafe { mach_continuous_time() as f64 * scale }
}
fn active() -> bool {
    #[cfg(feature = "app-lock-e2e")]
    if let Some(active) = e2e::active() {
        return active;
    }
    NSApplication::sharedApplication(MainThreadMarker::new().expect("AppKit main thread")).isActive()
}
fn availability() -> Availability {
    #[cfg(feature = "app-lock-e2e")]
    if let Some(available) = e2e::availability() {
        return available;
    }
    let context = unsafe { LAContext::new() };
    let error = unsafe { context.canEvaluatePolicy_error(LAPolicy::DeviceOwnerAuthentication) }
        .err()
        .map(|e| e.code());
    let available = LAST_AVAILABLE.with(|last| {
        let value = password_available(error, last.get());
        last.set(Some(value));
        value
    });
    let biometric = unsafe { context.canEvaluatePolicy_error(LAPolicy::DeviceOwnerAuthenticationWithBiometrics) };
    let biometry = if unsafe { context.biometryType() } == LABiometryType::TouchID {
        Biometry::TouchId
    } else {
        Biometry::None
    };
    let biometry_state = match biometric {
        Ok(()) => BiometryState::Usable,
        Err(error) => match error.code() {
            -7 => BiometryState::NotEnrolled,
            -8 => BiometryState::Lockout,
            _ => BiometryState::Unavailable,
        },
    };
    Availability {
        available,
        biometry,
        biometry_state,
    }
}
fn status(app: &AppHandle) -> Status {
    machine(app).machine.lock().expect("app lock").status()
}
pub(crate) fn is_locked(app: &AppHandle) -> bool {
    app.try_state::<Owner>().is_some_and(|owner| {
        let mut state = owner.machine.lock().expect("app lock");
        let before = (state.locked, state.busy());
        state.tick(now());
        if before != (state.locked, state.busy()) {
            owner.dirty.store(true, std::sync::atomic::Ordering::Relaxed);
        }
        state.locked
    })
}
fn result(app: &AppHandle, reason: Option<&str>) -> serde_json::Value {
    match reason {
        None => serde_json::json!({"ok":true,"status":status(app)}),
        Some(reason) => serde_json::json!({"ok":false,"reason":reason,"status":status(app)}),
    }
}
fn refresh(app: &AppHandle) {
    if machine(app).machine.lock().expect("app lock").busy() {
        return;
    }
    machine(app).machine.lock().expect("app lock").refresh(availability());
}
fn catalog(key: &str) -> String {
    // Both native and Web copy comes from the same shipped product catalog.
    static CATALOG: std::sync::OnceLock<serde_json::Value> = std::sync::OnceLock::new();
    CATALOG
        .get_or_init(|| {
            let source = if sys_locale::get_locale().is_some_and(|s| s.starts_with("zh")) {
                crate::ZH_PRODUCT_CATALOG
            } else {
                crate::EN_PRODUCT_CATALOG
            };
            serde_json::from_str::<serde_json::Value>(source).expect("shipped product catalog")["appLock"]["lock"]
                .clone()
        })
        .get(key)
        .and_then(|value| value.as_str())
        .unwrap_or(key)
        .to_owned()
}
fn persist(path: &std::path::Path, policy: Policy) -> Result<(), String> {
    use std::io::Write;
    let parent = path.parent().ok_or("missing App Lock preference directory")?;
    std::fs::create_dir_all(parent).map_err(|e| e.to_string())?;
    let mut file = tempfile::NamedTempFile::new_in(parent).map_err(|e| e.to_string())?;
    serde_json::to_writer(&mut file, &policy).map_err(|e| e.to_string())?;
    file.flush().map_err(|e| e.to_string())?;
    file.persist(path).map_err(|e| e.to_string())?;
    Ok(())
}

/// Called at the native entry, before any side effect. The closure contains
/// intent data only; it never runs while locked and is never written to disk.
pub(crate) fn defer(app: &AppHandle, action: impl FnOnce(&AppHandle) + Send + 'static) -> bool {
    defer_kind(app, EntryKind::Action, action)
}
pub(crate) fn defer_kind(app: &AppHandle, kind: EntryKind, action: impl FnOnce(&AppHandle) + Send + 'static) -> bool {
    defer_action(
        app,
        kind,
        PendingAction {
            execute: Box::new(action),
            replaced: None,
        },
    )
}
/// Replacement is an explicit queue transition, not a Drop side effect:
/// replay, rejected foreground requests and process exit never report failure.
pub(crate) fn defer_with_replacement(
    app: &AppHandle,
    action: impl FnOnce(&AppHandle) + Send + 'static,
    replaced: impl FnOnce(&AppHandle) + Send + 'static,
) -> bool {
    defer_action(
        app,
        EntryKind::Command,
        PendingAction {
            execute: Box::new(action),
            replaced: Some(Box::new(replaced)),
        },
    )
}
fn defer_action(app: &AppHandle, kind: EntryKind, action: PendingAction) -> bool {
    if !queue_action(app, kind, action) {
        return false;
    }
    let handle = app.clone();
    let _ = app.run_on_main_thread(move || {
        // Owner-only presentation avoids reentering the public entry gate.
        crate::focus_or_restore_main_window_unlocked(&handle);
        sync_windows(&handle);
        if machine(&handle).machine.lock().expect("app lock").should_prompt() {
            authenticate(&handle, Authentication::Unlock, None);
        }
    });
    true
}

pub(crate) fn queue_if_locked(app: &AppHandle, action: impl FnOnce(&AppHandle) + Send + 'static) -> bool {
    queue_kind(app, EntryKind::Navigation, action)
}
fn queue_kind(app: &AppHandle, kind: EntryKind, action: impl FnOnce(&AppHandle) + Send + 'static) -> bool {
    queue_action(
        app,
        kind,
        PendingAction {
            execute: Box::new(action),
            replaced: None,
        },
    )
}
fn queue_action(app: &AppHandle, kind: EntryKind, action: PendingAction) -> bool {
    let Some(owner) = app.try_state::<Owner>() else {
        return false;
    };
    // Publish the pending intent before an authentication completion can
    // observe the unlock. Native entry callbacks may arrive off the UI thread.
    let mut state = owner.machine.lock().expect("app lock");
    let before = (state.locked, state.busy());
    state.tick(now());
    if before != (state.locked, state.busy()) {
        owner.dirty.store(true, std::sync::atomic::Ordering::Relaxed);
    }
    if !state.locked {
        return false;
    }
    let replaced = match Entry::offer(&mut owner.pending.lock().expect("app lock pending entry"), kind, action) {
        Ok(replaced) => replaced,
        Err(_) => return true, // Still gated; foreground and offer unlock.
    };
    // A foreground-only Action must also preserve navigation that was already
    // waiting in the Workbench queue before locking.
    if kind == EntryKind::Action {
        return true;
    }
    // A newer locked entry supersedes an earlier navigation/update that was
    // still waiting for a Workbench before the app locked.
    if let Some(links) = app.try_state::<Mutex<avibe_runtime_host::deep_link::DeepLinks>>() {
        links.lock().expect("deep links").clear_pending();
    }
    if let Some(updates) = app.try_state::<crate::PendingDesktopUpdateOpen>() {
        if let Some(id) = updates.current() {
            updates.finish_delivery(id, true);
        }
    }
    drop(state);
    // Notify outside both owner locks; the callback may schedule native work.
    if let Some(notify) = replaced.and_then(|entry| entry.action.replaced) {
        notify(app);
    }
    true
}

pub(crate) fn ready(app: &AppHandle) {
    event(app, Event::Arrive);
}

pub(crate) async fn app_lock_status(window: Webview, app: AppHandle) -> Result<Status, String> {
    ensure_caller(&window)?;
    let (send, receive) = tokio::sync::oneshot::channel();
    let handle = app.clone();
    app.run_on_main_thread(move || {
        refresh(&handle);
        sync_windows(&handle);
        let _ = send.send(status(&handle));
    })
    .map_err(|e| e.to_string())?;
    receive.await.map_err(|e| e.to_string())
}
pub(crate) async fn app_lock_set_policy(
    window: Webview,
    app: AppHandle,
    policy: Policy,
) -> Result<serde_json::Value, String> {
    ensure_caller(&window)?;
    let (send, receive) = tokio::sync::oneshot::channel();
    let handle = app.clone();
    app.run_on_main_thread(move || change_policy(&handle, policy, send))
        .map_err(|e| e.to_string())?;
    receive.await.map_err(|e| e.to_string())
}
fn change_policy(app: &AppHandle, policy: Policy, send: Reply) {
    refresh(app);
    let old = status(app);
    if old.policy == policy {
        let _ = send.send(result(app, None));
    } else if !old.available {
        let _ = send.send(result(app, Some("unavailable")));
    } else if is_locked(app) {
        let _ = send.send(result(app, Some("busy")));
    } else {
        authenticate(app, Authentication::Change(policy), Some(send));
    }
    sync_windows(app);
}
fn ensure_caller(window: &Webview) -> Result<(), String> {
    if crate::pet::is_workbench(window) && window.url().is_ok_and(|url| !url.path().starts_with("/show/")) {
        Ok(())
    } else {
        Err("App Lock settings require the main local Workbench.".into())
    }
}
fn authenticate(app: &AppHandle, purpose: Authentication, reply: Option<Reply>) {
    refresh(app);
    let epoch = machine(app).machine.lock().expect("app lock").begin(purpose);
    let Some(epoch) = epoch else {
        if let Some(reply) = reply {
            let _ = reply.send(result(
                app,
                Some(if status(app).available { "busy" } else { "unavailable" }),
            ));
        }
        sync_windows(app);
        deliver_pending(app);
        return;
    };
    #[cfg(feature = "app-lock-e2e")]
    if e2e::authenticate(app, epoch, purpose, &reply) {
        PRESENTATION.with_borrow_mut(|p| p.reply = reply);
        sync_windows(app);
        e2e::finish_if_ready(app);
        return;
    }
    let context = unsafe { LAContext::new() };
    PRESENTATION.with_borrow_mut(|p| {
        p.context = Some(context.clone());
        p.reply = reply;
    });
    sync_windows(app);
    let handle = app.clone();
    let callback = RcBlock::new(move |success: Bool, error: *mut NSError| {
        let code = unsafe { error.as_ref() }.map(|error| error.code());
        let outcome = if success.as_bool() {
            Outcome::Success
        } else if matches!(code, Some(-2 | -4 | -9)) {
            Outcome::Cancelled
        } else {
            Outcome::Failed
        };
        let replied_at = now();
        let app = handle.clone();
        let _ = handle.run_on_main_thread(move || complete(&app, epoch, outcome, replied_at));
    });
    let reason = catalog(if matches!(purpose, Authentication::Change(_)) {
        "changeReason"
    } else {
        "unlockReason"
    });
    // Fresh context for every evaluation; device-owner policy supplies Apple's
    // password fallback (and Watch approval) without a custom credential UI.
    unsafe {
        context.evaluatePolicy_localizedReason_reply(
            LAPolicy::DeviceOwnerAuthentication,
            &NSString::from_str(&reason),
            &callback,
        );
    }
}
fn complete(app: &AppHandle, epoch: u64, outcome: Outcome, replied_at: f64) {
    machine(app)
        .machine
        .lock()
        .expect("app lock")
        .reply(epoch, outcome, active(), replied_at, now());
    finish_completion(app);
    sync_windows(app);
    deliver_pending(app);
}
fn finish_completion(app: &AppHandle) {
    let outcome = {
        let owner = machine(app);
        let mut state = owner.machine.lock().expect("app lock");
        let Some(completion) = state.take_completion() else {
            return;
        };
        let mut outcome = completion.outcome;
        if outcome == Outcome::Success {
            if let Authentication::Change(policy) = completion.purpose {
                if let Err(error) = persist(&owner.path, policy) {
                    eprintln!("App Lock preference: {error}");
                    outcome = Outcome::Failed;
                }
            }
        }
        state.commit(completion, outcome);
        outcome
    };
    let reply = PRESENTATION.with_borrow_mut(|p| {
        p.context = None;
        p.reply.take()
    });
    refresh(app);
    if let Some(reply) = reply {
        let reason = match outcome {
            Outcome::Success => None,
            Outcome::Cancelled => Some("cancelled"),
            Outcome::Failed => Some("failed"),
            Outcome::Unavailable => Some("unavailable"),
        };
        let _ = reply.send(result(app, reason));
    }
}
fn deliver_pending(app: &AppHandle) {
    if !is_locked(app) {
        let entry = machine(app).pending.lock().expect("pending entry").take();
        if let Some(entry) = entry {
            (entry.action.execute)(app);
        }
        if app
            .try_state::<Mutex<avibe_runtime_host::deep_link::DeepLinks>>()
            .is_some()
        {
            crate::apply_pending_deep_link(app);
        }
    }
}

/// Vault lifetime checks may preserve only a view temporarily hidden by this
/// owner. They still verify the original window and competing Workbench views.
fn directly_protected(view: &NSView) -> bool {
    HIDDEN.with_borrow(|hidden| hidden.iter().filter_map(Weak::load).any(|v| std::ptr::eq(&*v, view)))
}
pub(crate) fn protects_view(view: &NSView) -> bool {
    if directly_protected(view) {
        return true;
    }
    if view.isHidden() {
        return false;
    }
    // A Tauri WKWebView may sit inside a native container. Only an ancestor
    // hidden by this owner counts; a separately hidden/replaced page does not.
    unsafe { view.superview() }.is_some_and(|parent| protects_view(&parent))
}

// The approved app-lock frames use these product tokens from ui/src/index.css.
// Keep this small native palette in parity when the Web tokens change.
#[derive(Clone, Copy)]
enum Palette {
    Background,
    Foreground,
    Muted,
    Primary,
    PrimaryForeground,
    Failure,
}
fn palette(view: &NSView, token: Palette) -> Retained<NSColor> {
    let dark = view.effectiveAppearance().name().to_string().contains("Dark");
    let rgb = match (dark, token) {
        (false, Palette::Background) => 0xf4f6fb,
        (true, Palette::Background) => 0x080812,
        (false, Palette::Foreground) => 0x0a0a14,
        (true, Palette::Foreground) => 0xf5f1e8,
        (false, Palette::Muted) => 0x5c6079,
        (true, Palette::Muted) => 0x9ba3b8,
        (false, Palette::Primary) => 0x10b981,
        (true, Palette::Primary) => 0x5bffa0,
        (false, Palette::PrimaryForeground) => 0xffffff,
        (true, Palette::PrimaryForeground) => 0x080812,
        (false, Palette::Failure) => 0xe11d48,
        (true, Palette::Failure) => 0xff6b6b,
    };
    NSColor::colorWithSRGBRed_green_blue_alpha(
        f64::from((rgb >> 16) & 255) / 255.0,
        f64::from((rgb >> 8) & 255) / 255.0,
        f64::from(rgb & 255) / 255.0,
        1.0,
    )
}

struct ButtonFields {
    compact: Cell<bool>,
}
define_class!(
    #[unsafe(super(NSButton))]
    #[thread_kind = MainThreadOnly]
    #[name = "AvibeAppLockButton"]
    #[ivars = ButtonFields]
    struct LockButton;
    impl LockButton {
        #[unsafe(method(drawRect:))]
        fn draw(&self, rect: NSRect) {
            let compact = self.ivars().compact.get();
            let radius = if compact { 8.0 } else { self.bounds().size.height / 2.0 };
            let bounds = self.bounds();
            let inset = NSRect::new(NSPoint::new(bounds.origin.x + 0.5, bounds.origin.y + 0.5), NSSize::new(bounds.size.width - 1.0, bounds.size.height - 1.0));
            let path = NSBezierPath::bezierPathWithRoundedRect_xRadius_yRadius(inset, radius, radius);
            palette(self, if compact { Palette::Background } else { Palette::Primary })
                .colorWithAlphaComponent(if self.isEnabled() { 1.0 } else { 0.5 }).setFill();
            path.fill();
            if compact { palette(self, Palette::Muted).colorWithAlphaComponent(0.3).setStroke(); path.setLineWidth(1.0); path.stroke(); }
            let _: () = unsafe { msg_send![super(self), drawRect: rect] };
        }
    }
);
type RenderKey = (bool, bool, BiometryState, bool, Option<EntryKind>, bool);
struct ViewFields {
    rendered: Cell<Option<RenderKey>>,
    app: AppHandle,
    compact: Cell<bool>,
    title: Retained<NSTextField>,
    note: Retained<NSTextField>,
    pending: Retained<NSTextField>,
    button: Retained<LockButton>,
    logo: Retained<NSImageView>,
}
define_class!(
    #[unsafe(super(NSView))]
    #[thread_kind = MainThreadOnly]
    #[name = "AvibeAppLockView"]
    #[ivars = ViewFields]
    struct LockView;
    impl LockView {
        #[unsafe(method(isOpaque))]
        fn opaque(&self) -> bool { true }
        #[unsafe(method(acceptsFirstResponder))]
        fn accepts_first_responder(&self) -> bool { true }
        #[unsafe(method(drawRect:))]
        fn draw(&self, _rect: NSRect) { palette(self, Palette::Background).setFill(); NSRectFill(self.bounds()); }
        #[unsafe(method(layout))]
        fn layout(&self) {
            let _: () = unsafe { msg_send![super(self), layout] };
            self.arrange();
        }
        #[unsafe(method(unlock:))]
        fn unlock(&self, _sender: &AnyObject) { authenticate(&self.ivars().app, Authentication::Unlock, None); }
        #[unsafe(method(keyDown:))]
        fn key_down(&self, event: &NSEvent) {
            if matches!(event.keyCode(), 36 | 49 | 76) { authenticate(&self.ivars().app, Authentication::Unlock, None); }
        }
    }
);
impl LockView {
    fn new(app: &AppHandle, compact: bool, frame: NSRect, mtm: MainThreadMarker) -> Retained<Self> {
        let label = |size: f64| {
            let label = NSTextField::wrappingLabelWithString(&NSString::from_str(""), mtm);
            label.setFont(Some(&NSFont::systemFontOfSize(size)));
            label.setAlignment(NSTextAlignment::Center);
            label.setTextColor(Some(&NSColor::secondaryLabelColor()));
            label
        };
        let title = label(if compact { 18.0 } else { 24.0 });
        title.setTextColor(Some(&NSColor::labelColor()));
        title.setFont(Some(&NSFont::boldSystemFontOfSize(if compact { 18.0 } else { 24.0 })));
        title.setStringValue(&NSString::from_str(&catalog("lockedTitle")));
        let note = label(if compact { 12.0 } else { 14.0 });
        let pending = label(12.0);
        let button: Retained<LockButton> = unsafe {
            msg_send![super(LockButton::alloc(mtm).set_ivars(ButtonFields { compact: Cell::new(compact) })), initWithFrame: NSRect::ZERO]
        };
        button.setBordered(false);
        button.setImageHugsTitle(true);
        button.setFont(Some(&NSFont::systemFontOfSize(14.0)));
        let logo = NSImageView::initWithFrame(
            mtm.alloc(),
            NSRect::new(NSPoint::new(0.0, 0.0), NSSize::new(64.0, 64.0)),
        );
        let logo_data = NSData::with_bytes(include_bytes!("../../icons/128x128.png"));
        logo.setImage(NSImage::initWithData(mtm.alloc(), &logo_data).as_deref());
        let view: Retained<Self> = unsafe {
            msg_send![super(Self::alloc(mtm).set_ivars(ViewFields { rendered: Cell::new(None), app: app.clone(), compact: Cell::new(compact), title, note, pending, button, logo })), initWithFrame: frame]
        };
        view.setAutoresizingMask(
            NSAutoresizingMaskOptions::ViewWidthSizable | NSAutoresizingMaskOptions::ViewHeightSizable,
        );
        let f = view.ivars();
        for child in [&*f.title as &NSView, &*f.note, &*f.pending, &*f.button, &*f.logo] {
            view.addSubview(child);
        }
        unsafe {
            f.button.setTarget(Some(view.as_ref()));
            f.button.setAction(Some(sel!(unlock:)));
        }
        view.update();
        view.arrange();
        view
    }
    fn arrange(&self) {
        let f = self.ivars();
        let bounds = self.bounds();
        let width = (bounds.size.width - 48.0).clamp(120.0, if f.compact.get() { 320.0 } else { 384.0 });
        let left = (bounds.size.width - width) / 2.0;
        let mid = bounds.size.height / 2.0;
        let has_pending = !f.pending.stringValue().is_empty();
        let icon = if f.compact.get() { 48.0 } else { 64.0 };
        f.logo.setFrame(NSRect::new(
            NSPoint::new((bounds.size.width - icon) / 2.0, mid + 62.0),
            NSSize::new(icon, icon),
        ));
        f.title
            .setFrame(NSRect::new(NSPoint::new(left, mid + 22.0), NSSize::new(width, 30.0)));
        f.pending
            .setFrame(NSRect::new(NSPoint::new(left, mid - 28.0), NSSize::new(width, 44.0)));
        f.button.setFrame(NSRect::new(
            NSPoint::new(left, mid - if has_pending { 88.0 } else { 42.0 }),
            NSSize::new(width, 44.0),
        ));
        f.note.setFrame(NSRect::new(
            NSPoint::new(left, mid - if has_pending { 160.0 } else { 114.0 }),
            NSSize::new(width, 56.0),
        ));
    }
    fn update(&self) {
        let f = self.ivars();
        let owner = machine(&f.app);
        let state = owner.machine.lock().expect("app lock");
        let pending_kind = owner
            .pending
            .lock()
            .expect("pending entry")
            .as_ref()
            .map(|entry| entry.kind);
        let key = (
            state.busy(),
            state.failed,
            state.availability.biometry_state,
            f.compact.get(),
            pending_kind,
            self.effectiveAppearance().name().to_string().contains("Dark"),
        );
        if f.rendered.replace(Some(key)) == Some(key) {
            return;
        }
        f.title.setTextColor(Some(&palette(self, Palette::Foreground)));
        f.note.setTextColor(Some(&palette(
            self,
            if state.failed { Palette::Failure } else { Palette::Muted },
        )));
        f.pending.setTextColor(Some(&palette(self, Palette::Muted)));
        f.button.setBezelColor(Some(&palette(
            self,
            if f.compact.get() {
                Palette::Background
            } else {
                Palette::Primary
            },
        )));
        f.button.setContentTintColor(Some(&palette(
            self,
            if f.compact.get() {
                Palette::Foreground
            } else {
                Palette::PrimaryForeground
            },
        )));
        let verifying = state.busy();
        let usable = state.availability.biometry_state == BiometryState::Usable;
        let action = NSString::from_str(&catalog(if verifying {
            "verifying"
        } else if usable {
            "unlockTouchID"
        } else {
            "unlockPassword"
        }));
        f.button.setTitle(&action);
        let foreground = palette(
            self,
            if f.compact.get() {
                Palette::Foreground
            } else {
                Palette::PrimaryForeground
            },
        );
        let attributes = objc2_foundation::NSDictionary::from_slices(
            &[unsafe { NSForegroundColorAttributeName }],
            &[&*foreground as &AnyObject],
        );
        let title = unsafe {
            NSAttributedString::initWithString_attributes(
                MainThreadMarker::new().unwrap().alloc(),
                &action,
                Some(&attributes),
            )
        };
        f.button.setAttributedTitle(&title);
        f.button.ivars().compact.set(f.compact.get());
        f.button.setEnabled(!verifying);
        let symbol = NSImage::imageWithSystemSymbolName_accessibilityDescription(
            &NSString::from_str(if usable { "touchid" } else { "key" }),
            None,
        );
        if let Some(symbol) = &symbol {
            symbol.setSize(NSSize::new(18.0, 18.0));
        }
        f.button.setImage(symbol.as_deref());
        f.button.setImagePosition(NSCellImagePosition::ImageLeft);
        let note = if state.failed {
            "verificationFailed"
        } else {
            match state.availability.biometry_state {
                BiometryState::NotEnrolled => "macNotEnrolled",
                BiometryState::Lockout => "macLockout",
                BiometryState::Unavailable => "macUnavailable",
                _ if f.compact.get() => "restoreAllWindows",
                _ => "",
            }
        };
        f.note
            .setFont(Some(&NSFont::systemFontOfSize(if note == "restoreAllWindows" {
                12.0
            } else {
                14.0
            })));
        f.note.setStringValue(&NSString::from_str(&if note.is_empty() {
            String::new()
        } else {
            catalog(note)
        }));
        let key = pending_kind.and_then(|kind| match kind {
            EntryKind::Content => Some("pendingContent"),
            EntryKind::Navigation => Some("pendingLink"),
            EntryKind::Action | EntryKind::Command => None,
        });
        f.pending
            .setStringValue(&NSString::from_str(&key.map(catalog).unwrap_or_default()));
        self.arrange();
    }
}

pub(crate) fn sync_windows(app: &AppHandle) {
    let Some(mtm) = MainThreadMarker::new() else {
        let handle = app.clone();
        let _ = app.run_on_main_thread(move || sync_windows(&handle));
        return;
    };
    if PAINTING.with(|painting| painting.replace(true)) {
        return;
    }
    machine(app).dirty.store(false, std::sync::atomic::Ordering::Relaxed);
    sync_on_main(app, mtm);
    PAINTING.with(|painting| painting.set(false));
}
fn restore_cover(cover: Cover) {
    if let Some(window) = cover.window.load() {
        for view in cover.hidden.iter().filter_map(Weak::load) {
            if view.window().is_some_and(|w| std::ptr::eq(&*w, &*window)) {
                view.setHidden(false);
            }
        }
        window.setTitle(&cover.title);
        cover.view.removeFromSuperview();
        if let Some(responder) = cover.responder.and_then(|r| r.load()) {
            window.makeFirstResponder(Some(&responder));
        }
    }
    HIDDEN.with_borrow_mut(|hidden| hidden.retain(|v| v.load().is_some_and(|v| v.isHidden())));
}
fn is_quit_window(window: &NSWindow) -> bool {
    PRESENTATION.with_borrow(|p| {
        p.quit_window
            .as_ref()
            .and_then(Weak::load)
            .is_some_and(|w| std::ptr::eq(&*w, window))
    })
}
/// Preserve the existing Keep Runtime / Stop Runtime / Cancel question while
/// locked. Its exact, native-only NSAlert is the sole exempt window; no page
/// or caller can register a window or reveal underlying content through IPC.
pub(crate) fn show_quit_question(
    app: &AppHandle,
    catalog: &crate::NativeTrayCatalog,
    callback: impl FnOnce(tauri_plugin_dialog::MessageDialogResult) + Send + 'static,
) -> bool {
    show_lifecycle_alert(
        app,
        app.get_window(crate::MAIN_WINDOW),
        catalog.quit_title.clone(),
        catalog.quit_message.clone(),
        vec![
            catalog.quit_keep.clone(),
            catalog.quit_stop.clone(),
            catalog.cancel.clone(),
        ],
        callback,
    )
}
pub(crate) fn show_lifecycle_busy(app: &AppHandle, catalog: &crate::NativeTrayCatalog) -> bool {
    show_lifecycle_alert(
        app,
        app.get_window(crate::MAIN_WINDOW),
        catalog.busy_title.clone(),
        catalog.busy_message.clone(),
        vec![catalog.cancel.clone()],
        |_| {},
    )
}
/// W1 close/quit guards still ask before discarding unsaved work. Use native
/// catalog copy rather than exposing the covered page's title or message.
pub(crate) fn show_discard_question(
    app: &AppHandle,
    parent: &tauri::WebviewWindow,
    callback: impl FnOnce(bool) + Send + 'static,
) -> bool {
    let text = |key: &str| crate::product_text(key).unwrap_or_else(|| key.to_owned());
    show_lifecycle_alert(
        app,
        Some(parent.as_ref().window()),
        "Avibe".into(),
        text("apps.editor.confirmDiscardClose"),
        vec![text("apps.editor.discardAndClose"), text("common.cancel")],
        move |answer| callback(matches!(answer, tauri_plugin_dialog::MessageDialogResult::Yes)),
    )
}
fn show_lifecycle_alert(
    app: &AppHandle,
    parent: Option<tauri::Window>,
    title: String,
    message: String,
    buttons: Vec<String>,
    callback: impl FnOnce(tauri_plugin_dialog::MessageDialogResult) + Send + 'static,
) -> bool {
    if !app
        .try_state::<Owner>()
        .is_some_and(|owner| owner.machine.lock().expect("app lock").effective())
    {
        return false;
    }
    let handle = app.clone();
    crate::macos_terminate::on_main_queue(move || {
        // Repeated busy requests share the visible native alert. There is one
        // exact exemption; do not replace it while a sheet is still open.
        if PRESENTATION.with_borrow(|p| p.quit_alert.is_some()) {
            callback(tauri_plugin_dialog::MessageDialogResult::Cancel);
            return;
        }
        // Quit never waits for an already open unlock/change prompt.
        {
            let owner = machine(&handle);
            let mut state = owner.machine.lock().expect("app lock");
            state.cancel_for_lifecycle(now());
        }
        let (context, reply) = PRESENTATION.with_borrow_mut(|p| (p.context.take(), p.reply.take()));
        if let Some(context) = context {
            unsafe {
                context.invalidate();
            }
        }
        if let Some(reply) = reply {
            let _ = reply.send(result(&handle, Some("cancelled")));
        }
        let mtm = MainThreadMarker::new().expect("quit question on main thread");
        let alert = NSAlert::new(mtm);
        alert.setMessageText(&NSString::from_str(&title));
        alert.setInformativeText(&NSString::from_str(&message));
        for title in buttons {
            alert.addButtonWithTitle(&NSString::from_str(&title));
        }
        let window = alert.window();
        PRESENTATION.with_borrow_mut(|p| p.quit_window = Some(Weak::from_retained(&window)));
        sync_windows(&handle);
        let parent = parent
            .and_then(|window| window.ns_window().ok())
            .and_then(|window| unsafe { Retained::retain(window.cast::<NSWindow>()) });
        let Some(parent) = parent else {
            PRESENTATION.with_borrow_mut(|p| p.quit_window = None);
            callback(tauri_plugin_dialog::MessageDialogResult::Cancel);
            return;
        };
        PRESENTATION.with_borrow_mut(|p| p.quit_alert = Some(alert.clone()));
        let callback = RefCell::new(Some(callback));
        let completion = RcBlock::new(move |response: NSModalResponse| {
            PRESENTATION.with_borrow_mut(|p| {
                p.quit_window = None;
                p.quit_alert = None;
            });
            let answer = match response {
                1000 => tauri_plugin_dialog::MessageDialogResult::Yes,
                1001 => tauri_plugin_dialog::MessageDialogResult::No,
                _ => tauri_plugin_dialog::MessageDialogResult::Cancel,
            };
            if let Some(callback) = callback.borrow_mut().take() {
                callback(answer);
            }
        });
        // Return to both tao and the main dispatch queue immediately. Logout
        // replies, timers and LA callbacks must run while the sheet is open.
        parent.orderFront(None);
        alert.beginSheetModalForWindow_completionHandler(&parent, Some(&completion));
    });
    true
}
fn sync_on_main(app: &AppHandle, mtm: MainThreadMarker) {
    if !is_locked(app) {
        let (covers, hidden) =
            PRESENTATION.with_borrow_mut(|p| (std::mem::take(&mut p.covers), std::mem::take(&mut p.hidden_windows)));
        if !covers.is_empty() {
            crate::macos_vault_window::app_lock_did_unlock();
        }
        for cover in covers {
            restore_cover(cover);
        }
        HIDDEN.with_borrow_mut(Vec::clear);
        for window in hidden.into_iter().filter_map(|w| w.load()).filter(|w| floating(app, w)) {
            window.orderFront(None);
        }
        return;
    }
    PRESENTATION.with_borrow_mut(|p| p.covers.retain(|cover| cover.window.load().is_some()));
    let windows = NSApplication::sharedApplication(mtm).windows();
    for window in windows.iter() {
        if system_chrome(&window) {
            continue;
        }
        if is_quit_window(&window) {
            let cover = PRESENTATION.with_borrow_mut(|p| {
                p.covers
                    .iter()
                    .position(|c| c.window.load().is_some_and(|w| std::ptr::eq(&*w, &*window)))
                    .map(|at| p.covers.remove(at))
            });
            if let Some(cover) = cover {
                restore_cover(cover);
            }
            continue;
        }
        let hide = [crate::pet::PET_WINDOW, crate::update_popover::UPDATE_POPOVER]
            .iter()
            .any(|label| {
                app.get_window(label)
                    .and_then(|w| w.ns_window().ok())
                    .is_some_and(|p| std::ptr::eq(p.cast::<NSWindow>(), &*window))
            });
        if hide {
            if window.isVisible() {
                PRESENTATION.with_borrow_mut(|p| {
                    if !p
                        .hidden_windows
                        .iter()
                        .filter_map(Weak::load)
                        .any(|w| std::ptr::eq(&*w, &*window))
                    {
                        p.hidden_windows.push(Weak::from_retained(&window));
                    }
                });
                window.orderOut(None);
            }
            continue;
        }
        let Some(content) = window.contentView() else { continue };
        let existing = PRESENTATION.with_borrow(|p| {
            p.covers
                .iter()
                .find(|c| c.window.load().is_some_and(|w| std::ptr::eq(&*w, &*window)))
                .map(|c| c.view.clone())
        });
        let compact = !app
            .get_window(crate::MAIN_WINDOW)
            .and_then(|w| w.ns_window().ok())
            .is_some_and(|p| std::ptr::eq(p.cast::<NSWindow>(), &*window));
        let view = if let Some(view) = existing {
            view
        } else {
            let view = LockView::new(app, compact, content.bounds(), mtm);
            let responder = window.firstResponder().map(|r| Weak::from_retained(&r));
            PRESENTATION.with_borrow_mut(|p| {
                p.covers.push(Cover {
                    window: Weak::from_retained(&window),
                    view: view.clone(),
                    hidden: Vec::new(),
                    responder,
                    title: window.title(),
                })
            });
            view
        };
        if view.ivars().compact.replace(compact) != compact {
            view.ivars()
                .title
                .setFont(Some(&NSFont::boldSystemFontOfSize(if compact { 18.0 } else { 24.0 })));
            view.arrange();
        }
        let locked_title = NSString::from_str(&catalog("lockedTitle"));
        if window.title() != locked_title {
            window.setTitle(&locked_title);
        }
        view.setFrame(content.bounds());
        // Cover the whole native content tree, including AppKit sheets and
        // non-Tauri windows. Title-bar controls stay outside this subtree.
        for child in content.subviews().iter() {
            if std::ptr::eq(&*child, &*view as &NSView) || child.isHidden() {
                continue;
            }
            let weak = Weak::from_retained(&child);
            HIDDEN.with_borrow_mut(|hidden| hidden.push(weak.clone()));
            PRESENTATION.with_borrow_mut(|p| {
                if let Some(c) = p.covers.iter_mut().find(|c| std::ptr::eq(&*c.view, &*view)) {
                    c.hidden.push(weak);
                }
            });
            child.setHidden(true);
        }
        if unsafe { view.superview() }.is_none_or(|parent| !std::ptr::eq(&*parent, &*content)) {
            content.addSubview(&view);
        }
        window.makeFirstResponder(Some(&view));
        view.update();
        // Hooks may run while AppKit is rebuilding a title frame. Conceal
        // content synchronously, but let AppKit draw at its normal safe point.
        view.setNeedsDisplay(true);
    }
}

#[derive(Clone, Copy)]
enum Event {
    Leave,
    Arrive,
    SystemLeave(u8),
    SystemReturn,
    OtherApplication,
    Windows,
}
fn event(app: &AppHandle, event: Event) {
    if app.try_state::<Owner>().is_none() {
        return;
    }
    match event {
        Event::Leave => machine(app).machine.lock().expect("app lock").leave(now()),
        Event::Arrive => {
            refresh(app);
            if active() {
                let recovered = awake_on_console();
                let owner = machine(app);
                let mut state = owner.machine.lock().expect("app lock");
                state.reconcile(recovered);
                state.arrive(now());
            }
        }
        Event::SystemLeave(reason) => {
            machine(app).machine.lock().expect("app lock").block(reason, now());
            invalidate_native(app);
        }
        Event::OtherApplication => {
            let owner = machine(app);
            let mut state = owner.machine.lock().expect("app lock");
            if state.busy() {
                state.system_leave(now());
                drop(state);
                invalidate_native(app);
            }
        }
        Event::SystemReturn => {
            // Return hints cannot clear suppression or impersonate NSApp activation.
            machine(app).machine.lock().expect("app lock").tick(now());
        }
        Event::Windows => {}
    }
    finish_completion(app);
    sync_windows(app);
    if matches!(event, Event::Arrive) {
        if active() && machine(app).machine.lock().expect("app lock").should_prompt() {
            authenticate(app, Authentication::Unlock, None);
        }
        deliver_pending(app);
    }
}
fn invalidate_native(app: &AppHandle) {
    let (context, reply) = PRESENTATION.with_borrow_mut(|p| (p.context.take(), p.reply.take()));
    if let Some(context) = context {
        unsafe {
            context.invalidate();
        }
    }
    // S1: every definite departure settles an outstanding settings command.
    if let Some(reply) = reply {
        let _ = reply.send(result(app, Some("cancelled")));
    }
}

fn awake_on_console() -> bool {
    #[cfg(feature = "app-lock-e2e")]
    if let Some(facts) = e2e::recovery_facts() {
        return facts;
    }
    use std::ffi::c_void;
    #[link(name = "CoreGraphics", kind = "framework")]
    extern "C" {
        fn CGSessionCopyCurrentDictionary() -> *const c_void;
        fn CGMainDisplayID() -> u32;
        fn CGDisplayIsAsleep(display: u32) -> u32;
    }
    #[link(name = "CoreFoundation", kind = "framework")]
    extern "C" {
        fn CFDictionaryGetValue(dictionary: *const c_void, key: *const c_void) -> *const c_void;
        fn CFGetTypeID(value: *const c_void) -> usize;
        fn CFBooleanGetTypeID() -> usize;
        fn CFBooleanGetValue(value: *const c_void) -> u8;
        fn CFRelease(value: *const c_void);
    }
    // CGSession's Copy rule owns the dictionary. Validate the borrowed value's
    // CF type before reading it, and release the dictionary on every path.
    let recovered = unsafe {
        let dictionary = CGSessionCopyCurrentDictionary();
        if dictionary.is_null() {
            false
        } else {
            // Public kCGSessionOnConsoleKey is this CFSTR macro, not an exported symbol.
            let key = NSString::from_str("kCGSSessionOnConsoleKey");
            let value = CFDictionaryGetValue(dictionary, (&*key as *const NSString).cast());
            let on_console =
                !value.is_null() && CFGetTypeID(value) == CFBooleanGetTypeID() && CFBooleanGetValue(value) != 0;
            CFRelease(dictionary);
            on_console && CGDisplayIsAsleep(CGMainDisplayID()) == 0
        }
    };
    if !recovered {
        eprintln!("App Lock: foreground public facts inconclusive; retaining system suppression");
    }
    recovered
}

fn is_other_regular_application(
    pid: i32,
    bundle: Option<&str>,
    regular: bool,
    own_pid: i32,
    own_bundle: Option<&str>,
) -> bool {
    regular
        && pid > 0
        && pid != own_pid
        && !bundle.zip(own_bundle).is_some_and(|(other, own)| {
            other == own || other.strip_prefix(own).is_some_and(|suffix| suffix.starts_with('.'))
        })
}
fn observe_application_switch(app: &AppHandle, center: &NSNotificationCenter) {
    let handle = app.clone();
    let block = RcBlock::new(move |notification: NonNull<NSNotification>| {
        let notification = unsafe { notification.as_ref() };
        let application = notification.userInfo().and_then(|info| {
            info.objectForKey(unsafe { NSWorkspaceApplicationKey })
                .and_then(|object| object.downcast::<NSRunningApplication>().ok())
        });
        let Some(application) = application else {
            return;
        };
        let own = NSRunningApplication::currentApplication();
        if is_other_regular_application(
            application.processIdentifier(),
            application
                .bundleIdentifier()
                .as_ref()
                .map(|s| s.to_string())
                .as_deref(),
            application.activationPolicy() == NSApplicationActivationPolicy::Regular,
            own.processIdentifier(),
            own.bundleIdentifier().as_ref().map(|s| s.to_string()).as_deref(),
        ) {
            let app = handle.clone();
            let _ = handle.run_on_main_thread(move || event(&app, Event::OtherApplication));
        }
    });
    let observer = unsafe {
        center.addObserverForName_object_queue_usingBlock(
            Some(NSWorkspaceDidActivateApplicationNotification),
            None,
            None,
            &block,
        )
    };
    let center =
        unsafe { Retained::retain(center as *const _ as *mut NSNotificationCenter) }.expect("workspace center");
    PRESENTATION.with_borrow_mut(|p| p.observers.push((center, observer)));
}

fn observe(app: &AppHandle, center: &NSNotificationCenter, name: &NSString, kind: Event) {
    let handle = app.clone();
    let block = RcBlock::new(move |_: NonNull<NSNotification>| {
        if MainThreadMarker::new().is_some() {
            event(&handle, kind);
        } else {
            let app = handle.clone();
            let _ = handle.run_on_main_thread(move || event(&app, kind));
        }
    });
    let observer = unsafe { center.addObserverForName_object_queue_usingBlock(Some(name), None, None, &block) };
    // Notification centers retain their tokens, but retaining both documents
    // their lifetime explicitly and makes teardown possible.
    let center =
        unsafe { Retained::retain(center as *const _ as *mut NSNotificationCenter) }.expect("notification center");
    PRESENTATION.with_borrow_mut(|p| p.observers.push((center, observer)));
}
fn install_observers(
    app: &AppHandle,
    center: &NSNotificationCenter,
    workspace: &NSNotificationCenter,
    distributed: &NSNotificationCenter,
) {
    unsafe {
        observe(app, center, NSApplicationDidResignActiveNotification, Event::Leave);
        observe(app, center, NSApplicationDidBecomeActiveNotification, Event::Arrive);
        for name in [
            NSWindowDidBecomeKeyNotification,
            NSWindowDidChangeOcclusionStateNotification,
            NSWindowDidResizeNotification,
        ] {
            observe(app, center, name, Event::Windows);
        }
        for (name, reason) in [
            (NSWorkspaceWillSleepNotification, 1),
            (NSWorkspaceScreensDidSleepNotification, 2),
            (NSWorkspaceSessionDidResignActiveNotification, 4),
        ] {
            observe(app, workspace, name, Event::SystemLeave(reason));
        }
        for name in [
            NSWorkspaceDidWakeNotification,
            NSWorkspaceScreensDidWakeNotification,
            NSWorkspaceSessionDidBecomeActiveNotification,
        ] {
            observe(app, workspace, name, Event::SystemReturn);
        }
        observe_application_switch(app, workspace);
    }
    observe(
        app,
        distributed,
        &NSString::from_str("com.apple.screenIsLocked"),
        Event::SystemLeave(8),
    );
    observe(
        app,
        distributed,
        &NSString::from_str("com.apple.screenIsUnlocked"),
        Event::SystemReturn,
    );
}

pub(crate) fn init(app: &AppHandle) -> Result<(), String> {
    let path = app
        .path()
        .app_local_data_dir()
        .map_err(|e| e.to_string())?
        .join("app-lock.json");
    let policy = match std::fs::read(&path) {
        Ok(bytes) => serde_json::from_slice(&bytes).unwrap_or_else(|error| {
            eprintln!("Invalid App Lock preference; disabled: {error}");
            Policy::Off
        }),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Policy::Off,
        Err(e) => {
            eprintln!("Cannot read App Lock preference; disabled: {e}");
            Policy::Off
        }
    };
    app.manage(Owner {
        machine: Mutex::new(Machine::new(policy, availability())),
        pending: Mutex::new(None),
        path,
        dirty: std::sync::atomic::AtomicBool::new(false),
    });
    PRESENTATION.with_borrow_mut(|p| p.app = Some(app.clone()));
    install_window_hooks();
    install_observers(
        app,
        &NSNotificationCenter::defaultCenter(),
        &NSWorkspace::sharedWorkspace().notificationCenter(),
        &NSDistributedNotificationCenter::defaultCenter(),
    );
    sync_windows(app);
    let handle = app.clone();
    tauri::async_runtime::spawn(async move {
        loop {
            tokio::time::sleep(std::time::Duration::from_millis(250)).await;
            let needs_tick = {
                let owner = machine(&handle);
                let state = owner.machine.lock().expect("app lock");
                state.needs_tick() || owner.dirty.load(std::sync::atomic::Ordering::Relaxed)
            };
            if !needs_tick {
                continue;
            }
            let app = handle.clone();
            if handle
                .run_on_main_thread(move || {
                    let changed = {
                        let owner = machine(&app);
                        let mut state = owner.machine.lock().expect("app lock");
                        let before = (state.locked, state.busy());
                        state.tick(now());
                        before != (state.locked, state.busy())
                            || state.completion.is_some()
                            || owner.dirty.load(std::sync::atomic::Ordering::Relaxed)
                    };
                    finish_completion(&app);
                    if changed {
                        sync_windows(&app);
                    }
                })
                .is_err()
            {
                break;
            }
        }
    });
    Ok(())
}

// AppKit notifications are a catch-up path, not a presentation boundary: an
// order operation can expose pixels before DidBecomeKey is posted. Interpose
// at that operation to cover *all* NSWindows (including native Vault windows).
// Each hook calls the original implementation exactly once; unlocked behavior
// is unchanged. Hook pointers are immutable after installation on main thread.
type OrderFn = unsafe extern "C-unwind" fn(&NSWindow, objc2::runtime::Sel, isize, isize);
type AddFn = unsafe extern "C-unwind" fn(&NSView, objc2::runtime::Sel, &NSView);
type HiddenFn = unsafe extern "C-unwind" fn(&NSView, objc2::runtime::Sel, Bool);
type TitleFn = unsafe extern "C-unwind" fn(&NSWindow, objc2::runtime::Sel, &NSString);
static TITLE: std::sync::OnceLock<TitleFn> = std::sync::OnceLock::new();
type ContentFn = unsafe extern "C-unwind" fn(&NSWindow, objc2::runtime::Sel, *mut NSView);
static ADD: std::sync::OnceLock<AddFn> = std::sync::OnceLock::new();
static HIDE: std::sync::OnceLock<HiddenFn> = std::sync::OnceLock::new();
static CONTENT: std::sync::OnceLock<ContentFn> = std::sync::OnceLock::new();
type SendFn = unsafe extern "C-unwind" fn(&NSWindow, objc2::runtime::Sel, &NSEvent);
static ORDER: std::sync::OnceLock<OrderFn> = std::sync::OnceLock::new();
static SEND: std::sync::OnceLock<SendFn> = std::sync::OnceLock::new();
fn app_handle() -> Option<AppHandle> {
    PRESENTATION
        .try_with(|p| p.try_borrow().ok().and_then(|p| p.app.clone()))
        .ok()
        .flatten()
}
// AppKit's status item, menus and tooltips are chrome, not document
// content. They must retain their native targets (especially the Quit menu).
fn system_chrome(window: &NSWindow) -> bool {
    let mut class = Some(window.class());
    while let Some(current) = class {
        if matches!(
            current.name().to_bytes(),
            b"NSStatusBarWindow"
                | b"NSMenuWindow"
                | b"NSPopupMenuWindow"
                | b"_NSGlassEffectWindow"
                | b"NSToolTipPanel"
                | b"NSToolTipWindow"
        ) {
            return true;
        }
        class = current.superclass();
    }
    false
}
fn floating(app: &AppHandle, window: &NSWindow) -> bool {
    [crate::pet::PET_WINDOW, crate::update_popover::UPDATE_POPOVER]
        .iter()
        .any(|label| {
            app.get_window(label)
                .and_then(|w| w.ns_window().ok())
                .is_some_and(|p| std::ptr::eq(p.cast::<NSWindow>(), window))
        })
}
fn remember_floating(window: &NSWindow) {
    PRESENTATION.with_borrow_mut(|p| {
        if !p
            .hidden_windows
            .iter()
            .filter_map(Weak::load)
            .any(|w| std::ptr::eq(&*w, window))
        {
            let held = unsafe { Retained::retain(window as *const NSWindow as *mut NSWindow) }.expect("live window");
            p.hidden_windows.push(Weak::from_retained(&held));
        }
    });
}
unsafe extern "C-unwind" fn order(window: &NSWindow, selector: objc2::runtime::Sel, mode: isize, relative: isize) {
    if !PAINTING.with(Cell::get) {
        if let Some(app) = app_handle() {
            if mode == 0 {
                // A module hiding its own floating window supersedes an older
                // request to restore it. Our concealment never changes intent.
                PRESENTATION.with_borrow_mut(|p| {
                    p.hidden_windows
                        .retain(|w| w.load().is_some_and(|w| !std::ptr::eq(&*w, window)))
                });
            } else {
                sync_windows(&app);
                if is_locked(&app) && floating(&app, window) {
                    remember_floating(window);
                    return;
                }
            }
        }
    }
    unsafe {
        ORDER.get().expect("installed window order hook")(window, selector, mode, relative);
    }
}
unsafe extern "C-unwind" fn send(window: &NSWindow, selector: objc2::runtime::Sel, event: &NSEvent) {
    if let Some(app) = app_handle() {
        if is_locked(&app) {
            sync_windows(&app);
            // The overlay is now first responder, even if a still-running
            // WebKit page asked for focus since the preceding notification.
        }
    }
    unsafe {
        SEND.get().expect("installed window event hook")(window, selector, event);
    }
}
unsafe extern "C-unwind" fn add(view: &NSView, selector: objc2::runtime::Sel, child: &NSView) {
    unsafe {
        ADD.get().expect("installed view hook")(view, selector, child);
    }
    // Descendants of an already hidden content child cannot expose pixels.
    // In particular, never reenter layout from our own NSButton internals.
    if !PAINTING.with(Cell::get)
        && view
            .window()
            .and_then(|w| w.contentView())
            .is_some_and(|root| std::ptr::eq(&*root, view))
    {
        if let Some(app) = app_handle().filter(is_locked) {
            sync_windows(&app);
        }
    }
}
unsafe extern "C-unwind" fn content(window: &NSWindow, selector: objc2::runtime::Sel, view: *mut NSView) {
    unsafe {
        CONTENT.get().expect("installed content hook")(window, selector, view);
    }
    if !PAINTING.with(Cell::get) {
        if let Some(app) = app_handle().filter(is_locked) {
            sync_windows(&app);
        }
    }
}
unsafe extern "C-unwind" fn title(window: &NSWindow, selector: objc2::runtime::Sel, value: &NSString) {
    if !PAINTING.with(Cell::get) && !system_chrome(window) && !is_quit_window(window) {
        if let Some(app) = app_handle().filter(is_locked) {
            sync_windows(&app);
            let protected = PRESENTATION.with_borrow_mut(|p| {
                if let Some(cover) = p
                    .covers
                    .iter_mut()
                    .find(|c| c.window.load().is_some_and(|w| std::ptr::eq(&*w, window)))
                {
                    cover.title = value.to_owned();
                    true
                } else {
                    false
                }
            });
            // An initializing window may not belong to NSApp.windows yet.
            // Its first order operation will capture the real title and cover.
            if protected {
                unsafe {
                    TITLE.get().expect("installed title hook")(
                        window,
                        selector,
                        &NSString::from_str(&catalog("lockedTitle")),
                    );
                }
                return;
            }
        }
    }
    unsafe {
        TITLE.get().expect("installed title hook")(window, selector, value);
    }
}
unsafe extern "C-unwind" fn hidden(view: &NSView, selector: objc2::runtime::Sel, hidden: Bool) {
    let mut value = hidden;
    if !PAINTING.with(Cell::get) && directly_protected(view) {
        if hidden.as_bool() {
            // A real owner hides/retires its page while locked. Do not restore
            // that page on unlock, or let Vault mistake it for our cover.
            HIDDEN.with_borrow_mut(|views| views.retain(|v| v.load().is_some_and(|v| !std::ptr::eq(&*v, view))));
            PRESENTATION.with_borrow_mut(|p| {
                for cover in &mut p.covers {
                    cover
                        .hidden
                        .retain(|v| v.load().is_some_and(|v| !std::ptr::eq(&*v, view)));
                }
            });
        } else {
            value = Bool::YES;
        }
    }
    unsafe {
        HIDE.get().expect("installed hidden hook")(view, selector, value);
    }
    if !PAINTING.with(Cell::get)
        && view
            .window()
            .and_then(|w| w.contentView())
            .is_some_and(|root| unsafe { view.superview() }.is_some_and(|parent| std::ptr::eq(&*root, &*parent)))
    {
        if let Some(app) = app_handle().filter(is_locked) {
            sync_windows(&app);
        }
    }
}
fn install_window_hooks() {
    if ORDER.get().is_some() {
        return;
    }
    use objc2::runtime::Imp;
    unsafe {
        let method = NSWindow::class()
            .instance_method(sel!(setTitle:))
            .expect("NSWindow setTitle");
        let old = method.set_implementation(std::mem::transmute::<TitleFn, Imp>(title));
        let _ = TITLE.set(std::mem::transmute::<Imp, TitleFn>(old));
        let method = NSWindow::class()
            .instance_method(sel!(orderWindow:relativeTo:))
            .expect("NSWindow orderWindow");
        let old = method.set_implementation(std::mem::transmute::<OrderFn, Imp>(order));
        let _ = ORDER.set(std::mem::transmute::<Imp, OrderFn>(old));
        let method = NSView::class()
            .instance_method(sel!(addSubview:))
            .expect("NSView addSubview");
        let old = method.set_implementation(std::mem::transmute::<AddFn, Imp>(add));
        let _ = ADD.set(std::mem::transmute::<Imp, AddFn>(old));
        let method = NSView::class()
            .instance_method(sel!(setHidden:))
            .expect("NSView setHidden");
        let old = method.set_implementation(std::mem::transmute::<HiddenFn, Imp>(hidden));
        let _ = HIDE.set(std::mem::transmute::<Imp, HiddenFn>(old));
        let method = NSWindow::class()
            .instance_method(sel!(setContentView:))
            .expect("NSWindow setContentView");
        let old = method.set_implementation(std::mem::transmute::<ContentFn, Imp>(content));
        let _ = CONTENT.set(std::mem::transmute::<Imp, ContentFn>(old));
        let method = NSWindow::class()
            .instance_method(sel!(sendEvent:))
            .expect("NSWindow sendEvent");
        let old = method.set_implementation(std::mem::transmute::<SendFn, Imp>(send));
        let _ = SEND.set(std::mem::transmute::<Imp, SendFn>(old));
    }
}

#[cfg(feature = "app-lock-e2e")]
#[path = "macos/e2e.rs"]
pub(crate) mod e2e;
