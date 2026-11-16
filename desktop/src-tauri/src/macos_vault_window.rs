//! Shows the Vault sandbox's own window inside the app.
//!
//! A protected Vault step ends in a top-level window of the sandbox
//! (`https://sandbox.avibe.bot`): the passkey is created there, and every
//! approval, signature and reveal is confirmed there. The window finds the
//! request through `window.opener`, so it has to be a real script-opened window
//! of the Workbench's webview. The shell otherwise sends every new window to
//! the system browser, where that link does not exist.
//!
//! For exactly this case the shell answers WebKit's request for a new webview
//! itself, before wry does, and builds the child by hand. wry's own child
//! shares the opener's `WKUserContentController` (the shell's init scripts and
//! the `ipc` message handler would run in it), has no navigation rule, and does
//! not close on `window.close()`.
//!
//! What holds for a child:
//!
//! - **Only the sandbox origin.** It is created for a request to the sandbox
//!   origin, or for `about:blank` (the Workbench reserves the window in the
//!   approval click and points it at the sandbox once the pinned release is
//!   verified). Every navigation in it, by the opener, the page, a redirect or
//!   a nested request for a window, is refused unless it stays on the sandbox
//!   origin. Every other request for a window still goes to wry, and so to the
//!   browser. The shell enforces the origin, not which sandbox release loads;
//!   pinning the release is the Workbench's check, as in a browser.
//! - **Never the opener's pixels.** The Workbench origin also serves
//!   Agent-authored Show Pages, and an opener can draw in the `about:blank` it
//!   reserved. The native window is created ordered out. When a sandbox
//!   document commits it comes on screen empty: its webview is transparent, it
//!   cannot become key, and it ignores clicks, which pass to whatever is under
//!   it. WebKit goes on displaying the previous document's layers until the
//!   new one has something to show, so the webview is revealed only by the
//!   first presentation update WebKit delivers after that commit, which
//!   carries the sandbox document's layers. The empty window stays until
//!   then, at most [`RESERVE_DEADLINE`]. From the reveal on the document is
//!   cross-origin to the opener and can only be replaced by another sandbox
//!   document. A child that commits anything else is closed; one that has
//!   displayed nothing within the deadline is closed too.
//! - **Not without the user's attention.** WebKit on macOS lets scripts open
//!   windows without a user gesture, which a browser would require, so a page
//!   of the Workbench origin can make the sandbox's real window, with a real
//!   pending request's card, appear at a moment it chooses. It cannot put
//!   content of its own into the card. The shell bounds the moment: a child is
//!   on screen only while this app is active and the opener's window is on
//!   screen; not within [`SETTLE_AFTER_ARRIVAL`] of the app becoming active or
//!   that window coming back, whatever caused either (it waits); only one at a
//!   time; and not within [`QUIET_AFTER_CLOSE`] of another leaving the screen.
//!   The same is asked again before the reveal. A child that may not appear is
//!   closed, never kept for later. Its size is clamped. The title names the
//!   allowed host and ignores the page's own title, but it is not proof of
//!   identity: other native windows of this app can carry any title.
//! - **Nothing of the shell inside it.** It gets a `WKUserContentController` of
//!   its own with no scripts and no message handler, so nothing wry registered
//!   reaches it and closing it removes nothing from the opener, and default
//!   `WKPreferences` of its own with element fullscreen, picture in picture and
//!   video presentation modes off. WebKit also copies the opener's custom URL
//!   schemes (`ipc`, `tauri`) into the child's configuration and offers no way
//!   to remove them; wry's handler for them reads fields that only its own
//!   webview class has. Every such handler is therefore guarded: a request
//!   from a child fails before the handler runs. Tauri never learns the child
//!   exists, so it has no label, no capability and no page Tauri could
//!   classify.
//! - **Opener and lifetime.** The child is created from the configuration WebKit
//!   hands over, which carries the opener link and the opener's website data
//!   store, persistent or not. `window.close()` closes the native window;
//!   closing the native window destroys the page, which the opener sees as a
//!   closed popup. A committed navigation of the opener, or its window being
//!   closed or destroyed, closes its children.
//!
//! Three of these rest on WebKit methods outside its public API: the
//! presentation update (`_doAfterNextPresentationUpdate:`), destroying the page
//! (`_close`), and the feature list of `WKPreferences`. Each is checked to
//! exist before anything is installed; without one there is no child and no
//! marker. That they exist says nothing about what they do: their behaviour is
//! checked only by the WebView probe, on the macOS versions CI runs it on.
//!
//! The Workbench learns that this works from
//! `window.__AVIBE_DESKTOP_VAULT_PASSKEY__`, defined only when the hooks above
//! are installed on this webview, the system is one of [`PROBED_MACOS`], and
//! the running app's own code signature associates it with the sandbox's
//! passkeys. Without all three nothing here is installed and every window
//! request is handled as before.

use std::cell::{Cell, RefCell};
use std::ffi::{c_char, c_void};
use std::ptr::NonNull;
use std::rc::Rc;
use std::sync::{Mutex, OnceLock};
use std::time::{Duration, Instant};

use block2::{DynBlock, RcBlock};
use objc2::rc::{Retained, Weak};
use objc2::runtime::{AnyClass, AnyObject, Bool, Imp, NSObject, ProtocolObject, Sel};
use objc2::{define_class, msg_send, sel, ClassType, DefinedClass, MainThreadMarker, MainThreadOnly};
use objc2_app_kit::{
    NSApplication, NSApplicationDidBecomeActiveNotification, NSAutoresizingMaskOptions, NSBackingStoreType, NSView,
    NSWindow, NSWindowDelegate, NSWindowDidChangeOcclusionStateNotification, NSWindowDidDeminiaturizeNotification,
    NSWindowDidMiniaturizeNotification, NSWindowOrderingMode, NSWindowStyleMask,
};
use objc2_core_foundation::{CFRetained, CFString};
use objc2_foundation::{
    ns_string, NSArray, NSError, NSNotification, NSNotificationCenter, NSObjectProtocol, NSOperationQueue, NSPoint,
    NSProcessInfo, NSRect, NSSize, NSString, NSURL,
};
use objc2_security::SecTask;
use objc2_web_kit::{
    WKFrameInfo, WKMediaCaptureType, WKNavigation, WKNavigationAction, WKNavigationActionPolicy, WKNavigationDelegate,
    WKNavigationResponse, WKNavigationResponsePolicy, WKPermissionDecision, WKPreferences, WKSecurityOrigin,
    WKUIDelegate, WKURLSchemeTask, WKUserContentController, WKUserScript, WKUserScriptInjectionTime, WKWebView,
    WKWebViewConfiguration, WKWindowFeatures,
};
use tauri::{AppHandle, Manager, WebviewWindow, WindowEvent};
use url::Url;

/// Tells the Workbench, before any of its scripts run, that protected Vault
/// passkey steps work inside this app. Top-level document only.
const DESKTOP_VAULT_PASSKEY_MARKER: &str =
    "if (window.self === window.top) Object.defineProperty(window, '__AVIBE_DESKTOP_VAULT_PASSKEY__', { value: true });";

/// The entitlement that lets WebKit ask the system for a site's passkeys.
const ASSOCIATED_DOMAINS: &str = "com.apple.developer.associated-domains";
const SANDBOX_CREDENTIALS: &str = "webcredentials:sandbox.avibe.bot";
/// The macOS major versions on which the WebView probe has passed in CI. It
/// is a list, not a floor: what a child may display rests on WebKit methods
/// outside its public API, whose behaviour only the probe checks, so a system
/// it has not passed on gets no marker. Add a version when CI passes on it.
///
/// Passkey PRF, which the protected tier derives its key from, starts with
/// macOS 15, but 15 is not here: on a hosted macOS 15 runner the probe could
/// not read the sandbox page back from the window, so nothing shows there
/// that an opener's page is never displayed.
const PROBED_MACOS: [isize; 1] = [26];

