//! The native Show window's four title-bar controls (ui/design:
//! show-pages/desktop-native-controls/group-*). Annotate and Share dispatch
//! events to the window's own route; Chat and Browser call its native owner.
//! No page IPC is granted for these actions.
//!
//! On macOS 26 and later the system draws the title from the traffic lights
//! and cuts it off a fixed distance from the right edge, whatever accessory
//! sits there, so a long title runs into the button. There the window's own
//! title is hidden and drawn instead by [`Title`]: the same font, colour and
//! place, ending before the button with a trailing "…", and hidden when even
//! that does not fit. The window's `title` itself is untouched, so the Window
//! menu, Mission Control and VoiceOver still name it in full.
//!
//! The standalone rendering probe supplies an explicit lock-free entry adapter.

use std::{
    cell::{Cell, RefCell},
    sync::Arc,
};

use objc2::{
    define_class, msg_send,
    rc::{Retained, Weak},
    sel, AnyThread, DefinedClass, MainThreadMarker, MainThreadOnly, Message,
};
use objc2_app_kit::{
    NSAccessibility, NSAutoresizingMaskOptions, NSBezierPath, NSButton, NSButtonType, NSCellImagePosition, NSColor,
    NSEvent, NSFont, NSFontWeightMedium, NSImage, NSImageSymbolConfiguration, NSLayoutAttribute, NSLineBreakMode,
    NSObjectNSKeyValueBindingCreation, NSTextField, NSTitlebarAccessoryViewController, NSToolbar, NSToolbarDisplayMode,
    NSTrackingArea, NSTrackingAreaOptions, NSView, NSWindow, NSWindowButton, NSWindowStyleMask,
    NSWindowTitleVisibility, NSWindowToolbarStyle, NSWindowWillCloseNotification,
};
use objc2_foundation::{NSNotification, NSNotificationCenter, NSPoint, NSProcessInfo, NSRect, NSSize, NSString};
use tauri::Manager;

/// The open event, dispatched only while the top document is still on
/// `route`, the path the window was opened at; the path is JSON-quoted.
fn open_script(route: &str, event: &str) -> String {
    let event = serde_json::to_string(event).expect("an event is JSON");
    let route = serde_json::to_string(route).expect("a path is JSON");
    format!("if(window.self===window.top&&location.pathname==={route})window.dispatchEvent(new CustomEvent({event}));")
}

/// The symbol's point size: a toolbar symbol's, as Safari's and Preview's.
const SYMBOL: f64 = 14.0;
const WASH: f64 = 20.0;
const GAP: f64 = 4.0;
/// The button, square, in points.
const BUTTON: f64 = 28.0;
/// From the button to the window's right edge: the traffic lights' inset
/// from the left edge.
const RIGHT: f64 = 9.0;
/// The accessory's width: four buttons, gaps, trailing inset and leading space.
const WIDTH: f64 = BUTTON * 4.0 + GAP * 3.0 + RIGHT + 4.0;
/// Between the end of the title and the accessory.
const TITLE_GAP: f64 = 8.0;
/// Between the zoom button and the title, as the system lays it out.
const TITLE_LEAD: f64 = 13.0;
/// The narrowest title worth showing: a character or two and "…". Narrower,
/// the title is hidden.
const TITLE_MIN: f64 = 40.0;

/// How wide the title may be in a title bar `bar` points wide whose title
/// starts at `leading`: up to the control group, less a gap.
fn title_width(bar: f64, leading: f64) -> f64 {
    (bar - leading - WIDTH - TITLE_GAP).max(0.0)
}

/// Whether a title `width` points wide is worth showing at all.
fn shows_title(width: f64) -> bool {
    width >= TITLE_MIN
}

/// The system lays the title out from the traffic lights on macOS 26 and
/// later; earlier, it centres it, and is left alone.
fn draws_own_title() -> bool {
    NSProcessInfo::processInfo().operatingSystemVersion().majorVersion >= 26
}

pub struct HostFields {
    press: Box<dyn Fn(Action)>,
    /// The drawn title, if there is one.
    title: RefCell<Option<Weak<Title>>>,
}

