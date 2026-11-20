//! The native Show window's title-bar share button (ui/design:
//! show-pages/desktop-native-entry/titlebar-*): the system share symbol at the
//! right of the title bar, with no label, sized and coloured as a toolbar
//! symbol (design/titlebar-size, option A). Pressing it asks the window's
//! current document to open its share popover by dispatching
//! `avibe:show-share-open`. That is Rust talking to the page; no IPC is
//! granted for it. A document that does not listen (another route, another
//! site) simply ignores it.
//!
//! On macOS 26 and later the system draws the title from the traffic lights
//! and cuts it off a fixed distance from the right edge, whatever accessory
//! sits there, so a long title runs into the button. There the window's own
//! title is hidden and drawn instead by [`Title`]: the same font, colour and
//! place, ending before the button with a trailing "…", and hidden when even
//! that does not fit. The window's `title` itself is untouched, so the Window
//! menu, Mission Control and VoiceOver still name it in full.
//!
//! It names no crate item, so a probe can compile it from its source file.

use std::cell::RefCell;

use objc2::{
    define_class, msg_send,
    rc::{Retained, Weak},
    runtime::AnyObject,
    sel, DefinedClass, MainThreadMarker, MainThreadOnly, Message,
};
use objc2_app_kit::{
    NSAccessibility, NSAutoresizingMaskOptions, NSButton, NSColor, NSFont, NSFontWeightMedium, NSImage,
    NSImageSymbolConfiguration, NSLayoutAttribute, NSLineBreakMode, NSObjectNSKeyValueBindingCreation, NSTextField,
    NSTitlebarAccessoryViewController, NSView, NSWindow, NSWindowButton, NSWindowTitleVisibility,
    NSWindowWillCloseNotification,
};
use objc2_foundation::{NSNotification, NSNotificationCenter, NSPoint, NSProcessInfo, NSRect, NSSize, NSString};

/// The open event, dispatched only while the top document is still on
/// `route`, the path the window was opened at; the path is JSON-quoted.
fn open_script(route: &str) -> String {
    let route = serde_json::to_string(route).expect("a path is JSON");
    format!(
        "if(window.self===window.top&&location.pathname==={route})window.dispatchEvent(new CustomEvent('avibe:show-share-open'));"
    )
}

/// The symbol's point size: a toolbar symbol's, as Safari's and Preview's.
const SYMBOL: f64 = 15.0;
/// The button, square, in points.
const BUTTON: f64 = 28.0;
/// From the button to the window's right edge: the traffic lights' inset
/// from the left edge.
const RIGHT: f64 = 9.0;
/// The accessory's width: the button, its inset, and a little room before it.
const WIDTH: f64 = BUTTON + RIGHT + 4.0;
/// Between the end of the title and the accessory.
const TITLE_GAP: f64 = 8.0;
/// Between the zoom button and the title, as the system lays it out.
const TITLE_LEAD: f64 = 13.0;
/// The narrowest title worth showing: a character or two and "…". Narrower,
/// the title is hidden.
const TITLE_MIN: f64 = 40.0;