/// How long a child may exist without displaying a sandbox document. The
/// Workbench verifies the pinned release (15 s at most) before it navigates a
/// reserved window, and the sandbox gives up pairing after 30 s.
const RESERVE_DEADLINE: Duration = Duration::from_secs(60);
/// The flows open one window at a time; this bounds what the main webview's
/// pages can hold, hidden ones included.
const MAX_CHILDREN: usize = 4;
/// After a child leaves the screen, no other appears for this long. Longer than
/// the default double-click interval (half a second), so a click meant for the
/// window that just closed never meets a new one in its place; shorter than a
/// person needs to go back to the Workbench, approve another request and have
/// its sandbox release verified and loaded.
const QUIET_AFTER_CLOSE: Duration = Duration::from_millis(750);
/// After this app becomes the active one, or the opener's window comes back
/// from hidden or minimized, no child is on screen for this long. Whatever
/// brought the app forward (a notification the user clicked, a link, a page
/// asking for the window), the user gets time to see where they are before a
/// card can appear. A child whose document commits meanwhile waits.
const SETTLE_AFTER_ARRIVAL: Duration = Duration::from_secs(2);
/// The size of a child's content, whatever the opener asks for.
const DEFAULT_SIZE: (f64, f64) = (520.0, 760.0);
const LEAST_SIZE: (f64, f64) = (360.0, 400.0);
const MOST_SIZE: (f64, f64) = (900.0, 1000.0);

/// The one origin a child may show.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct Origin {
    pub(crate) scheme: String,
    pub(crate) host: String,
    /// `None` is the scheme's default port.
    pub(crate) port: Option<u16>,
}

impl Origin {
    fn sandbox() -> Self {
        Self {
            scheme: "https".to_owned(),
            host: "sandbox.avibe.bot".to_owned(),
            port: None,
        }
    }

    fn holds(&self, url: &Url) -> bool {
        url.scheme() == self.scheme
            && url.host_str() == Some(self.host.as_str())
            && url.port() == self.port
            && url.username().is_empty()
            && url.password().is_none()
    }

    /// What the window's title bar says: where its content comes from.
    fn title(&self) -> String {
        match self.port {
            Some(port) => format!("{}:{port}", self.host),
            None => self.host.clone(),
        }
    }
}

#[derive(Clone)]
pub(crate) struct Policy {
    pub(crate) origin: Origin,
    /// How long a child may exist without displaying a document of `origin`.
    pub(crate) reserve_deadline: Duration,
}

/// What the opener asked a new window for.
#[derive(Debug, PartialEq, Eq)]
enum Request {
    /// A sandbox document.
    Sandbox,
    /// The blank window reserved ahead of one.
    Reserve,
    /// Anything else stays with wry, which hands web links to the browser.
    Other,
}

fn classify(url: Option<&Url>, origin: &Origin) -> Request {
    match url {
        Some(url) if origin.holds(url) => Request::Sandbox,
        Some(url) if is_blank(url) => Request::Reserve,
        _ => Request::Other,
    }
}

fn is_blank(url: &Url) -> bool {
    url.as_str() == "about:blank"
}

/// Whether a child may start a navigation. `committed` is whether it has shown
/// a sandbox document; before that its main frame may still hold the reserved
/// blank page.
fn child_may_navigate(
    origin: &Origin,
    url: Option<&Url>,
    new_context: bool,
    download: bool,
    main_frame: bool,
    committed: bool,
) -> bool {
    if new_context || download {
        return false;
    }
    url.is_some_and(|url| origin.holds(url) || (main_frame && !committed && is_blank(url)))
}

#[derive(Debug, PartialEq, Eq)]
enum Commit {
    /// A sandbox document: the window may be on screen.
    Show,
    /// Still the reserved blank page: the window stays hidden.
    Wait,
    /// Anything else must never be displayed.
    Close,
}

fn commit_outcome(origin: &Origin, url: Option<&Url>, committed: bool) -> Commit {
    match url {
        Some(url) if origin.holds(url) => Commit::Show,
        Some(url) if !committed && is_blank(url) => Commit::Wait,
        _ => Commit::Close,
    }
}

/// What decides whether a child whose sandbox document has committed may be
/// on screen. WebKit lets scripts open windows without a user gesture, so these
/// stand in for the attention a browser would demand.
#[derive(Clone, Copy, Debug)]
struct Attention {
    /// The user is in this app.
    app_active: bool,
    /// How long ago this app last became the active one.
    since_app_active: Option<Duration>,
    /// The opener's window is on screen: visible, on the active Space, not
    /// minimized.
    opener_on_screen: bool,
    /// How long ago the opener's window last came back on screen.
    since_opener_on_screen: Option<Duration>,
    /// Another child of this opener is on screen.
    sibling_on_screen: bool,
    /// How long ago a child of this opener last left the screen.
    since_sibling_left: Option<Duration>,
}

#[derive(Debug, PartialEq, Eq)]
enum Appearance {
    Now,
    /// Not yet: ask again after this long.
    After(Duration),
    /// The child is closed, never kept for a later moment.
    Never,
}

fn appearance(attention: Attention) -> Appearance {
    if !attention.app_active || !attention.opener_on_screen {
        return Appearance::Never;
    }
    let arrived = [attention.since_app_active, attention.since_opener_on_screen]
        .into_iter()
        .flatten()
        .min();
    if let Some(remaining) = arrived.and_then(|since| SETTLE_AFTER_ARRIVAL.checked_sub(since)) {
        if !remaining.is_zero() {
            return Appearance::After(remaining);
        }
    }
    if attention.sibling_on_screen
        || attention
            .since_sibling_left
            .is_some_and(|since| since < QUIET_AFTER_CLOSE)
    {
        return Appearance::Never;
    }
    Appearance::Now
}

/// The content size of a child for the size its opener asked for.
fn child_size(width: Option<f64>, height: Option<f64>) -> (f64, f64) {
    let clamp = |asked: Option<f64>, fallback: f64, least: f64, most: f64| match asked {
        Some(asked) if asked.is_finite() => asked.clamp(least, most),
        _ => fallback,
    };
    (
        clamp(width, DEFAULT_SIZE.0, LEAST_SIZE.0, MOST_SIZE.0),
        clamp(height, DEFAULT_SIZE.1, LEAST_SIZE.1, MOST_SIZE.1),
    )
}

/// Whether this build may tell the Workbench that Vault passkeys work in-app.
fn advertises_passkeys(macos_major: isize, associated_domains: Option<&[String]>) -> bool {
    PROBED_MACOS.contains(&macos_major)
        && associated_domains.is_some_and(|domains| domains.iter().any(|domain| domain == SANDBOX_CREDENTIALS))
}

/// The associated domains in the running process's own code signature, as the
/// kernel holds them. A build flag would also be true for an ad-hoc or
/// development build, where WebKit is refused the passkey.
fn signed_associated_domains() -> Option<Vec<String>> {
    // SAFETY: Both calls take the current task and a constant name; the error
    // out-parameter is optional.
    let value = unsafe {
        let task = SecTask::from_self(None)?;
        task.value_for_entitlement(&CFString::from_static_str(ASSOCIATED_DOMAINS), std::ptr::null_mut())?
    };
    // SAFETY: A Core Foundation object is an Objective-C object, and the
    // ownership `CFRetained` held moves to `Retained`. Its type is checked next.
    let value: Retained<AnyObject> = unsafe { Retained::from_raw(CFRetained::into_raw(value).as_ptr().cast())? };
    let domains = value.downcast::<NSArray>().ok()?;
    Some(
        domains
            .iter()
            .filter_map(|domain| domain.downcast_ref::<NSString>().map(ToString::to_string))
            .collect(),
    )
}

/// Makes the main window able to show the sandbox's windows, and says so to the
/// Workbench, when this system and this build can complete a passkey there.
pub fn install(window: &WebviewWindow) {
    let macos_major = NSProcessInfo::processInfo().operatingSystemVersion().majorVersion;
    if !advertises_passkeys(macos_major, signed_associated_domains().as_deref()) {
        return;
    }
    install_with(
        window,
        Policy {
            origin: Origin::sandbox(),
            reserve_deadline: RESERVE_DEADLINE,
        },
    );
}

