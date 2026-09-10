use std::cell::RefCell;

use objc2::rc::Retained;
use objc2::{define_class, msg_send, sel, AnyThread, DefinedClass};
use objc2_foundation::{NSAppleEventDescriptor, NSAppleEventManager, NSObject, NSObjectProtocol};
use tauri::plugin::{Builder, TauriPlugin};
use tauri::RunEvent;

const GET_URL_EVENT: u32 = u32::from_be_bytes(*b"GURL");
const DIRECT_OBJECT: u32 = u32::from_be_bytes(*b"----");

struct HandlerState {
    receive: Box<dyn Fn(String)>,
}

define_class!(
    #[unsafe(super = NSObject)]
    #[name = "AvibeRawDeepLinkHandler"]
    #[ivars = HandlerState]
    struct RawDeepLinkHandler;

    unsafe impl NSObjectProtocol for RawDeepLinkHandler {}

    impl RawDeepLinkHandler {
        #[unsafe(method(handleGetURLEvent:withReplyEvent:))]
        fn handle_get_url(&self, event: &NSAppleEventDescriptor, _reply: &NSAppleEventDescriptor) {
            if let Some(raw) = raw_url(event) {
                (self.ivars().receive)(raw);
            }
        }
    }
);

impl RawDeepLinkHandler {
    fn new(receive: impl Fn(String) + 'static) -> Retained<Self> {
        let allocated = Self::alloc().set_ivars(HandlerState {
            receive: Box::new(receive),
        });
        unsafe { msg_send![super(allocated), init] }
    }
}

thread_local! {
    static HANDLER: RefCell<Option<Retained<RawDeepLinkHandler>>> = const { RefCell::new(None) };
}

fn raw_url(event: &NSAppleEventDescriptor) -> Option<String> {
    let descriptor: Option<Retained<NSAppleEventDescriptor>> =
        unsafe { msg_send![event, paramDescriptorForKeyword: DIRECT_OBJECT] };
    descriptor?.stringValue().map(|text| text.to_string())
}

fn install(receive: impl Fn(String) + 'static) {
    let handler = RawDeepLinkHandler::new(receive);
    let manager = NSAppleEventManager::sharedAppleEventManager();
    unsafe {
        let _: () = msg_send![
            &*manager,
            setEventHandler: &*handler,
            andSelector: sel!(handleGetURLEvent:withReplyEvent:),
            forEventClass: GET_URL_EVENT,
            andEventID: GET_URL_EVENT,
        ];
    }
    HANDLER.with(|current| *current.borrow_mut() = Some(handler));
}

fn remove() {
    HANDLER.with(|current| {
        let Some(handler) = current.borrow_mut().take() else {
            return;
        };
        let manager = NSAppleEventManager::sharedAppleEventManager();
        unsafe {
            let _: () = msg_send![
                &*manager,
                removeEventHandlerForEventClass: GET_URL_EVENT,
                andEventID: GET_URL_EVENT,
            ];
        }
        drop(handler);
    });
}

pub fn init() -> TauriPlugin<tauri::Wry> {
    Builder::new("macos-deep-link")
        .setup(|app, _| {
            let app = app.clone();
            install(move |raw| crate::receive_native_deep_link(&app, [raw]));
            Ok(())
        })
        .on_event(|_, event| {
            if matches!(event, RunEvent::Exit) {
                remove();
            }
        })
        .build()
}

#[cfg(test)]
mod tests {
    use std::sync::{Arc, Mutex};

    use avibe_runtime_host::deep_link::{parse_deep_link, DeepLinks};
    use avibe_runtime_host::LoopbackOrigin;
    use objc2::ClassType;
    use objc2_foundation::NSString;

    use super::*;

    fn native_event(raw: Option<&str>) -> Retained<NSAppleEventDescriptor> {
        let event: Retained<NSAppleEventDescriptor> = unsafe {
            msg_send![
                NSAppleEventDescriptor::class(),
                appleEventWithEventClass: GET_URL_EVENT,
                eventID: GET_URL_EVENT,
                targetDescriptor: None::<&NSAppleEventDescriptor>,
                returnID: -1_i16,
                transactionID: 0_i32,
            ]
        };
        if let Some(raw) = raw {
            let parameter = NSAppleEventDescriptor::descriptorWithString(&NSString::from_str(raw));
            unsafe {
                let _: () = msg_send![&*event, setParamDescriptor: &*parameter, forKeyword: DIRECT_OBJECT];
            }
        }
        event
    }

    #[test]
    fn native_descriptor_text_keeps_the_literal_grammar_visible_to_the_parser() {
        let valid = "avibe://session/target";
        assert!(parse_deep_link(valid).is_some());
        for raw in [
            valid,
            "avibe://session/ignored/../target",
            "avibe://session/ignored/%2E%2E/target",
            "avibe://session/target\0ignored",
            "avibe://session/你好",
        ] {
            let event = native_event(Some(raw));
            let received = raw_url(&event).unwrap();
            assert_eq!(received, raw);
            assert_eq!(parse_deep_link(&received).is_some(), raw == valid);
        }
        assert!(raw_url(&native_event(None)).is_none());
    }

    #[test]
    fn the_native_selector_feeds_literal_input_to_the_shared_stash_without_an_appkit_loop() {
        let origin = LoopbackOrigin::parse("http://127.0.0.1:39567").unwrap();
        let links = Arc::new(Mutex::new(DeepLinks::default()));
        let receiver = links.clone();
        let handler = RawDeepLinkHandler::new(move |raw| receiver.lock().unwrap().receive([raw]));
        let reply = NSAppleEventDescriptor::nullDescriptor();
        for (raw, expected_path) in [
            ("avibe://session/target", Some("/chat/target")),
            ("avibe://session/ignored/../target", None),
            ("avibe://session/ignored/%2E%2E/target", None),
        ] {
            let event = native_event(Some(raw));
            unsafe {
                let _: () = msg_send![&*handler, handleGetURLEvent: &*event, withReplyEvent: &*reply];
            }
            let navigation = links
                .lock()
                .unwrap()
                .workbench_navigation(&origin, &origin.navigation_url());
            assert_eq!(navigation.as_ref().map(|url| url.path()), expected_path);
        }
    }
}