/// How wide the title may be in a title bar `bar` points wide whose title
/// starts at `leading`: up to the share button, less a gap.
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
    press: Box<dyn Fn()>,
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
        fn press(&self, _sender: Option<&AnyObject>) {
            (self.ivars().press)();
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
    fn new(mtm: MainThreadMarker, height: f64, press: Box<dyn Fn()>) -> Retained<Self> {
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

/// Adds the button to the title bar of `window`, a native Show window, and on
/// macOS 26 and later draws its title to end before it. `label` is the
/// button's spoken name and tooltip. A press runs `press`, on the main thread.
pub fn install_on(window: &NSWindow, mtm: MainThreadMarker, label: &str, press: Box<dyn Fn()>) -> bool {
    let label = NSString::from_str(label);
    let Some(image) = NSImage::imageWithSystemSymbolName_accessibilityDescription(
        &NSString::from_str("square.and.arrow.up"),
        Some(&label),
    ) else {
        return false;
    };
    // SAFETY: Reading a constant AppKit defines.
    let symbol = NSImageSymbolConfiguration::configurationWithPointSize_weight(SYMBOL, unsafe { NSFontWeightMedium });
    let image = image.imageWithSymbolConfiguration(&symbol).unwrap_or(image);
    let height = window.frame().size.height - window.contentLayoutRect().size.height;
    let host = Host::new(mtm, height, press);
    // SAFETY: the host implements `press:` and outlives the button it holds.
    let button = unsafe { NSButton::buttonWithImage_target_action(&image, Some(&host), Some(sel!(press:)), mtm) };
    button.setBordered(false);
    button.setContentTintColor(Some(&NSColor::labelColor()));
    button.setToolTip(Some(&label));
    button.setFrame(NSRect::new(
        NSPoint::new(WIDTH - RIGHT - BUTTON, ((height - BUTTON) / 2.0).round()),
        NSSize::new(BUTTON, BUTTON),
    ));
    host.addSubview(&button);
    let accessory = NSTitlebarAccessoryViewController::new(mtm);
    accessory.setView(&host);
    accessory.setLayoutAttribute(NSLayoutAttribute::Right);
    window.addTitlebarAccessoryViewController(&accessory);
    if draws_own_title() {
        if let Some(title) = install_title(window, mtm, height) {
            *host.ivars().title.borrow_mut() = Some(Weak::from_retained(&title));
        }
    }
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

/// Adds the button to a native Show window opened at `route`; a press
/// dispatches the open event into its current document if that document is
/// still on `route`, checked natively and again in the script. Away from it
/// (another page, or a `pushState` elsewhere) a press does nothing; back on it,
/// it works again.
pub fn install(window: &tauri::WebviewWindow, label: String, route: String) {
    let owned = window.clone();
    let _ = window.run_on_main_thread(move || {
        let Some(mtm) = MainThreadMarker::new() else { return };
        let Ok(pointer) = owned.ns_window() else { return };
        // SAFETY: Tauri's live NSWindow, on its main thread.
        let Some(ns_window) = (unsafe { Retained::retain(pointer.cast::<NSWindow>()) }) else {
            return;
        };
        let page = owned.clone();
        let script = open_script(&route);
        install_on(
            &ns_window,
            mtm,
            &label,
            Box::new(move || {
                if page.url().is_ok_and(|url| url.path() == route) {
                    let _ = page.eval(&script);
                }
            }),
        );
    });
}

/// Presses the button `install_on` added to `window`, as a click would. The
/// probe (`examples/show_share_probe.rs`) drives the shipping button with it.
#[allow(dead_code)]
pub fn press(window: &NSWindow) -> bool {
    let controllers = window.titlebarAccessoryViewControllers();
    for controller in controllers.iter() {
        let view = controller.view();
        if let Some(host) = view.downcast_ref::<Host>() {
            for subview in host.subviews().iter() {
                if let Some(button) = subview.downcast_ref::<NSButton>() {
                    // SAFETY: a plain button click, on the main thread.
                    unsafe { button.performClick(None) };
                    return true;
                }
            }
        }
    }
    false
}

/// The drawn title's text, whether it shows, how far it reaches, where the
/// button starts, its natural width (title-bar points), and whether a click on
/// it reaches the title bar. The probe checks them.
#[allow(dead_code)]
pub fn title_state(window: &NSWindow) -> Option<(String, bool, f64, f64, f64, bool)> {
    let zoom = window.standardWindowButton(NSWindowButton::ZoomButton)?;
    // SAFETY: Reading the button's own superview.
    let bar = unsafe { zoom.superview() }?;
    let title = bar.subviews().iter().find_map(|view| view.downcast::<Title>().ok())?;
    let label = &title.ivars().label;
    let end = title.frame().origin.x + label.frame().size.width;
    let button = window
        .titlebarAccessoryViewControllers()
        .iter()
        .find_map(|controller| controller.view().downcast::<Host>().ok())?;
    // SAFETY: Reading the accessory view's own superview.
    let container = unsafe { button.superview() };
    let start = bar.convertPoint_fromView(button.frame().origin, container.as_deref()).x + (WIDTH - RIGHT - BUTTON);
    // A click on the title lands on the title bar, not on the drawn title.
    let middle = NSPoint::new(
        title.frame().origin.x + 10.0,
        bar.frame().origin.y + bar.frame().size.height / 2.0,
    );
    let hit = bar.hitTest(middle);
    let passes =
        hit.is_some_and(|view| view.downcast_ref::<Title>().is_none() && view.downcast_ref::<NSTextField>().is_none());
    Some((
        label.stringValue().to_string(),
        !label.isHidden(),
        end,
        start,
        label.intrinsicContentSize().width,
        passes,
    ))
}

/// The views `install_on` added to `window` and that are still in it: the
/// button's accessory and the drawn title. The probe watches them go.
#[allow(dead_code)]
pub fn installed(window: &NSWindow) -> Vec<Retained<NSView>> {
    let mut views: Vec<Retained<NSView>> = window
        .titlebarAccessoryViewControllers()
        .iter()
        .filter_map(|controller| controller.view().downcast::<Host>().ok())
        .map(Retained::into_super)
        .collect();
    // SAFETY: Reading the button's own superview.
    if let Some(bar) = window
        .standardWindowButton(NSWindowButton::ZoomButton)
        .and_then(|zoom| unsafe { zoom.superview() })
    {
        views.extend(
            bar.subviews()
                .iter()
                .filter_map(|view| view.downcast::<Title>().ok())
                .map(Retained::into_super),
        );
    }
    views
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_open_script_dispatches_the_event_the_page_listens_for() {
        let script = open_script("/apps/show/ses%3A1");
        assert!(script.contains("new CustomEvent('avibe:show-share-open')"));
        assert!(script.starts_with("if(window.self===window.top&&location.pathname===\"/apps/show/ses%3A1\")"));
        // A path cannot break out of its string.
        assert!(open_script("/a\");alert(1);//").contains(r#""/a\");alert(1);//""#));
    }

    // A 1040 pt Show window's title bar, with the system's title starting at
    // 82 pt, after the traffic lights.
    #[test]
    fn a_title_ends_before_the_share_button_with_room_to_spare() {
        let width = title_width(1040.0, 82.0);
        assert_eq!(width, 1040.0 - 82.0 - WIDTH - TITLE_GAP);
        // The share button starts at 1040 - RIGHT - BUTTON.
        assert!(82.0 + width + TITLE_GAP <= 1040.0 - RIGHT - BUTTON);
        assert!(shows_title(width));
        // The narrowest Show window keeps a long stretch of title.
        assert!(title_width(720.0, 82.0) > 500.0);
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
