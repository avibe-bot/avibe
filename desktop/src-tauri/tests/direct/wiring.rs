use super::{
    choices::{Choices, Mode},
    shell::{remote_instance_permission, window_arguments, WRY_DEFAULT_ARGUMENTS},
    wiring::{Effect, Material, Published, Routing, Target, Wiring},
};
use std::time::Duration;
use tauri::webview::{PermissionKind, PermissionResponse};
use tokio::time::Instant;

const ACCOUNT: &str = "account-1";
const ONE: &str = "https://one-app.avibe.bot";
const TWO: &str = "https://two-app.avibe.bot";

fn target(origin: &str) -> Target {
    Target {
        account: ACCOUNT.to_owned(),
        origin: origin.to_owned(),
        instance: format!("instance-of-{origin}"),
    }
}

fn shown(origin: &str, runtime: &str) -> Published {
    Published {
        target: target(origin),
        runtime: runtime.to_owned(),
        document: true,
    }
}

/// The same view while the instance's document is not in it.
fn away(origin: &str, runtime: &str) -> Published {
    Published {
        document: false,
        ..shown(origin, runtime)
    }
}

fn material(port: u16) -> Material {
    Material {
        proxy: format!("127.0.0.1:{port}").parse().unwrap(),
        username: "avibe".to_owned(),
        password: "listener-credential".to_owned(),
    }
}

fn install(session: u64, window: &str) -> Effect {
    Effect::Install {
        session,
        window: window.to_owned(),
    }
}

fn open(runtime: &str) -> Effect {
    Effect::Open {
        runtime: runtime.to_owned(),
    }
}

fn retire(session: u64, detach: &[&str]) -> Effect {
    Effect::Retire {
        session,
        detach: detach.iter().map(|window| (*window).to_owned()).collect(),
    }
}

fn live() -> Wiring {
    Wiring::new(Routing::Live, false, Choices::unsaved())
}

/// A live session for the instance view `runtime` of `origin`, as the shell
/// starts one: told to open it, then given its client.
fn opened(wiring: &mut Wiring, origin: &str, runtime: &str, now: Instant) -> u64 {
    let published = shown(origin, runtime);
    assert_eq!(wiring.reconcile(Some(&published), |_| true, now), [open(runtime)]);
    wiring.open(published.target, material(4100), Some(runtime), now)
}

#[test]
fn every_window_of_one_instance_shares_one_session_and_is_routed_once_it_answers() {
    let now = Instant::now();
    let mut wiring = live();
    let session = opened(&mut wiring, ONE, "main-runtime-1", now);
    assert_eq!(wiring.asks_through(session), Some("main-runtime-1"));
    assert_eq!(wiring.origin(session), Some(ONE));
    let (host, route) = wiring.route(session).expect("a route");
    assert_eq!((host, route.proxy.port()), ("one-app.avibe.bot", 4100));

    // A Show window and an Editor window opened from that view share it.
    assert_eq!(wiring.shared(ACCOUNT, ONE), Some(session));
    assert_eq!(wiring.window_created(session, "show-1"), []);
    assert_eq!(wiring.shared(ACCOUNT, ONE), Some(session));
    assert_eq!(wiring.window_created(session, "editor-1"), []);
    // Another instance, or another account's view of this one, does not.
    assert_eq!(wiring.shared(ACCOUNT, TWO), None);
    assert_eq!(wiring.shared("account-2", ONE), None);

    // No route before the first answer; with it, one on every window.
    assert_eq!(wiring.probed(session, false), []);
    assert_eq!(
        wiring.probed(session, true),
        [
            install(session, "main-runtime-1"),
            install(session, "show-1"),
            install(session, "editor-1")
        ]
    );
    // A window opened later is routed as it is made, and none twice.
    assert_eq!(wiring.window_created(session, "show-2"), [install(session, "show-2")]);
    assert_eq!(wiring.window_created(session, "show-2"), []);
    assert_eq!(wiring.probed(session, true), []);
    // Losing the direct link later leaves the route: the listener serves the tunnel path.
    assert_eq!(wiring.probed(session, false), []);
    // The same view, still shown: nothing more to do.
    assert_eq!(wiring.reconcile(Some(&shown(ONE, "main-runtime-1")), |_| true, now), []);
    assert!(wiring.busy());
}

