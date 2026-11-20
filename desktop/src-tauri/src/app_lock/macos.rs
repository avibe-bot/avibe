//! AppKit presentation and LocalAuthentication. All Cocoa state stays on the
//! main thread; the managed machine is the entry gate shared by every caller.
use super::*;
use block2::RcBlock;
use objc2::rc::{Retained, Weak};
use objc2::runtime::{AnyObject, Bool};
use objc2::{define_class, msg_send, sel, ClassType, DefinedClass, MainThreadMarker, MainThreadOnly};
use objc2_app_kit::*;
use objc2_foundation::{
    NSDistributedNotificationCenter, NSError, NSNotification, NSNotificationCenter, NSObjectProtocol, NSPoint, NSRect,
    NSSize, NSString,
};
use objc2_local_authentication::{LABiometryType, LAContext, LAPolicy};
use std::cell::{Cell, RefCell};
use std::path::PathBuf;
use std::ptr::NonNull;
use std::sync::Mutex;
use tauri::{AppHandle, Manager, Webview};

type Entry = Box<dyn FnOnce(&AppHandle) + Send>;
type Reply = tokio::sync::oneshot::Sender<serde_json::Value>;
struct Owner {
    machine: Mutex<Machine>,
    pending: Mutex<Option<Entry>>,
    path: PathBuf,
}
struct Cover {
    window: Weak<NSWindow>,
    view: Retained<LockView>,
    hidden: Vec<Weak<NSView>>,
    responder: Option<Weak<NSResponder>>,
}
#[derive(Default)]
struct Presentation {
    app: Option<AppHandle>,
    covers: Vec<Cover>,
    hidden_windows: Vec<Weak<NSWindow>>,
    observers: Vec<(
        Retained<NSNotificationCenter>,
        Retained<objc2::runtime::ProtocolObject<dyn NSObjectProtocol>>,
    )>,
    context: Option<Retained<LAContext>>,
    reply: Option<Reply>,
    quit_window: Option<Weak<NSWindow>>,
}
thread_local! {
    static PRESENTATION: RefCell<Presentation> = RefCell::new(Presentation::default());
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
    let available = unsafe { context.canEvaluatePolicy_error(LAPolicy::DeviceOwnerAuthentication) }.is_ok();
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
        state.tick(now());
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
    if machine(app).machine.lock().expect("app lock").authenticating.is_some() {
        return;
    }
    machine(app).machine.lock().expect("app lock").refresh(availability());
}
fn catalog(key: &str) -> String {
    // Both native and Web copy comes from the same shipped product catalog.
    let catalog = if sys_locale::get_locale().is_some_and(|s| s.starts_with("zh")) {
        crate::ZH_PRODUCT_CATALOG
    } else {
        crate::EN_PRODUCT_CATALOG
    };
    serde_json::from_str::<serde_json::Value>(catalog)
        .ok()
        .and_then(|value| {
            value
                .pointer(&format!("/appLock/lock/{key}"))
                .and_then(|s| s.as_str())
                .map(str::to_owned)
        })
        .unwrap_or_else(|| key.to_owned())
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
    if !queue_if_locked(app, action) {
        return false;
    }
    let handle = app.clone();
    let _ = app.run_on_main_thread(move || {
        // Owner-only presentation avoids reentering the public entry gate.
        crate::focus_or_restore_main_window_unlocked(&handle);
        sync_windows(&handle);
        authenticate(&handle, Authentication::Unlock, None);
    });
    true
}

