//! The native Show window's title-bar share button (ui/design:
//! show-pages/desktop-native-entry/titlebar-*): the system share symbol at the
//! right of the title bar, with no label. Pressing it asks the window's
//! current document to open its share popover by dispatching
//! `avibe:show-share-open`. That is Rust talking to the page; no IPC is
//! granted for it. A document that does not listen (another route, another
//! site) simply ignores it.
//!
//! It names no crate item, so a probe can compile it from its source file.

use objc2::{
    define_class, msg_send, rc::Retained, runtime::AnyObject, sel, DefinedClass, MainThreadMarker, MainThreadOnly,
};
use objc2_app_kit::{NSButton, NSImage, NSLayoutAttribute, NSTitlebarAccessoryViewController, NSView, NSWindow};
use objc2_foundation::{NSPoint, NSRect, NSSize, NSString};

/// The open event, dispatched only while the top document is still on
/// `route`, the path the window was opened at; the path is JSON-quoted.
fn open_script(route: &str) -> String {
    let route = serde_json::to_string(route).expect("a path is JSON");
    format!(
        "if(window.self===window.top&&location.pathname==={route})window.dispatchEvent(new CustomEvent('avibe:show-share-open'));"
    )
}
/// The accessory's width and the title bar's height, in points.
const WIDTH: f64 = 36.0;
const HEIGHT: f64 = 28.0;
const BUTTON: f64 = 24.0;

pub struct HostFields {
    press: Box<dyn Fn()>,
}

define_class!(
    /// The accessory's view, and the button's target: it owns what a press
    /// does, and lives exactly as long as the title bar holds it.
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
    }
);

impl Host {
    fn new(mtm: MainThreadMarker, press: Box<dyn Fn()>) -> Retained<Self> {
        let frame = NSRect::new(NSPoint::new(0.0, 0.0), NSSize::new(WIDTH, HEIGHT));
        let this = Self::alloc(mtm).set_ivars(HostFields { press });
        // SAFETY: NSView's designated initialiser.
        unsafe { msg_send![super(this), initWithFrame: frame] }
    }
}

/// Adds the button to the title bar of `window`, a native Show window.
/// `label` is its spoken name and tooltip. A press runs `press`, on the main
/// thread.
pub fn install_on(window: &NSWindow, mtm: MainThreadMarker, label: &str, press: Box<dyn Fn()>) -> bool {
    let label = NSString::from_str(label);
    let Some(image) = NSImage::imageWithSystemSymbolName_accessibilityDescription(
        &NSString::from_str("square.and.arrow.up"),
        Some(&label),
    ) else {
        return false;
    };
    let host = Host::new(mtm, press);
    // SAFETY: the host implements `press:` and outlives the button it holds.
    let button = unsafe { NSButton::buttonWithImage_target_action(&image, Some(&host), Some(sel!(press:)), mtm) };
    button.setBordered(false);
    button.setToolTip(Some(&label));
    button.setFrame(NSRect::new(
        NSPoint::new((WIDTH - BUTTON) / 2.0, (HEIGHT - BUTTON) / 2.0),
        NSSize::new(BUTTON, BUTTON),
    ));
    host.addSubview(&button);
    let accessory = NSTitlebarAccessoryViewController::new(mtm);
    accessory.setView(&host);
    accessory.setLayoutAttribute(NSLayoutAttribute::Right);
    window.addTitlebarAccessoryViewController(&accessory);
    true
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
}
