//! The 「连接方式」 submenu as every system builds it (ui/design:
//! desktop/connection-choice/menu-*): its words, its ticks, whether its
//! third choice is offered, and its place, from the same functions the real
//! menus are built and set from. A real
//! menu can only be built on the main thread on macOS, which no test runs
//! on; `menu.rs` builds one on Windows.
use super::{chosen, ConnectionRows, ConnectionState, MENU_AUTOMATIC, MENU_DIRECT, MENU_TUNNEL};
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

/// The first two choices are always there. The third, "direct and relay
/// only", is there when the product catalog has its string, and then it is
/// the Devices page's own, last.
#[test]
fn the_submenu_is_the_devices_pages_title_and_its_choices_in_order() {
    let rows = ConnectionRows::read().expect("the product catalog has the title and the first two choices");
    let text = |key: &str| crate::product_text(&format!("devices.connection.{key}"));
    assert_eq!(Some(&rows.title), text("title").as_ref());
    let mut expected = vec![
        (
            MENU_AUTOMATIC,
            Mode::Automatic,
            text("automatic").expect("catalog string"),
        ),
        (MENU_TUNNEL, Mode::Tunnel, text("tunnelOnly").expect("catalog string")),
    ];
    expected.extend(text("directOnly").map(|text| (MENU_DIRECT, Mode::Direct, text)));
    assert_eq!(rows.choices, expected);
    // Different strings, none of them empty.
    let mut words: Vec<&String> = rows.choices.iter().map(|choice| &choice.2).collect();
    words.push(&rows.title);
    assert!(!words.iter().any(|text| text.is_empty()));
    let unique: std::collections::BTreeSet<&&String> = words.iter().collect();
    assert_eq!(unique.len(), words.len());
}

#[test]
fn the_tick_is_the_choice_of_the_instance_on_show_and_this_computers_own_is_greyed() {
    let mut wiring = Wiring::new(Routing::Live, false, Choices::unsaved());
    wiring.choose("account-1", ONE, Mode::Tunnel);
    let state = |shown: Option<&Published>| ConnectionState::of(chosen(shown, &wiring));
    let usable = |ticked| ConnectionState {
        enabled: true,
        ticked,
        direct: true,
    };

    // The instance whose choice was made, and only that one of that account.
    assert_eq!(state(Some(&shown("account-1", ONE))), usable(Mode::Tunnel));
    assert_eq!(state(Some(&shown("account-1", TWO))), usable(Mode::Automatic));
    assert_eq!(state(Some(&shown("account-2", ONE))), usable(Mode::Automatic));
    // This computer's own instance has no choice of path: greyed, as a new
    // menu is built, and without the third choice.
    let greyed = ConnectionState {
        enabled: false,
        ticked: Mode::Automatic,
        direct: false,
    };
    assert_eq!(state(None), greyed);
    assert_eq!(ConnectionState::of(None), greyed);

    // Chosen back: the tick follows, for that instance alone.
    wiring.choose("account-1", TWO, Mode::Direct);
    wiring.choose("account-1", ONE, Mode::Automatic);
    let state = |shown: Option<&Published>| ConnectionState::of(chosen(shown, &wiring));
    assert_eq!(state(Some(&shown("account-1", ONE))), usable(Mode::Automatic));
    assert_eq!(state(Some(&shown("account-1", TWO))), usable(Mode::Direct));
}

/// "Direct and relay only" is offered only where a path that is not the
/// tunnel is possible: this system and configuration can route, and the
/// instance's address is one the direct path serves. Elsewhere the item is
/// absent, not greyed; the two other choices stay.
#[test]
fn the_third_choice_is_offered_only_for_an_instance_that_could_be_routed() {
    let custom = "https://avibe.example.com";
    let offered = |wiring: &Wiring, origin: &str| {
        let state = ConnectionState::of(chosen(Some(&shown("account-1", origin)), wiring));
        assert!(state.enabled);
        state.direct
    };
    let live = Wiring::new(Routing::Live, false, Choices::unsaved());
    assert!(offered(&live, ONE));
    assert!(!offered(&live, custom));
    assert!(offered(
        &Wiring::new(Routing::AtCreation, false, Choices::unsaved()),
        ONE
    ));
    // A configured proxy, or a system that cannot route one window's host.
    assert!(!offered(&Wiring::new(Routing::Live, true, Choices::unsaved()), ONE));
    assert!(!offered(
        &Wiring::new(Routing::Unsupported, false, Choices::unsaved()),
        ONE
    ));
    // Whatever the choice is now, also when it is the third one.
    let mut chosen_direct = Wiring::new(Routing::Live, false, Choices::unsaved());
    chosen_direct.choose("account-1", ONE, Mode::Direct);
    assert!(offered(&chosen_direct, ONE));
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
