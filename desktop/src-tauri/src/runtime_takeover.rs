use std::sync::{atomic::Ordering, Arc};

use avibe_runtime_host::{BootstrapNoticeCode, BootstrapStatus, RuntimeHost, StatusSink};
use serde::Deserialize;
use tauri::{AppHandle, Manager};
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons, MessageDialogResult};

pub const MENU_ID: &str = "manage-local-runtime";
pub const OPEN_URL: &str = "avibe://runtime";

#[derive(Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Catalog {
    pub menu: String,
    title: String,
    message: String,
    manage: String,
    independent: String,
    later: String,
    select_home: String,
    home_message: String,
    busy: String,
    supervised: String,
    unavailable: String,
    managed: String,
}

fn catalog() -> Catalog {
    super::native_catalog_for_locales(sys_locale::get_locales()).takeover
}

pub async fn resolve(
    app: &AppHandle,
    host: &Arc<RuntimeHost>,
    mut status: BootstrapStatus,
    sink: &dyn StatusSink,
) -> BootstrapStatus {
    if status.notice.code == BootstrapNoticeCode::DataHomeRequired {
        let app = app.clone();
        let selected = tauri::async_runtime::spawn_blocking(move || {
            let c = catalog();
            if !app
                .dialog()
                .message(c.home_message)
                .title(c.title)
                .buttons(MessageDialogButtons::OkCancelCustom(c.select_home.clone(), c.later))
                .blocking_show()
            {
                return None;
            }
            app.dialog()
                .file()
                .set_title(c.select_home)
                .blocking_pick_folder()?
                .into_path()
                .ok()
        })
        .await
        .ok()
        .flatten();
        match selected {
            Some(home) if host.select_home(&home).is_ok() => status = host.bootstrap(sink).await,
            _ => return status,
        }
    }
    if status.notice.code != BootstrapNoticeCode::TakeoverRequired {
        return status;
    }
    let external = host
        .external_runtime()
        .unwrap_or_else(|| avibe_runtime_host::takeover::ExternalRuntime {
            home: status.origin.clone(),
            service: None,
            ui: None,
            reason: Some("identity_unknown".into()),
        });
    let app = app.clone();
    let choice = tauri::async_runtime::spawn_blocking(move || {
        let c = catalog();
        let message = match external.reason.as_deref() {
            None => c.message,
            Some("busy") => c.busy,
            Some("supervised") => c.supervised,
            Some(_) => c.unavailable,
        }
        .replace("{{home}}", &external.home);
        let buttons = if external.reason.is_none() {
            MessageDialogButtons::YesNoCancelCustom(c.manage.clone(), c.independent.clone(), c.later)
        } else {
            MessageDialogButtons::OkCancelCustom(c.independent.clone(), c.later)
        };
        match app
            .dialog()
            .message(message)
            .title(c.title)
            .buttons(buttons)
            .blocking_show_with_result()
        {
            MessageDialogResult::Custom(value) if value == c.manage && external.reason.is_none() => Some(true),
            MessageDialogResult::Custom(value) if value == c.independent => Some(false),
            _ => None,
        }
    })
    .await
    .ok()
    .flatten();
    match choice {
        Some(take_over) => match host.choose_management(take_over).await {
            Ok(()) => host.bootstrap(sink).await,
            Err(_) => {
                let mut failed = status;
                failed.notice.code = BootstrapNoticeCode::TakeoverFailed;
                sink.publish(failed.clone());
                failed
            }
        },
        None => status,
    }
}

/// Reopen the management decision without allowing the Workbench to stop a
/// service through IPC. The subsequent native dialog owns confirmation.
pub fn request(app: AppHandle) {
    let host = app.state::<super::Shell>().host.clone();
    let activity = app.state::<super::Shell>().activity.clone();
    let previous = activity.load(Ordering::SeqCst);
    if !matches!(previous, super::ACTIVITY_IDLE | super::ACTIVITY_MONITOR)
        || activity
            .compare_exchange(previous, super::ACTIVITY_BOOTSTRAP, Ordering::SeqCst, Ordering::SeqCst)
            .is_err()
    {
        return;
    }
    if host.has_owned_runtime() {
        activity.store(previous, Ordering::SeqCst);
        let c = catalog();
        app.dialog().message(c.managed).title(c.title).show(|_| {});
        return;
    }
    if host.request_management().is_ok() && super::return_to_bootstrap(&app) {
        super::spawn_owned_bootstrap(app);
    } else {
        activity.store(previous, Ordering::SeqCst);
    }
}

/// Only native boolean state crosses this boundary; no path, version text, or
/// page-supplied value can become executable JavaScript.
pub fn management_script(managed: bool) -> &'static str {
    if managed {
        "window.__AVIBE_DESKTOP_MANAGED_CONNECTION__ = true; window.dispatchEvent(new Event('avibe:runtime-management'));"
    } else {
        "window.__AVIBE_DESKTOP_MANAGED_CONNECTION__ = false; window.dispatchEvent(new Event('avibe:runtime-management'));"
    }
}