/// [`install`] without its admission, for an origin the caller names. The
/// shell's only caller is `install`. The hermetic WebView probe compiles this
/// file into itself and enters here with a loopback origin, because it can
/// neither serve `https://sandbox.avibe.bot` nor carry the entitlement.
pub(crate) fn install_with(window: &WebviewWindow, policy: Policy) {
    let app = window.app_handle().clone();
    let events = window.clone();
    let _ = window.with_webview(move |platform| {
        let Some(mtm) = MainThreadMarker::new() else {
            return;
        };
        // SAFETY: Tauri hands out the live WKWebView, on the main thread, for
        // the duration of this closure.
        let Some(webview) = (unsafe { Retained::retain(platform.inner().cast::<WKWebView>()) }) else {
            return;
        };
        let key = Retained::as_ptr(&webview) as usize;
        let installed = STATE.with_borrow(|state| {
            state
                .openers
                .iter()
                .any(|opener| opener.key == key && opener.webview.load().is_some())
        });
        if installed {
            return;
        }
        if !adopt(&webview) {
            eprintln!("the webview changed shape; Vault sandbox windows stay in the system browser");
            return;
        }
        let Some(window) = webview.window() else {
            return;
        };
        let observers = observe_arrivals(key, &window);
        let on_screen = is_on_screen(&window);
        STATE.with_borrow_mut(|state| {
            state.policy.get_or_insert(policy);
            state
                .openers
                .retain(|opener| opener.key != key && opener.webview.load().is_some());
            state.openers.push(Opener {
                key,
                webview: Weak::from_retained(&webview),
                window: Weak::from_retained(&window),
                app: app.clone(),
                on_screen,
                on_screen_since: None,
                child_left_screen: None,
                observers,
            });
        });
        // Only now is the claim true for this webview. Its controller is the
        // one wry registered the other markers on.
        // SAFETY: Main thread; the script is a constant.
        unsafe {
            let marker = WKUserScript::initWithSource_injectionTime_forMainFrameOnly(
                mtm.alloc(),
                &NSString::from_str(DESKTOP_VAULT_PASSKEY_MARKER),
                WKUserScriptInjectionTime::AtDocumentStart,
                true,
            );
            webview.configuration().userContentController().addUserScript(&marker);
        }
        events.on_window_event(move |event| {
            // The shell hides its window when the user closes it; either way
            // the children go with it.
            let gone = match event {
                WindowEvent::CloseRequested { .. } => false,
                WindowEvent::Destroyed => true,
                _ => return,
            };
            if MainThreadMarker::new().is_some() {
                opener_closed(key, gone);
            } else {
                let _ = app.run_on_main_thread(move || opener_closed(key, gone));
            }
        });
    });
}

/// The opener committed a new document: the one that opened its children is
/// gone, and so is the request they were answering.
pub fn opener_navigated(window: &WebviewWindow) {
    let _ = window.with_webview(|platform| {
        if MainThreadMarker::new().is_some() {
            close_children_of(platform.inner() as usize);
        }
    });
}

/// The opener's window was closed by the user (`gone` is false: the shell
/// keeps it, hidden) or destroyed.
fn opener_closed(key: usize, gone: bool) {
    close_children_of(key);
    if gone {
        let observers = STATE.with_borrow_mut(|state| {
            let index = state.openers.iter().position(|opener| opener.key == key)?;
            Some(state.openers.remove(index).observers)
        });
        let center = NSNotificationCenter::defaultCenter();
        for observer in observers.into_iter().flatten() {
            let observer: &AnyObject = observer.as_ref();
            // SAFETY: The token `addObserverForName` returned, removed once.
            unsafe { center.removeObserver(observer) };
        }
    }
}

/// Whether a window is where the user can see it.
fn is_on_screen(window: &NSWindow) -> bool {
    window.isVisible() && window.isOnActiveSpace() && !window.isMiniaturized()
}

type Observer = Retained<ProtocolObject<dyn NSObjectProtocol>>;

/// Records the two arrivals [`SETTLE_AFTER_ARRIVAL`] counts from: this app
/// becoming the active one, whatever caused it, and the opener's window coming
/// back on screen. Returns the window's observers, which end with the opener.
fn observe_arrivals(opener: usize, window: &NSWindow) -> Vec<Observer> {
    let center = NSNotificationCenter::defaultCenter();
    let watching_app = STATE.with_borrow(|state| state.app_observer.is_some());
    if !watching_app {
        let became_active = RcBlock::new(|_: NonNull<NSNotification>| {
            STATE.with_borrow_mut(|state| state.app_active_since = Some(Instant::now()));
        });
        // SAFETY: Main thread; the block is called on the posting thread,
        // which for AppKit's notifications is the main thread.
        let observer = unsafe {
            center.addObserverForName_object_queue_usingBlock(
                Some(NSApplicationDidBecomeActiveNotification),
                None,
                None,
                &became_active,
            )
        };
        // Kept for the life of the process.
        STATE.with_borrow_mut(|state| state.app_observer = Some(observer));
    }
    // SAFETY: As above; the statics are AppKit's notification names.
    unsafe {
        [
            NSWindowDidChangeOcclusionStateNotification,
            NSWindowDidDeminiaturizeNotification,
            NSWindowDidMiniaturizeNotification,
        ]
        .into_iter()
        .map(|name| {
            let changed = RcBlock::new(move |_: NonNull<NSNotification>| opener_window_changed(opener));
            center.addObserverForName_object_queue_usingBlock(Some(name), Some(window), None, &changed)
        })
        .collect()
    }
}

/// The opener's window was shown, hidden, minimized, restored or moved between
/// Spaces: note the moment it comes back on screen.
fn opener_window_changed(key: usize) {
    let window = STATE.with_borrow(|state| {
        state
            .openers
            .iter()
            .find(|opener| opener.key == key)
            .and_then(|opener| opener.window.load())
    });
    note_opener_on_screen(key, window.is_some_and(|window| is_on_screen(&window)));
}

/// Records whether the opener's window is on screen, and returns how long ago
/// it last came back. AppKit reports some of these changes late, so every
/// decision about a child also passes what it sees through here: a window found
/// on screen that was last seen off it has, at the latest, come back now.
fn note_opener_on_screen(key: usize, on_screen: bool) -> Option<Duration> {
    STATE.with_borrow_mut(|state| {
        let opener = state.openers.iter_mut().find(|opener| opener.key == key)?;
        if on_screen && !opener.on_screen {
            opener.on_screen_since = Some(Instant::now());
        }
        opener.on_screen = on_screen;
        opener.on_screen_since.map(|since| since.elapsed())
    })
}

fn close_children_of(opener: usize) {
    let children: Vec<u64> = STATE.with_borrow(|state| {
        state
            .children
            .iter()
            .filter(|child| child.opener == opener)
            .map(|child| child.id)
            .collect()
    });
    for id in children {
        close(id, false);
    }
}

struct Opener {
    /// The webview's address, which is how WebKit names it in callbacks.
    key: usize,
    /// Proves the address still belongs to the webview that was installed.
    webview: Weak<WKWebView>,
    window: Weak<NSWindow>,
    app: AppHandle,
    /// Whether its window was on screen when last looked at, and when it last
    /// came back.
    on_screen: bool,
    on_screen_since: Option<Instant>,
    /// When a child of this opener last left the screen.
    child_left_screen: Option<Instant>,
    observers: Vec<Observer>,
}

struct Child {
    id: u64,
    opener: usize,
    window: Retained<SandboxWindow>,
    webview: Retained<SandboxWebView>,
    /// WebKit and AppKit hold their delegates weakly; this keeps it alive.
    _delegate: Retained<ChildDelegate>,
    /// A sandbox document has committed. Before that the main frame may hold
    /// the reserved blank page; after it, only sandbox documents.
    committed: bool,
    /// The native window is on screen. Its webview stays transparent, and the
    /// window takes neither clicks nor keys, until the sandbox document has
    /// been presented.
    on_screen: bool,
}

#[derive(Default)]
struct State {
    policy: Option<Policy>,
    openers: Vec<Opener>,
    children: Vec<Child>,
    next_id: u64,
    /// When this app last became the active one, and what tells us.
    app_active_since: Option<Instant>,
    app_observer: Option<Observer>,
}

thread_local! {
    /// Main thread only: every entry point is a WebKit or AppKit callback.
    static STATE: RefCell<State> = RefCell::new(State::default());
}

