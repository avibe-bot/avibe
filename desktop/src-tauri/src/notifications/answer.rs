//! The owner of one answer given in a macOS notification. The answer reaches
//! the Runtime only from the current local Workbench document, with the user's
//! own authority, through the fenced receiver `__AVIBE_DESKTOP_ANSWER__`
//! (ui/src/desktop/desktopAnswerReceiver.ts, contract version 1,
//! docs/plans/2026-11-21-desktop-d2a-notification-actions.md). The Runtime
//! owns idempotency by operation ID, so a document replaced mid-answer is asked
//! again with the same ID and the Runtime answers `duplicate`.
//!
//! Nothing is retried after the Runtime answered, and nothing claims success it
//! did not see: an answer that cannot be delivered within the deadline, or
//! whose instance changed, is reported as not delivered.
use std::time::{Duration, Instant};

use serde::Deserialize;
use tauri::{AppHandle, Manager, Webview};

use super::macos::{Picked, Question};
use super::{AnswerCatalog, NotificationCopy};

const DEADLINE: Duration = Duration::from_secs(30);
const POLL: Duration = Duration::from_millis(300);
const WAIT: Duration = Duration::from_millis(500);

/// The receiver's settled answer.
#[derive(Clone, Debug, Default, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub(super) struct Settled {
    outcome: String,
    #[serde(default)]
    original_outcome: Option<String>,
    #[serde(default)]
    answer_kind: Option<String>,
    #[serde(default)]
    choice: Option<String>,
}

/// What the user is told about one answer: nothing when it went through,
/// else why it was not sent or not delivered.
#[derive(Debug, PartialEq, Eq)]
pub(super) enum Told {
    Nothing,
    NotSent(String),
    NotDelivered(String),
}

pub(super) fn told(settled: Option<&Settled>, labels: &AnswerCatalog) -> Told {
    let Some(settled) = settled else {
        return Told::NotDelivered(labels.unavailable.clone());
    };
    // A replay reports what the original attempt became. Without an original
    // outcome (still pending, or retired without one) nothing proves it went
    // through, so it is not reported as sent.
    let outcome = match (settled.outcome.as_str(), settled.original_outcome.as_deref()) {
        ("duplicate", None) => "unavailable",
        ("duplicate", Some(original)) => original,
        (outcome, _) => outcome,
    };
    match outcome {
        "accepted" => Told::Nothing,
        "already_handled" => Told::NotSent(match (settled.answer_kind.as_deref(), settled.choice.as_deref()) {
            (Some("choice"), Some(choice)) => labels.already_choice.replace("{{choice}}", choice),
            _ => labels.already_text.clone(),
        }),
        "stale" => Told::NotSent(labels.stale.clone()),
        "forbidden" | "not_found" => Told::NotSent(labels.gone.clone()),
        _ => Told::NotDelivered(labels.unavailable.clone()),
    }
}

/// An answer whose fate is not known yet: asked again, with the same
/// operation ID, until the deadline.
fn uncertain(settled: &Settled) -> bool {
    settled.outcome == "unavailable" || (settled.outcome == "duplicate" && settled.original_outcome.is_none())
}

/// The receiver request for one pick, as the document validates it.
fn request(id: &str, question: &Question, picked: &Picked) -> String {
    let (kind, text) = match picked {
        Picked::Choice(text) => ("choice", text),
        Picked::Text(text) => ("text", text),
    };
    serde_json::json!({
        "id": id,
        "sessionId": question.session_id,
        "messageId": question.message_id,
        "revision": question.revision,
        "kind": kind,
        "text": text,
    })
    .to_string()
}

/// One operation ID per click, unique across launches.
pub(super) fn operation_id() -> String {
    static NEXT: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);
    let started = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|elapsed| elapsed.as_nanos())
        .unwrap_or_default();
    let sequence = NEXT.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
    format!("desktop-answer-{started}-{}-{sequence}", std::process::id())
}

/// The receiver's document identity, read only from the top document at the
/// local Runtime `origin`; empty when there is no receiver yet.
fn identify(origin: &str) -> String {
    let origin = serde_json::to_string(origin).expect("origin");
    format!(
        "(()=>{{if(window.self!==window.top||location.origin!=={origin})return '';\
const c=window.__AVIBE_DESKTOP_ANSWER__;return c&&c.version===1&&typeof c.document==='string'?c.document:'';}})()"
    )
}

