//! Delay one actual WKWebView eval at its execution boundary. Product script
//! generation and evaluation are unchanged; this fixture only schedules them.
use block2::{DynBlock, RcBlock};
use objc2::{
    rc::Retained,
    runtime::{AnyObject, Imp, Sel},
    sel,
};
use objc2_foundation::{NSError, NSString};
use std::{cell::RefCell, sync::OnceLock};
use tauri::Webview;

type Completion = DynBlock<dyn Fn(*mut AnyObject, *mut NSError)>;
type Evaluate = unsafe extern "C-unwind" fn(&AnyObject, Sel, &NSString, Option<&Completion>);
struct Held {
    view: Retained<AnyObject>,
    script: Retained<NSString>,
    completion: Option<RcBlock<dyn Fn(*mut AnyObject, *mut NSError)>>,
}
thread_local! {
    static ARMED: RefCell<bool> = const { RefCell::new(false) };
    static HELD: RefCell<Option<Held>> = const { RefCell::new(None) };
}
static ORIGINAL: OnceLock<Imp> = OnceLock::new();

pub fn install(view: &Webview) {
    view.with_webview(|platform| {
        ORIGINAL.get_or_init(|| unsafe {
            let view = &*platform.inner().cast::<AnyObject>();
            let method = view
                .class()
                .instance_method(sel!(evaluateJavaScript:completionHandler:))
                .unwrap();
            method.set_implementation(std::mem::transmute::<Evaluate, Imp>(evaluate))
        });
    })
    .unwrap();
}
pub fn arm() {
    assert!(HELD.with(|held| held.borrow().is_none()));
    ARMED.with(|armed| *armed.borrow_mut() = true);
}
pub fn release() {
    let held = HELD.with(|held| held.borrow_mut().take()).expect("held native event");
    unsafe {
        original()(
            &held.view,
            sel!(evaluateJavaScript:completionHandler:),
            &held.script,
            held.completion.as_deref(),
        );
    }
    super::super::emit(serde_json::json!({"event":"account_event_released"}));
}
unsafe fn original() -> Evaluate {
    unsafe { std::mem::transmute::<Imp, Evaluate>(*ORIGINAL.get().unwrap()) }
}
unsafe extern "C-unwind" fn evaluate(
    view: &AnyObject,
    selector: Sel,
    script: &NSString,
    completion: Option<&Completion>,
) {
    let text = script.to_string();
    let hold = ARMED.with(|armed| {
        let mut armed = armed.borrow_mut();
        if *armed && text.contains("avibe:desktop-client-state") && text.contains("\"catalog\"") {
            *armed = false;
            true
        } else {
            false
        }
    });
    unsafe {
        if hold {
            HELD.with(|held| {
                *held.borrow_mut() = Some(Held {
                    view: Retained::retain((view as *const AnyObject).cast_mut()).unwrap(),
                    script: Retained::retain((script as *const NSString).cast_mut()).unwrap(),
                    completion: completion.and_then(|block| RcBlock::copy((block as *const Completion).cast_mut())),
                });
            });
            super::super::emit(serde_json::json!({"event":"account_event_held"}));
        } else {
            original()(view, selector, script, completion);
        }
    }
}