#[test]
fn a_session_ends_with_its_instance_view() {
    let now = Instant::now();
    // Sign-out, or back to this computer: no instance view is published.
    let mut wiring = live();
    let session = opened(&mut wiring, ONE, "main-runtime-1", now);
    wiring.window_created(session, "show-1");
    wiring.probed(session, true);
    // The Show window closed first: only windows still open are detached.
    assert_eq!(
        wiring.reconcile(None, |window| window != "show-1", now),
        [retire(session, &["main-runtime-1"])]
    );
    assert!(!wiring.busy());
    assert_eq!(wiring.route(session).map(|_| ()), None);
    assert_eq!(wiring.asks_through(session), None);
    assert_eq!(wiring.shared(ACCOUNT, ONE), None);

    // The last window closed: nothing is left to detach.
    let mut wiring = live();
    let session = opened(&mut wiring, ONE, "main-runtime-1", now);
    wiring.probed(session, true);
    assert_eq!(wiring.reconcile(None, |_| false, now), [retire(session, &[])]);

    // A switch to another instance: the old session ends, a new one starts.
    let mut wiring = live();
    let first = opened(&mut wiring, ONE, "main-runtime-1", now);
    wiring.probed(first, true);
    assert_eq!(
        wiring.reconcile(
            Some(&shown(TWO, "main-runtime-2")),
            |window| window == "main-runtime-2",
            now
        ),
        [retire(first, &[]), open("main-runtime-2")]
    );
    let second = wiring.open(target(TWO), material(4200), Some("main-runtime-2"), now);
    assert_ne!(first, second);
    assert_eq!(wiring.shared(ACCOUNT, ONE), None);
    assert_eq!(wiring.shared(ACCOUNT, TWO), Some(second));

    // Signing in to the same instance again makes a new view, and a new session.
    assert_eq!(
        wiring.reconcile(Some(&shown(TWO, "main-runtime-3")), |_| true, now),
        [retire(second, &[]), open("main-runtime-3")]
    );

    // Another account at the same origin and view label is another session.
    let mut wiring = live();
    let session = opened(&mut wiring, ONE, "main-runtime-1", now);
    let mut other = shown(ONE, "main-runtime-1");
    other.target.account = "account-2".to_owned();
    assert_eq!(
        wiring.reconcile(Some(&other), |_| true, now),
        [retire(session, &[]), open("main-runtime-1")]
    );
}

