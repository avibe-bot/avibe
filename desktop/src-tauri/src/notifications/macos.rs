//! macOS notifications through `UNUserNotificationCenter`: the one owner of
//! authorization, posting, withdrawal, and what the user does with a
//! notification (open its subject, or answer an Agent's question). The
//! deprecated `NSUserNotificationCenter` is no longer used, so every post and
//! every response goes through this center and its delegate.
//!
//! An executable outside an app bundle (a development build) has no
//! notification identity; [`available`] is false there and callers keep the
//! plugin path.
use std::cell::RefCell;
use std::collections::HashSet;
use std::ptr::NonNull;
use std::sync::{Arc, Mutex};

use avibe_runtime_host::notifications::{
    AnswerRequest, NotificationReceipt, NotificationRegistry, NotificationSnapshot, MAX_ANSWER_OPTIONS,
};
use block2::RcBlock;
use objc2::rc::Retained;
use objc2::runtime::{Bool, ProtocolObject};
use objc2::{define_class, msg_send, sel, AnyThread, DefinedClass};
use objc2_foundation::{NSArray, NSBundle, NSDictionary, NSError, NSObject, NSObjectProtocol, NSSet, NSString};
use objc2_user_notifications::{
    UNAuthorizationOptions, UNAuthorizationStatus, UNMutableNotificationContent, UNNotification, UNNotificationAction,
    UNNotificationActionOptions, UNNotificationCategory, UNNotificationCategoryOptions,
    UNNotificationDefaultActionIdentifier, UNNotificationPresentationOptions, UNNotificationRequest,
    UNNotificationResponse, UNNotificationSettings, UNTextInputNotificationAction, UNUserNotificationCenter,
    UNUserNotificationCenterDelegate,
};
use serde::{Deserialize, Serialize};
use tauri::plugin::{Builder, TauriPlugin};
use tauri::{AppHandle, RunEvent};

use super::{AnswerCatalog, NotificationCopy};

/// The `userInfo` key holding the `avibe://` link a click opens.
const OPENS_KEY: &str = "avibe_opens";
/// The `userInfo` key holding the question an answer action answers.
const QUESTION_KEY: &str = "avibe_question";
const CHOICE_ACTION: &str = "avibe.choice.";
const REPLY_ACTION: &str = "avibe.reply";
/// Categories are registered as one set; only the recent option sets stay.
const MAX_CATEGORIES: usize = 32;
/// macOS cuts a long action title off; keep the start of it.
const ACTION_TITLE_CHARS: usize = 40;

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

fn authorization_label(status: UNAuthorizationStatus) -> &'static str {
    match status {
        UNAuthorizationStatus::NotDetermined => "not_determined",
        status if permission(status) == Permission::Granted => "authorized",
        _ => "denied",
    }
}

/// What macOS currently allows, read without asking.
pub(super) async fn authorization() -> &'static str {
    if !available() {
        return "unavailable";
    }
    let (sender, receiver) = tokio::sync::oneshot::channel();
    {
        let sender = Mutex::new(Some(sender));
        let settings = RcBlock::new(move |settings: NonNull<UNNotificationSettings>| {
            // SAFETY: the center passes a valid settings object for the call.
            let status = unsafe { settings.as_ref() }.authorizationStatus();
            if let Some(sender) = sender.lock().expect("authorization reply").take() {
                let _ = sender.send(authorization_label(status));
            }
        });
        center().getNotificationSettingsWithCompletionHandler(&settings);
    }
    receiver.await.unwrap_or("unavailable")
}

/// Avibe's own page in System Settings › Notifications.
pub(super) fn settings_url(identifier: &str) -> String {
    format!("x-apple.systempreferences:com.apple.Notifications-Settings.extension?id={identifier}")
}

/// What an answer from a notification needs to reach the Runtime: the
/// question, the options it offered with their revision, and the presentation
/// epoch it was posted in, so an answer never reaches another instance.
#[derive(Clone, Debug, PartialEq, Eq, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub(super) struct Question {
    pub session_id: String,
    pub message_id: String,
    pub revision: String,
    pub options: Vec<String>,
    pub epoch: u64,
    pub source: Option<String>,
}

/// What one post carries besides its words.
#[derive(Clone, Default)]
struct Extra {
    thread: Option<String>,
    opens: Option<String>,
    category: Option<String>,
    question: Option<String>,
}