define_class!(
    /// A child's webview. The class is what the URL scheme guard recognizes.
    #[unsafe(super(WKWebView))]
    #[thread_kind = MainThreadOnly]
    #[name = "AvibeVaultSandboxWebView"]
    struct SandboxWebView;
);

struct WindowFields {
    /// The sandbox document has been presented; see `appear` and `reveal`.
    revealed: Cell<bool>,
}

define_class!(
    /// A child's window. Until its page is displayed it cannot take the
    /// keyboard, so no key can reach a page the user cannot see.
    #[unsafe(super(NSWindow))]
    #[thread_kind = MainThreadOnly]
    #[name = "AvibeVaultSandboxWindow"]
    #[ivars = WindowFields]
    struct SandboxWindow;

    impl SandboxWindow {
        #[unsafe(method(canBecomeKeyWindow))]
        fn can_become_key(&self) -> bool {
            self.ivars().revealed.get()
        }

        #[unsafe(method(canBecomeMainWindow))]
        fn can_become_main(&self) -> bool {
            self.ivars().revealed.get()
        }
    }
);

struct ChildFields {
    id: u64,
}

define_class!(
    #[unsafe(super(NSObject))]
    #[thread_kind = MainThreadOnly]
    #[name = "AvibeVaultSandboxWindowDelegate"]
    #[ivars = ChildFields]
    struct ChildDelegate;

    unsafe impl NSObjectProtocol for ChildDelegate {}

    unsafe impl WKNavigationDelegate for ChildDelegate {
        #[unsafe(method(webView:decidePolicyForNavigationAction:decisionHandler:))]
        fn decide_action(
            &self,
            _webview: &WKWebView,
            action: &WKNavigationAction,
            handler: &DynBlock<dyn Fn(WKNavigationActionPolicy)>,
        ) {
            // SAFETY: WebKit calls this on the main thread with a live action.
            let (url, target_is_main, download) = unsafe {
                let download = action.respondsToSelector(sel!(shouldPerformDownload)) && action.shouldPerformDownload();
                let target_is_main = action.targetFrame().map(|frame| frame.isMainFrame());
                (nsurl(action.request().URL()), target_is_main, download)
            };
            let allowed = self.child_state().is_some_and(|(origin, committed)| {
                child_may_navigate(
                    &origin,
                    url.as_ref(),
                    target_is_main.is_none(),
                    download,
                    target_is_main == Some(true),
                    committed,
                )
            });
            handler.call((if allowed {
                WKNavigationActionPolicy::Allow
            } else {
                WKNavigationActionPolicy::Cancel
            },));
        }

        #[unsafe(method(webView:decidePolicyForNavigationResponse:decisionHandler:))]
        fn decide_response(
            &self,
            _webview: &WKWebView,
            response: &WKNavigationResponse,
            handler: &DynBlock<dyn Fn(WKNavigationResponsePolicy)>,
        ) {
            // The response is where a redirect ended up. Nothing is downloaded.
            // SAFETY: WebKit calls this on the main thread with a live response.
            let (url, showable) = unsafe { (nsurl(response.response().URL()), response.canShowMIMEType()) };
            let allowed = showable
                && self
                    .child_state()
                    .is_some_and(|(origin, _)| url.is_some_and(|url| origin.holds(&url)));
            handler.call((if allowed {
                WKNavigationResponsePolicy::Allow
            } else {
                WKNavigationResponsePolicy::Cancel
            },));
        }

        #[unsafe(method(webView:didCommitNavigation:))]
        fn did_commit(&self, webview: &WKWebView, _navigation: Option<&WKNavigation>) {
            // SAFETY: Main thread; the webview is live for this call.
            let url = unsafe { nsurl(webview.URL()) };
            committed(self.ivars().id, url.as_ref());
        }

        #[unsafe(method(webViewWebContentProcessDidTerminate:))]
        fn process_terminated(&self, _webview: &WKWebView) {
            close(self.ivars().id, false);
        }
    }

    unsafe impl WKUIDelegate for ChildDelegate {
        /// The sandbox's window never opens another.
        #[unsafe(method_id(webView:createWebViewWithConfiguration:forNavigationAction:windowFeatures:))]
        fn create_web_view(
            &self,
            _webview: &WKWebView,
            _configuration: &WKWebViewConfiguration,
            _action: &WKNavigationAction,
            _features: &WKWindowFeatures,
        ) -> Option<Retained<WKWebView>> {
            None
        }

        /// The sandbox's window uses neither camera nor microphone, and
        /// WebKit's own prompt must never come up for a window that is hidden.
        #[unsafe(method(webView:requestMediaCapturePermissionForOrigin:initiatedByFrame:type:decisionHandler:))]
        fn media_capture(
            &self,
            _webview: &WKWebView,
            _origin: &WKSecurityOrigin,
            _frame: &WKFrameInfo,
            _kind: WKMediaCaptureType,
            handler: &DynBlock<dyn Fn(WKPermissionDecision)>,
        ) {
            handler.call((WKPermissionDecision::Deny,));
        }

        /// `window.close()`, from the page or from its opener.
        #[unsafe(method(webViewDidClose:))]
        fn did_close(&self, _webview: &WKWebView) {
            close(self.ivars().id, false);
        }
    }

    unsafe impl NSWindowDelegate for ChildDelegate {
        /// The user closed the window.
        #[unsafe(method(windowWillClose:))]
        fn will_close(&self, _notification: &NSNotification) {
            close(self.ivars().id, true);
        }
    }
);

impl ChildDelegate {
    fn new(mtm: MainThreadMarker, id: u64) -> Retained<Self> {
        let this = Self::alloc(mtm).set_ivars(ChildFields { id });
        // SAFETY: `NSObject`'s designated initializer.
        unsafe { msg_send![super(this), init] }
    }

    /// The allowed origin and whether this child has shown a sandbox document;
    /// `None` once the child is closed.
    fn child_state(&self) -> Option<(Origin, bool)> {
        let id = self.ivars().id;
        STATE.with_borrow(|state| {
            let child = state.children.iter().find(|child| child.id == id)?;
            Some((state.policy.as_ref()?.origin.clone(), child.committed))
        })
    }
}

fn nsurl(url: Option<Retained<NSURL>>) -> Option<Url> {
    url.and_then(|url| url.absoluteString())
        .and_then(|url| Url::parse(&url.to_string()).ok())
}

/// A document committed in a child's main frame.
fn committed(id: u64, url: Option<&Url>) {
    let outcome = STATE.with_borrow_mut(|state| {
        let origin = state.policy.as_ref()?.origin.clone();
        let child = state.children.iter_mut().find(|child| child.id == id)?;
        let outcome = commit_outcome(&origin, url, child.committed);
        let first = outcome == Commit::Show && !child.committed;
        if outcome == Commit::Show {
            child.committed = true;
        }
        Some((outcome, first))
    });
    match outcome {
        Some((Commit::Close, _)) => close(id, false),
        Some((Commit::Show, true)) => appear(id),
        _ => {}
    }
}

/// What the user's attention is on, for one child. `None` when the child or its
/// opener is gone.
fn attention(id: u64, mtm: MainThreadMarker) -> Option<(Attention, Retained<NSWindow>, AppHandle)> {
    let (key, opener, sibling_on_screen, since_app_active, since_sibling_left, app) = STATE.with_borrow(|state| {
        let child = state.children.iter().find(|child| child.id == id)?;
        let opener = state.openers.iter().find(|opener| opener.key == child.opener)?;
        let sibling_on_screen = state
            .children
            .iter()
            .any(|other| other.opener == child.opener && other.id != id && other.on_screen);
        Some((
            opener.key,
            opener.window.load()?,
            sibling_on_screen,
            state.app_active_since.map(|since| since.elapsed()),
            opener.child_left_screen.map(|left| left.elapsed()),
            opener.app.clone(),
        ))
    })?;
    let opener_on_screen = is_on_screen(&opener);
    let attention = Attention {
        app_active: NSApplication::sharedApplication(mtm).isActive(),
        since_app_active,
        opener_on_screen,
        since_opener_on_screen: note_opener_on_screen(key, opener_on_screen),
        sibling_on_screen,
        since_sibling_left,
    };
    Some((attention, opener, app))
}