define_class!(
    /// The accessory's view, and the button's target: it owns what a press
    /// does, and lives exactly as long as the title bar holds it. It also
    /// owns taking everything out again when its window closes.
    #[unsafe(super(NSView))]
    #[thread_kind = MainThreadOnly]
    #[name = "AvibeShowShareTitleBarHost"]
    #[ivars = HostFields]
    pub struct Host;

    impl Host {
        #[unsafe(method(press:))]
        fn press(&self, sender: Option<&NSButton>) {
            if let Some(action) = sender.and_then(|sender| ACTIONS.get(sender.tag() as usize)) {
                (self.ivars().press)(*action);
            }
        }

        /// The window closes, whether the person closed it or the shell
        /// retired it: the drawn title lets go of the window, which its
        /// binding holds, and leaves the title bar, and the accessory, with
        /// this view and its press, leaves too. Nothing installed is left
        /// holding the window or held by it.
        #[unsafe(method(windowWillClose:))]
        fn window_will_close(&self, notification: &NSNotification) {
            // Removing the accessory releases this view; keep it for the call.
            let _this = self.retain();
            // SAFETY: Removing this observer, which `install_on` added.
            unsafe { NSNotificationCenter::defaultCenter().removeObserver(self) };
            if let Some(title) = self.ivars().title.borrow_mut().take().and_then(|title| title.load()) {
                title.detach();
            }
            let Some(window) = notification.object().and_then(|object| object.downcast::<NSWindow>().ok()) else {
                return;
            };
            let view: &NSView = self;
            let index = window
                .titlebarAccessoryViewControllers()
                .iter()
                .position(|controller| std::ptr::eq(&*controller.view(), view));
            if let Some(index) = index {
                window.removeTitlebarAccessoryViewControllerAtIndex(index as isize);
            }
        }
    }
);

impl Host {
    fn new(mtm: MainThreadMarker, height: f64, press: Box<dyn Fn(Action)>) -> Retained<Self> {
        let frame = NSRect::new(NSPoint::new(0.0, 0.0), NSSize::new(WIDTH, height));
        let this = Self::alloc(mtm).set_ivars(HostFields {
            press,
            title: RefCell::new(None),
        });
        // SAFETY: NSView's designated initialiser.
        unsafe { msg_send![super(this), initWithFrame: frame] }
    }
}

pub struct TitleFields {
    label: Retained<NSTextField>,
}

define_class!(
    /// The window's title, drawn as the system draws it, from where the
    /// system starts it to just before the share button. It follows the
    /// window's `title`, cuts it off with "…", and hides it when too narrow.
    /// It takes no clicks, so dragging and double-clicking the title bar
    /// still work, and VoiceOver hears the window's own title instead.
    #[unsafe(super(NSView))]
    #[thread_kind = MainThreadOnly]
    #[name = "AvibeShowShareTitle"]
    #[ivars = TitleFields]
    pub struct Title;

    impl Title {
        #[unsafe(method(setFrameSize:))]
        fn set_frame_size(&self, size: NSSize) {
            // SAFETY: NSView's own implementation.
            let _: () = unsafe { msg_send![super(self), setFrameSize: size] };
            self.lay_out(size);
        }

        #[unsafe(method(hitTest:))]
        fn hit_test(&self, _point: NSPoint) -> *mut NSView {
            std::ptr::null_mut()
        }
    }
);

impl Title {
    fn new(mtm: MainThreadMarker, window: &NSWindow, frame: NSRect) -> Retained<Self> {
        let label = NSTextField::labelWithString(&NSString::from_str(""), mtm);
        label.setFont(Some(&NSFont::titleBarFontOfSize(0.0)));
        label.setTextColor(Some(&NSColor::windowFrameTextColor()));
        label.setLineBreakMode(NSLineBreakMode::ByTruncatingTail);
        label.setAccessibilityElement(false);
        // SAFETY: `value` binds an NSString, and a window's `title` is one.
        // The binding holds the window, which holds the label: the host
        // unbinds it when the window closes (`windowWillClose:`).
        unsafe {
            label.bind_toObject_withKeyPath_options(
                &NSString::from_str("value"),
                window,
                &NSString::from_str("title"),
                None,
            )
        };
        let this = Self::alloc(mtm).set_ivars(TitleFields { label });
        // SAFETY: NSView's designated initialiser.
        let this: Retained<Self> = unsafe { msg_send![super(this), initWithFrame: frame] };
        this.addSubview(&this.ivars().label);
        this.setAutoresizingMask(NSAutoresizingMaskOptions::ViewWidthSizable);
        this.lay_out(frame.size);
        this
    }

