//! Inspect and dismiss only this fixture process's native modal window.
use objc2::{msg_send, rc::Retained, runtime::AnyObject, MainThreadMarker};
use objc2_app_kit::{NSApplication, NSView};
use objc2_foundation::{NSObjectProtocol, NSString};
use serde_json::{json, Value};

unsafe fn buttons(view: &NSView, found: &mut Vec<Retained<AnyObject>>) {
    if view.respondsToSelector(objc2::sel!(performClick:)) && view.respondsToSelector(objc2::sel!(title)) {
        if let Some(button) = Retained::retain((view as *const NSView).cast_mut().cast::<AnyObject>()) {
            found.push(button);
        }
    }
    for child in view.subviews() {
        buttons(&child, found);
    }
}

pub fn inspect(choice: Option<usize>) -> Value {
    let main = MainThreadMarker::new().expect("fixture dialog must run on main thread");
    let app = NSApplication::sharedApplication(main);
    let Some(window) = app.modalWindow() else {
        return json!({"event":"dialog","buttons":[]});
    };
    let mut found = vec![];
    unsafe {
        if let Some(content) = window.contentView() {
            buttons(&content, &mut found);
        }
    }
    let titles: Vec<_> = found
        .iter()
        .map(|button| {
            let title: Retained<NSString> = unsafe { msg_send![&**button, title] };
            title.to_string()
        })
        .collect();
    if let Some(index) = choice {
        let button = found.get(index).expect("fixture modal button missing");
        // Never send input to another application's window.
        unsafe {
            let _: () = msg_send![&**button, performClick: std::ptr::null::<AnyObject>()];
        }
    }
    json!({"event":"dialog","buttons":titles})
}
