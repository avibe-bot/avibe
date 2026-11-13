//! Exact-delivery identifiers at the OS boundary. Sessionless notifications
//! stay on the existing plugin path. No preference, copy, or badge logic here.
use std::sync::{Arc, Mutex};

use avibe_runtime_host::notifications::{NotificationReceipt, NotificationRegistry, NotificationSnapshot};
use tauri::AppHandle;
use tauri_plugin_notification::NotificationExt;

use super::NotificationCopy;

pub(super) fn show(
    app: &AppHandle,
    receipt: &Arc<NotificationReceipt>,
    copy: NotificationCopy,
    _registry: Arc<Mutex<NotificationRegistry>>,
) -> Result<(), String> {
    if receipt.session_id().is_none() {
        return app
            .notification()
            .builder()
            .title(copy.title)
            .body(copy.body)
            .show()
            .map_err(|e| e.to_string());
    }
    #[cfg(target_os = "macos")]
    macos::show(app, receipt, &copy);
    #[cfg(windows)]
    windows::show(app, receipt, &copy).map_err(|e| e.to_string())?;
    #[cfg(all(unix, not(target_os = "macos")))]
    {
        // Tauri only indexes local ids for removal while an action listener
        // exists. on_close alone does not enable that tracker. The default
        // click action has no visible button; Avibe does not handle it here.
        static TRACKING: std::sync::Once = std::sync::Once::new();
        TRACKING.call_once(|| {
            let _ = app.notification().on_action(|_| {});
        });
        let id = receipt.id();
        app.notification()
            .builder()
            .id(id)
            .extra("session_id", receipt.session_id())
            .title(copy.title)
            .body(copy.body)
            .on_close(move || {
                _registry.lock().expect("notification registry lock").forget(id);
            })
            .show()
            .map_err(|e| e.to_string())?;
    }
    Ok(())
}

/// Fresh native/plugin posts are retried by the existing unread refresh using
/// the SAME exact key. Confirmed-absent native keys are eventually forgotten.
pub(super) fn withdraw(app: &AppHandle, receipts: NotificationSnapshot) -> Result<Vec<i32>, String> {
    #[cfg(target_os = "macos")]
    {
        let _ = app;
        Ok(macos::withdraw(receipts))
    }
    #[cfg(windows)]
    {
        windows::withdraw(app, receipts).map_err(|e| e.to_string())
    }
    #[cfg(all(unix, not(target_os = "macos")))]
    {
        app.notification()
            .remove_active(
                receipts
                    .iter()
                    .filter(|receipt| receipt.retired())
                    .map(|receipt| receipt.id())
                    .collect(),
            )
            .map_err(|e| e.to_string())?;
        // The plugin's on_close confirms removal and forgets the receipt.
        Ok(Vec::new())
    }
}

// Native history distinguishes active posts from dismissed/rejected ones. A
// fresh absent key may still be queued; after the bounded delivery grace it
// must not become a permanent tombstone or an unbounded unread registry entry.
#[cfg(any(target_os = "macos", windows, test))]
fn reconcile_posts(
    receipts: NotificationSnapshot,
    mut present: impl FnMut(&NotificationReceipt) -> bool,
) -> (NotificationSnapshot, Vec<i32>) {
    let mut remove = Vec::new();
    let mut forget = Vec::new();
    for receipt in receipts {
        if present(&receipt) {
            if receipt.retired() {
                remove.push(receipt);
            }
        } else if !receipt.awaiting_native_delivery() {
            forget.push(receipt.id());
        }
    }
    (remove, forget)
}

// Keep the plugin's existing legacy center and authorization behavior in 3.2.7.
// A modern UNUserNotificationCenter migration is a separate change.
#[cfg(target_os = "macos")]
#[allow(deprecated)]
mod macos {
    use objc2_foundation::{NSString, NSUserNotification, NSUserNotificationCenter};

    use super::*;

    fn identifier(receipt: &NotificationReceipt) -> String {
        format!("avibe:{}:{}", receipt.session_id().unwrap_or_default(), receipt.key())
    }