    /// Unbinds the label from the window and leaves the title bar.
    fn detach(&self) {
        self.ivars().label.unbind(&NSString::from_str("value"));
        self.removeFromSuperview();
    }

    /// The label fills the width it has, centred on the title bar as the
    /// system's title is, and shows only if a useful part of it fits.
    fn lay_out(&self, size: NSSize) {
        let label = &self.ivars().label;
        let height = label.intrinsicContentSize().height;
        label.setFrame(NSRect::new(
            NSPoint::new(0.0, ((size.height - height) / 2.0).round()),
            NSSize::new(size.width, height),
        ));
        label.setHidden(!shows_title(size.width));
    }
}

/// Native control order, shared with the web popover anchors.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Action {
    Annotate,
    Chat,
    Share,
    Browser,
}
const ACTIONS: [Action; 4] = [Action::Annotate, Action::Chat, Action::Share, Action::Browser];
const SYMBOLS: [&str; 4] = [
    "plus.bubble",
    "message",
    "square.and.arrow.up",
    "arrow.up.forward.square",
];

pub struct ButtonFields {
    hovered: Cell<bool>,
}
define_class!(
    #[unsafe(super(NSButton))]
    #[thread_kind = MainThreadOnly]
    #[name = "AvibeShowTitleBarButton"]
    #[ivars = ButtonFields]
    pub struct TitlebarButton;
    impl TitlebarButton {
        #[unsafe(method(mouseEntered:))]
        fn mouse_entered(&self, _event: Option<&NSEvent>) {
            self.ivars().hovered.set(true);
            NSView::setNeedsDisplay(self, true);
        }
        #[unsafe(method(mouseExited:))]
        fn mouse_exited(&self, _event: Option<&NSEvent>) {
            self.ivars().hovered.set(false);
            NSView::setNeedsDisplay(self, true);
        }
        #[unsafe(method(drawRect:))]
        fn draw_rect(&self, rect: NSRect) {
            if self.ivars().hovered.get() {
                NSColor::labelColor().colorWithAlphaComponent(0.04).setFill();
                NSBezierPath::bezierPathWithRoundedRect_xRadius_yRadius(
                    NSRect::new(NSPoint::new((BUTTON - WASH) / 2.0, (BUTTON - WASH) / 2.0), NSSize::new(WASH, WASH)),
                    6.0, 6.0,
                ).fill();
            }
            // SAFETY: NSButton draws its symbol and keyboard focus as usual.
            let _: () = unsafe { msg_send![super(self), drawRect: rect] };
        }
    }
);

impl TitlebarButton {
    fn new(mtm: MainThreadMarker, frame: NSRect) -> Retained<Self> {
        let this = Self::alloc(mtm).set_ivars(ButtonFields {
            hovered: Cell::new(false),
        });
        // SAFETY: NSButton's designated initializer.
        let this: Retained<Self> = unsafe { msg_send![super(this), initWithFrame: frame] };
        // SAFETY: The view owns this tracking area; its owner receives the two implemented selectors.
        let tracking = unsafe {
            NSTrackingArea::initWithRect_options_owner_userInfo(
                NSTrackingArea::alloc(),
                this.bounds(),
                NSTrackingAreaOptions::MouseEnteredAndExited
                    | NSTrackingAreaOptions::ActiveInActiveApp
                    | NSTrackingAreaOptions::InVisibleRect,
                Some(&this),
                None,
            )
        };
        this.addTrackingArea(&tracking);
        this
    }
}