#[test]
fn the_choice_is_per_account_and_instance_and_ends_or_starts_only_that_session() {
    let now = Instant::now();
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("direct-paths.json");
    let mut wiring = Wiring::new(Routing::Live, false, Choices::load(path.clone()));
    assert_eq!(wiring.mode(ACCOUNT, ONE), Mode::Automatic);
    let session = opened(&mut wiring, ONE, "main-runtime-1", now);
    wiring.window_created(session, "show-1");
    wiring.probed(session, true);

    // Another instance's choice touches nothing here.
    assert_eq!(wiring.choose(ACCOUNT, TWO, Mode::Tunnel), []);
    assert_eq!(wiring.choose("account-2", ONE, Mode::Tunnel), []);
    assert!(wiring.busy());

    // "Cloudflare tunnel only" for this one: its route comes off every
    // window and its client goes, at once.
    assert_eq!(
        wiring.choose(ACCOUNT, ONE, Mode::Tunnel),
        [retire(session, &["main-runtime-1", "show-1"])]
    );
    assert_eq!(wiring.mode(ACCOUNT, ONE), Mode::Tunnel);
    assert!(!wiring.busy());
    // No session while it stays chosen, whatever is shown.
    let published = shown(ONE, "main-runtime-1");
    assert_eq!(wiring.reconcile(Some(&published), |_| true, now), []);
    assert_eq!(wiring.window_created(session, "show-2"), []);
    assert_eq!(wiring.shared(ACCOUNT, ONE), None);

    // It is kept on this computer, by account and origin, and nothing else is.
    let reloaded = Choices::load(path.clone());
    assert_eq!(reloaded.mode(ACCOUNT, ONE), Mode::Tunnel);
    assert_eq!(reloaded.mode(ACCOUNT, TWO), Mode::Tunnel);
    assert_eq!(reloaded.mode("account-3", ONE), Mode::Automatic);
    let kept = std::fs::read_to_string(&path).unwrap();
    assert!(kept.contains(&format!("{ACCOUNT} {ONE}")), "{kept}");
    for absent in ["instance-of-", "listener-credential", "127.0.0.1", "4100"] {
        assert!(!kept.contains(absent), "{absent} in {kept}");
    }

    // Back to automatic: nothing ends, and the shown instance gets a session again.
    assert_eq!(wiring.choose(ACCOUNT, ONE, Mode::Automatic), []);
    assert_eq!(
        wiring.reconcile(Some(&published), |_| true, now),
        [open("main-runtime-1")]
    );
    // An automatic choice is not kept as an entry.
    assert_eq!(wiring.choose(ACCOUNT, TWO, Mode::Automatic), []);
    let kept = std::fs::read_to_string(&path).unwrap();
    assert!(!kept.contains(&format!("{ACCOUNT} {ONE}\":")), "{kept}");
    assert!(!kept.contains(&format!("{ACCOUNT} {TWO}\":")), "{kept}");
    assert!(kept.contains(&format!("account-2 {ONE}")), "{kept}");
}

#[test]
fn where_a_route_is_given_only_at_creation_a_first_open_only_asks() {
    let now = Instant::now();
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("direct-paths.json");
    let mut wiring = Wiring::new(Routing::AtCreation, false, Choices::load(path.clone()));

    assert!(wiring.routes_at_creation() && !live().routes_at_creation());
    // Never answered before: the instance view is created without a route.
    assert!(!wiring.starts_at_creation(&target(ONE)));
    // Once it is shown, a session only asks whether the instance answers.
    let published = shown(ONE, "main-runtime-1");
    assert_eq!(
        wiring.reconcile(Some(&published), |_| true, now),
        [open("main-runtime-1")]
    );
    let scout = wiring.open(target(ONE), material(4100), Some("main-runtime-1"), now);
    assert_eq!(wiring.asks_through(scout), Some("main-runtime-1"));
    // It routes nothing: no window shares it, none is answered for it.
    assert_eq!(wiring.shared(ACCOUNT, ONE), None);
    assert_eq!(wiring.window_created(scout, "show-1"), []);
    assert_eq!(wiring.reconcile(Some(&published), |_| true, now), []);
    assert_eq!(wiring.probed(scout, false), []);
    assert!(!wiring.starts_at_creation(&target(ONE)));
    // The first answer is remembered, and the scout ends.
    assert_eq!(wiring.probed(scout, true), [retire(scout, &[])]);
    assert!(!wiring.busy());
    // The windows that are open stay as they are: no second scout.
    assert_eq!(wiring.reconcile(Some(&published), |_| true, now), []);

    // What is remembered: the account and the origin, nothing more.
    let kept = std::fs::read_to_string(&path).unwrap();
    assert!(kept.contains(&format!("{ACCOUNT} {ONE}")), "{kept}");
    for absent in ["instance-of-", "listener-credential", "127.0.0.1", "4100"] {
        assert!(!kept.contains(absent), "{absent} in {kept}");
    }

    // From then on, also after a restart, the next instance view of that
    // account and instance is created with a route.
    let mut wiring = Wiring::new(Routing::AtCreation, false, Choices::load(path.clone()));
    assert!(wiring.starts_at_creation(&target(ONE)));
    assert!(!wiring.starts_at_creation(&target(TWO)));
    let mut other_account = target(ONE);
    other_account.account = "account-2".to_owned();
    assert!(!wiring.starts_at_creation(&other_account));

    let session = wiring.open(target(ONE), material(4300), None, now);
    // Its window does not exist yet: nothing is asked through it.
    assert_eq!(wiring.asks_through(session), None);
    assert_eq!(wiring.shared(ACCOUNT, ONE), None);
    assert_eq!(
        wiring.window_created(session, "main-runtime-2"),
        [Effect::Answer {
            session,
            window: "main-runtime-2".to_owned()
        }]
    );
    // Until its window is the published instance view, it waits.
    assert_eq!(wiring.reconcile(None, |_| true, now), []);
    assert_eq!(wiring.asks_through(session), None);
    // Its window became the published instance view.
    let published = shown(ONE, "main-runtime-2");
    assert_eq!(wiring.reconcile(Some(&published), |_| true, now), []);
    assert_eq!(wiring.asks_through(session), Some("main-runtime-2"));
    assert_eq!(wiring.probed(session, true), []);
    // An app window opened from it shares the session and is answered too.
    assert_eq!(wiring.shared(ACCOUNT, ONE), Some(session));
    assert_eq!(
        wiring.window_created(session, "show-1"),
        [Effect::Answer {
            session,
            window: "show-1".to_owned()
        }]
    );

    // The listener ends for good: the client goes, and the open windows are
    // carried by their route's own fallback. Nothing is rebuilt under them.
    assert_eq!(
        wiring.listener_ended(session, now),
        [retire(session, &["main-runtime-2", "show-1"])]
    );
    assert_eq!(
        wiring.reconcile(Some(&published), |_| true, now + Duration::from_secs(600)),
        []
    );
    // The next window made for the instance gets a route again.
    assert!(wiring.starts_at_creation(&target(ONE)));

    // "Cloudflare tunnel only" stops that too.
    assert_eq!(wiring.choose(ACCOUNT, ONE, Mode::Tunnel), []);
    assert!(!wiring.starts_at_creation(&target(ONE)));
}