pub(crate) fn queue_if_locked(app: &AppHandle, action: impl FnOnce(&AppHandle) + Send + 'static) -> bool {
    if !is_locked(app) {
        return false;
    }
    *machine(app).pending.lock().expect("app lock pending entry") = Some(Box::new(action));
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
        let app = handle.clone();
        let _ = handle.run_on_main_thread(move || complete(&app, epoch, purpose, outcome));
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
fn complete(app: &AppHandle, epoch: u64, purpose: Authentication, mut outcome: Outcome) {
    {
        let owner = machine(app);
        let mut state = owner.machine.lock().expect("app lock");
        if state.epoch != epoch || state.authenticating != Some(purpose) {
            return;
        }
        if outcome == Outcome::Success {
            if let Authentication::Change(policy) = purpose {
                if let Err(error) = persist(&owner.path, policy) {
                    eprintln!("App Lock preference: {error}");
                    outcome = Outcome::Failed;
                }
            }
        }
        state.complete(epoch, outcome, active(), now());
    }
    let reply = PRESENTATION.with_borrow_mut(|p| {
        p.context = None;
        p.reply.take()
    });
    refresh(app);
    sync_windows(app);
    if let Some(reply) = reply {
        let reason = match outcome {
            Outcome::Success => None,
            Outcome::Cancelled => Some("cancelled"),
            Outcome::Failed => Some("failed"),
            Outcome::Unavailable => Some("unavailable"),
        };
        let _ = reply.send(result(app, reason));
    }
    deliver_pending(app);
}
fn deliver_pending(app: &AppHandle) {
    if !is_locked(app) {
        let entry = machine(app).pending.lock().expect("pending entry").take();
        if let Some(entry) = entry {
            entry(app);
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

struct ViewFields {
    app: AppHandle,
    compact: Cell<bool>,
    title: Retained<NSTextField>,
    note: Retained<NSTextField>,
    pending: Retained<NSTextField>,
    button: Retained<NSButton>,
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
        fn draw(&self, _rect: NSRect) { NSColor::windowBackgroundColor().setFill(); NSRectFill(self.bounds()); }
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
            let label = NSTextField::labelWithString(&NSString::from_str(""), mtm);
            label.setFont(Some(&NSFont::systemFontOfSize(size)));
            label.setAlignment(NSTextAlignment::Center);
            label.setTextColor(Some(&NSColor::secondaryLabelColor()));
            label
        };
        let title = label(if compact { 17.0 } else { 22.0 });
        title.setTextColor(Some(&NSColor::labelColor()));
        title.setFont(Some(&NSFont::boldSystemFontOfSize(if compact { 17.0 } else { 22.0 })));
        title.setStringValue(&NSString::from_str(&catalog("lockedTitle")));
        let note = label(13.0);
        let pending = label(12.0);
        let button = unsafe { NSButton::buttonWithTitle_target_action(&NSString::from_str(""), None, None, mtm) };
        button.setBezelStyle(NSBezelStyle::Push);
        button.setFont(Some(&NSFont::systemFontOfSize(14.0)));
        let logo = NSImageView::initWithFrame(
            mtm.alloc(),
            NSRect::new(NSPoint::new(0.0, 0.0), NSSize::new(64.0, 64.0)),
        );
        logo.setImage(NSApplication::sharedApplication(mtm).applicationIconImage().as_deref());
        let view: Retained<Self> = unsafe {
            msg_send![super(Self::alloc(mtm).set_ivars(ViewFields { app: app.clone(), compact: Cell::new(compact), title, note, pending, button, logo })), initWithFrame: frame]
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
        let width = (bounds.size.width - 48.0).clamp(120.0, if f.compact.get() { 300.0 } else { 340.0 });
        let left = (bounds.size.width - width) / 2.0;
        let mid = bounds.size.height / 2.0;
        let icon = if f.compact.get() { 48.0 } else { 64.0 };
        f.logo.setFrame(NSRect::new(
            NSPoint::new((bounds.size.width - icon) / 2.0, mid + 62.0),
            NSSize::new(icon, icon),
        ));
        f.title
            .setFrame(NSRect::new(NSPoint::new(left, mid + 22.0), NSSize::new(width, 30.0)));
        f.pending
            .setFrame(NSRect::new(NSPoint::new(left, mid - 6.0), NSSize::new(width, 24.0)));
        f.button
            .setFrame(NSRect::new(NSPoint::new(left, mid - 59.0), NSSize::new(width, 44.0)));
        f.note
            .setFrame(NSRect::new(NSPoint::new(left, mid - 124.0), NSSize::new(width, 56.0)));
    }
    fn update(&self) {
        let f = self.ivars();
        let owner = machine(&f.app);
        let state = owner.machine.lock().expect("app lock");
        let verifying = state.authenticating.is_some();
        let usable = state.availability.biometry_state == BiometryState::Usable;
        f.button.setTitle(&NSString::from_str(&catalog(if verifying {
            "verifying"
        } else if usable {
            "unlockTouchID"
        } else {
            "unlockPassword"
        })));
        f.button.setEnabled(!verifying);
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
        f.note.setStringValue(&NSString::from_str(&if note.is_empty() {
            String::new()
        } else {
            catalog(note)
        }));
        f.pending.setStringValue(&NSString::from_str(
            &if owner.pending.lock().expect("pending entry").is_some() {
                catalog("pendingContent")
            } else {
                String::new()
            },
        ));
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
    if !app
        .try_state::<Owner>()
        .is_some_and(|owner| owner.machine.lock().expect("app lock").effective())
    {
        return false;
    }
    let catalog = catalog.clone();
    let handle = app.clone();
    let _ = app.run_on_main_thread(move || {
        // Quit never waits for an already open unlock/change prompt.
        {
            let owner = machine(&handle);
            let mut state = owner.machine.lock().expect("app lock");
            state.epoch = state.epoch.wrapping_add(1);
            state.authenticating = None;
            state.auto_prompted = true;
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
        alert.setMessageText(&NSString::from_str(&catalog.quit_title));
        alert.setInformativeText(&NSString::from_str(&catalog.quit_message));
        for title in [&catalog.quit_keep, &catalog.quit_stop, &catalog.cancel] {
            alert.addButtonWithTitle(&NSString::from_str(title));
        }
        let window = alert.window();
        PRESENTATION.with_borrow_mut(|p| p.quit_window = Some(Weak::from_retained(&window)));
        sync_windows(&handle);
        let response = alert.runModal();
        PRESENTATION.with_borrow_mut(|p| p.quit_window = None);
        let answer = match response {
            1000 => tauri_plugin_dialog::MessageDialogResult::Yes,
            1001 => tauri_plugin_dialog::MessageDialogResult::No,
            _ => tauri_plugin_dialog::MessageDialogResult::Cancel,
        };
        callback(answer);
    });
    true
}
fn sync_on_main(app: &AppHandle, mtm: MainThreadMarker) {
    if !is_locked(app) {
        let (covers, hidden) =
            PRESENTATION.with_borrow_mut(|p| (std::mem::take(&mut p.covers), std::mem::take(&mut p.hidden_windows)));
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
                })
            });
            view
        };
        if view.ivars().compact.replace(compact) != compact {
            view.ivars()
                .title
                .setFont(Some(&NSFont::boldSystemFontOfSize(if compact { 17.0 } else { 22.0 })));
            view.arrange();
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
        view.displayIfNeeded();
        window.displayIfNeeded();
    }
}

#[derive(Clone, Copy)]
enum Event {
    Leave,
    Arrive,
    SystemLeave(u8),
    SystemReturn(u8),
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
                machine(app).machine.lock().expect("app lock").arrive(now());
            }
        }
        Event::SystemLeave(reason) => {
            machine(app).machine.lock().expect("app lock").block(reason, now());
            let (context, reply) = PRESENTATION.with_borrow_mut(|p| (p.context.take(), p.reply.take()));
            if let Some(context) = context {
                unsafe {
                    context.invalidate();
                }
            }
            if let Some(reply) = reply {
                let _ = reply.send(result(app, Some("cancelled")));
            }
        }
        Event::SystemReturn(reason) => {
            refresh(app);
            machine(app)
                .machine
                .lock()
                .expect("app lock")
                .unblock(reason, active(), now());
        }
        Event::Windows => {}
    }
    sync_windows(app);
    if matches!(event, Event::Arrive | Event::SystemReturn(_)) {
        if active() && machine(app).machine.lock().expect("app lock").should_prompt() {
            authenticate(app, Authentication::Unlock, None);
        }
        deliver_pending(app);
    }
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
        Err(e) => return Err(format!("App Lock preference: {e}")),
    };
    app.manage(Owner {
        machine: Mutex::new(Machine::new(policy, availability())),
        pending: Mutex::new(None),
        path,
    });
    PRESENTATION.with_borrow_mut(|p| p.app = Some(app.clone()));
    install_window_hooks();
    let center = NSNotificationCenter::defaultCenter();
    unsafe {
        observe(app, &center, NSApplicationDidResignActiveNotification, Event::Leave);
        observe(app, &center, NSApplicationDidBecomeActiveNotification, Event::Arrive);
        for name in [
            NSWindowDidBecomeKeyNotification,
            NSWindowDidChangeOcclusionStateNotification,
            NSWindowDidResizeNotification,
        ] {
            observe(app, &center, name, Event::Windows);
        }
        let workspace = NSWorkspace::sharedWorkspace().notificationCenter();
        for (name, reason) in [
            (NSWorkspaceWillSleepNotification, 1),
            (NSWorkspaceScreensDidSleepNotification, 2),
            (NSWorkspaceSessionDidResignActiveNotification, 4),
        ] {
            observe(app, &workspace, name, Event::SystemLeave(reason));
        }
        for (name, reason) in [
            (NSWorkspaceDidWakeNotification, 1),
            (NSWorkspaceScreensDidWakeNotification, 2),
            (NSWorkspaceSessionDidBecomeActiveNotification, 4),
        ] {
            observe(app, &workspace, name, Event::SystemReturn(reason));
        }
    }
    let distributed = NSDistributedNotificationCenter::defaultCenter();
    observe(
        app,
        &distributed,
        &NSString::from_str("com.apple.screenIsLocked"),
        Event::SystemLeave(8),
    );
    observe(
        app,
        &distributed,
        &NSString::from_str("com.apple.screenIsUnlocked"),
        Event::SystemReturn(8),
    );
    sync_windows(app);
    let handle = app.clone();
    tauri::async_runtime::spawn(async move {
        loop {
            tokio::time::sleep(std::time::Duration::from_millis(250)).await;
            let app = handle.clone();
            if handle
                .run_on_main_thread(move || {
                    let changed = {
                        let owner = machine(&app);
                        let mut state = owner.machine.lock().expect("app lock");
                        let before = state.locked;
                        state.tick(now());
                        before != state.locked
                    };
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
    if view.window().is_some() && !PAINTING.with(Cell::get) {
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
    if view.window().is_some() && !PAINTING.with(Cell::get) {
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