fn content(copy: &NotificationCopy, extra: &Extra) -> Retained<UNMutableNotificationContent> {
    let content = UNMutableNotificationContent::new();
    content.setTitle(&NSString::from_str(&copy.title));
    if let Some(subtitle) = &copy.subtitle {
        content.setSubtitle(&NSString::from_str(subtitle));
    }
    content.setBody(&NSString::from_str(&copy.body));
    if let Some(thread) = &extra.thread {
        content.setThreadIdentifier(&NSString::from_str(thread));
    }
    if let Some(category) = &extra.category {
        content.setCategoryIdentifier(&NSString::from_str(category));
    }
    let entries = [(OPENS_KEY, &extra.opens), (QUESTION_KEY, &extra.question)]
        .into_iter()
        .filter_map(|(key, value)| {
            value
                .as_ref()
                .map(|value| (NSString::from_str(key), NSString::from_str(value)))
        })
        .collect::<Vec<_>>();
    if !entries.is_empty() {
        let keys = entries.iter().map(|(key, _)| &**key).collect::<Vec<_>>();
        let values = entries.iter().map(|(_, value)| &**value).collect::<Vec<_>>();
        let info: Retained<NSDictionary<NSString, NSString>> = NSDictionary::from_slices(&keys, &values);
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
fn post(identifier: String, copy: NotificationCopy, extra: Extra, refused: impl Fn() + 'static) {
    let refused = Arc::new(refused);
    authorized(move |granted| {
        if !granted {
            refused();
            return;
        }
        let request = UNNotificationRequest::requestWithIdentifier_content_trigger(
            &NSString::from_str(&identifier),
            &content(&copy, &extra),
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
    let extra = Extra {
        thread: receipt.session_id().map(str::to_owned),
        opens: receipt.opens(),
        ..Extra::default()
    };
    post(identifier(receipt), copy, extra, move || {
        registry.lock().expect("notification registry lock").forget(id);
    });
}

fn action_title(option: &str) -> String {
    if option.chars().count() <= ACTION_TITLE_CHARS {
        return option.to_owned();
    }
    let mut title = option.chars().take(ACTION_TITLE_CHARS - 1).collect::<String>();
    title.truncate(title.trim_end().len());
    title.push('…');
    title
}

/// One category per distinct option set and wording: its option buttons, then the reply field.
fn category_identifier(options: &[String], labels: &AnswerCatalog) -> String {
    use sha2::{Digest, Sha256};

    let mut digest = Sha256::new();
    for part in options.iter().chain([&labels.reply, &labels.send, &labels.placeholder]) {
        digest.update((part.len() as u64).to_be_bytes());
        digest.update(part.as_bytes());
    }
    let hash = digest.finalize();
    let short = hash[..12].iter().map(|byte| format!("{byte:02x}")).collect::<String>();
    format!("avibe.answer.{short}")
}

thread_local! {
    // Registered on the main thread only, as one set.
    static CATEGORIES: RefCell<Vec<(String, Retained<UNNotificationCategory>)>> = const { RefCell::new(Vec::new()) };
}

fn register_category(options: &[String], labels: &AnswerCatalog) -> String {
    let identifier = category_identifier(options, labels);
    CATEGORIES.with(|categories| {
        let mut categories = categories.borrow_mut();
        if let Some(index) = categories.iter().position(|(known, _)| *known == identifier) {
            let entry = categories.remove(index);
            categories.push(entry);
            return;
        }
        let mut actions = options
            .iter()
            .enumerate()
            .map(|(index, option)| {
                UNNotificationAction::actionWithIdentifier_title_options(
                    &NSString::from_str(&format!("{CHOICE_ACTION}{index}")),
                    &NSString::from_str(&action_title(option)),
                    UNNotificationActionOptions::empty(),
                )
            })
            .collect::<Vec<_>>();
        actions.push(Retained::into_super(
            UNTextInputNotificationAction::actionWithIdentifier_title_options_textInputButtonTitle_textInputPlaceholder(
                &NSString::from_str(REPLY_ACTION),
                &NSString::from_str(&labels.reply),
                UNNotificationActionOptions::empty(),
                &NSString::from_str(&labels.send),
                &NSString::from_str(&labels.placeholder),
            ),
        ));
        let category = UNNotificationCategory::categoryWithIdentifier_actions_intentIdentifiers_options(
            &NSString::from_str(&identifier),
            &NSArray::from_retained_slice(&actions),
            &NSArray::from_retained_slice(&[]),
            UNNotificationCategoryOptions::empty(),
        );
        categories.push((identifier.clone(), category));
        while categories.len() > MAX_CATEGORIES {
            categories.remove(0);
        }
        let set = categories
            .iter()
            .map(|(_, category)| category.clone())
            .collect::<Vec<_>>();
        center().setNotificationCategories(&NSSet::from_retained_slice(&set));
    });
    identifier
}

/// Posts an Agent's question: the first options as buttons and a reply
/// field when the Runtime gave the option set a revision. Without one (an
/// older Runtime) it is an ordinary notification that opens the Session.
pub(super) fn show_question(
    receipt: &Arc<NotificationReceipt>,
    copy: NotificationCopy,
    request: &AnswerRequest,
    source: Option<String>,
    epoch: u64,
    labels: &AnswerCatalog,
    registry: Arc<Mutex<NotificationRegistry>>,
) {
    let mut extra = Extra {
        thread: Some(request.session_id.clone()),
        opens: receipt.opens(),
        ..Extra::default()
    };
    if let Some(revision) = &request.revision {
        let shown = request
            .options
            .iter()
            .take(MAX_ANSWER_OPTIONS)
            .cloned()
            .collect::<Vec<_>>();
        extra.category = Some(register_category(&shown, labels));
        let question = Question {
            session_id: request.session_id.clone(),
            message_id: request.message_id.clone(),
            revision: revision.clone(),
            options: shown,
            epoch,
            source,
        };
        extra.question = serde_json::to_string(&question).ok();
    }
    let id = receipt.id();
    post(identifier(receipt), copy, extra, move || {
        registry.lock().expect("notification registry lock").forget(id);
    });
}

/// Posts a one-off notice that no unread reconciliation tracks; a click opens `opens`.
pub(super) fn notice(copy: NotificationCopy, opens: Option<String>) {
    // Unique across launches, so a notice never replaces an earlier one still shown.
    static NEXT: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);
    let started = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|elapsed| elapsed.as_nanos())
        .unwrap_or_default();
    let sequence = NEXT.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
    let extra = Extra {
        opens,
        ..Extra::default()
    };
    post(format!("avibe:notice:{started}:{sequence}"), copy, extra, || {});
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

/// A string the notification a response answers carries under `key`.
fn carried(response: &UNNotificationResponse, key: &str) -> Option<String> {
    let info = response.notification().request().content().userInfo();
    let value = info.objectForKey(&NSString::from_str(key))?;
    value.downcast_ref::<NSString>().map(|value| value.to_string())
}

/// What one action on an answerable notification says.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(super) enum Picked {
    Choice(String),
    Text(String),
}

/// Only an offered option or a non-empty reply is an answer.
fn picked(question: &Question, action: &str, typed: Option<String>) -> Option<Picked> {
    if action == REPLY_ACTION {
        return typed.filter(|text| !text.trim().is_empty()).map(Picked::Text);
    }
    let index = action.strip_prefix(CHOICE_ACTION)?.parse::<usize>().ok()?;
    question.options.get(index).cloned().map(Picked::Choice)
}

fn typed(response: &UNNotificationResponse) -> Option<String> {
    if !response.respondsToSelector(sel!(userText)) {
        return None;
    }
    // SAFETY: a text input response answers `userText` with a string.
    let text: Option<Retained<NSString>> = unsafe { msg_send![response, userText] };
    text.map(|text| text.to_string())
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

        // A click opens what the notification is about; an answer action hands
        // the question and the pick to the answer owner.
        #[unsafe(method(userNotificationCenter:didReceiveNotificationResponse:withCompletionHandler:))]
        fn did_receive(
            &self,
            _center: &UNUserNotificationCenter,
            response: &UNNotificationResponse,
            completion: &block2::DynBlock<dyn Fn()>,
        ) {
            let app = &self.ivars().app;
            let action = response.actionIdentifier();
            // SAFETY: a framework constant string.
            if action.isEqualToString(unsafe { UNNotificationDefaultActionIdentifier }) {
                match carried(response, OPENS_KEY) {
                    Some(link) => crate::receive_native_deep_link(app, [link]),
                    None => crate::focus_or_restore_main_window(app),
                }
            } else if let Some(question) =
                carried(response, QUESTION_KEY).and_then(|question| serde_json::from_str::<Question>(&question).ok())
            {
                if let Some(picked) = picked(&question, &action.to_string(), typed(response)) {
                    super::answer::submit(app, question, picked, carried(response, OPENS_KEY));
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
pub(super) mod tests {
    use super::*;

    fn labels() -> AnswerCatalog {
        AnswerCatalog {
            reply: "Reply…".into(),
            send: "Send".into(),
            placeholder: "Reply to this conversation".into(),
            not_sent: String::new(),
            not_delivered: String::new(),
            already_choice: String::new(),
            already_text: String::new(),
            stale: String::new(),
            gone: String::new(),
            unavailable: String::new(),
        }
    }

    pub(in crate::notifications) fn question() -> Question {
        Question {
            session_id: "ses_a".into(),
            message_id: "m1".into(),
            revision: "0".repeat(64),
            options: vec!["✅ Merge".into(), "Not yet".into()],
            epoch: 3,
            source: Some("avibe-app · Dock fix".into()),
        }
    }

    #[test]
    fn identifiers_keep_session_identity_and_do_not_replace_newer_deliveries() {
        let mut registry = NotificationRegistry::default();
        let first = registry.reserve(Some("ses_a".into())).unwrap();
        let next = registry.reserve(Some("ses_a".into())).unwrap();
        assert!(identifier(&first).starts_with("avibe:ses_a:"));
        assert_ne!(identifier(&first), identifier(&next));
    }

    #[test]
    fn an_action_picks_only_an_offered_option_or_nonempty_text() {
        let question = question();
        assert_eq!(
            picked(&question, "avibe.choice.0", None),
            Some(Picked::Choice("✅ Merge".into()))
        );
        assert_eq!(
            picked(&question, "avibe.choice.1", Some("ignored".into())),
            Some(Picked::Choice("Not yet".into()))
        );
        assert_eq!(picked(&question, "avibe.choice.2", None), None);
        assert_eq!(picked(&question, "avibe.choice.x", None), None);
        assert_eq!(
            picked(&question, "avibe.reply", Some("先补 changelog".into())),
            Some(Picked::Text("先补 changelog".into()))
        );
        assert_eq!(picked(&question, "avibe.reply", Some("  ".into())), None);
        assert_eq!(picked(&question, "avibe.reply", None), None);
        assert_eq!(
            picked(&question, "com.apple.UNNotificationDismissActionIdentifier", None),
            None
        );
    }

    #[test]
    fn a_question_round_trips_through_the_notification_it_rides_on() {
        let question = question();
        let carried = serde_json::to_string(&question).unwrap();
        assert_eq!(serde_json::from_str::<Question>(&carried).unwrap(), question);
    }

    #[test]
    fn one_category_per_option_set_and_wording() {
        let options = vec!["Yes".to_owned(), "No".to_owned()];
        let id = category_identifier(&options, &labels());
        assert!(id.starts_with("avibe.answer.") && id.len() == "avibe.answer.".len() + 24);
        assert_eq!(id, category_identifier(&options, &labels()));
        assert_ne!(id, category_identifier(&["No".to_owned(), "Yes".to_owned()], &labels()));
        assert_ne!(id, category_identifier(&["YesNo".to_owned()], &labels()));
        let mut chinese = labels();
        chinese.reply = "回复…".into();
        assert_ne!(id, category_identifier(&options, &chinese));
    }

    #[test]
    fn a_long_option_is_cut_to_a_readable_button() {
        assert_eq!(action_title("✅ Merge"), "✅ Merge");
        let long = action_title(&"选".repeat(80));
        assert_eq!(long.chars().count(), ACTION_TITLE_CHARS);
        assert!(long.ends_with('…'));
    }

    #[test]
    fn settings_report_the_decision_without_asking() {
        assert_eq!(
            authorization_label(UNAuthorizationStatus::NotDetermined),
            "not_determined"
        );
        assert_eq!(authorization_label(UNAuthorizationStatus::Authorized), "authorized");
        assert_eq!(authorization_label(UNAuthorizationStatus::Provisional), "authorized");
        assert_eq!(authorization_label(UNAuthorizationStatus::Denied), "denied");
        assert_eq!(
            settings_url("bot.avibe.desktop"),
            "x-apple.systempreferences:com.apple.Notifications-Settings.extension?id=bot.avibe.desktop"
        );
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