/// Calls `step(id)` on the main thread after `wait`.
fn later(app: AppHandle, wait: Duration, id: u64, step: fn(u64)) {
    tauri::async_runtime::spawn(async move {
        tokio::time::sleep(wait).await;
        let _ = app.run_on_main_thread(move || step(id));
    });
}

/// Puts a child whose sandbox document has committed on screen over its
/// opener's window, waits when the user has only just arrived, or closes it
/// when it may not appear.
///
/// What comes on screen is the window with a transparent webview. It ignores
/// clicks, which pass to whatever is under it, and cannot become key. WebKit
/// keeps displaying the previous document's layers until the new one has
/// something to show, and the previous document here is a blank page its
/// opener could draw in. The webview is revealed by the first presentation
/// update WebKit delivers after this point, which carries the sandbox
/// document's layers.
fn appear(id: u64) {
    let Some(mtm) = MainThreadMarker::new() else {
        return;
    };
    let waiting = STATE.with_borrow(|state| {
        state
            .children
            .iter()
            .find(|child| child.id == id)
            .filter(|child| child.committed && !child.on_screen)
            .map(|child| (child.window.clone(), child.webview.clone()))
    });
    let Some((window, webview)) = waiting else {
        return;
    };
    let Some((attention, parent, app)) = attention(id, mtm) else {
        close(id, false);
        return;
    };
    match appearance(attention) {
        Appearance::Never => close(id, false),
        Appearance::After(wait) => later(app, wait, id, appear),
        Appearance::Now => {
            STATE.with_borrow_mut(|state| {
                if let Some(child) = state.children.iter_mut().find(|child| child.id == id) {
                    child.on_screen = true;
                }
            });
            let over = parent.frame();
            let size = window.frame().size;
            window.setFrameOrigin(NSPoint::new(
                over.origin.x + (over.size.width - size.width) / 2.0,
                over.origin.y + (over.size.height - size.height) / 2.0,
            ));
            // Attaching orders the window in above its parent.
            // SAFETY: Both windows are live and distinct; the child is
            // detached again before it closes.
            unsafe { parent.addChildWindow_ordered(&window, NSWindowOrderingMode::Above) };
            let presented = RcBlock::new(move || reveal(id));
            // SAFETY: `adopt` checked that the webview class has this method,
            // which takes one block and calls it on the main thread.
            let _: () = unsafe { msg_send![&*webview, _doAfterNextPresentationUpdate: &*presented] };
        }
    }
}

/// The sandbox document is what the webview now displays. If the user is still
/// here, show it and give the window the keyboard, which a passkey ceremony
/// needs; if they have only just come back, wait; if they left, close it.
fn reveal(id: u64) {
    let Some(mtm) = MainThreadMarker::new() else {
        return;
    };
    let veiled = STATE.with_borrow(|state| {
        state
            .children
            .iter()
            .find(|child| child.id == id)
            .filter(|child| child.committed && child.on_screen && !child.window.ivars().revealed.get())
            .map(|child| (child.window.clone(), child.webview.clone()))
    });
    let Some((window, webview)) = veiled else {
        return;
    };
    let Some((attention, _, app)) = attention(id, mtm) else {
        close(id, false);
        return;
    };
    // This child is the one on screen; the rules about others do not apply.
    let attention = Attention {
        sibling_on_screen: false,
        since_sibling_left: None,
        ..attention
    };
    match appearance(attention) {
        Appearance::Never => close(id, false),
        Appearance::After(wait) => later(app, wait, id, reveal),
        Appearance::Now => {
            window.ivars().revealed.set(true);
            webview.setAlphaValue(1.0);
            window.setIgnoresMouseEvents(false);
            window.makeKeyAndOrderFront(None);
            window.makeFirstResponder(Some(&webview));
        }
    }
}

/// Ends a child. Its opener can no longer reach it and the native window
/// closes now; the page is destroyed on the next turn of the main run loop,
/// which the opener sees as a closed popup. `window_closing` is set when AppKit
/// is already closing the window.
fn close(id: u64, window_closing: bool) {
    let child = STATE.with_borrow_mut(|state| {
        let index = state.children.iter().position(|child| child.id == id)?;
        let child = state.children.remove(index);
        if child.on_screen {
            if let Some(opener) = state.openers.iter_mut().find(|opener| opener.key == child.opener) {
                opener.child_left_screen = Some(Instant::now());
            }
        }
        Some(child)
    });
    let Some(child) = child else {
        return;
    };
    // SAFETY: Main thread; every object is live and owned by `child`.
    unsafe {
        child.webview.setNavigationDelegate(None);
        child.webview.setUIDelegate(None);
    }
    child.window.setDelegate(None);
    let was_key = child.window.isKeyWindow();
    let parent = child.window.parentWindow();
    if let Some(parent) = &parent {
        parent.removeChildWindow(&child.window);
    }
    if !window_closing {
        child.window.close();
    }
    child.window.makeFirstResponder(None);
    child.webview.removeFromSuperview();
    if was_key {
        if let Some(parent) = &parent {
            parent.makeKeyWindow();
        }
    }
    // This may be running inside one of WebKit's own callbacks on this
    // webview (a commit, `window.close()`, a crashed process). The page is
    // destroyed, and the objects released, once that callback has returned.
    // Releasing the webview alone would not destroy the page while WebKit
    // still awaits a presentation update for it.
    let remains = Cell::new(Some(child));
    let destroy = RcBlock::new(move || {
        if let Some(child) = remains.take() {
            // SAFETY: Main thread. `adopt` checked that the webview class has
            // `_close`, which takes no argument.
            let _: () = unsafe { msg_send![&*child.webview, _close] };
        }
    });
    // SAFETY: The block runs once, on the main thread.
    unsafe { NSOperationQueue::mainQueue().addOperationWithBlock(&destroy) };
}

type CreateImp = unsafe extern "C-unwind" fn(
    &AnyObject,
    Sel,
    &WKWebView,
    &WKWebViewConfiguration,
    &WKNavigationAction,
    &WKWindowFeatures,
) -> *mut WKWebView;

/// wry's answer to a request for a new webview, which the shell defers to for
/// everything that is not a sandbox window, and the delegate class it came from.
static WRY_CREATE: OnceLock<(usize, CreateImp)> = OnceLock::new();

/// Puts the shell in front of wry for this webview's new-window requests and
/// guards the custom URL schemes a child would inherit. Returns whether both
/// hold; a child is never created otherwise.
fn adopt(webview: &WKWebView) -> bool {
    // SAFETY: Main thread; the delegate and the configuration are live objects.
    unsafe {
        let Some(delegate) = webview.UIDelegate() else {
            return false;
        };
        let delegate: &AnyObject = delegate.as_ref();
        let class = delegate.class() as *const AnyClass as usize;
        let selector = sel!(webView:createWebViewWithConfiguration:forNavigationAction:windowFeatures:);
        // wry defines one UI delegate class for every webview it builds, so the
        // first webview adopts it for the process.
        let hooked = WRY_CREATE.get_or_init(|| {
            let Some(method) = delegate.class().instance_method(selector) else {
                return (0, create_web_view as CreateImp);
            };
            // SAFETY: The selector fixes the signature of both implementations.
            let wry_create = std::mem::transmute::<Imp, CreateImp>(method.implementation());
            method.set_implementation(std::mem::transmute::<CreateImp, Imp>(create_web_view));
            (class, wry_create)
        });
        if hooked.0 != class {
            return false;
        }
        // A child is revealed by WebKit's presentation update and by nothing
        // else, and its page is destroyed by `_close`; without either method
        // there is no child.
        if !webview.respondsToSelector(sel!(_doAfterNextPresentationUpdate:))
            || !webview.respondsToSelector(sel!(_close))
        {
            return false;
        }
        let Some(mtm) = MainThreadMarker::new() else {
            return false;
        };
        if child_preferences(mtm).is_none() {
            return false;
        }
        if !guard_url_schemes() {
            return false;
        }
        // Tauri registers `ipc` on every webview. If the guard does not cover
        // its handler, the guard did not find what a child would inherit.
        let Some(ipc) = webview.configuration().urlSchemeHandlerForURLScheme(ns_string!("ipc")) else {
            return false;
        };
        let ipc: &AnyObject = ipc.as_ref();
        [sel!(webView:startURLSchemeTask:), sel!(webView:stopURLSchemeTask:)]
            .into_iter()
            .all(|selector| {
                ipc.class()
                    .instance_method(selector)
                    .is_some_and(|method| scheme_original(method as *const _ as usize).is_some())
            })
    }
}

