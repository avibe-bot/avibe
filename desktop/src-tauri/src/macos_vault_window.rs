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
//!   document commits it comes on screen empty: its webview is transparent.
//!   WebKit goes on displaying the previous document's layers until the new
//!   one has something to show, so the webview is revealed only by the first
//!   presentation update WebKit delivers after that commit, which carries the
//!   sandbox document's layers. The empty window stays until then, at most
//!   [`RESERVE_DEADLINE`]. From then on the document is cross-origin to the
//!   opener and can only be replaced by another sandbox document. A child that
//!   commits anything else is closed; one that has displayed nothing within
//!   the deadline is closed too.
//! - **No input before the user has seen the card.** WebKit on macOS lets
//!   scripts open windows without a user gesture, which a browser would
//!   require, so a page of the Workbench origin can make the sandbox's real
//!   window, with a real pending request's card, appear at a moment it
//!   chooses. It cannot put content of its own into the card. And the sandbox
//!   arms its Confirm half a second after it builds the card, whether or not
//!   the card is on screen. So being displayed is not the same as taking
//!   input. A sandbox window takes clicks and keys only while this app is the
//!   active one and the window is visible, and that has held without a break
//!   for [`LIVE_AFTER_FIRST_SIGHT`] the first time the card is seen and
//!   [`LIVE_AFTER_RETURN`] every later time, and never sooner than
//!   [`LIVE_AFTER_RETURN`] after the user last arrived: the app became the
//!   active one, or the opener's window came back from hidden, minimized or
//!   another Space. Until then every key for the window, and every click on
//!   its content, is dropped where the window receives it and is not passed
//!   to what is underneath; clicks on the title bar are delivered, so the
//!   window can be moved or closed. The card is on screen the whole time. When
//!   it is displayed it is put in front of this app's other windows and given
//!   the keyboard, and the shell never moves it or gives it the keyboard
//!   again: nothing about the window changes on screen when it starts to take
//!   input. Another window of the app can be put in front of it later, by the
//!   user or by a page.
//! - **Only for the Workbench the user is looking at.** The web view that
//!   asked must be the one in front of the user: still in the native window
//!   the support was installed for, not hidden and with no hidden view around
//!   it, with no other web view of that window in front of it, that window on
//!   screen, and this app the active one. The shell hides a Workbench's
//!   web view under its account screen and before it retires one, while the
//!   page keeps running. A child whose first sandbox document commits while
//!   that does not hold, or while another child is on screen over the same
//!   native window, is closed, never kept for later. Its size is clamped. The title
//!   names the allowed host and ignores the page's own title, but it is not
//!   proof of identity: other native windows of this app can carry any title.
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
//!   closed or destroyed, closes its children. So does its web view leaving
//!   its place: every web view wry builds announces being hidden, shown, and
//!   moved into or out of a window (three methods added to wry's class, each
//!   calling its superclass; nothing inherited is replaced, and nothing is
//!   added if wry's class defines one already). On each announcement every
//!   opener is read again, and the children of one that is no longer in place
//!   are closed and their pages destroyed before the announcement returns.
//!   That covers a web view the shell hides, one it closes without hiding,
//!   and one that another web view was created in front of. (The one case in
//!   which an announcement reads nothing is the registry being in use at that
//!   instant; no path that reaches it is known, since nothing touches a view
//!   while the registry is held, and the reads at admission, at the commit,
//!   at the reveal and for every event do not depend on announcements.) The
//!   shell erases a retired Workbench's data store after hiding its web
//!   view, so a child's page is gone before the erase starts. A web view that is not in place
//!   is refused a window when it asks (`window.open` returns `null`), so a
//!   sandbox window exists only for an opener in place: one that is not in
//!   place cannot get one, and one that leaves its place loses those it has
//!   in that turn. An announcement only ever closes:
//!   the input rule does not wait for one, and wherever it matters the opener
//!   is read afresh. Nothing is observed and no web view is held, so nothing
//!   here depends on when a web view is freed. The first window an opener
//!   asks for when it has none is itself an arrival for the input rule, from
//!   the moment it is asked for.
//! - **Every Workbench web view.** The support is installed on the web view
//!   the app starts with and on each Runtime replacement, which the shell
//!   shows in a child web view of the same native window after the user
//!   switches instance. The account screen is not an opener: its requests for
//!   a window go to wry as before, and its page gets no marker.
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
    NSApplication, NSApplicationDidBecomeActiveNotification, NSAutoresizingMaskOptions, NSBackingStoreType, NSEvent,
    NSEventType, NSView, NSWindow, NSWindowDelegate, NSWindowDidChangeOcclusionStateNotification,
    NSWindowDidDeminiaturizeNotification, NSWindowDidMiniaturizeNotification, NSWindowOcclusionState,
    NSWindowOrderingMode, NSWindowStyleMask,
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
use tauri::{AppHandle, Manager, Webview, WindowEvent};
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
/// How long a card must have been in front of the user, without interruption,
/// before its window takes a click or a key: the first time it is seen, and
/// every time the user comes back to it. The first is longer than the default
/// double-click interval (half a second), so a click begun on something else
/// never lands on a card that has just appeared in its place. The second gives
/// a user who has just arrived, or been brought, time to see where they are.
/// The sandbox arms its own Confirm half a second after it builds the card,
/// whether or not the card is on screen, so these are what make sure the card
/// was seen first.
const LIVE_AFTER_FIRST_SIGHT: Duration = Duration::from_millis(750);
const LIVE_AFTER_RETURN: Duration = Duration::from_secs(2);
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