/// Runs `call` against the receiver only in the top document at `origin`
/// whose receiver is still `document`: `"stale"` for any other document,
/// `"missing"` without a receiver, else the call's own string answer.
fn fenced(document: &str, origin: &str, call: &str) -> String {
    let document = serde_json::to_string(document).expect("document");
    let origin = serde_json::to_string(origin).expect("origin");
    format!(
        "(()=>{{if(window.self!==window.top||location.origin!=={origin})return 'stale';\
const c=window.__AVIBE_DESKTOP_ANSWER__;if(!c||c.version!==1)return 'missing';if(c.document!=={document})return 'stale';\
return String({call});}})()"
    )
}

/// The current Workbench document of the local instance. A remote instance's
/// document never receives an answer; the local Runtime refuses a question it
/// does not hold.
fn current(app: &AppHandle) -> Option<(Webview, String)> {
    if !crate::instance_client::local_selected(app) {
        return None;
    }
    let (view, origin) = crate::compose::current(app)?;
    let local = crate::compose::local_origin(&url::Url::parse(&origin).ok()?)?;
    (local == origin).then_some((view, origin))
}

async fn deliver(app: &AppHandle, id: &str, request: &str) -> Option<Settled> {
    let deadline = Instant::now() + DEADLINE;
    let status = format!("c.status({})", serde_json::to_string(id).expect("id"));
    let mut last = None;
    while Instant::now() < deadline {
        if let Some((view, origin)) = current(app) {
            let document = crate::compose::eval(&view, identify(&origin)).await.unwrap_or_default();
            if !document.is_empty() {
                let begun =
                    crate::compose::eval(&view, fenced(&document, &origin, &format!("c.begin({request})"))).await;
                match begun.as_deref() {
                    Some("invalid") => return None,
                    Some("started") => {
                        while Instant::now() < deadline {
                            match crate::compose::eval(&view, fenced(&document, &origin, &status))
                                .await
                                .as_deref()
                            {
                                Some("pending") => tokio::time::sleep(POLL).await,
                                // A replaced or reloaded document is asked again with the same ID.
                                None | Some("stale" | "missing" | "unknown") => break,
                                Some(settled) => {
                                    let Ok(settled) = serde_json::from_str::<Settled>(settled) else {
                                        return None;
                                    };
                                    if !uncertain(&settled) {
                                        return Some(settled);
                                    }
                                    last = Some(settled);
                                    break;
                                }
                            }
                        }
                    }
                    _ => {}
                }
            }
        }
        tokio::time::sleep(WAIT).await;
    }
    last
}

