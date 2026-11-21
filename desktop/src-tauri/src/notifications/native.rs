//! Exact-delivery identifiers at the OS boundary. No preference, copy, or
//! badge logic here. On macOS every post goes through the
//! `UNUserNotificationCenter` owner (`super::macos`); a development build
//! without a bundle identity, Windows sessionless posts and Linux keep the
//! plugin path.
use std::sync::{Arc, Mutex};

use avibe_runtime_host::notifications::{NotificationReceipt, NotificationRegistry, NotificationSnapshot};
use tauri::AppHandle;
use tauri_plugin_notification::NotificationExt;

use super::NotificationCopy;

fn plugin_notice(app: &AppHandle, copy: NotificationCopy) -> Result<(), String> {
    app.notification()
        .builder()
        .title(copy.title)
        .body(copy.body)
        .show()
        .map_err(|e| e.to_string())
}

/// A one-off notice nothing withdraws (Ask Vibey's, for example). On macOS a
/// click opens `opens`, an `avibe://` link, when there is one.
pub(crate) fn notice(app: &AppHandle, copy: NotificationCopy, opens: Option<String>) -> Result<(), String> {
    #[cfg(target_os = "macos")]
    if super::macos::available() {
        super::macos::notice(copy, opens);
        return Ok(());
    }
    let _ = opens;
    plugin_notice(app, copy)
}

pub(super) fn show(
    app: &AppHandle,
    receipt: &Arc<NotificationReceipt>,
    copy: NotificationCopy,
    registry: Arc<Mutex<NotificationRegistry>>,
) -> Result<(), String> {
    #[cfg(target_os = "macos")]
    if super::macos::available() {
        super::macos::show(receipt, copy, registry);
        return Ok(());
    }
    if receipt.session_id().is_none() {
        return plugin_notice(app, copy);
    }
    #[cfg(target_os = "macos")]
    {
        // A development build has no notification identity to withdraw by.
        let _ = registry;
        plugin_notice(app, copy)?;
    }
    #[cfg(windows)]
    {
        let _ = registry;
        windows::show(app, receipt, &copy).map_err(|e| e.to_string())?;
    }
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
                registry.lock().expect("notification registry lock").forget(id);
            })
            .show()
            .map_err(|e| e.to_string())?;
    }
    Ok(())
}

/// Fresh native/plugin posts are retried by the existing unread refresh using
/// the SAME exact key. Confirmed-absent native keys are eventually forgotten,
/// here or, on macOS, once the center has listed what it still shows.
pub(super) fn withdraw(
    app: &AppHandle,
    receipts: NotificationSnapshot,
    registry: Arc<Mutex<NotificationRegistry>>,
) -> Result<(), String> {
    #[cfg(target_os = "macos")]
    {
        let _ = app;
        if super::macos::available() {
            super::macos::withdraw(receipts, registry);
        }
        Ok(())
    }
    #[cfg(windows)]
    {
        let removed = windows::withdraw(app, receipts).map_err(|e| e.to_string())?;
        let mut registry = registry.lock().expect("notification registry lock");
        for id in removed {
            registry.forget(id);
        }
        Ok(())
    }
    #[cfg(all(unix, not(target_os = "macos")))]
    {
        let _ = registry;
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
        Ok(())
    }
}

// Native history distinguishes active posts from dismissed/rejected ones. A
// fresh absent key may still be queued; after the bounded delivery grace it
// must not become a permanent tombstone or an unbounded unread registry entry.
#[cfg(any(target_os = "macos", windows, test))]
pub(super) fn reconcile_posts(
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

// Windows limits Tag and Group to 16 characters. Full Session identity stays
// in the receipt and launch metadata; groups are NEVER removed.
#[cfg(any(windows, test))]
fn windows_labels(receipt: &NotificationReceipt) -> (String, String) {
    use sha2::{Digest, Sha256};

    let digest = Sha256::digest(receipt.session_id().unwrap_or_default().as_bytes());
    let group: String = digest[..8].iter().map(|byte| format!("{byte:02x}")).collect();
    (receipt.key()[..16].into(), group)
}

#[cfg(windows)]
mod windows {
    use ::windows::core::{Result, HSTRING};
    use ::windows::Data::Xml::Dom::XmlDocument;
    use ::windows::UI::Notifications::{ToastNotification, ToastNotificationManager};

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
        let (tag, group) = windows_labels(receipt);
        (HSTRING::from(tag), HSTRING::from(group))
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
}

#[cfg(test)]
mod lifecycle_tests {
    use std::collections::HashMap;
    use std::time::Duration;

    use super::*;

    #[test]
    fn windows_labels_are_short_exact_delivery_keys_not_session_wide_deletion_keys() {
        let mut registry = NotificationRegistry::default();
        let first = registry.reserve(Some("ses_a".into())).unwrap();
        let next = registry.reserve(Some("ses_a".into())).unwrap();
        let other = registry.reserve(Some("ses_b".into())).unwrap();
        let (first_tag, first_group) = windows_labels(&first);
        let (next_tag, next_group) = windows_labels(&next);
        let (_, other_group) = windows_labels(&other);
        assert_eq!(first_tag.len(), 16);
        assert_eq!(first_group.len(), 16);
        assert!(first_group.bytes().all(|byte| byte.is_ascii_hexdigit()));
        assert_ne!(first_tag, next_tag);
        assert_eq!(first_group, next_group);
        assert_ne!(first_group, other_group);
    }

    fn posted(registry: &mut NotificationRegistry, session: &str) -> Arc<NotificationReceipt> {
        let receipt = registry.reserve(Some(session.into())).unwrap();
        receipt.confirm_workbench_session(true);
        receipt.posted();
        registry.reconcile(vec![receipt.clone()], &HashMap::from([(session.into(), 1)]));
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
    fn notification_removal_reconciles_exact_retired_history_keys() {
        let mut registry = NotificationRegistry::default();
        let read = posted(&mut registry, "read");
        let unread = posted(&mut registry, "unread");
        let snapshot = registry.snapshot();
        registry.reconcile(snapshot, &HashMap::from([(String::from("unread"), 1)]));

        let (remove, forget) = reconcile_posts(registry.posted_snapshot(), |receipt| receipt.id() == read.id());

        assert_eq!(
            remove.iter().map(|receipt| receipt.id()).collect::<Vec<_>>(),
            [read.id()]
        );
        assert!(forget.is_empty());
        assert!(!unread.retired());
    }
}