/// Install the four controls. Labels are localized by the caller; presses stay native.
pub fn install_on(window: &NSWindow, mtm: MainThreadMarker, labels: &[String; 4], press: Box<dyn Fn(Action)>) -> bool {
    // Use AppKit's compact title/toolbar row (40 pt on the current macOS).
    // AppKit positions the traffic lights and owns the content boundary,
    // including full-screen transitions; no controls are moved by hand.
    window.setToolbarStyle(NSWindowToolbarStyle::UnifiedCompact);
    let toolbar = NSToolbar::initWithIdentifier(NSToolbar::alloc(mtm), &NSString::from_str("AvibeShowControls"));
    toolbar.setDisplayMode(NSToolbarDisplayMode::IconOnly);
    toolbar.setAllowsUserCustomization(false);
    window.setToolbar(Some(&toolbar));
    let height = window.frame().size.height - window.contentLayoutRect().size.height;
    let host = Host::new(mtm, height, press);
    for (index, symbol_name) in SYMBOLS.iter().enumerate() {
        let label = NSString::from_str(&labels[index]);
        let Some(image) =
            NSImage::imageWithSystemSymbolName_accessibilityDescription(&NSString::from_str(symbol_name), Some(&label))
        else {
            return false;
        };
        // SAFETY: Reading a constant AppKit defines.
        let symbol =
            NSImageSymbolConfiguration::configurationWithPointSize_weight(SYMBOL, unsafe { NSFontWeightMedium });
        let image = image.imageWithSymbolConfiguration(&symbol).unwrap_or(image);
        let button = TitlebarButton::new(
            mtm,
            NSRect::new(
                NSPoint::new(4.0 + index as f64 * (BUTTON + GAP), ((height - BUTTON) / 2.0).round()),
                NSSize::new(BUTTON, BUTTON),
            ),
        );
        button.setButtonType(NSButtonType::MomentaryPushIn);
        button.setImagePosition(NSCellImagePosition::ImageOnly);
        button.setTitle(&NSString::from_str(""));
        button.setImage(Some(&image));
        button.setBordered(false);
        button.setContentTintColor(Some(&NSColor::labelColor()));
        button.setToolTip(Some(&label));
        button.setAccessibilityLabel(Some(&label));
        button.setTag(index as isize);
        // SAFETY: The host implements press: and owns each button.
        unsafe {
            button.setTarget(Some(&host));
            button.setAction(Some(sel!(press:)));
        }
        host.addSubview(&button);
    }
    let accessory = NSTitlebarAccessoryViewController::new(mtm);
    accessory.setView(&host);
    accessory.setLayoutAttribute(NSLayoutAttribute::Right);
    window.addTitlebarAccessoryViewController(&accessory);
    if draws_own_title() {
        if let Some(title) = install_title(window, mtm, height) {
            *host.ivars().title.borrow_mut() = Some(Weak::from_retained(&title));
        }
    }
    // Unlike the main Workbench's overlay, the Show WebView starts below the
    // native row. Tauri/AppKit may have enabled a full-size content view.
    let mut style = window.styleMask();
    style.remove(NSWindowStyleMask::FullSizeContentView);
    window.setStyleMask(style);
    // SAFETY: The host implements `windowWillClose:`; the center holds it
    // weakly, and it removes itself there. The window is only matched.
    unsafe {
        NSNotificationCenter::defaultCenter().addObserver_selector_name_object(
            &host,
            sel!(windowWillClose:),
            Some(NSWindowWillCloseNotification),
            Some(window),
        )
    };
    true
}

/// Hides the system's title and draws [`Title`] where it was, in the view
/// that holds the traffic lights.
fn install_title(window: &NSWindow, mtm: MainThreadMarker, height: f64) -> Option<Retained<Title>> {
    let zoom = window.standardWindowButton(NSWindowButton::ZoomButton)?;
    // SAFETY: Reading the button's own superview.
    let bar = unsafe { zoom.superview() }?;
    let leading = zoom.frame().origin.x + zoom.frame().size.width + TITLE_LEAD;
    let width = title_width(bar.bounds().size.width, leading);
    let title = Title::new(
        mtm,
        window,
        NSRect::new(NSPoint::new(leading, 0.0), NSSize::new(width, height)),
    );
    bar.addSubview(&title);
    window.setTitleVisibility(NSWindowTitleVisibility::Hidden);
    Some(title)
}