unsafe extern "C-unwind" fn create_web_view(
    delegate: &AnyObject,
    selector: Sel,
    opener: &WKWebView,
    configuration: &WKWebViewConfiguration,
    action: &WKNavigationAction,
    features: &WKWindowFeatures,
) -> *mut WKWebView {
    // SAFETY: WebKit calls this on the main thread with live arguments.
    let url = unsafe { nsurl(action.request().URL()) };
    let key = opener as *const WKWebView as usize;
    let admitted = STATE.with_borrow(|state| {
        let opener = state.openers.iter().find(|opener| opener.key == key)?;
        opener.webview.load()?;
        let policy = state.policy.clone()?;
        match classify(url.as_ref(), &policy.origin) {
            Request::Other => None,
            Request::Sandbox | Request::Reserve => {
                Some((state.children.len() < MAX_CHILDREN).then(|| (policy, opener.app.clone())))
            }
        }
    });
    match admitted {
        // Not this module's request: wry sends it to the browser or drops it.
        None => {
            let (_, wry_create) = WRY_CREATE.get().expect("installed before it can be called");
            // SAFETY: The arguments are the ones WebKit passed for this selector.
            unsafe { wry_create(delegate, selector, opener, configuration, action, features) }
        }
        Some(None) => std::ptr::null_mut(),
        Some(Some((policy, app))) => match open_child(key, configuration, features, &policy, app) {
            Some(webview) => Retained::autorelease_return(Retained::into_super(webview)),
            None => std::ptr::null_mut(),
        },
    }
}

/// Builds a child, hidden, from the configuration WebKit prepared for it.
fn open_child(
    opener: usize,
    configuration: &WKWebViewConfiguration,
    features: &WKWindowFeatures,
    policy: &Policy,
    app: AppHandle,
) -> Option<Retained<SandboxWebView>> {
    let mtm = MainThreadMarker::new()?;
    // SAFETY: Main thread; `configuration` is WebKit's copy of the opener's,
    // made for this request.
    let (window, webview) = unsafe {
        // The copy still points at the opener's controller, with every script
        // and message handler wry registered, and at the opener's preferences.
        // The child gets an empty controller and preferences of its own.
        let shell_controller = configuration.userContentController();
        let shell_preferences = configuration.preferences();
        let empty = WKUserContentController::new(mtm);
        configuration.setUserContentController(&empty);
        let preferences = child_preferences(mtm)?;
        configuration.setPreferences(&preferences);
        let (width, height) = child_size(
            features.width().map(|width| width.doubleValue()),
            features.height().map(|height| height.doubleValue()),
        );
        let frame = NSRect::new(NSPoint::new(0.0, 0.0), NSSize::new(width, height));
        let webview: Retained<SandboxWebView> = msg_send![
            super(SandboxWebView::alloc(mtm).set_ivars(())),
            initWithFrame: frame,
            configuration: configuration
        ];
        let held = webview.configuration();
        let (held_controller, held_preferences) = (held.userContentController(), held.preferences());
        if !std::ptr::eq(&*held_controller, &*empty)
            || std::ptr::eq(&*held_controller, &*shell_controller)
            || std::ptr::eq(&*held_preferences, &*shell_preferences)
            || !child_features_off(&held_preferences)
        {
            return None;
        }
        let window: Retained<SandboxWindow> = msg_send![
            super(SandboxWindow::alloc(mtm).set_ivars(WindowFields {
                revealed: Cell::new(false),
            })),
            initWithContentRect: frame,
            styleMask: NSWindowStyleMask::Titled | NSWindowStyleMask::Closable | NSWindowStyleMask::Resizable,
            backing: NSBackingStoreType::Buffered,
            defer: false
        ];
        // This module owns the window; AppKit must not release it on close.
        window.setReleasedWhenClosed(false);
        let _: () = msg_send![&window, setRestorable: false];
        (window, webview)
    };
    // The title names the only origin the window can load. It is the page's
    // title that is ignored; it is not proof of what the window is.
    window.setTitle(&NSString::from_str(&policy.origin.title()));
    // Until the sandbox document has been presented, the webview draws
    // nothing and the window passes clicks through. See `appear`.
    webview.setAlphaValue(0.0);
    webview.setAutoresizingMask(
        NSAutoresizingMaskOptions::ViewWidthSizable | NSAutoresizingMaskOptions::ViewHeightSizable,
    );
    window.setIgnoresMouseEvents(true);
    let content = NSView::initWithFrame(mtm.alloc(), webview.frame());
    content.addSubview(&webview);
    window.setContentView(Some(&content));
    let id = STATE.with_borrow_mut(|state| {
        state.next_id += 1;
        state.next_id
    });
    let delegate = ChildDelegate::new(mtm, id);
    // SAFETY: Main thread; the delegate is kept alive by the child's entry.
    unsafe {
        webview.setNavigationDelegate(Some(ProtocolObject::from_ref(&*delegate)));
        webview.setUIDelegate(Some(ProtocolObject::from_ref(&*delegate)));
    }
    window.setDelegate(Some(ProtocolObject::from_ref(&*delegate)));
    STATE.with_borrow_mut(|state| {
        state.children.push(Child {
            id,
            opener,
            window,
            webview: webview.clone(),
            _delegate: delegate,
            committed: false,
            on_screen: false,
        });
    });
    let deadline = policy.reserve_deadline;
    tauri::async_runtime::spawn(async move {
        tokio::time::sleep(deadline).await;
        let _ = app.run_on_main_thread(move || {
            let waiting = STATE.with_borrow(|state| {
                state
                    .children
                    .iter()
                    .any(|child| child.id == id && !child.window.ivars().revealed.get())
            });
            if waiting {
                close(id, false);
            }
        });
    });
    Some(webview)
}

/// WebKit features a child is created without: the sandbox's window uses
/// neither picture in picture nor a video presentation mode, and both can put
/// content on screen outside the window.
const FEATURES_OFF: [&str; 3] = [
    "AllowsPictureInPictureMediaPlayback",
    "PictureInPictureAPIEnabled",
    "VideoPresentationModeAPIEnabled",
];

/// Default preferences, not the opener's, with element fullscreen and the
/// features above off. `None` when WebKit does not confirm each one is off;
/// a child is not created then.
fn child_preferences(mtm: MainThreadMarker) -> Option<Retained<WKPreferences>> {
    // SAFETY: Main thread. The feature list and its accessors are WebKit's
    // own, checked to exist before they are called.
    unsafe {
        let preferences = WKPreferences::new(mtm);
        preferences.setElementFullscreenEnabled(false);
        if WKPreferences::class().class_method(sel!(_features)).is_none()
            || !preferences.respondsToSelector(sel!(_setEnabled:forFeature:))
            || !preferences.respondsToSelector(sel!(_isEnabledForFeature:))
        {
            return None;
        }
        let features: Retained<NSArray> = msg_send![WKPreferences::class(), _features];
        for feature in features.iter() {
            let key: Option<Retained<NSString>> = msg_send![&*feature, key];
            if key.is_some_and(|key| FEATURES_OFF.contains(&key.to_string().as_str())) {
                let _: () = msg_send![&*preferences, _setEnabled: false, forFeature: &*feature];
            }
        }
        child_features_off(&preferences).then_some(preferences)
    }
}

/// Whether these preferences have element fullscreen and every feature in
/// [`FEATURES_OFF`] off, as WebKit itself reports them.
fn child_features_off(preferences: &WKPreferences) -> bool {
    // SAFETY: Main thread. `child_preferences` checked the accessors exist; a
    // preferences object is only ever passed here after that.
    unsafe {
        if preferences.isElementFullscreenEnabled()
            || WKPreferences::class().class_method(sel!(_features)).is_none()
            || !preferences.respondsToSelector(sel!(_isEnabledForFeature:))
        {
            return false;
        }
        let features: Retained<NSArray> = msg_send![WKPreferences::class(), _features];
        let mut off = 0;
        for feature in features.iter() {
            let key: Option<Retained<NSString>> = msg_send![&*feature, key];
            if !key.is_some_and(|key| FEATURES_OFF.contains(&key.to_string().as_str())) {
                continue;
            }
            let enabled: bool = msg_send![preferences, _isEnabledForFeature: &*feature];
            if enabled {
                return false;
            }
            off += 1;
        }
        off == FEATURES_OFF.len()
    }
}