#[test]
fn a_session_whose_window_never_becomes_the_instance_view_ends() {
    let now = Instant::now();
    let mut choices = Choices::unsaved();
    choices.remember(ACCOUNT, ONE);
    let mut wiring = Wiring::new(Routing::AtCreation, false, choices);
    let session = wiring.open(target(ONE), material(4100), None, now);
    wiring.window_created(session, "main-runtime-2");
    let waiting = |wiring: &mut Wiring, after: u64| wiring.reconcile(None, |_| true, now + Duration::from_secs(after));
    assert_eq!(waiting(&mut wiring, 59), []);
    assert_eq!(waiting(&mut wiring, 60), [retire(session, &["main-runtime-2"])]);
    assert!(!wiring.busy());

    // Another view was published instead: the same.
    let session = wiring.open(target(ONE), material(4100), None, now);
    wiring.window_created(session, "main-runtime-3");
    assert_eq!(
        wiring.reconcile(
            Some(&shown(ONE, "main-runtime-4")),
            |_| true,
            now + Duration::from_secs(60)
        ),
        [retire(session, &["main-runtime-3"])]
    );
}

#[test]
fn a_configured_proxy_means_no_session_and_no_route() {
    let now = Instant::now();
    let published = shown(ONE, "main-runtime-1");
    let mut wiring = Wiring::new(Routing::Live, true, Choices::unsaved());
    assert_eq!(wiring.reconcile(Some(&published), |_| true, now), []);
    assert!(!wiring.busy());

    // Also where routes are given at creation, for an instance that answered before.
    let mut choices = Choices::unsaved();
    choices.remember(ACCOUNT, ONE);
    let mut wiring = Wiring::new(Routing::AtCreation, true, choices);
    assert!(!wiring.starts_at_creation(&target(ONE)));
    assert_eq!(wiring.reconcile(Some(&published), |_| true, now), []);
    // And for one that never did: not even asked.
    assert_eq!(wiring.reconcile(Some(&shown(TWO, "main-runtime-1")), |_| true, now), []);
}