/// Adds the controls to a native Show window opened at `route`; a press
/// dispatches the open event into its current document if that document is
/// still on `route`, checked natively and again in the script. Away from it
/// (another page, or a `pushState` elsewhere) a press does nothing; back on it,
/// it works again.
pub fn install(
    window: &tauri::WebviewWindow,
    labels: [String; 4],
    destination: tauri::Url,
    action: impl Fn(Action) + Send + Sync + 'static,
) {
    let owned = window.clone();
    let _ = window.run_on_main_thread(move || {
        let Some(mtm) = MainThreadMarker::new() else { return };
        let Ok(pointer) = owned.ns_window() else { return };
        // SAFETY: Tauri's live NSWindow, on its main thread.
        let Some(ns_window) = (unsafe { Retained::retain(pointer.cast::<NSWindow>()) }) else {
            return;
        };
        let page = owned.clone();
        // The window starts at about:blank while Runtime cookies are projected.
        // Bind to its registered destination, never the provisional document.
        let origin = destination.origin();
        let route = destination.path().to_owned();
        // Keep the document check inside the replayed operation: a window may
        // navigate while authentication is pending. One entry guards all four
        // actions before either page eval or the native owner callback runs.
        let perform = Arc::new(move |pressed| {
            if !page
                .url()
                .is_ok_and(|url| url.origin() == origin && url.path() == route)
            {
                return;
            }
            match pressed {
                Action::Annotate => {
                    let _ = page.eval(open_script(&route, "avibe:show-annotate-open"));
                }
                Action::Share => {
                    let _ = page.eval(open_script(&route, "avibe:show-share-open"));
                }
                Action::Chat | Action::Browser => action(pressed),
            }
        });
        install_on(
            &ns_window,
            mtm,
            &labels,
            Box::new(move |pressed| {
                if !owned
                    .url()
                    .is_ok_and(|url| url.origin() == destination.origin() && url.path() == destination.path())
                {
                    return;
                }
                let pending = perform.clone();
                if !crate::app_lock::defer_kind(owned.app_handle(), crate::app_lock::EntryKind::Command, move |_| {
                    pending(pressed)
                }) {
                    perform(pressed);
                }
            }),
        );
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_open_script_dispatches_the_event_the_page_listens_for() {
        let script = open_script("/apps/show/ses%3A1", "avibe:show-share-open");
        assert!(script.contains(r#"new CustomEvent("avibe:show-share-open")"#));
        assert!(script.starts_with("if(window.self===window.top&&location.pathname===\"/apps/show/ses%3A1\")"));
        // A path cannot break out of its string.
        assert!(open_script("/a\");alert(1);//", "avibe:show-share-open").contains(r#""/a\");alert(1);//""#));
    }

    // A 1040 pt Show window's title bar, with the system's title starting at
    // 82 pt, after the traffic lights.
    #[test]
    fn a_title_ends_before_the_control_group_with_room_to_spare() {
        let width = title_width(1040.0, 82.0);
        assert_eq!(width, 1040.0 - 82.0 - WIDTH - TITLE_GAP);
        // The first button starts after the accessory's leading spacer.
        assert!(82.0 + width + TITLE_GAP <= 1040.0 - WIDTH + 4.0);
        assert!(shows_title(width));
        // The narrowest Show window keeps a long stretch of title.
        assert!(title_width(720.0, 82.0) > 480.0);
    }

    #[test]
    fn a_title_bar_too_narrow_for_any_useful_title_shows_none() {
        let just = 82.0 + WIDTH + TITLE_GAP + TITLE_MIN;
        assert!(shows_title(title_width(just, 82.0)));
        assert!(!shows_title(title_width(just - 1.0, 82.0)));
        assert_eq!(title_width(100.0, 82.0), 0.0);
        assert!(!shows_title(title_width(100.0, 82.0)));
    }
}
