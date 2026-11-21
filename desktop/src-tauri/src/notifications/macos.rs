//! macOS notifications through `UNUserNotificationCenter`: the one owner of
//! authorization, posting, withdrawal, and what a click on a notification
//! opens. The deprecated `NSUserNotificationCenter` is no longer used, so every
//! post and every response goes through this center and its delegate.
//!
//! An executable outside an app bundle (a development build) has no
//! notification identity; [`available`] is false there and callers keep the
//! plugin path.
use std::cell::RefCell;
use std::collections::HashSet;
use std::ptr::NonNull;
use std::sync::{Arc, Mutex};

use avibe_runtime_host::notifications::{NotificationReceipt, NotificationRegistry, NotificationSnapshot};
use block2::RcBlock;
use objc2::rc::Retained;
use objc2::runtime::{Bool, ProtocolObject};
use objc2::{define_class, msg_send, AnyThread, DefinedClass};
use objc2_foundation::{NSArray, NSBundle, NSDictionary, NSError, NSObject, NSObjectProtocol, NSString};
use objc2_user_notifications::{
    UNAuthorizationOptions, UNAuthorizationStatus, UNMutableNotificationContent, UNNotification,
    UNNotificationDefaultActionIdentifier, UNNotificationPresentationOptions, UNNotificationRequest,
    UNNotificationResponse, UNNotificationSettings, UNUserNotificationCenter, UNUserNotificationCenterDelegate,
};
use tauri::plugin::{Builder, TauriPlugin};
use tauri::{AppHandle, RunEvent};

use super::NotificationCopy;

/// The `userInfo` key holding the `avibe://` link a click opens.
const OPENS_KEY: &str = "avibe_opens";

/// Whether this process has a bundle identity the notification center accepts.
pub(super) fn available() -> bool {
    NSBundle::mainBundle().bundleIdentifier().is_some()
}

fn center() -> Retained<UNUserNotificationCenter> {
    UNUserNotificationCenter::currentNotificationCenter()
}

/// The exact delivery key of one receipt; a newer post never replaces an older one.
pub(super) fn identifier(receipt: &NotificationReceipt) -> String {
    format!("avibe:{}:{}", receipt.session_id().unwrap_or_default(), receipt.key())
}

/// What the center's answer to "may Avibe post?" means for one post.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Permission {
    Granted,
    Refused,
    Ask,
}

fn permission(status: UNAuthorizationStatus) -> Permission {
    match status {
        UNAuthorizationStatus::NotDetermined => Permission::Ask,
        UNAuthorizationStatus::Authorized | UNAuthorizationStatus::Provisional | UNAuthorizationStatus::Ephemeral => {
            Permission::Granted
        }
        _ => Permission::Refused,
    }
}

/// Runs `then` with whether a post may be made, asking macOS once when the user
/// has not decided yet. The question is asked in context: by the first post.
fn authorized(then: impl Fn(bool) + 'static) {
    let then = Arc::new(then);
    let settings = RcBlock::new(move |settings: NonNull<UNNotificationSettings>| {
        // SAFETY: the center passes a valid settings object for the call.
        let status = unsafe { settings.as_ref() }.authorizationStatus();
        match permission(status) {
            Permission::Granted => then(true),
            Permission::Refused => then(false),
            Permission::Ask => {
                let then = then.clone();
                let answer = RcBlock::new(move |granted: Bool, _error: *mut NSError| then(granted.as_bool()));
                center().requestAuthorizationWithOptions_completionHandler(
                    UNAuthorizationOptions::Alert | UNAuthorizationOptions::Sound,
                    &answer,
                );
            }
        }
    });
    center().getNotificationSettingsWithCompletionHandler(&settings);
}