#[test]
fn system_proxy_settings_that_rule_the_route_out_mean_no_session_and_no_route() {
    let now = Instant::now();
    let published = shown(ONE, "main-runtime-1");
    let mut wiring = live();
    wiring.system_proxy(true);
    assert_eq!(wiring.reconcile(Some(&published), |_| true, now), []);
    // They are read again at every look: once they allow it, a session starts.
    wiring.system_proxy(false);
    let session = opened(&mut wiring, ONE, "main-runtime-1", now);
    wiring.window_created(session, "show-1");
    wiring.probed(session, true);
    // A proxy configured while a session runs ends it, route first.
    wiring.system_proxy(true);
    assert_eq!(
        wiring.reconcile(Some(&published), |_| true, now),
        [retire(session, &["main-runtime-1", "show-1"])]
    );
    assert_eq!(wiring.reconcile(Some(&published), |_| true, now), []);

    // Where routes are given at creation: no route for an instance that
    // answered before, and no asking for one that never did.
    let mut choices = Choices::unsaved();
    choices.remember(ACCOUNT, ONE);
    let mut wiring = Wiring::new(Routing::AtCreation, false, choices);
    assert!(wiring.starts_at_creation(&target(ONE)));
    wiring.system_proxy(true);
    assert!(!wiring.starts_at_creation(&target(ONE)));
    assert_eq!(wiring.reconcile(Some(&shown(TWO, "main-runtime-1")), |_| true, now), []);
    wiring.system_proxy(false);
    assert_eq!(
        wiring.reconcile(Some(&shown(TWO, "main-runtime-1")), |_| true, now),
        [open("main-runtime-1")]
    );
}

#[test]
fn before_macos_14_there_is_no_session_and_no_route() {
    assert_eq!(Routing::on_macos(13), Routing::Unsupported);
    assert_eq!(Routing::on_macos(14), Routing::Live);
    assert_eq!(Routing::on_macos(26), Routing::Live);
    let now = Instant::now();
    let mut wiring = Wiring::new(Routing::on_macos(13), false, Choices::unsaved());
    assert_eq!(wiring.reconcile(Some(&shown(ONE, "main-runtime-1")), |_| true, now), []);
    assert!(!wiring.starts_at_creation(&target(ONE)));
    assert!(!wiring.busy());
}

#[test]
fn a_listener_that_ended_takes_its_route_off_and_is_not_replaced_at_once() {
    let now = Instant::now();
    let mut wiring = live();
    let session = opened(&mut wiring, ONE, "main-runtime-1", now);
    wiring.window_created(session, "show-1");
    wiring.probed(session, true);
    assert_eq!(
        wiring.listener_ended(session, now),
        [retire(session, &["main-runtime-1", "show-1"])]
    );
    // Ending twice, or an answer that arrives afterwards, does nothing.
    assert_eq!(wiring.listener_ended(session, now), []);
    assert_eq!(wiring.probed(session, true), []);
    let published = shown(ONE, "main-runtime-1");
    let later = |seconds: u64| now + Duration::from_secs(seconds);
    assert_eq!(wiring.reconcile(Some(&published), |_| true, later(59)), []);
    assert_eq!(
        wiring.reconcile(Some(&published), |_| true, later(60)),
        [open("main-runtime-1")]
    );

    // The same pause when no client could be started for the view at all.
    let mut wiring = live();
    assert_eq!(
        wiring.reconcile(Some(&published), |_| true, now),
        [open("main-runtime-1")]
    );
    wiring.declined("main-runtime-1", now);
    assert_eq!(wiring.reconcile(Some(&published), |_| true, later(59)), []);
    assert_eq!(
        wiring.reconcile(Some(&published), |_| true, later(60)),
        [open("main-runtime-1")]
    );
    // Another view is not held back by it.
    wiring.declined("main-runtime-1", later(60));
    assert_eq!(
        wiring.reconcile(Some(&shown(ONE, "main-runtime-2")), |_| true, later(61)),
        [open("main-runtime-2")]
    );
}

