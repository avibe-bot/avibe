use super::{
    choices::Mode,
    shell::{ConnectionMenu, MENU_AUTOMATIC, MENU_DIRECT, MENU_TUNNEL},
};
use tauri::menu::{Menu, MenuItemKind, Submenu};

/// The submenu is the Devices page's own strings, by key, and its items
/// are check items in the frame's order (ui/design:
/// desktop/connection-choice/menu-*). The third, "direct and relay only",
/// is in the submenu only while the instance on show could be routed.
#[test]
fn the_connection_submenu_holds_the_choices_in_the_devices_pages_words() {
    let app = tauri::test::mock_app();
    let menu = ConnectionMenu::build(app.handle())
        .expect("menu")
        .expect("the product catalog has the title and the first two choices");
    let text = |key: &str| crate::product_text(&format!("devices.connection.{key}"));
    assert_eq!(Some(menu.submenu.text().expect("title")), text("title"));
    let items = |menu: &ConnectionMenu<tauri::test::MockRuntime>| -> Vec<(String, String)> {
        menu.submenu
            .items()
            .expect("items")
            .into_iter()
            .map(|item| match item {
                MenuItemKind::Check(check) => (check.id().as_ref().to_owned(), check.text().expect("text")),
                _ => panic!("every choice is a check item"),
            })
            .collect()
    };
    let two = vec![
        (MENU_AUTOMATIC.to_owned(), text("automatic").expect("catalog string")),
        (MENU_TUNNEL.to_owned(), text("tunnelOnly").expect("catalog string")),
    ];
    let mut three = two.clone();
    three.extend(text("directOnly").map(|text| (MENU_DIRECT.to_owned(), text)));
    // As built, for this computer's own instance: the two.
    assert_eq!(items(&menu), two);
    assert_eq!(menu.direct.is_some(), three.len() == 3);

    let state = |menu: &ConnectionMenu<tauri::test::MockRuntime>| {
        (
            menu.submenu.is_enabled().expect("enabled"),
            menu.automatic.is_checked().expect("automatic"),
            menu.tunnel.is_checked().expect("tunnel"),
            menu.direct
                .as_ref()
                .is_some_and(|direct| direct.is_checked().expect("direct")),
        )
    };
    // Greyed until an instance of another computer is on show.
    assert_eq!(state(&menu), (false, true, false, false));
    // The tick is the choice of the instance on show, whatever was clicked;
    // the third item comes and goes with whether that instance could be
    // routed, once each way however often it is shown.
    let has_direct = menu.direct.is_some();
    for (chosen, expected, listed) in [
        (Some((Mode::Tunnel, true)), (true, false, true, false), &three),
        (Some((Mode::Direct, true)), (true, false, false, has_direct), &three),
        (Some((Mode::Direct, true)), (true, false, false, has_direct), &three),
        (Some((Mode::Automatic, false)), (true, true, false, false), &two),
        (Some((Mode::Tunnel, false)), (true, false, true, false), &two),
        (Some((Mode::Automatic, true)), (true, true, false, false), &three),
        (None, (false, true, false, false), &two),
    ] {
        // The system ticks a clicked item itself.
        menu.automatic.set_checked(true).expect("tick");
        menu.tunnel.set_checked(true).expect("tick");
        if let Some(direct) = &menu.direct {
            direct.set_checked(true).expect("tick");
        }
        menu.show(chosen);
        assert_eq!(state(&menu), expected, "{chosen:?}");
        assert_eq!(&items(&menu), listed, "{chosen:?}");
    }
}

/// In the tray menu and the application submenu alike, the submenu comes
/// right after "Switch instance" and before "Sign out"; without it (a system
/// that cannot route) the account's items are as they were.
#[test]
fn the_connection_submenu_follows_switch_instance_in_both_menus() {
    let app = tauri::test::mock_app();
    let handle = app.handle();
    let ids = |items: Vec<MenuItemKind<tauri::test::MockRuntime>>| -> Vec<String> {
        items.iter().map(|item| item.id().as_ref().to_owned()).collect()
    };
    let account = [
        "desktop-client-account",
        "desktop-client-instances",
        "desktop-client-sign-out",
    ];

    let connection = ConnectionMenu::build(handle).expect("menu").expect("strings");
    let tray = Menu::new(handle).expect("tray");
    let application = Submenu::new(handle, "Avibe", true).expect("application");
    crate::append_account_items(handle, &tray, Some(&application), Some(&connection.submenu)).expect("items");
    let expected = [account[0], account[1], "direct-connection", account[2]];
    assert_eq!(ids(tray.items().expect("tray items")), expected);
    assert_eq!(ids(application.items().expect("application items")), expected);

    let tray = Menu::new(handle).expect("tray");
    let application = Submenu::new(handle, "Avibe", true).expect("application");
    crate::append_account_items(handle, &tray, Some(&application), None).expect("items");
    assert_eq!(ids(tray.items().expect("tray items")), account);
    assert_eq!(ids(application.items().expect("application items")), account);
}