/// What decides whether a child whose first sandbox document has just committed
/// comes on screen. A child that does not is closed, never kept for later.
#[derive(Clone, Copy, Debug)]
struct Attention {
    /// The user is in this app.
    app_active: bool,
    /// The web view that asked is the one the user is looking at: not hidden,
    /// in a native window that is visible, on the active Space and not
    /// minimized.
    opener_on_screen: bool,
    /// Another child is on screen over the same native window.
    sibling_on_screen: bool,
}

fn may_appear(attention: Attention) -> bool {
    attention.app_active && attention.opener_on_screen && !attention.sibling_on_screen
}

/// How long until a displayed window takes clicks and keys: `Some(ZERO)` when
/// it does now, `None` when the card is not in front of the user at all.
///
/// `seen_for` is how long the card has been displayed, in a visible window, in
/// the active app, without interruption; `None` when it is not at this moment.
/// `first_sight` is whether that is the first such period. `arrived_for` is
/// how long ago the user last arrived: this app became the active one, or the
/// opener's window came back on screen, whether or not the card existed then.
///
/// WebKit lets a page open the sandbox's window without a user gesture and at a
/// moment it chooses, and the sandbox's own delay before Confirm runs whether
/// or not anyone can see the card. This is what stands in for the gesture: no
/// click and no key reaches a card the user has not had in front of them for
/// the interval.
fn input_wait(seen_for: Option<Duration>, first_sight: bool, arrived_for: Option<Duration>) -> Option<Duration> {
    let seen_for = seen_for?;
    let needed = if first_sight {
        LIVE_AFTER_FIRST_SIGHT
    } else {
        LIVE_AFTER_RETURN
    };
    let since_sight = needed.saturating_sub(seen_for);
    let since_arrival = arrived_for.map_or(Duration::ZERO, |arrived_for| {
        LIVE_AFTER_RETURN.saturating_sub(arrived_for)
    });
    Some(since_sight.max(since_arrival))
}

/// Whether an event is input that a window not yet live must not receive.
/// Clicks, scrolls, gestures and keys are; what AppKit sends a window about
/// itself is not. An event type this list does not know counts as input.
fn is_input(kind: NSEventType) -> bool {
    ![
        NSEventType::AppKitDefined,
        NSEventType::SystemDefined,
        NSEventType::ApplicationDefined,
        NSEventType::Periodic,
        NSEventType::CursorUpdate,
        NSEventType::MouseEntered,
        NSEventType::MouseExited,
    ]
    .contains(&kind)
}