fn content(
    copy: &NotificationCopy,
    thread: Option<&str>,
    opens: Option<&str>,
) -> Retained<UNMutableNotificationContent> {
    let content = UNMutableNotificationContent::new();
    content.setTitle(&NSString::from_str(&copy.title));
    content.setBody(&NSString::from_str(&copy.body));
    if let Some(thread) = thread {
        content.setThreadIdentifier(&NSString::from_str(thread));
    }
    if let Some(opens) = opens {
        let key = NSString::from_str(OPENS_KEY);
        let value = NSString::from_str(opens);
        let info: Retained<NSDictionary<NSString, NSString>> = NSDictionary::from_slices(&[&*key], &[&*value]);
        // SAFETY: a string-to-string dictionary is a valid property list for `userInfo`.
        unsafe {
            let info: Retained<NSDictionary> = Retained::cast_unchecked(info);
            content.setUserInfo(&info);
        }
    }
    content
}

/// Posts one notification once macOS allows it. `refused` runs when it is not
/// posted (permission refused or the center rejected it), so the caller can
/// forget the receipt it reserved. The legacy center posted silently; so does this.
fn post(
    identifier: String,
    copy: NotificationCopy,
    thread: Option<String>,
    opens: Option<String>,
    refused: impl Fn() + 'static,
) {
    let refused = Arc::new(refused);
    authorized(move |granted| {
        if !granted {
            refused();
            return;
        }
        let request = UNNotificationRequest::requestWithIdentifier_content_trigger(
            &NSString::from_str(&identifier),
            &content(&copy, thread.as_deref(), opens.as_deref()),
            None,
        );
        let refused = refused.clone();
        let added = RcBlock::new(move |error: *mut NSError| {
            if !error.is_null() {
                eprintln!("The desktop notification could not be submitted to the operating system.");
                refused();
            }
        });
        center().addNotificationRequest_withCompletionHandler(&request, Some(&added));
    });
}

/// Posts a receipt's notification under its exact delivery key.
pub(super) fn show(
    receipt: &Arc<NotificationReceipt>,
    copy: NotificationCopy,
    registry: Arc<Mutex<NotificationRegistry>>,
) {
    let id = receipt.id();
    post(
        identifier(receipt),
        copy,
        receipt.session_id().map(str::to_owned),
        receipt.opens(),
        move || {
            registry.lock().expect("notification registry lock").forget(id);
        },
    );
}

/// Posts a one-off notice that no unread reconciliation tracks.
pub(super) fn notice(copy: NotificationCopy) {
    // Unique across launches, so a notice never replaces an earlier one still shown.
    static NEXT: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);
    let started = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|elapsed| elapsed.as_nanos())
        .unwrap_or_default();
    let sequence = NEXT.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
    post(format!("avibe:notice:{started}:{sequence}"), copy, None, None, || {});
}

/// Reconciles the snapshot with what macOS still shows: retired posts are
/// removed, and posts confirmed absent are forgotten after their grace.
pub(super) fn withdraw(receipts: NotificationSnapshot, registry: Arc<Mutex<NotificationRegistry>>) {
    let receipts = RefCell::new(Some(receipts));
    let delivered = RcBlock::new(move |shown: NonNull<NSArray<UNNotification>>| {
        let Some(receipts) = receipts.borrow_mut().take() else {
            return;
        };
        // SAFETY: the center passes a valid array for the call.
        let shown: HashSet<String> = unsafe { shown.as_ref() }
            .iter()
            .map(|notification| notification.request().identifier().to_string())
            .collect();
        let (remove, mut forgotten) =
            super::native::reconcile_posts(receipts, |receipt| shown.contains(&identifier(receipt)));
        if !remove.is_empty() {
            let keys = remove
                .iter()
                .map(|receipt| NSString::from_str(&identifier(receipt)))
                .collect::<Vec<_>>();
            center().removeDeliveredNotificationsWithIdentifiers(&NSArray::from_retained_slice(&keys));
            forgotten.extend(remove.iter().map(|receipt| receipt.id()));
        }
        let mut registry = registry.lock().expect("notification registry lock");
        for id in forgotten {
            registry.forget(id);
        }
    });
    center().getDeliveredNotificationsWithCompletionHandler(&delivered);
}

