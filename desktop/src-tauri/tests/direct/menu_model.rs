//! The 「连接方式」 submenu as every system builds it (ui/design:
//! desktop/connection-choice/menu-*): its words, its ticks and its place,
//! from the same functions the real menus are built and set from. A real
//! menu can only be built on the main thread on macOS, which no test runs
//! on; `menu.rs` builds one on Windows.
use super::{chosen, ConnectionRows, ConnectionState, MENU_AUTOMATIC, MENU_TUNNEL};
use crate::direct::{
    choices::{Choices, Mode},
    wiring::{Published, Routing, Target, Wiring},
};
use crate::{account_items, AccountItem};

const ONE: &str = "https://one-app.avibe.bot";
const TWO: &str = "https://two-app.avibe.bot";

fn shown(account: &str, origin: &str) -> Published {
    Published {
        target: Target {
            account: account.to_owned(),
            origin: origin.to_owned(),
            instance: format!("instance-of-{origin}"),
        },
        runtime: "main-runtime-1".to_owned(),
        document: true,
    }
}

#[test]
fn the_submenu_is_the_devices_pages_title_and_its_two_choices_in_order() {
    let rows = ConnectionRows::read().expect("the product catalog has the three strings");
    let text = |key: &str| crate::product_text(&format!("devices.connection.{key}")).expect("catalog string");
    assert_eq!(rows.title, text("title"));
    assert_eq!(
        rows.choices,
        [
            (MENU_AUTOMATIC, Mode::Automatic, text("automatic")),
            (MENU_TUNNEL, Mode::Tunnel, text("tunnelOnly")),
        ]
    );
    // Three different strings, none of them empty.
    assert!(![&rows.title, &rows.choices[0].2, &rows.choices[1].2]
        .iter()
        .any(|text| text.is_empty()));
    assert_ne!(rows.choices[0].2, rows.choices[1].2);
}

#[test]
fn the_tick_is_the_choice_of_the_instance_on_show_and_this_computers_own_is_greyed() {
    let mut wiring = Wiring::new(Routing::Live, false, Choices::unsaved());
    wiring.choose("account-1", ONE, Mode::Tunnel);
    let state = |shown: Option<&Published>| ConnectionState::of(chosen(shown, &wiring));
    let usable = |ticked| ConnectionState { enabled: true, ticked };

    // The instance whose choice was made, and only that one of that account.
    assert_eq!(state(Some(&shown("account-1", ONE))), usable(Mode::Tunnel));
    assert_eq!(state(Some(&shown("account-1", TWO))), usable(Mode::Automatic));
    assert_eq!(state(Some(&shown("account-2", ONE))), usable(Mode::Automatic));
    // This computer's own instance has no choice of path: greyed, and as a
    // new menu is built.
    let greyed = ConnectionState {
        enabled: false,
        ticked: Mode::Automatic,
    };
    assert_eq!(state(None), greyed);
    assert_eq!(ConnectionState::of(None), greyed);

    // Chosen back: the tick follows, for that instance alone.
    wiring.choose("account-1", TWO, Mode::Tunnel);
    wiring.choose("account-1", ONE, Mode::Automatic);
    let state = |shown: Option<&Published>| ConnectionState::of(chosen(shown, &wiring));
    assert_eq!(state(Some(&shown("account-1", ONE))), usable(Mode::Automatic));
    assert_eq!(state(Some(&shown("account-1", TWO))), usable(Mode::Tunnel));
}

#[test]
fn the_submenu_comes_right_after_switch_instance() {
    let copy = |id, key| AccountItem::Copy { id, key };
    let account = || copy("desktop-client-account", "accountService");
    let instances = || copy("desktop-client-instances", "switchInstance");
    let sign_out = || copy("desktop-client-sign-out", "signOut");
    assert_eq!(
        account_items(true),
        [account(), instances(), AccountItem::Connection, sign_out()]
    );
    // Without it (a system that cannot route) the account's items are as
    // they were.
    assert_eq!(account_items(false), [account(), instances(), sign_out()]);
}