#[test]
fn an_instance_the_direct_path_cannot_name_gets_no_session() {
    let now = Instant::now();
    let mut wiring = live();
    for (account, origin, instance) in [
        ("", ONE, "one"),
        (ACCOUNT, ONE, ""),
        (ACCOUNT, "http://one-app.avibe.bot", "one"),
        (ACCOUNT, "https://", "one"),
        (ACCOUNT, "", "one"),
    ] {
        let published = Published {
            target: Target {
                account: account.to_owned(),
                origin: origin.to_owned(),
                instance: instance.to_owned(),
            },
            runtime: "main-runtime-1".to_owned(),
            document: true,
        };
        assert_eq!(
            wiring.reconcile(Some(&published), |_| true, now),
            [],
            "{account:?} {origin:?} {instance:?}"
        );
    }
}

#[test]
fn choices_read_what_they_can_and_treat_the_rest_as_automatic() {
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("direct-paths.json");
    // A later build's mode, an entry of another shape, and one this build reads.
    std::fs::write(
        &path,
        format!(
            r#"{{"version":1,"choices":{{"{ACCOUNT} {ONE}":{{"mode":"relay","reverted":null}},"{ACCOUNT} {TWO}":7,"account-2 {ONE}":{{"mode":"tunnel","reverted":{{"at":1,"reason":"unreachable"}}}}}},"reached":["{ACCOUNT} {TWO}"]}}"#
        ),
    )
    .unwrap();
    let choices = Choices::load(path.clone());
    assert_eq!(choices.mode(ACCOUNT, ONE), Mode::Automatic);
    assert_eq!(choices.mode(ACCOUNT, TWO), Mode::Automatic);
    assert_eq!(choices.mode("account-2", ONE), Mode::Tunnel);
    assert!(choices.reached(ACCOUNT, TWO));
    assert!(!choices.reached(ACCOUNT, ONE));

    // Another version, or no JSON at all: an empty file, not an error.
    for content in [
        r#"{"version":2,"choices":{},"reached":["account-1 https://one-app.avibe.bot"]}"#,
        "not json",
        "",
    ] {
        std::fs::write(&path, content).unwrap();
        let choices = Choices::load(path.clone());
        assert_eq!(choices.mode(ACCOUNT, ONE), Mode::Automatic);
        assert!(!choices.reached(ACCOUNT, ONE), "{content}");
    }

    // With nowhere to keep them, a choice still holds for this run.
    let mut choices = Choices::unsaved();
    choices.set(ACCOUNT, ONE, Mode::Tunnel);
    choices.remember(ACCOUNT, TWO);
    assert_eq!(choices.mode(ACCOUNT, ONE), Mode::Tunnel);
    assert!(choices.reached(ACCOUNT, TWO));

    // A directory that does not exist yet is made on the first save.
    let nested = directory.path().join("made").join("direct-paths.json");
    let mut choices = Choices::load(nested.clone());
    choices.set(ACCOUNT, ONE, Mode::Tunnel);
    assert_eq!(Choices::load(nested).mode(ACCOUNT, ONE), Mode::Tunnel);
}

/// A Windows window of another computer's instance refuses notifications,
/// so its page never registers a push worker; every other permission gets
/// no answer of the shell's, and is asked as it always was.
#[test]
fn a_remote_instance_window_refuses_notifications_and_nothing_else() {
    assert!(matches!(
        remote_instance_permission(PermissionKind::Notifications),
        PermissionResponse::Deny
    ));
    for kind in [
        PermissionKind::Microphone,
        PermissionKind::Camera,
        PermissionKind::Geolocation,
        PermissionKind::ClipboardRead,
        PermissionKind::DisplayCapture,
        PermissionKind::Midi,
        PermissionKind::Sensors,
    ] {
        assert!(
            matches!(remote_instance_permission(kind), PermissionResponse::Default),
            "{kind:?}"
        );
    }
}