fn is_key(kind: NSEventType) -> bool {
    [NSEventType::KeyDown, NSEventType::KeyUp, NSEventType::FlagsChanged].contains(&kind)
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
pub fn install(window: &Webview) {
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
pub(crate) fn install_with(window: &Webview, policy: Policy) {
    let app = window.app_handle().clone();
    // The native window the web view is in. Several openers can share it.
    let events = window.window();
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
        // First let go of every opener whose web view is gone, or whose
        // address this one took. Only then is this one entered: an address
        // can be reused, and nothing of an earlier web view may be mistaken
        // for, or take away, what belongs to this one.
        let gone = STATE.with_borrow_mut(|state| {
            state.policy.get_or_insert(policy);
            let (kept, gone): (Vec<Opener>, Vec<Opener>) = std::mem::take(&mut state.openers)
                .into_iter()
                .partition(|opener| opener.key != key && opener.webview.load().is_some());
            state.openers = kept;
            gone
        });
        gone.into_iter().for_each(Opener::stop_watching);
        let (sight, observers) = observe_arrivals(&webview, &window);
        STATE.with_borrow_mut(|state| {
            state.openers.push(Opener {
                key,
                sight,
                webview: Weak::from_retained(&webview),
                app: app.clone(),
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
pub fn opener_navigated(window: &Webview) {
    let _ = window.with_webview(|platform| {
        if MainThreadMarker::new().is_some() {
            close_children_of(platform.inner() as usize, Destroy::Now);
        }
    });
}

/// The opener's window was closed by the user (`gone` is false: the shell
/// keeps it, hidden) or destroyed.
fn opener_closed(key: usize, gone: bool) {
    close_children_of(key, Destroy::Now);
    if gone {
        let opener = STATE.with_borrow_mut(|state| {
            let index = state.openers.iter().position(|opener| opener.key == key)?;
            Some(state.openers.remove(index))
        });
        if let Some(opener) = opener {
            opener.stop_watching();
        }
    }
}

/// Whether a window is where the user can see it.
fn is_on_screen(window: &NSWindow) -> bool {
    window.isVisible() && window.isOnActiveSpace() && !window.isMiniaturized()
}

/// The native window an opener's web view was installed in, if the web view is
/// still in it. A web view the shell has closed is out of its window and is
/// no longer an opener, whether or not it has been freed by then.
fn in_home_window(webview: &WKWebView) -> Option<Retained<NSWindow>> {
    let key = webview as *const WKWebView as usize;
    let home = ARRIVALS.with_borrow(|arrivals| arrivals.sights.home(key).and_then(Weak::load))?;
    let window = webview.window()?;
    std::ptr::eq(&*window, &*home).then_some(window)
}

/// Whether another web view of the same window that is not hidden lies in
/// front of this one. The shell covers a Workbench by hiding it, but a web
/// view shown under another one is covered all the same.
fn covered_by_a_web_view(webview: &WKWebView) -> bool {
    // SAFETY: Main thread; a view's superview lives as long as the view is in it.
    let Some(parent) = (unsafe { webview.superview() }) else {
        return false;
    };
    let own = webview.frame();
    let overlaps = |other: NSRect| {
        other.origin.x < own.origin.x + own.size.width
            && own.origin.x < other.origin.x + other.size.width
            && other.origin.y < own.origin.y + own.size.height
            && own.origin.y < other.origin.y + other.size.height
    };
    // Later subviews are drawn in front of earlier ones.
    let mut in_front = false;
    for view in parent.subviews().iter() {
        if std::ptr::addr_eq(&*view as *const NSView, webview as *const WKWebView) {
            in_front = true;
        } else if in_front && !view.isHidden() && view.isKindOfClass(WKWebView::class()) && overlaps(view.frame()) {
            return true;
        }
    }
    false
}

/// The native window an opener's web view is in, if the web view is where a
/// Workbench the user can be looking at is. Three things, read each time: it
/// is still in the window it was installed in; neither it nor a view around it
/// is hidden; and no other web view of that window lies in front of it. A
/// Workbench under the app's account screen, or one being retired, is hidden
/// while its page keeps running.
fn in_place(webview: &WKWebView) -> Option<Retained<NSWindow>> {
    let window = in_home_window(webview)?;
    (!webview.isHiddenOrHasHiddenAncestor() && !covered_by_a_web_view(webview)).then_some(window)
}

/// The native window an opener's web view is shown in, if it is what the user
/// is looking at: in place, in a window that is on screen.
fn shown_in(webview: &WKWebView) -> Option<Retained<NSWindow>> {
    in_place(webview).filter(|window| is_on_screen(window))
}

fn app_is_active(mtm: MainThreadMarker) -> bool {
    NSApplication::sharedApplication(mtm).isActive()
}

type Observer = Retained<ProtocolObject<dyn NSObjectProtocol>>;

/// When the user last arrived, and what it is told by. Kept apart from `STATE`
/// because a sandbox window reads it while it handles an event.
#[derive(Default)]
struct Arrivals {
    /// The latest of: this app became the active one; an opener came back
    /// into view (its window from hidden, minimized or another Space, or its
    /// web view from hidden); an opener that has no sandbox window asked for
    /// one. The last is a rule, not an observation: the shell does not take
    /// a web view's announcements as word of how long a Workbench has been in
    /// front of the user (they only close windows), so the request itself is
    /// the arrival. Coming out from under another web view or a hidden view
    /// around it counts only if the shell looked while it held. Input does
    /// not depend on this record: every event reads the opener afresh.
    last: Option<Instant>,
    sights: Sights<Weak<NSWindow>>,
    app_observer: Option<Observer>,
}

/// What is known about each opener's web view, found by the address WebKit
/// names it with. `H` is the native window the support was installed for.
///
/// An address is not an identity: a web view can be freed and a later one
/// given its address. So each entry also has a number of its own, and an
/// opener gives up its entry by that number, never by address.
struct Sights<H> {
    next: u64,
    entries: Vec<Sight<H>>,
}

struct Sight<H> {
    id: u64,
    key: usize,
    home: H,
    /// Whether the web view was shown when last looked at.
    shown: bool,
}

impl<H> Default for Sights<H> {
    fn default() -> Self {
        Self {
            next: 0,
            entries: Vec::new(),
        }
    }
}

impl<H> Sights<H> {
    /// A web view at `key` becomes an opener; `shown` is how it is first seen,
    /// which is not an arrival. What was known about an earlier web view at
    /// that address is dropped. Returns the number that names this entry.
    fn install(&mut self, key: usize, home: H, shown: bool) -> u64 {
        self.entries.retain(|sight| sight.key != key);
        self.next += 1;
        self.entries.push(Sight {
            id: self.next,
            key,
            home,
            shown,
        });
        self.next
    }

    /// Drops the entry `id` names and no other. It may be gone already, its
    /// address taken by a later opener, whose entry must stay.
    fn remove(&mut self, id: u64) {
        self.entries.retain(|sight| sight.id != id);
    }

    fn home(&self, key: usize) -> Option<&H> {
        self.entries
            .iter()
            .find(|sight| sight.key == key)
            .map(|sight| &sight.home)
    }

    /// Records how the web view at `key` is seen now, hidden included, and
    /// returns whether it has come back: shown, and not shown when last
    /// looked at.
    fn look(&mut self, key: usize, shown: bool) -> bool {
        self.entries
            .iter_mut()
            .find(|sight| sight.key == key)
            .is_some_and(|sight| {
                // Always recorded, whichever way it is seen.
                let was_shown = std::mem::replace(&mut sight.shown, shown);
                shown && !was_shown
            })
    }
}

thread_local! {
    static ARRIVALS: RefCell<Arrivals> = RefCell::new(Arrivals::default());
}

/// How long ago the user last arrived.
fn arrived_for() -> Option<Duration> {
    ARRIVALS.with_borrow(|arrivals| arrivals.last.map(|last| last.elapsed()))
}

/// Looks at an opener, records whether it is shown, and returns the window it
/// is shown in. AppKit reports some changes late and some not at all, so
/// whoever needs the answer passes what it sees through here: an opener found
/// shown that was last seen not shown has, at the latest, come back now.
fn look_at_opener(webview: &WKWebView) -> Option<Retained<NSWindow>> {
    let key = webview as *const WKWebView as usize;
    let window = shown_in(webview);
    let shown = window.is_some();
    ARRIVALS.with_borrow_mut(|arrivals| {
        if arrivals.sights.look(key, shown) {
            arrivals.last = Some(Instant::now());
        }
    });
    window
}

/// Notes the arrivals AppKit announces: this app becoming the active one,
/// however that came about, and the opener's window being shown, restored or
/// uncovered. Returns the number of the opener's entry and the window's
/// observers, which end with the opener.
fn observe_arrivals(webview: &Retained<WKWebView>, window: &Retained<NSWindow>) -> (u64, Vec<Observer>) {
    let center = NSNotificationCenter::defaultCenter();
    if ARRIVALS.with_borrow(|arrivals| arrivals.app_observer.is_none()) {
        let became_active = RcBlock::new(|_: NonNull<NSNotification>| {
            ARRIVALS.with_borrow_mut(|arrivals| arrivals.last = Some(Instant::now()));
        });
        // SAFETY: Main thread; AppKit posts this notification on the main
        // thread, where the block then runs.
        let observer = unsafe {
            center.addObserverForName_object_queue_usingBlock(
                Some(NSApplicationDidBecomeActiveNotification),
                None,
                None,
                &became_active,
            )
        };
        // Kept for the life of the process.
        ARRIVALS.with_borrow_mut(|arrivals| arrivals.app_observer = Some(observer));
    }
    // The entry has to exist before the web view can be judged to be in its
    // window; how it is first seen is then recorded without being an arrival.
    let key = Retained::as_ptr(webview) as usize;
    let sight = ARRIVALS.with_borrow_mut(|arrivals| arrivals.sights.install(key, Weak::from_retained(window), false));
    let shown = shown_in(webview).is_some();
    ARRIVALS.with_borrow_mut(|arrivals| {
        if shown {
            // Seen shown for the first time: recorded, and not an arrival.
            let _ = arrivals.sights.look(key, true);
        }
    });
    let watched = Weak::from_retained(webview);
    // SAFETY: As above; the statics are AppKit's notification names.
    let observers = unsafe {
        [
            NSWindowDidChangeOcclusionStateNotification,
            NSWindowDidDeminiaturizeNotification,
            NSWindowDidMiniaturizeNotification,
        ]
        .into_iter()
        .map(|name| {
            let watched = watched.clone();
            let changed = RcBlock::new(move |_: NonNull<NSNotification>| {
                if let Some(webview) = watched.load() {
                    look_at_opener(&webview);
                }
            });
            let window: &AnyObject = window.as_ref();
            center.addObserverForName_object_queue_usingBlock(Some(name), Some(window), None, &changed)
        })
        .collect()
    };
    (sight, observers)
}

/// Whether an opener may have one more sandbox window: it is in place, and
/// the app has fewer than the most it may have.
fn admits(opener_in_place: bool, children: usize) -> bool {
    opener_in_place && children < MAX_CHILDREN
}

type AnnounceImp = unsafe extern "C-unwind" fn(&AnyObject, Sel);

/// wry's web view class, once the three announcements were added to it; `None`
/// when they could not be.
static ANNOUNCING: OnceLock<Option<usize>> = OnceLock::new();

/// The class wry builds its web views from: the direct subclass of `WKWebView`
/// in this web view's ancestry. The object's own class can be one the system
/// derived from it.
fn wry_web_view_class(webview: &WKWebView) -> Option<&'static AnyClass> {
    let mut class: &'static AnyClass = webview.class();
    loop {
        let parent = class.superclass()?;
        if std::ptr::eq(parent, WKWebView::class()) {
            return Some(class);
        }
        class = parent;
    }
}

/// Makes every web view wry builds tell this module when it is hidden, shown,
/// or moved into or out of a window, which is also how it is closed. AppKit
/// calls these on a view for itself and for a hidden view around it.
///
/// The three methods are added to wry's class; nothing inherited is replaced,
/// which would change every web view in the process, a sandbox window's own
/// included. Each calls its superclass first. If wry's class already defines
/// one of them, nothing is added and the support is not installed.
fn announce_web_views(webview: &WKWebView) -> bool {
    let Some(class) = wry_web_view_class(webview) else {
        return false;
    };
    let key = class as *const AnyClass as usize;
    let announcing = ANNOUNCING.get_or_init(|| {
        let methods: [(Sel, AnnounceImp); 3] = [
            (sel!(viewDidHide), view_did_hide),
            (sel!(viewDidUnhide), view_did_unhide),
            (sel!(viewDidMoveToWindow), view_did_move_to_window),
        ];
        // The class's own methods only; inherited ones are left alone.
        let own = class.instance_methods();
        if methods
            .iter()
            .any(|(selector, _)| own.iter().any(|method| method.name() == *selector))
        {
            return None;
        }
        for (selector, imp) in methods {
            // SAFETY: The class is live for the process; the implementation
            // has the signature the encoding states: no result, no argument.
            let added = unsafe {
                objc2::ffi::class_addMethod(
                    std::ptr::from_ref(class).cast_mut(),
                    selector,
                    std::mem::transmute::<AnnounceImp, Imp>(imp),
                    c"v@:".as_ptr(),
                )
            };
            if !added.as_bool() {
                return None;
            }
        }
        Some(key)
    });
    *announcing == Some(key)
}

unsafe extern "C-unwind" fn view_did_hide(this: &AnyObject, _selector: Sel) {
    // SAFETY: The method this one was added over, on the same object.
    let _: () = unsafe { msg_send![super(this, WKWebView::class()), viewDidHide] };
    web_view_changed();
}

unsafe extern "C-unwind" fn view_did_unhide(this: &AnyObject, _selector: Sel) {
    // SAFETY: As above.
    let _: () = unsafe { msg_send![super(this, WKWebView::class()), viewDidUnhide] };
    web_view_changed();
}

unsafe extern "C-unwind" fn view_did_move_to_window(this: &AnyObject, _selector: Sel) {
    // SAFETY: As above.
    let _: () = unsafe { msg_send![super(this, WKWebView::class()), viewDidMoveToWindow] };
    web_view_changed();
}

/// A web view of the app was hidden, shown, put into a window or taken out of
/// one. Whichever it was, every opener is read again, and the sandbox windows
/// of one that is no longer in place are closed and their pages destroyed, at
/// once: a retired Workbench's data store may be erased next.
///
/// An announcement only ever closes. It is what makes closing prompt; whether
/// an opener is in view is still read afresh wherever it matters, and nothing
/// in the input rule waits for one.
///
/// Nothing is read if the registry is gone or in use at this instant. No path
/// that reaches the second is known: the registry is never held across a call
/// that touches a view.
fn web_view_changed() {
    // AppKit can call this while the process is ending and the registry is
    // already gone; then there is nothing left to close.
    let openers = STATE.try_with(|state| {
        state.try_borrow().ok().map(|state| {
            state
                .openers
                .iter()
                .map(|opener| (opener.key, opener.webview.load()))
                .collect::<Vec<_>>()
        })
    });
    let Ok(Some(openers)) = openers else {
        return;
    };
    for (key, webview) in openers {
        if webview.is_none_or(|webview| in_place(&webview).is_none()) {
            close_children_of(key, Destroy::Now);
        }
    }
}

/// When a closed child's page is destroyed.
#[derive(Clone, Copy, PartialEq, Eq)]
enum Destroy {
    /// Before this returns. For everything that is not a callback of the
    /// child's own window or web view.
    Now,
    /// One turn of the main run loop later: the close is being made inside a
    /// callback of the child's window or of its web view.
    AfterThisTurn,
}

fn close_children_of(opener: usize, destroy: Destroy) {
    let children: Vec<u64> = STATE.with_borrow(|state| {
        state
            .children
            .iter()
            .filter(|child| child.opener == opener)
            .map(|child| child.id)
            .collect()
    });
    for id in children {
        close(id, false, destroy);
    }
}

struct Opener {
    /// The webview's address, which is how WebKit names it in callbacks.
    key: usize,
    /// Its entry among the sights, which it alone gives up.
    sight: u64,
    /// Proves the address still belongs to the webview that was installed.
    webview: Weak<WKWebView>,
    app: AppHandle,
    observers: Vec<Observer>,
}

impl Opener {
    /// Ends every observation made for this opener.
    fn stop_watching(self) {
        let center = NSNotificationCenter::defaultCenter();
        for observer in self.observers {
            let observer: &AnyObject = observer.as_ref();
            // SAFETY: The token `addObserverForName` returned, removed once.
            unsafe { center.removeObserver(observer) };
        }
        ARRIVALS.with_borrow_mut(|arrivals| arrivals.sights.remove(self.sight));
    }
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
    /// The native window is on screen: empty until the sandbox document has
    /// been presented, then displayed. Whether it takes input is the window's
    /// own matter; see `SandboxWindow`.
    on_screen: bool,
}

#[derive(Default)]
struct State {
    policy: Option<Policy>,
    openers: Vec<Opener>,
    children: Vec<Child>,
    next_id: u64,
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
    displayed: Cell<bool>,
    /// When the current unbroken period began in which the card is displayed,
    /// the window visible and this app active. `None` outside such a period.
    seen_since: Cell<Option<Instant>>,
    /// Such a period has ended before, so the user is coming back to the card.
    seen_before: Cell<bool>,
    /// The web view that asked for this window.
    opener: Weak<WKWebView>,
}

define_class!(
    /// A child's window. It is the one place where input for the card is
    /// admitted or dropped: every mouse, scroll and key event for a window
    /// passes its `sendEvent:`, and key equivalents its `performKeyEquivalent:`.
    #[unsafe(super(NSWindow))]
    #[thread_kind = MainThreadOnly]
    #[name = "AvibeVaultSandboxWindow"]
    #[ivars = WindowFields]
    struct SandboxWindow;

    impl SandboxWindow {
        #[unsafe(method(sendEvent:))]
        fn send_event(&self, event: &NSEvent) {
            if self.admits(event) {
                // SAFETY: The superclass implementation, with its argument.
                let _: () = unsafe { msg_send![super(self), sendEvent: event] };
            }
        }

        #[unsafe(method(performKeyEquivalent:))]
        fn perform_key_equivalent(&self, event: &NSEvent) -> bool {
            // SAFETY: The superclass implementation, with its argument.
            self.is_live() && unsafe { msg_send![super(self), performKeyEquivalent: event] }
        }

        /// A displayed card may have the keyboard before it takes input:
        /// keys are dropped at `sendEvent:` like clicks. So nothing about the
        /// window changes on screen at the moment it turns live.
        #[unsafe(method(canBecomeKeyWindow))]
        fn can_become_key(&self) -> bool {
            self.ivars().displayed.get()
        }

        #[unsafe(method(canBecomeMainWindow))]
        fn can_become_main(&self) -> bool {
            self.ivars().displayed.get()
        }
    }
);

impl SandboxWindow {
    /// Brings the record of whether the user has the card in front of them up
    /// to this moment, and returns how long until the window takes input.
    /// Called for every event, so a change the window was not told about in
    /// time can only make the wait longer: a period found to have begun is
    /// counted from now.
    fn input_wait(&self) -> Option<Duration> {
        let fields = self.ivars();
        // A card whose opener is not what the user is looking at is on its
        // way out (see `web_view_changed`); until it is gone it is not live.
        let opener_shown = fields
            .opener
            .load()
            .is_some_and(|opener| look_at_opener(&opener).is_some());
        let seen = fields.displayed.get()
            && opener_shown
            && self.occlusionState().contains(NSWindowOcclusionState::Visible)
            && MainThreadMarker::new().is_some_and(app_is_active);
        match (seen, fields.seen_since.get()) {
            (true, None) => fields.seen_since.set(Some(Instant::now())),
            (false, Some(_)) => {
                fields.seen_since.set(None);
                fields.seen_before.set(true);
            }
            _ => {}
        }
        input_wait(
            fields.seen_since.get().map(|since| since.elapsed()),
            !fields.seen_before.get(),
            arrived_for(),
        )
    }

    fn is_live(&self) -> bool {
        self.input_wait() == Some(Duration::ZERO)
    }

    /// Whether an event is delivered. A window that is not live, whether still
    /// empty or displayed, drops every key, and every click, scroll and
    /// gesture on its content. It does not pass them to what is under it
    /// either: a click aimed at the card must not land on something else. The
    /// title bar keeps working, so the window can be moved or closed.
    fn admits(&self, event: &NSEvent) -> bool {
        let kind = event.r#type();
        if !is_input(kind) || self.is_live() {
            return true;
        }
        if is_key(kind) {
            return false;
        }
        let Some(content) = self.contentView() else {
            return false;
        };
        let at = event.locationInWindow();
        let content = content.frame();
        let inside = at.x >= content.origin.x
            && at.x < content.origin.x + content.size.width
            && at.y >= content.origin.y
            && at.y < content.origin.y + content.size.height;
        !inside
    }
}

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
            close(self.ivars().id, false, Destroy::AfterThisTurn);
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
            close(self.ivars().id, false, Destroy::AfterThisTurn);
        }
    }

    unsafe impl NSWindowDelegate for ChildDelegate {
        /// The user closed the window.
        #[unsafe(method(windowWillClose:))]
        fn will_close(&self, _notification: &NSNotification) {
            close(self.ivars().id, true, Destroy::AfterThisTurn);
        }

        /// AppKit's word on whether any of the window can be seen changed:
        /// it was hidden with the app or its main window, or shown again, and
        /// by AppKit's documentation also minimized, moved to another Space,
        /// fully covered, or the display slept. Each ends or begins a period
        /// in which the user can see the card.
        #[unsafe(method(windowDidChangeOcclusionState:))]
        fn occlusion_changed(&self, _notification: &NSNotification) {
            let id = self.ivars().id;
            let window = STATE.with_borrow(|state| {
                state
                    .children
                    .iter()
                    .find(|child| child.id == id)
                    .map(|child| child.window.clone())
            });
            if let Some(window) = window {
                window.input_wait();
            }
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
        Some((Commit::Close, _)) => close(id, false, Destroy::AfterThisTurn),
        Some((Commit::Show, true)) => appear(id),
        _ => {}
    }
}

/// What the user's attention is on, for one child, and the native window its
/// opener is shown in. `None` when the child or its opener is gone, or the
/// opener is not what the user is looking at.
fn attention(id: u64, mtm: MainThreadMarker) -> Option<(Attention, Retained<NSWindow>)> {
    let opener = STATE.with_borrow(|state| {
        let child = state.children.iter().find(|child| child.id == id)?;
        let opener = state.openers.iter().find(|opener| opener.key == child.opener)?;
        opener.webview.load()
    })?;
    let window = look_at_opener(&opener)?;
    // One on screen for each native window, whichever web view in it asked.
    let sibling_on_screen = STATE.with_borrow(|state| {
        state.children.iter().any(|other| {
            other.id != id
                && other.on_screen
                && other
                    .window
                    .parentWindow()
                    .is_some_and(|parent| std::ptr::eq(&*parent, &*window))
        })
    });
    let attention = Attention {
        app_active: app_is_active(mtm),
        opener_on_screen: true,
        sibling_on_screen,
    };
    Some((attention, window))
}

/// Puts a child whose first sandbox document has committed on screen over its
/// opener's window, or closes it when the user is not there or another child
/// is on screen.
///
/// What comes on screen is the window with a transparent webview. It cannot
/// become key and takes no input; clicks on it go nowhere. WebKit
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
    let Some((_, parent)) = attention(id, mtm).filter(|(attention, _)| may_appear(*attention)) else {
        close(id, false, Destroy::AfterThisTurn);
        return;
    };
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
    // SAFETY: Both windows are live and distinct; the child is detached again
    // before it closes.
    unsafe { parent.addChildWindow_ordered(&window, NSWindowOrderingMode::Above) };
    let presented = RcBlock::new(move || reveal(id));
    // SAFETY: `adopt` checked that the webview class has this method, which
    // takes one block and calls it on the main thread.
    let _: () = unsafe { msg_send![&*webview, _doAfterNextPresentationUpdate: &*presented] };
}

/// The sandbox document is what the webview now displays: show it, in front of
/// this app's other windows and with the keyboard, which a passkey ceremony
/// needs. The window does not take input yet. From here on that is decided
/// event by event, by how long the user has had the card in front of them, and
/// nothing about the window changes on screen when it does.
fn reveal(id: u64) {
    let found = STATE.with_borrow(|state| {
        let child = state.children.iter().find(|child| child.id == id)?;
        (child.committed && child.on_screen && !child.window.ivars().displayed.get())
            .then(|| (child.window.clone(), child.webview.clone()))
    });
    let Some((window, webview)) = found else {
        return;
    };
    // Nothing announces every way an opener can leave the user's view, so it
    // is read once more before its card is put in front and given the
    // keyboard.
    let opener_in_view = window
        .ivars()
        .opener
        .load()
        .is_some_and(|opener| look_at_opener(&opener).is_some());
    if !opener_in_view {
        close(id, false, Destroy::AfterThisTurn);
        return;
    }
    window.ivars().displayed.set(true);
    webview.setAlphaValue(1.0);
    // The ordinary order-front: behind the active app's windows if this app
    // is not the active one.
    window.makeKeyAndOrderFront(None);
    window.makeFirstResponder(Some(&webview));
    window.input_wait();
}

/// Ends a child. Its opener can no longer reach it and the native window
/// closes now; the page is destroyed when `destroy` says, which the opener
/// sees as a closed popup. `window_closing` is set when AppKit is already
/// closing the window.
fn close(id: u64, window_closing: bool, destroy: Destroy) {
    let child = STATE.with_borrow_mut(|state| {
        let index = state.children.iter().position(|child| child.id == id)?;
        Some(state.children.remove(index))
    });
    let Some(child) = child else {
        return;
    };
    // The webview keeps its delegate until the page is destroyed. The child is
    // out of the registry, so from here the delegate refuses every navigation.
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
    // Releasing the webview alone would not destroy the page while WebKit
    // still awaits a presentation update for it.
    let destroy_page = move |child: Child| {
        // SAFETY: Main thread. `adopt` checked that the webview class has
        // `_close`, which takes no argument.
        let _: () = unsafe { msg_send![&*child.webview, _close] };
    };
    match destroy {
        Destroy::Now => destroy_page(child),
        Destroy::AfterThisTurn => {
            // The page is destroyed, and the objects released, once WebKit's
            // callback on this webview has returned.
            let remains = Cell::new(Some(child));
            let later = RcBlock::new(move || {
                if let Some(child) = remains.take() {
                    destroy_page(child);
                }
            });
            // SAFETY: The block runs once, on the main thread.
            unsafe { NSOperationQueue::mainQueue().addOperationWithBlock(&later) };
        }
    }
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
        // A Workbench's sandbox windows are closed the moment its web view is
        // hidden, covered or closed, which only the web views themselves say.
        if !announce_web_views(webview) {
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
    // Only a web view the support was installed on, and only while it is in
    // place: in the window it was installed in, not hidden, not covered. A
    // sandbox window exists only for such an opener, so one that is not in
    // place is refused here as one that leaves its place loses those it has.
    // The weak reference alone is not relied on to say that a web view is
    // still there: a closed web view need not have been freed yet.
    let in_place = in_place(opener).is_some();
    let admitted = STATE.with_borrow(|state| {
        let opener = state.openers.iter().find(|opener| opener.key == key)?;
        let policy = state.policy.clone()?;
        match classify(url.as_ref(), &policy.origin) {
            Request::Other => None,
            Request::Sandbox | Request::Reserve => Some(
                admits(opener.webview.load().is_some() && in_place, state.children.len())
                    .then(|| (policy, opener.app.clone())),
            ),
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
        Some(Some((policy, app))) => match open_child(opener, configuration, features, &policy, app) {
            Some(webview) => Retained::autorelease_return(Retained::into_super(webview)),
            None => std::ptr::null_mut(),
        },
    }
}

/// Builds a child, hidden, from the configuration WebKit prepared for it.
fn open_child(
    opener: &WKWebView,
    configuration: &WKWebViewConfiguration,
    features: &WKWindowFeatures,
    policy: &Policy,
    app: AppHandle,
) -> Option<Retained<SandboxWebView>> {
    let mtm = MainThreadMarker::new()?;
    // SAFETY: WebKit passes its live opener for the length of this call.
    let opener_view = Weak::from_retained(&unsafe { Retained::retain(std::ptr::from_ref(opener).cast_mut()) }?);
    let opener = std::ptr::from_ref(opener) as usize;
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
                displayed: Cell::new(false),
                seen_since: Cell::new(None),
                seen_before: Cell::new(false),
                opener: opener_view,
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
    // nothing. See `appear`.
    webview.setAlphaValue(0.0);
    webview.setAutoresizingMask(
        NSAutoresizingMaskOptions::ViewWidthSizable | NSAutoresizingMaskOptions::ViewHeightSizable,
    );
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
    let first = STATE.with_borrow_mut(|state| {
        let first = !state.children.iter().any(|child| child.opener == opener);
        state.children.push(Child {
            id,
            opener,
            window,
            webview: webview.clone(),
            _delegate: delegate,
            committed: false,
            on_screen: false,
        });
        first
    });
    if first {
        // The first window an opener asks for when it has none is an arrival,
        // from the moment it is asked for. Nothing in the input rule rests on
        // being told when a Workbench came back in front of the user; the
        // shell does not assume how long it has been there.
        ARRIVALS.with_borrow_mut(|arrivals| arrivals.last = Some(Instant::now()));
    }
    let deadline = policy.reserve_deadline;
    tauri::async_runtime::spawn(async move {
        tokio::time::sleep(deadline).await;
        let _ = app.run_on_main_thread(move || {
            let waiting = STATE.with_borrow(|state| {
                state
                    .children
                    .iter()
                    .any(|child| child.id == id && !child.window.ivars().displayed.get())
            });
            if waiting {
                close(id, false, Destroy::Now);
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
    fn a_window_is_admitted_only_for_an_opener_in_place_and_below_the_cap() {
        assert!(admits(true, 0));
        assert!(admits(true, MAX_CHILDREN - 1));
        assert!(!admits(true, MAX_CHILDREN));
        // Hidden, covered, or out of its window: whatever the count.
        assert!(!admits(false, 0));
        assert!(!admits(false, MAX_CHILDREN));
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
            opener_on_screen: true,
            sibling_on_screen: false,
        };
        assert!(may_appear(attentive));
        // The user is in another app, or the Workbench is hidden, minimized or
        // on another Space.
        assert!(!may_appear(Attention {
            app_active: false,
            ..attentive
        }));
        assert!(!may_appear(Attention {
            opener_on_screen: false,
            ..attentive
        }));
        // One sandbox window at a time.
        assert!(!may_appear(Attention {
            sibling_on_screen: true,
            ..attentive
        }));
    }

    #[test]
    fn a_card_takes_input_only_after_the_user_has_had_it_in_front_of_them() {
        let ms = Duration::from_millis;
        let live = Some(Duration::ZERO);
        // (seen for, first sight, arrived how long ago) -> wait
        for (seen_for, first_sight, arrived_for, expected) in [
            // Not displayed, covered, minimized, or the user is in another
            // app: no input, however long the card was seen before.
            (None, true, None, None),
            (None, false, Some(ms(60_000)), None),
            // First sight: three quarters of a second on screen.
            (Some(ms(0)), true, None, Some(ms(750))),
            (Some(ms(500)), true, None, Some(ms(250))),
            (Some(ms(749)), true, None, Some(ms(1))),
            (Some(ms(750)), true, None, live),
            (Some(ms(60_000)), true, Some(ms(60_000)), live),
            // The user comes back to a card: two seconds.
            (Some(ms(0)), false, None, Some(ms(2000))),
            (Some(ms(750)), false, None, Some(ms(1250))),
            (Some(ms(1999)), false, None, Some(ms(1))),
            (Some(ms(2000)), false, None, live),
            // The user arrived less than two seconds ago (the app came
            // forward, or the main window came back), whether or not the card
            // existed then: the later of the two moments decides.
            (Some(ms(750)), true, Some(ms(800)), Some(ms(1200))),
            (Some(ms(100)), true, Some(ms(1900)), Some(ms(650))),
            (Some(ms(5000)), true, Some(ms(0)), Some(ms(2000))),
            (Some(ms(5000)), false, Some(ms(1500)), Some(ms(500))),
            (Some(ms(2000)), false, Some(ms(2000)), live),
        ] {
            assert_eq!(
                input_wait(seen_for, first_sight, arrived_for),
                expected,
                "seen for {seen_for:?}, first sight {first_sight}, arrived {arrived_for:?} ago"
            );
        }
    }

    #[test]
    fn an_opener_coming_back_into_view_is_an_arrival() {
        let mut sights = Sights::default();
        sights.install(0x1000, (), true);
        // Still shown: nothing happened.
        assert!(!sights.look(0x1000, true));
        // Hidden is remembered, so that shown again is coming back.
        assert!(!sights.look(0x1000, false));
        assert!(!sights.look(0x1000, false));
        assert!(sights.look(0x1000, true));
        assert!(!sights.look(0x1000, true));
        // First seen hidden, then shown: it has come into view.
        sights.install(0x2000, (), false);
        assert!(sights.look(0x2000, true));
        // A web view that was never installed is nobody's opener.
        assert!(!sights.look(0x3000, true));
        assert!(sights.home(0x3000).is_none());
    }

    #[test]
    fn a_reused_address_does_not_cost_the_later_opener_its_entry() {
        let mut sights = Sights::default();
        // A web view is installed, then freed; its opener still holds the
        // number of its entry.
        let earlier = sights.install(0x1000, "first window", true);
        // A later web view is given the same address and installed. It starts
        // without the earlier one's history.
        let later = sights.install(0x1000, "second window", false);
        assert_ne!(earlier, later);
        assert_eq!(sights.home(0x1000), Some(&"second window"));
        // The earlier opener is let go only now: the later one's entry stays.
        sights.remove(earlier);
        assert_eq!(sights.home(0x1000), Some(&"second window"));
        assert!(sights.look(0x1000, true), "the later opener is still looked at");
        // And the other order: let go first, then installed.
        let mut sights = Sights::default();
        let earlier = sights.install(0x1000, "first window", true);
        sights.remove(earlier);
        assert!(sights.home(0x1000).is_none());
        sights.install(0x1000, "second window", true);
        assert_eq!(sights.home(0x1000), Some(&"second window"));
        // Its own opener letting go removes it.
        let own = sights.install(0x2000, "third window", true);
        sights.remove(own);
        assert!(sights.home(0x2000).is_none());
    }

    #[test]
    fn a_window_that_is_not_live_gets_only_what_appkit_tells_it_about_itself() {
        for input in [
            NSEventType::LeftMouseDown,
            NSEventType::LeftMouseUp,
            NSEventType::RightMouseDown,
            NSEventType::OtherMouseDown,
            NSEventType::LeftMouseDragged,
            NSEventType::MouseMoved,
            NSEventType::ScrollWheel,
            NSEventType::KeyDown,
            NSEventType::KeyUp,
            NSEventType::FlagsChanged,
            NSEventType::Gesture,
            NSEventType::Magnify,
            NSEventType::Pressure,
            // A kind of event this code has never heard of.
            NSEventType(999),
        ] {
            assert!(is_input(input), "{input:?}");
        }
        for own in [
            NSEventType::AppKitDefined,
            NSEventType::SystemDefined,
            NSEventType::ApplicationDefined,
            NSEventType::Periodic,
            NSEventType::CursorUpdate,
            NSEventType::MouseEntered,
            NSEventType::MouseExited,
        ] {
            assert!(!is_input(own), "{own:?}");
        }
        assert!(is_key(NSEventType::KeyDown) && is_key(NSEventType::KeyUp) && is_key(NSEventType::FlagsChanged));
        assert!(!is_key(NSEventType::LeftMouseDown));
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