type SchemeImp = unsafe extern "C-unwind" fn(&AnyObject, Sel, &WKWebView, &ProtocolObject<dyn WKURLSchemeTask>);

/// The implementation each guarded URL scheme method had, by method address.
static SCHEME_ORIGINALS: Mutex<Vec<(usize, SchemeImp)>> = Mutex::new(Vec::new());

fn scheme_original(method: usize) -> Option<SchemeImp> {
    SCHEME_ORIGINALS
        .lock()
        .ok()?
        .iter()
        .find(|(guarded, _)| *guarded == method)
        .map(|(_, original)| *original)
}

/// Guards every URL scheme handler class registered at run time, which is how
/// wry makes one per scheme. Returns `false` when the classes cannot be listed.
fn guard_url_schemes() -> bool {
    let Some(classes) = dynamic_classes() else {
        return false;
    };
    let Ok(mut originals) = SCHEME_ORIGINALS.lock() else {
        return false;
    };
    let guards = [
        (sel!(webView:startURLSchemeTask:), guarded_start as SchemeImp),
        (sel!(webView:stopURLSchemeTask:), guarded_stop as SchemeImp),
    ];
    for class in classes {
        // The class's own methods: reading them runs no code of the class.
        for method in class.instance_methods().iter() {
            let Some((_, guard)) = guards.iter().find(|(selector, _)| *selector == method.name()) else {
                continue;
            };
            let key = *method as *const _ as usize;
            if originals.iter().any(|(guarded, _)| *guarded == key) {
                continue;
            }
            // SAFETY: The selector fixes the signature of both implementations.
            unsafe {
                let original = std::mem::transmute::<Imp, SchemeImp>(method.implementation());
                originals.push((key, original));
                method.set_implementation(std::mem::transmute::<SchemeImp, Imp>(*guard));
            }
        }
    }
    true
}

/// Classes created at run time, as wry's and objc2's are. `objc_enumerateClasses`
/// exists from macOS 13; it is looked up by name so that the app still starts on
/// older systems, where this module is never installed.
fn dynamic_classes() -> Option<Vec<&'static AnyClass>> {
    type Enumerate = unsafe extern "C-unwind" fn(
        *const c_void,
        *const c_char,
        *const c_void,
        *const c_void,
        &DynBlock<dyn Fn(NonNull<AnyClass>, NonNull<Bool>)>,
    );
    extern "C" {
        fn dlsym(handle: *mut c_void, symbol: *const c_char) -> *mut c_void;
    }
    // `RTLD_DEFAULT` and `OBJC_DYNAMIC_CLASSES`, from <dlfcn.h> and <objc/runtime.h>.
    let default_handle = -2isize as *mut c_void;
    let dynamic_classes = -1isize as *const c_void;
    // SAFETY: The symbol name is a constant C string.
    let symbol = unsafe { dlsym(default_handle, c"objc_enumerateClasses".as_ptr()) };
    if symbol.is_null() {
        return None;
    }
    // SAFETY: The signature is the one <objc/runtime.h> declares.
    let enumerate = unsafe { std::mem::transmute::<*mut c_void, Enumerate>(symbol) };
    let found = Rc::new(RefCell::new(Vec::new()));
    let sink = found.clone();
    let block = RcBlock::new(move |class: NonNull<AnyClass>, _stop: NonNull<Bool>| {
        // SAFETY: A registered class lives for the rest of the process.
        sink.borrow_mut().push(unsafe { class.as_ref() });
    });
    // SAFETY: The block is only called during this call.
    unsafe {
        enumerate(
            dynamic_classes,
            std::ptr::null(),
            std::ptr::null(),
            std::ptr::null(),
            &block,
        )
    };
    let classes = found.borrow().clone();
    Some(classes)
}

fn is_child(webview: &WKWebView) -> bool {
    webview.downcast_ref::<SandboxWebView>().is_some()
}

unsafe extern "C-unwind" fn guarded_start(
    handler: &AnyObject,
    selector: Sel,
    webview: &WKWebView,
    task: &ProtocolObject<dyn WKURLSchemeTask>,
) {
    let original = handler
        .class()
        .instance_method(selector)
        .and_then(|method| scheme_original(method as *const _ as usize));
    match original {
        // SAFETY: The arguments are the ones WebKit passed for this selector.
        Some(original) if !is_child(webview) => unsafe { original(handler, selector, webview, task) },
        _ => {
            // NSURLErrorUnsupportedURL: the scheme does not exist for a child.
            let error = NSError::new(-1002, ns_string!("NSURLErrorDomain"));
            // SAFETY: The task has not been answered or stopped yet.
            unsafe { task.didFailWithError(&error) };
        }
    }
}