    pub(super) fn show(app: &AppHandle, receipt: &NotificationReceipt, copy: &NotificationCopy) {
        // Exactly the application identity setup the plugin's sender uses,
        // including its unbundled development-build fallback.
        let _ = notify_rust::set_application(if tauri::is_dev() {
            "com.apple.Terminal"
        } else {
            &app.config().identifier
        });
        let notification = NSUserNotification::new();
        notification.setIdentifier(Some(&NSString::from_str(&identifier(receipt))));
        notification.setTitle(Some(&NSString::from_str(&copy.title)));
        notification.setInformativeText(Some(&NSString::from_str(&copy.body)));
        notification.setHasActionButton(false);
        NSUserNotificationCenter::defaultUserNotificationCenter().deliverNotification(&notification);
    }

    pub(super) fn withdraw(receipts: NotificationSnapshot) -> Vec<i32> {
        let center = NSUserNotificationCenter::defaultUserNotificationCenter();
        let shown = center.deliveredNotifications();
        let (remove, mut forgotten) = reconcile_posts(receipts, |receipt| {
            shown.iter().any(|delivered| {
                delivered
                    .identifier()
                    .is_some_and(|key| identifier(receipt) == key.to_string())
            })
        });
        for receipt in remove {
            if let Some(delivered) = shown.iter().find(|delivered| {
                delivered
                    .identifier()
                    .is_some_and(|key| identifier(&receipt) == key.to_string())
            }) {
                center.removeDeliveredNotification(&delivered);
                forgotten.push(receipt.id());
            }
        }
        forgotten
    }

    #[test]
    fn identifiers_keep_session_identity_and_do_not_replace_newer_deliveries() {
        let mut registry = NotificationRegistry::default();
        let first = registry.reserve(Some("ses_a".into())).unwrap();
        let next = registry.reserve(Some("ses_a".into())).unwrap();
        assert!(identifier(&first).starts_with("avibe:ses_a:"));
        assert_ne!(identifier(&first), identifier(&next));
    }
}

#[cfg(windows)]
mod windows {
    use ::windows::core::{Result, HSTRING};
    use ::windows::Data::Xml::Dom::XmlDocument;
    use ::windows::UI::Notifications::{ToastNotification, ToastNotificationManager};
    use sha2::{Digest, Sha256};

    use super::*;

    fn app_id(app: &AppHandle) -> Result<HSTRING> {
        // Match the plugin: only the installed application has its own AUMID.
        let exe = tauri::utils::platform::current_exe().map_err(|_| ::windows::core::Error::from_thread())?;
        let directory = exe.parent().map(|p| p.to_string_lossy()).unwrap_or_default();
        Ok(
            if directory.ends_with("\\target\\debug") || directory.ends_with("\\target\\release") {
                HSTRING::from("{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe")
            } else {
                HSTRING::from(&app.config().identifier)
            },
        )
    }

    fn labels(receipt: &NotificationReceipt) -> (HSTRING, HSTRING) {
        // Windows limits Tag and Group to 16 characters. Full Session identity
        // stays in the receipt and launch metadata; groups are NEVER removed.
        let group = format!(
            "{:x}",
            Sha256::digest(receipt.session_id().unwrap_or_default().as_bytes())
        );
        (HSTRING::from(&receipt.key()[..16]), HSTRING::from(&group[..16]))
    }

    pub(super) fn show(app: &AppHandle, receipt: &NotificationReceipt, copy: &NotificationCopy) -> Result<()> {
        let xml = XmlDocument::new()?;
        // Match notify-rust's title/empty-subtitle/body, short lifetime and
        // silent default. Text nodes escape Runtime labels without XML markup.
        xml.LoadXml(&HSTRING::from("<toast duration=\"short\"><visual><binding template=\"ToastGeneric\"><text/><text/><text/></binding></visual><audio silent=\"true\"/></toast>"))?;
        let text = xml.GetElementsByTagName(&HSTRING::from("text"))?;
        text.Item(0)?
            .AppendChild(&xml.CreateTextNode(&HSTRING::from(&copy.title))?)?;
        text.Item(2)?
            .AppendChild(&xml.CreateTextNode(&HSTRING::from(&copy.body))?)?;
        xml.DocumentElement()?.SetAttribute(
            &HSTRING::from("launch"),
            &HSTRING::from(receipt.session_id().unwrap_or_default()),
        )?;
        let toast = ToastNotification::CreateToastNotification(&xml)?;
        let (tag, group) = labels(receipt);
        toast.SetTag(&tag)?;
        toast.SetGroup(&group)?;
        ToastNotificationManager::CreateToastNotifierWithId(&app_id(app)?)?.Show(&toast)
    }