/// The link a response opens, read back from the notification it answers.
fn opens(response: &UNNotificationResponse) -> Option<String> {
    let info = response.notification().request().content().userInfo();
    let key = NSString::from_str(OPENS_KEY);
    let value = info.objectForKey(&key)?;
    value.downcast_ref::<NSString>().map(|link| link.to_string())
}

struct DelegateState {
    app: AppHandle,
}

define_class!(
    #[unsafe(super = NSObject)]
    #[name = "AvibeNotificationDelegate"]
    #[ivars = DelegateState]
    struct Delegate;

    unsafe impl NSObjectProtocol for Delegate {}

    unsafe impl UNUserNotificationCenterDelegate for Delegate {
        // A post already passed the focus gate; show it even if Avibe became
        // active in the meantime, as the legacy center did.
        #[unsafe(method(userNotificationCenter:willPresentNotification:withCompletionHandler:))]
        fn will_present(
            &self,
            _center: &UNUserNotificationCenter,
            _notification: &UNNotification,
            completion: &block2::DynBlock<dyn Fn(UNNotificationPresentationOptions)>,
        ) {
            completion.call((UNNotificationPresentationOptions::Banner | UNNotificationPresentationOptions::List,));
        }

        // A click on the notification opens what it is about. Any other action
        // is not offered yet, and is answered without doing anything.
        #[unsafe(method(userNotificationCenter:didReceiveNotificationResponse:withCompletionHandler:))]
        fn did_receive(
            &self,
            _center: &UNUserNotificationCenter,
            response: &UNNotificationResponse,
            completion: &block2::DynBlock<dyn Fn()>,
        ) {
            // SAFETY: a framework constant string.
            let default = unsafe { UNNotificationDefaultActionIdentifier };
            if response.actionIdentifier().isEqualToString(default) {
                let app = &self.ivars().app;
                match opens(response) {
                    Some(link) => crate::receive_native_deep_link(app, [link]),
                    None => crate::focus_or_restore_main_window(app),
                }
            }
            completion.call(());
        }
    }
);

impl Delegate {
    fn new(app: AppHandle) -> Retained<Self> {
        let allocated = Self::alloc().set_ivars(DelegateState { app });
        unsafe { msg_send![super(allocated), init] }
    }
}

thread_local! {
    // The center holds its delegate weakly.
    static DELEGATE: RefCell<Option<Retained<Delegate>>> = const { RefCell::new(None) };
}

/// The center's delegate is set in plugin setup, before the application
/// finishes launching, so a click that launched Avibe still reaches it.
pub fn init() -> TauriPlugin<tauri::Wry> {
    Builder::new("macos-notifications")
        .setup(|app, _| {
            if available() {
                let delegate = Delegate::new(app.clone());
                center().setDelegate(Some(ProtocolObject::from_ref(&*delegate)));
                DELEGATE.with(|current| *current.borrow_mut() = Some(delegate));
            }
            Ok(())
        })
        .on_event(|_, event| {
            if matches!(event, RunEvent::Exit) {
                DELEGATE.with(|current| {
                    if current.borrow_mut().take().is_some() {
                        center().setDelegate(None);
                    }
                });
            }
        })
        .build()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn identifiers_keep_session_identity_and_do_not_replace_newer_deliveries() {
        let mut registry = NotificationRegistry::default();
        let first = registry.reserve(Some("ses_a".into())).unwrap();
        let next = registry.reserve(Some("ses_a".into())).unwrap();
        assert!(identifier(&first).starts_with("avibe:ses_a:"));
        assert_ne!(identifier(&first), identifier(&next));
    }

    #[test]
    fn only_an_undecided_user_is_asked_and_only_a_grant_posts() {
        assert_eq!(permission(UNAuthorizationStatus::NotDetermined), Permission::Ask);
        for granted in [
            UNAuthorizationStatus::Authorized,
            UNAuthorizationStatus::Provisional,
            UNAuthorizationStatus::Ephemeral,
        ] {
            assert_eq!(permission(granted), Permission::Granted);
        }
        assert_eq!(permission(UNAuthorizationStatus::Denied), Permission::Refused);
        assert_eq!(permission(UNAuthorizationStatus(99)), Permission::Refused);
    }
}