unsafe extern "C-unwind" fn guarded_stop(
    handler: &AnyObject,
    selector: Sel,
    webview: &WKWebView,
    task: &ProtocolObject<dyn WKURLSchemeTask>,
) {
    if is_child(webview) {
        return;
    }
    let original = handler
        .class()
        .instance_method(selector)
        .and_then(|method| scheme_original(method as *const _ as usize));
    if let Some(original) = original {
        // SAFETY: The arguments are the ones WebKit passed for this selector.
        unsafe { original(handler, selector, webview, task) }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn url(raw: &str) -> Url {
        Url::parse(raw).expect("test URL")
    }

    #[test]
    fn only_the_exact_sandbox_origin_is_the_sandbox() {
        let sandbox = Origin::sandbox();
        for raw in [
            "https://sandbox.avibe.bot/",
            "https://sandbox.avibe.bot/r/abc/index.html#mode=authorize&id=1",
            "https://sandbox.avibe.bot:443/",
            "HTTPS://SANDBOX.AVIBE.BOT/",
        ] {
            assert!(sandbox.holds(&url(raw)), "{raw}");
        }
        // WebKit and this parser both read a backslash as a path separator
        // here, so the host is still the sandbox.
        assert!(sandbox.holds(&url("https://sandbox.avibe.bot\\@evil.example/")));
        for raw in [
            "http://sandbox.avibe.bot/",
            "https://sandbox.avibe.bot:8443/",
            "https://sandbox.avibe.bot./",
            "https://avibe.bot/",
            "https://evil.sandbox.avibe.bot/",
            "https://sandbox.avibe.bot.evil.example/",
            "https://sandbox.avibe.bot@evil.example/",
            "https://user@sandbox.avibe.bot/",
            "https://user:secret@sandbox.avibe.bot/",
            "wss://sandbox.avibe.bot/",
            "about:blank",
            "blob:https://sandbox.avibe.bot/7f1c",
            "data:text/html,sandbox.avibe.bot",
            "javascript:alert(1)",
            "file:///sandbox.avibe.bot",
        ] {
            assert!(!sandbox.holds(&url(raw)), "{raw}");
        }
        assert_eq!(sandbox.title(), "sandbox.avibe.bot");
    }

    #[test]
    fn a_window_is_opened_only_for_the_sandbox_or_its_reserved_blank_page() {
        let sandbox = Origin::sandbox();
        assert_eq!(
            classify(Some(&url("https://sandbox.avibe.bot/#mode=setup")), &sandbox),
            Request::Sandbox
        );
        assert_eq!(classify(Some(&url("about:blank")), &sandbox), Request::Reserve);
        for raw in [
            "about:blank#x",
            "about:blank?x",
            "about:srcdoc",
            "https://github.com/avibe-bot/avibe",
            "http://127.0.0.1:5123/show/page",
            "https://avibe.bot/",
            "blob:http://127.0.0.1:5123/7f1c",
            "javascript:alert(1)",
            "data:text/html,hi",
            "file:///etc/hosts",
        ] {
            assert_eq!(classify(Some(&url(raw)), &sandbox), Request::Other, "{raw}");
        }
        assert_eq!(classify(None, &sandbox), Request::Other);
    }

    #[test]
    fn a_child_only_navigates_within_the_sandbox() {
        let sandbox = Origin::sandbox();
        let page = url("https://sandbox.avibe.bot/r/abc/index.html");
        let blank = url("about:blank");
        let elsewhere = url("http://127.0.0.1:5123/show/page");
        // (url, new context, download, main frame, committed) -> allowed
        for (url, new_context, download, main_frame, committed, allowed) in [
            (Some(&page), false, false, true, false, true),
            (Some(&page), false, false, true, true, true),
            (Some(&page), false, false, false, true, true),
            // The reserved page, until a sandbox document has been shown.
            (Some(&blank), false, false, true, false, true),
            (Some(&blank), false, false, true, true, false),
            (Some(&blank), false, false, false, false, false),
            // The opener, the page or a redirect leaving the sandbox.
            (Some(&elsewhere), false, false, true, false, false),
            (Some(&elsewhere), false, false, true, true, false),
            (Some(&elsewhere), false, false, false, true, false),
            // A nested window, a download, or no destination at all.
            (Some(&page), true, false, false, true, false),
            (Some(&page), false, true, true, true, false),
            (None, false, false, true, false, false),
        ] {
            assert_eq!(
                child_may_navigate(&sandbox, url, new_context, download, main_frame, committed),
                allowed,
                "{url:?} new_context={new_context} download={download} main_frame={main_frame} committed={committed}"
            );
        }
    }

    #[test]
    fn a_child_is_on_screen_only_for_a_committed_sandbox_document() {
        let sandbox = Origin::sandbox();
        let page = url("https://sandbox.avibe.bot/r/abc/index.html");
        let blank = url("about:blank");
        assert_eq!(commit_outcome(&sandbox, Some(&page), false), Commit::Show);
        assert_eq!(commit_outcome(&sandbox, Some(&page), true), Commit::Show);
        assert_eq!(commit_outcome(&sandbox, Some(&blank), false), Commit::Wait);
        // A shown window never goes back to a page its opener can script.
        assert_eq!(commit_outcome(&sandbox, Some(&blank), true), Commit::Close);
        for committed in [false, true] {
            assert_eq!(
                commit_outcome(&sandbox, Some(&url("http://127.0.0.1:5123/")), committed),
                Commit::Close
            );
            assert_eq!(commit_outcome(&sandbox, None, committed), Commit::Close);
        }
    }

    #[test]
    fn a_child_appears_only_with_the_users_attention_and_alone() {
        let attentive = Attention {
            app_active: true,
            since_app_active: None,
            opener_on_screen: true,
            since_opener_on_screen: None,
            sibling_on_screen: false,
            since_sibling_left: None,
        };
        assert_eq!(appearance(attentive), Appearance::Now);
        // The user is in another app, or the Workbench is hidden, minimized or
        // on another Space: closed, however long ago they were here.
        for elsewhere in [
            Attention {
                app_active: false,
                ..attentive
            },
            Attention {
                opener_on_screen: false,
                ..attentive
            },
            Attention {
                app_active: false,
                since_app_active: Some(Duration::from_millis(100)),
                ..attentive
            },
        ] {
            assert_eq!(appearance(elsewhere), Appearance::Never, "{elsewhere:?}");
        }
        // One sandbox window at a time, and not in the place of one that just
        // closed.
        assert_eq!(
            appearance(Attention {
                sibling_on_screen: true,
                ..attentive
            }),
            Appearance::Never
        );
        for (since, expected) in [
            (Duration::ZERO, Appearance::Never),
            (Duration::from_millis(749), Appearance::Never),
            (Duration::from_millis(750), Appearance::Now),
            (Duration::from_secs(60), Appearance::Now),
        ] {
            assert_eq!(
                appearance(Attention {
                    since_sibling_left: Some(since),
                    ..attentive
                }),
                expected,
                "{since:?}"
            );
        }
    }

    #[test]
    fn a_child_waits_while_the_user_has_only_just_arrived() {
        let attentive = Attention {
            app_active: true,
            since_app_active: None,
            opener_on_screen: true,
            since_opener_on_screen: None,
            sibling_on_screen: false,
            since_sibling_left: None,
        };
        let ms = Duration::from_millis;
        // The app became active, or its window came back: wait out the rest.
        for (since_app_active, since_opener_on_screen, expected) in [
            (Some(ms(0)), None, Appearance::After(ms(2000))),
            (Some(ms(500)), None, Appearance::After(ms(1500))),
            (None, Some(ms(1999)), Appearance::After(ms(1))),
            (Some(ms(2000)), None, Appearance::Now),
            (Some(ms(60_000)), Some(ms(60_000)), Appearance::Now),
            // The more recent arrival counts.
            (Some(ms(60_000)), Some(ms(300)), Appearance::After(ms(1700))),
            (Some(ms(300)), Some(ms(60_000)), Appearance::After(ms(1700))),
        ] {
            assert_eq!(
                appearance(Attention {
                    since_app_active,
                    since_opener_on_screen,
                    ..attentive
                }),
                expected,
                "{since_app_active:?} {since_opener_on_screen:?}"
            );
        }
        // Waiting comes first: the other rules are judged when it is over.
        assert_eq!(
            appearance(Attention {
                since_app_active: Some(ms(100)),
                sibling_on_screen: true,
                since_sibling_left: Some(ms(0)),
                ..attentive
            }),
            Appearance::After(ms(1900))
        );
        // A user who left is not waited for.
        assert_eq!(
            appearance(Attention {
                app_active: false,
                since_opener_on_screen: Some(ms(100)),
                ..attentive
            }),
            Appearance::Never
        );
    }

    #[test]
    fn a_child_has_a_size_within_fixed_bounds_whatever_is_asked() {
        assert_eq!(child_size(None, None), (520.0, 760.0));
        assert_eq!(child_size(Some(440.0), Some(620.0)), (440.0, 620.0));
        assert_eq!(child_size(Some(5000.0), Some(5000.0)), (900.0, 1000.0));
        assert_eq!(child_size(Some(1.0), Some(0.0)), (360.0, 400.0));
        assert_eq!(child_size(Some(-300.0), None), (360.0, 760.0));
        assert_eq!(child_size(Some(f64::INFINITY), Some(f64::NAN)), (520.0, 760.0));
    }

    #[test]
    fn the_marker_needs_a_probed_macos_and_the_signed_association() {
        let signed = vec![
            "webcredentials:avibe.bot".to_owned(),
            "webcredentials:sandbox.avibe.bot".to_owned(),
        ];
        assert!(advertises_passkeys(26, Some(&signed)));
        // Not a floor: neither older systems (15 has passkey PRF, but the
        // probe has not passed there) nor ones newer than CI has run on.
        for unproven in [13, 14, 15, 16, 25, 27, 99] {
            assert!(!advertises_passkeys(unproven, Some(&signed)), "{unproven}");
        }
        assert!(!advertises_passkeys(26, None));
        assert!(!advertises_passkeys(26, Some(&[])));
        for other in [
            "webcredentials:avibe.bot",
            "webcredentials:sandbox.avibe.bot.evil.example",
            "webcredentials:*.avibe.bot",
            "applinks:sandbox.avibe.bot",
            "sandbox.avibe.bot",
        ] {
            assert!(!advertises_passkeys(26, Some(&[other.to_owned()])), "{other}");
        }
    }

    #[test]
    fn a_build_without_the_signed_association_does_not_advertise_passkeys() {
        // The test binary is signed ad hoc, like a development build: whatever
        // the system, its real signature carries no associated domain.
        assert_eq!(signed_associated_domains(), None);
        for macos in PROBED_MACOS {
            assert!(!advertises_passkeys(macos, signed_associated_domains().as_deref()));
        }
    }

    #[test]
    fn the_marker_is_defined_on_the_top_level_document_only() {
        assert!(DESKTOP_VAULT_PASSKEY_MARKER.starts_with("if (window.self === window.top)"));
        assert!(DESKTOP_VAULT_PASSKEY_MARKER
            .contains("Object.defineProperty(window, '__AVIBE_DESKTOP_VAULT_PASSKEY__', { value: true })"));
    }
}