    pub(super) fn withdraw(app: &AppHandle, receipts: NotificationSnapshot) -> Result<Vec<i32>> {
        let app_id = app_id(app)?;
        let history = ToastNotificationManager::History()?;
        let shown = history.GetHistoryWithId(&app_id)?;
        let labels_in_history = shown
            .into_iter()
            .map(|toast| Ok((toast.Tag()?, toast.Group()?)))
            .collect::<Result<Vec<_>>>()?;
        let (remove, mut forgotten) = reconcile_posts(receipts, |receipt| labels_in_history.contains(&labels(receipt)));
        for receipt in remove {
            let (tag, group) = labels(&receipt);
            history.RemoveGroupedTagWithId(&tag, &group, &app_id)?;
            forgotten.push(receipt.id());
        }
        Ok(forgotten)
    }

    #[test]
    fn labels_are_short_exact_delivery_keys_not_session_wide_deletion_keys() {
        let mut registry = NotificationRegistry::default();
        let first = registry.reserve(Some("ses_a".into())).unwrap();
        let next = registry.reserve(Some("ses_a".into())).unwrap();
        let other = registry.reserve(Some("ses_b".into())).unwrap();
        let (first_tag, first_group) = labels(&first);
        let (next_tag, next_group) = labels(&next);
        let (_, other_group) = labels(&other);
        assert_eq!(first_tag.len(), 16);
        assert_eq!(first_group.len(), 16);
        assert_ne!(first_tag, next_tag);
        assert_eq!(first_group, next_group);
        assert_ne!(first_group, other_group);
    }
}

#[cfg(test)]
mod lifecycle_tests {
    use std::collections::HashMap;
    use std::time::Duration;

    use super::*;

    fn posted(registry: &mut NotificationRegistry, session: &str) -> Arc<NotificationReceipt> {
        let receipt = registry.reserve(Some(session.into())).unwrap();
        receipt.confirm_workbench_session();
        receipt.posted();
        receipt
    }

    #[tokio::test(start_paused = true)]
    async fn history_reconciliation_bounds_absent_posts_without_removing_newer_or_unread_posts() {
        let mut registry = NotificationRegistry::default();
        let read = posted(&mut registry, "a");
        let dismissed_unread = posted(&mut registry, "b");
        let active_unread = posted(&mut registry, "c");
        let snapshot = registry.snapshot();
        registry.reconcile(snapshot, &HashMap::from([("b".into(), 1), ("c".into(), 1)]));
        let (remove, forget) =
            reconcile_posts(registry.posted_snapshot(), |receipt| receipt.id() == active_unread.id());
        assert!(remove.is_empty()); // the read post may still be in the OS queue
        assert!(forget.is_empty());
        tokio::time::advance(Duration::from_secs(30)).await;
        let newer = posted(&mut registry, "a");
        let (remove, forget) = reconcile_posts(registry.posted_snapshot(), |receipt| {
            receipt.id() == active_unread.id() || receipt.id() == newer.id()
        });
        assert!(remove.is_empty()); // neither live newer nor unread is withdrawn
        assert_eq!(forget, [read.id(), dismissed_unread.id()]);
        for id in forget {
            registry.forget(id);
        }
        assert_eq!(registry.posted_snapshot().len(), 2);
    }

    #[test]
    fn present_history_removes_only_retired_exact_keys() {
        let mut registry = NotificationRegistry::default();
        let read = posted(&mut registry, "a");
        let snapshot = registry.snapshot();
        registry.reconcile(snapshot, &HashMap::new());
        let newer = posted(&mut registry, "a");
        let (remove, forget) = reconcile_posts(registry.posted_snapshot(), |_| true);
        assert_eq!(remove.iter().map(|r| r.id()).collect::<Vec<_>>(), [read.id()]);
        assert!(!newer.retired());
        assert!(forget.is_empty());
    }
}