#[test]
fn a_view_between_two_documents_keeps_its_session_for_a_while() {
    let now = Instant::now();
    let later = |seconds: u64| now + Duration::from_secs(seconds);
    let mut wiring = live();
    // No session starts for a view that does not hold the instance's document yet.
    assert_eq!(wiring.reconcile(Some(&away(ONE, "main-runtime-1")), |_| true, now), []);
    let session = opened(&mut wiring, ONE, "main-runtime-1", now);
    wiring.window_created(session, "show-1");
    wiring.probed(session, true);

    // The view reloads: the session and its routes stay, and nothing is
    // asked through a document that is not the instance's.
    assert_eq!(
        wiring.reconcile(Some(&away(ONE, "main-runtime-1")), |_| true, later(10)),
        []
    );
    assert_eq!(wiring.asks_through(session), None);
    assert_eq!(wiring.shared(ACCOUNT, ONE), Some(session));
    assert_eq!(
        wiring.reconcile(Some(&away(ONE, "main-runtime-1")), |_| true, later(39)),
        []
    );
    // The document is back: asking goes on, and a later absence is counted anew.
    assert_eq!(
        wiring.reconcile(Some(&shown(ONE, "main-runtime-1")), |_| true, later(40)),
        []
    );
    assert_eq!(wiring.asks_through(session), Some("main-runtime-1"));
    assert_eq!(
        wiring.reconcile(Some(&away(ONE, "main-runtime-1")), |_| true, later(100)),
        []
    );
    assert_eq!(
        wiring.reconcile(Some(&away(ONE, "main-runtime-1")), |_| true, later(129)),
        []
    );
    // Half a minute without the instance's document ends it, route first.
    assert_eq!(
        wiring.reconcile(Some(&away(ONE, "main-runtime-1")), |_| true, later(130)),
        [retire(session, &["main-runtime-1", "show-1"])]
    );

    // What really ends a session does not wait: sign-out, a switch, and the
    // choice, each while the view is between documents.
    for ending in 0..3 {
        let mut wiring = live();
        let session = opened(&mut wiring, ONE, "main-runtime-1", now);
        assert_eq!(
            wiring.reconcile(Some(&away(ONE, "main-runtime-1")), |_| true, later(1)),
            []
        );
        let effects = match ending {
            0 => wiring.reconcile(None, |_| true, later(2)),
            1 => wiring.reconcile(Some(&away(TWO, "main-runtime-2")), |_| true, later(2)),
            _ => wiring.choose(ACCOUNT, ONE, Mode::Tunnel),
        };
        assert_eq!(effects, [retire(session, &[])], "{ending}");
    }
}

/// A routed window gets back every argument `wry` gives an unrouted one, so
/// the two differ in the route alone. The constant is `wry` 0.57.0's own
/// composition (`src/webview2/mod.rs`: its `--disable-features` line, and
/// `--autoplay-policy=no-user-gesture-required` because its `autoplay` is on
/// by default and Tauri leaves it on). A change of the `wry` version fails
/// here until that file has been read again.
#[test]
fn a_routed_window_keeps_the_arguments_wry_gives_every_window() {
    assert_eq!(
        WRY_DEFAULT_ARGUMENTS,
        "--disable-features=msWebOOUI,msPdfOOUI,msSmartScreenProtection --autoplay-policy=no-user-gesture-required"
    );
    assert_eq!(
        window_arguments("--proxy-pac-url=data:x"),
        format!("{WRY_DEFAULT_ARGUMENTS} --proxy-pac-url=data:x")
    );
    let lock = include_str!(concat!(env!("CARGO_MANIFEST_DIR"), "/../Cargo.lock"));
    let wry = lock
        .split("[[package]]")
        .find(|package| package.contains("\nname = \"wry\"\n"))
        .expect("wry is a dependency");
    assert!(
        wry.contains("\nversion = \"0.57.0\"\n"),
        "wry changed: read its default browser arguments again (src/webview2/mod.rs) and update WRY_DEFAULT_ARGUMENTS"
    );
}