/// Delivers one pick and tells the user only when it did not go through.
pub(super) fn submit(app: &AppHandle, id: String, question: Question, picked: Picked, opens: Option<String>) {
    let app = app.clone();
    tauri::async_runtime::spawn(async move {
        let settled = deliver(&app, &id, &request(&id, &question, &picked)).await;
        let catalog = crate::native_catalog_for_locales(sys_locale::get_locales()).notifications;
        let (subtitle, body) = match told(settled.as_ref(), &catalog.answer) {
            Told::Nothing => return,
            Told::NotSent(reason) => (catalog.answer.not_sent.clone(), reason),
            Told::NotDelivered(reason) => (catalog.answer.not_delivered.clone(), reason),
        };
        let copy = NotificationCopy {
            title: question.source.clone().unwrap_or_else(|| "Avibe".to_owned()),
            subtitle: Some(subtitle),
            body,
        };
        let handle = app.clone();
        let _ = app.run_on_main_thread(move || {
            if handle
                .try_state::<super::Notifications>()
                .is_some_and(|notifications| notifications.enabled())
            {
                super::macos::notice(copy, opens);
            }
        });
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    fn labels() -> AnswerCatalog {
        AnswerCatalog {
            reply: String::new(),
            send: String::new(),
            placeholder: String::new(),
            not_sent: "Not sent".into(),
            not_delivered: "Not delivered".into(),
            already_choice: "Already answered \"{{choice}}\" on another device.".into(),
            already_text: "Already answered on another device.".into(),
            stale: "stale".into(),
            gone: "gone".into(),
            unavailable: "unavailable".into(),
        }
    }

    fn settled(outcome: &str) -> Settled {
        Settled {
            outcome: outcome.into(),
            ..Settled::default()
        }
    }

    #[test]
    fn only_an_answer_that_went_through_is_silent() {
        let labels = labels();
        assert_eq!(told(Some(&settled("accepted")), &labels), Told::Nothing);
        // A replay whose original has no outcome proves nothing was sent.
        assert_eq!(
            told(Some(&settled("duplicate")), &labels),
            Told::NotDelivered("unavailable".into())
        );
        assert!(uncertain(&settled("duplicate")) && uncertain(&settled("unavailable")));
        assert!(!uncertain(&settled("accepted")) && !uncertain(&settled("stale")));
        let replayed = |original: &str| Settled {
            original_outcome: Some(original.into()),
            ..settled("duplicate")
        };
        assert_eq!(told(Some(&replayed("accepted")), &labels), Told::Nothing);
        assert_eq!(told(Some(&replayed("stale")), &labels), Told::NotSent("stale".into()));
        assert_eq!(told(Some(&settled("stale")), &labels), Told::NotSent("stale".into()));
        assert_eq!(told(Some(&settled("forbidden")), &labels), Told::NotSent("gone".into()));
        assert_eq!(told(Some(&settled("not_found")), &labels), Told::NotSent("gone".into()));
        assert_eq!(
            told(Some(&settled("unavailable")), &labels),
            Told::NotDelivered("unavailable".into())
        );
        assert_eq!(
            told(Some(&settled("anything")), &labels),
            Told::NotDelivered("unavailable".into())
        );
        assert_eq!(told(None, &labels), Told::NotDelivered("unavailable".into()));
    }

    #[test]
    fn another_device_quotes_its_choice_but_never_typed_text() {
        let labels = labels();
        let choice = Settled {
            answer_kind: Some("choice".into()),
            choice: Some("✅ Merge".into()),
            ..settled("already_handled")
        };
        assert_eq!(
            told(Some(&choice), &labels),
            Told::NotSent("Already answered \"✅ Merge\" on another device.".into())
        );
        let text = Settled {
            answer_kind: Some("text".into()),
            choice: None,
            ..settled("already_handled")
        };
        assert_eq!(
            told(Some(&text), &labels),
            Told::NotSent("Already answered on another device.".into())
        );
    }

    #[test]
    fn the_receiver_request_names_the_question_and_the_pick() {
        let question = super::super::macos::tests::question();
        let choice: serde_json::Value =
            serde_json::from_str(&request("op", &question, &Picked::Choice("✅ Merge".into()))).unwrap();
        assert_eq!(
            choice,
            serde_json::json!({
                "id": "op",
                "sessionId": "ses_a",
                "messageId": "m1",
                "revision": "0".repeat(64),
                "kind": "choice",
                "text": "✅ Merge",
            })
        );
        let text: serde_json::Value =
            serde_json::from_str(&request("op", &question, &Picked::Text("先补 \"changelog\"".into()))).unwrap();
        assert_eq!(text["kind"], "text");
        assert_eq!(text["text"], "先补 \"changelog\"");
    }

    #[test]
    fn every_call_is_fenced_to_one_top_document_of_the_local_runtime_and_receiver() {
        let script = fenced("doc-1", "http://127.0.0.1:5123", "c.status(\"op\")");
        assert!(
            script.contains(r#"location.origin!=="http://127.0.0.1:5123""#),
            "{script}"
        );
        assert!(script.contains("window.self!==window.top"));
        assert!(script.contains("__AVIBE_DESKTOP_ANSWER__"));
        assert!(script.contains(r#"c.document!=="doc-1""#));
        assert!(script.ends_with("return String(c.status(\"op\"));})()"));
        let identify = identify("http://127.0.0.1:5123");
        assert!(identify.contains("__AVIBE_DESKTOP_ANSWER__") && identify.contains("c.version===1"));
    }

    #[test]
    fn operation_ids_are_distinct_and_within_the_runtime_bound() {
        let first = operation_id();
        let second = operation_id();
        assert_ne!(first, second);
        assert!(first.len() <= 128);
    }
}
