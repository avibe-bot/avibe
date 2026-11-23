//! Real native checkmarks; preferences are in the peer tempdir. No OS login or CU worker.
use super::*;
use serde_json::{json, Value};
pub(super) struct Login(pub bool);
struct Items {
    mini: CheckMenuItem<tauri::Wry>,
}

pub(super) fn run(app: &AppHandle, path: &std::path::Path, click: Option<&str>) -> tauri::Result<Value> {
    if app.try_state::<NativeMenus>().is_none() {
        app.manage(Login(true));
        app.manage(computer_use::Controller::e2e_menu());
        let (mini, _) = pet::tray_items(app)?;
        app.manage(Items { mini });
        let plain = |id: &str| MenuItem::with_id(app, id, id, true, None::<&str>);
        let check = |id: &str, enabled| CheckMenuItem::with_id(app, id, id, true, enabled, None::<&str>);
        app.manage(NativeMenus {
            tray: Menu::new(app)?,
            application: None,
            status: plain("fixture-status")?,
            stop: plain(STOP_MENU_ID)?,
            settings: plain(SETTINGS_MENU_ID)?,
            login: check(LOGIN_MENU_ID, true)?,
            notifications: check(
                notifications::MENU_ID,
                app.state::<notifications::Notifications>().enabled(),
            )?,
            auxiliary_note: None,
            auxiliary_visible: AtomicBool::new(false),
            computer_use: check(
                COMPUTER_USE_MENU_ID,
                computer_use_menu_state(&app.state::<computer_use::Controller>().view()).1,
            )?,
            stop_present: AtomicBool::new(false),
            displayed: Mutex::new(None),
        });
    }
    let prerelease = updater::e2e_menu(app, path);
    let direct = direct::shell::app_lock_menu::run(app, click.filter(|id| id.starts_with("direct-connection-")))?;
    let menus = app.state::<NativeMenus>();
    let mini = app.state::<Items>();
    let updater = app.state::<updater::Updater>();
    let items = [
        (LOGIN_MENU_ID, &menus.login, app.state::<Login>().0),
        (
            notifications::MENU_ID,
            &menus.notifications,
            app.state::<notifications::Notifications>().enabled(),
        ),
        (pet::MENU_ID, &mini.mini, app.state::<pet::Pet>().enabled()),
        (
            COMPUTER_USE_MENU_ID,
            &menus.computer_use,
            computer_use_menu_state(&app.state::<computer_use::Controller>().view()).1,
        ),
        (updater::CHANNEL_ID, &updater.channel_menu, prerelease),
    ];
    if let Some(id) = click.filter(|id| !id.starts_with("direct-connection-")) {
        let (_, item, _) = items.iter().find(|(key, _, _)| *key == id).expect("known checkmark");
        item.set_checked(!item.is_checked()?)?;
        handle_menu(app, id);
    }
    let mut result = serde_json::Map::new();
    for (id, item, saved) in items {
        result.insert(id.into(), json!({"saved": saved, "checked": item.is_checked()?}));
    }
    result.insert(
        "direct-connection-automatic".into(),
        json!({"saved": direct["saved"] == "automatic", "checked": direct["automatic"]}),
    );
    result.insert(
        "direct-connection-tunnel".into(),
        json!({"saved": direct["saved"] == "tunnel", "checked": direct["tunnel"]}),
    );
    let files: std::collections::BTreeMap<String, Vec<u8>> = std::fs::read_dir(path)
        .unwrap()
        .filter_map(|entry| {
            let entry = entry.ok()?;
            entry.file_type().ok()?.is_file().then_some(())?;
            Some((
                entry.file_name().to_string_lossy().into_owned(),
                std::fs::read(entry.path()).ok()?,
            ))
        })
        .collect();
    Ok(json!({"items": result, "files": files}))
}
