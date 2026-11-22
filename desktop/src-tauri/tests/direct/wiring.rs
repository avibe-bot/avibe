use super::{
    choices::{Choices, Mode},
    shell::{remote_instance_permission, window_arguments, WRY_DEFAULT_ARGUMENTS},
    wiring::{Effect, Material, Pace, Published, Routing, Target, Wiring},
    RefreshOutcome,
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
    // Nothing in the app will change, so the shell has to keep looking.
    assert!(wiring.awaits(Some(&published)));
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
        // A custom domain, and a look-alike of a managed address.
        (ACCOUNT, "https://avibe.example.com", "one"),
        (ACCOUNT, "https://one-app.avibe.bot.example.com", "one"),
        (ACCOUNT, "https://one-app.avibe.bot:8443", "one"),
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
        // And nothing to keep looking for.
        assert!(!wiring.awaits(Some(&published)), "{account:?} {origin:?} {instance:?}");
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

    assert!(wiring.selects(session, "main-runtime-1"));
    assert!(!wiring.selects(session, "main-runtime-2") && !wiring.selects(session + 1, "main-runtime-1"));
    // The view reloads: the session and its routes stay, and nothing is
    // asked through a document that is not the instance's. It is still the
    // selection: an answer that was on its way is not "the selection changed".
    assert_eq!(
        wiring.reconcile(Some(&away(ONE, "main-runtime-1")), |_| true, later(10)),
        []
    );
    assert_eq!(wiring.asks_through(session), None);
    assert!(wiring.selects(session, "main-runtime-1"));
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
    // A session that ended selects nothing.
    assert!(!wiring.selects(session, "main-runtime-1"));

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
    // By lines: a Windows checkout ends them differently.
    let lock = include_str!(concat!(env!("CARGO_MANIFEST_DIR"), "/../Cargo.lock"));
    let mut lines = lock.lines().skip_while(|line| line.trim() != r#"name = "wry""#);
    assert_eq!(
        lines.next().map(str::trim),
        Some(r#"name = "wry""#),
        "wry is a dependency"
    );
    assert_eq!(
        lines.next().map(str::trim),
        Some(r#"version = "0.57.0""#),
        "wry changed: read its default browser arguments again (src/webview2/mod.rs) and update WRY_DEFAULT_ARGUMENTS"
    );
}

/// The shell looks again by itself only while a session could still start
/// for the view on show with nothing changing in the app.
#[test]
fn the_shell_keeps_looking_only_while_a_session_could_still_start() {
    let now = Instant::now();
    let published = shown(ONE, "main-runtime-1");
    let mut wiring = live();
    assert!(!wiring.awaits(None));
    // Before its session exists, and while the system's proxy settings
    // rule it out (they can clear without the app hearing of it).
    assert!(wiring.awaits(Some(&published)));
    wiring.system_proxy(true);
    assert!(wiring.awaits(Some(&published)));
    wiring.system_proxy(false);
    // Not once the view has its session.
    let session = opened(&mut wiring, ONE, "main-runtime-1", now);
    assert!(!wiring.awaits(Some(&published)));
    // Not while the choice forbids it: choosing again is an event of the app.
    assert_eq!(wiring.choose(ACCOUNT, ONE, Mode::Tunnel), [retire(session, &[])]);
    assert!(!wiring.awaits(Some(&published)));
    // Not where nothing can ever be routed.
    assert!(!Wiring::new(Routing::Unsupported, false, Choices::unsaved()).awaits(Some(&published)));
    assert!(!Wiring::new(Routing::Live, true, Choices::unsaved()).awaits(Some(&published)));

    // Where routes are given at creation: only until the instance has
    // answered once. After that the next window is what starts a session.
    let mut wiring = Wiring::new(Routing::AtCreation, false, Choices::unsaved());
    assert!(wiring.awaits(Some(&published)));
    let scout = wiring.open(target(ONE), material(4100), Some("main-runtime-1"), now);
    assert!(!wiring.awaits(Some(&published)));
    assert_eq!(wiring.probed(scout, true), [retire(scout, &[])]);
    assert!(!wiring.awaits(Some(&published)));
}

/// A fresh answer is renewed at four of its five minutes. No link: a ladder
/// that ends at four times an hour. A link kept through an unanswered
/// rendezvous: asked about again within the core's bound, so it does not
/// run out between two rare tries, and never in a spin.
#[test]
fn the_next_rendezvous_comes_before_a_kept_admission_runs_out() {
    let now = Instant::now();
    let after = |pace: &mut Pace, outcome: RefreshOutcome| pace.next(&outcome, now).duration_since(now).as_millis();
    let retained = |milliseconds: u64| RefreshOutcome::Retained {
        retry_before: Duration::from_millis(milliseconds),
    };

    // An instance that never answers: 5 s, 15 s, 1 min, 5 min, then every 15 min.
    let mut pace = Pace::default();
    assert_eq!(
        [(); 7].map(|()| after(&mut pace, RefreshOutcome::Retired)),
        [5_000, 15_000, 60_000, 300_000, 900_000, 900_000, 900_000]
    );
    // An answer: four minutes, and the ladder starts over after it.
    assert_eq!(after(&mut pace, RefreshOutcome::Renewed), 240_000);
    assert_eq!(after(&mut pace, RefreshOutcome::Renewed), 240_000);

    // A missed renewal with the link kept: the core's bound, every time,
    // however far the ladder has grown.
    assert_eq!(
        [(); 6].map(|()| after(&mut pace, retained(5_000))),
        [5_000, 5_000, 5_000, 5_000, 5_000, 5_000]
    );
    // The ladder where it is the sooner of the two.
    let mut fresh = Pace::default();
    assert_eq!(after(&mut fresh, retained(60_000)), 5_000);
    assert_eq!(after(&mut fresh, retained(60_000)), 15_000);
    assert_eq!(after(&mut fresh, retained(60_000)), 60_000);
    // Nearly out: soon, but not in a spin.
    assert_eq!(after(&mut pace, retained(2_500)), 2_500);
    assert_eq!(after(&mut pace, retained(300)), 1_000);
    assert_eq!(after(&mut pace, retained(0)), 1_000);
    // The kept link is lost after all: asked for again from the ladder's
    // first step, not from where the kept tries had got to.
    assert_eq!(after(&mut pace, RefreshOutcome::Retired), 5_000);
    assert_eq!(after(&mut pace, RefreshOutcome::Retired), 15_000);
    // And an answer that comes back is renewed as any other.
    assert_eq!(after(&mut pace, RefreshOutcome::Renewed), 240_000);
    assert_eq!(after(&mut pace, RefreshOutcome::Retired), 5_000);
}

/// A session that starts after an app window was opened routes that window
/// too: one that replaces a session whose listener ended, and one that
/// starts when the choice or the system's proxy settings allow it again.
#[test]
fn a_session_that_starts_later_routes_the_app_windows_already_open() {
    let now = Instant::now();
    let published = shown(ONE, "main-runtime-1");
    let mut wiring = live();
    let first = opened(&mut wiring, ONE, "main-runtime-1", now);
    // A Show window, opened with the session and registered with it; an
    // Editor window of another instance and one of another account.
    assert_eq!(wiring.app_window_created("show-1", ACCOUNT, ONE, Some(first)), []);
    assert_eq!(wiring.app_window_created("editor-2", ACCOUNT, TWO, None), []);
    assert_eq!(wiring.app_window_created("editor-3", "account-2", ONE, None), []);
    wiring.probed(first, true);

    // The listener ends: both of the session's windows lose their route.
    assert_eq!(
        wiring.listener_ended(first, now),
        [retire(first, &["main-runtime-1", "show-1"])]
    );
    // After the pause a new session starts for the same view, and takes the
    // Show window that is still open with it; a second one, opened while no
    // session existed, too. Not the windows of another instance or account.
    wiring.app_window_created("show-2", ACCOUNT, ONE, None);
    let later = now + Duration::from_secs(60);
    assert_eq!(
        wiring.reconcile(Some(&published), |_| true, later),
        [open("main-runtime-1")]
    );
    let second = wiring.open(target(ONE), material(4200), Some("main-runtime-1"), later);
    assert_eq!(wiring.probed(second, false), []);
    assert_eq!(
        wiring.probed(second, true),
        [
            install(second, "main-runtime-1"),
            install(second, "show-1"),
            install(second, "show-2")
        ]
    );
    // A window that found no session when it was about to be made, because
    // the session started while it was being made, joins it when it exists:
    // routed at once, since the session has its answer. And only once.
    assert_eq!(
        wiring.app_window_created("show-3", ACCOUNT, ONE, None),
        [install(second, "show-3")]
    );
    // An app window's label is used again once its window has closed. The
    // window that now has it is a new one and is routed as one, also when
    // the shell has not yet looked and seen the old one gone.
    assert_eq!(
        wiring.app_window_created("show-3", ACCOUNT, ONE, None),
        [install(second, "show-3")]
    );
    // And it is the session's window once, not twice: one route to take off.
    assert_eq!(
        wiring.listener_ended(second, later),
        [retire(second, &["main-runtime-1", "show-1", "show-2", "show-3"])]
    );
    // Before the session's first answer it joins without a route yet, and
    // gets one with the others.
    let mut early = live();
    let session = opened(&mut early, ONE, "main-runtime-1", now);
    assert_eq!(early.app_window_created("show-1", ACCOUNT, ONE, None), []);
    assert_eq!(
        early.probed(session, true),
        [install(session, "main-runtime-1"), install(session, "show-1")]
    );

    // An app window that closed meanwhile is not waited for.
    let mut wiring = live();
    wiring.app_window_created("show-1", ACCOUNT, ONE, None);
    wiring.app_window_created("show-2", ACCOUNT, ONE, None);
    assert_eq!(
        wiring.reconcile(Some(&published), |window| window != "show-1", now),
        [open("main-runtime-1")]
    );
    let session = wiring.open(target(ONE), material(4100), Some("main-runtime-1"), now);
    assert_eq!(
        wiring.probed(session, true),
        [install(session, "main-runtime-1"), install(session, "show-2")]
    );

    // Where a route is given only at creation, a window that was created
    // without one cannot be given one later: nothing is taken along.
    let mut choices = Choices::unsaved();
    choices.remember(ACCOUNT, ONE);
    let mut wiring = Wiring::new(Routing::AtCreation, false, choices);
    assert_eq!(wiring.app_window_created("show-1", ACCOUNT, ONE, None), []);
    let session = wiring.open(target(ONE), material(4100), None, now);
    assert_eq!(
        wiring.window_created(session, "main-runtime-2"),
        [Effect::Answer {
            session,
            window: "main-runtime-2".to_owned()
        }]
    );
    assert_eq!(wiring.reconcile(Some(&shown(ONE, "main-runtime-2")), |_| true, now), []);
    assert_eq!(wiring.probed(session, true), []);
    // Nor does a window join a session there by being registered: only the
    // route it was created with makes it the session's.
    assert_eq!(wiring.app_window_created("show-2", ACCOUNT, ONE, None), []);
    let answer = |window: &str| Effect::Answer {
        session,
        window: window.to_owned(),
    };
    assert_eq!(
        wiring.app_window_created("show-3", ACCOUNT, ONE, Some(session)),
        [answer("show-3")]
    );
    // A label used again for a new window: its listener's challenge is
    // answered in the new window too, or WebView2 would ask the person.
    assert_eq!(
        wiring.app_window_created("show-3", ACCOUNT, ONE, Some(session)),
        [answer("show-3")]
    );
    // A window created for a session that has ended meanwhile joins nothing.
    assert_eq!(wiring.app_window_created("show-4", ACCOUNT, ONE, Some(session + 7)), []);
    assert_eq!(
        wiring.listener_ended(session, now),
        [retire(session, &["main-runtime-2", "show-3"])]
    );
}

/// A route or a challenge answer that was queued for a window is put on it
/// only if the session still routes that window when its turn comes.
#[test]
fn work_queued_for_a_window_is_for_a_session_that_still_routes_it() {
    let now = Instant::now();
    let mut wiring = live();
    let session = opened(&mut wiring, ONE, "main-runtime-1", now);
    // Not before the session's first answer: nothing is routed yet.
    assert!(!wiring.routes(session, "main-runtime-1"));
    assert_eq!(wiring.app_window_created("show-1", ACCOUNT, ONE, None), []);
    wiring.probed(session, true);
    assert!(wiring.routes(session, "main-runtime-1") && wiring.routes(session, "show-1"));
    assert!(!wiring.routes(session, "show-2") && !wiring.routes(session + 1, "show-1"));
    // A window that closed is no longer routed, once the shell has looked.
    assert_eq!(
        wiring.reconcile(Some(&shown(ONE, "main-runtime-1")), |window| window != "show-1", now),
        []
    );
    assert!(!wiring.routes(session, "show-1"));
    // The session ended: whatever is still queued for its windows is dropped.
    assert_eq!(
        wiring.choose(ACCOUNT, ONE, Mode::Tunnel),
        [retire(session, &["main-runtime-1"])]
    );
    assert!(!wiring.routes(session, "main-runtime-1"));

    // The same where a route is given at creation: a window is routed from
    // the moment it is registered, and no longer once its session ended.
    let mut choices = Choices::unsaved();
    choices.remember(ACCOUNT, ONE);
    let mut wiring = Wiring::new(Routing::AtCreation, false, choices);
    let session = wiring.open(target(ONE), material(4100), None, now);
    assert!(!wiring.routes(session, "main-runtime-2"));
    wiring.window_created(session, "main-runtime-2");
    assert!(wiring.routes(session, "main-runtime-2"));
    wiring.listener_ended(session, now);
    assert!(!wiring.routes(session, "main-runtime-2"));
}

/// A session that the system's proxy settings have just ruled out still
/// exists until the shell's next look. A window made in that moment does
/// not join it, so it never gets a route that the look would take off; it
/// is remembered, and the session that starts once the settings allow it
/// takes it along.
#[test]
fn a_window_does_not_join_a_session_that_is_about_to_end() {
    let now = Instant::now();
    let published = shown(ONE, "main-runtime-1");
    let mut wiring = live();
    let session = opened(&mut wiring, ONE, "main-runtime-1", now);
    wiring.probed(session, true);
    // The settings are read when the window is about to be made.
    wiring.system_proxy(true);
    assert_eq!(wiring.app_window_created("show-1", ACCOUNT, ONE, None), []);
    assert!(!wiring.routes(session, "show-1"));
    // The look ends the session; only the instance view had a route.
    assert_eq!(
        wiring.reconcile(Some(&published), |_| true, now),
        [retire(session, &["main-runtime-1"])]
    );
    // The settings clear: a new session, and the window with it.
    wiring.system_proxy(false);
    assert_eq!(
        wiring.reconcile(Some(&published), |_| true, now),
        [open("main-runtime-1")]
    );
    let next = wiring.open(target(ONE), material(4200), Some("main-runtime-1"), now);
    assert_eq!(
        wiring.probed(next, true),
        [install(next, "main-runtime-1"), install(next, "show-1")]
    );

    // The same for a choice that is about to end the session's instance:
    // choosing ends it at once, so there is nothing left to join.
    assert_eq!(
        wiring.choose(ACCOUNT, ONE, Mode::Tunnel),
        [retire(next, &["main-runtime-1", "show-1"])]
    );
    assert_eq!(wiring.app_window_created("show-2", ACCOUNT, ONE, None), []);
}

#[test]
fn a_page_closes_shared_connections_only_from_the_sole_routed_window() {
    let now = Instant::now();
    let mut wiring = live();
    let session = opened(&mut wiring, ONE, "main-runtime-1", now);
    wiring.app_window_created("show-1", ACCOUNT, ONE, None);
    // Before the first answer no window has the route: none is on it.
    assert_eq!(wiring.routed_by("main-runtime-1"), None);
    assert!(!wiring.sole(session, "main-runtime-1", |_| true));

    wiring.probed(session, true);
    assert_eq!(wiring.routed_by("main-runtime-1"), Some(session));
    assert_eq!(wiring.routed_by("show-1"), Some(session));
    assert_eq!(wiring.routed_by("editor-9"), None);
    // The instance view and an app window share the listener: neither is alone.
    assert!(!wiring.sole(session, "main-runtime-1", |_| true));
    assert!(!wiring.sole(session, "show-1", |_| true));
    // The app window has closed, whether or not the wiring has heard: the view is.
    assert!(wiring.sole(session, "main-runtime-1", |window| window != "show-1"));
    // An app window whose instance view is gone is alone too.
    assert!(wiring.sole(session, "show-1", |window| window == "show-1"));
    // Never a window without the route, or another session's.
    assert!(!wiring.sole(session, "editor-9", |_| false));
    assert!(!wiring.sole(session + 1, "main-runtime-1", |_| false));

    // The choice of the tunnel ends the session: no window is on a route.
    wiring.choose(ACCOUNT, ONE, Mode::Tunnel);
    assert_eq!(wiring.routed_by("main-runtime-1"), None);
    assert!(!wiring.sole(session, "main-runtime-1", |_| false));
}

#[test]
fn only_an_instance_that_could_be_routed_is_one_whose_page_gets_a_bridge() {
    let custom = "https://avibe.example.com";
    let mut wiring = live();
    assert!(wiring.routable(ONE));
    // Whatever the choice of path: its page can still read and change it.
    wiring.choose(ACCOUNT, ONE, Mode::Tunnel);
    assert!(wiring.routable(ONE));
    // An address the direct path does not serve, and anything that is not
    // an instance's own origin.
    for origin in [custom, "http://one-app.avibe.bot", "one-app.avibe.bot", ""] {
        assert!(!wiring.routable(origin), "{origin}");
    }
    // A configured proxy, or a system that cannot route.
    assert!(!Wiring::new(Routing::Live, true, Choices::unsaved()).routable(ONE));
    assert!(!Wiring::new(Routing::Unsupported, false, Choices::unsaved()).routable(ONE));
    assert!(Wiring::new(Routing::AtCreation, false, Choices::unsaved()).routable(ONE));
}

#[test]
fn an_instance_that_answered_before_is_routed_before_its_view_first_loads() {
    let now = Instant::now();
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("direct-paths.json");
    let mut wiring = Wiring::new(Routing::Live, false, Choices::load(path.clone()));

    // Never answered: the view is created as it always was, asked once it
    // is shown, and routed with the first answer. WebKit already has its
    // connection by then, so that view keeps the tunnel while it lives.
    assert!(!wiring.starts_at_creation(&target(ONE)));
    let first = opened(&mut wiring, ONE, "main-runtime-1", now);
    assert_eq!(wiring.window_created(first, "show-1"), []);
    assert_eq!(
        wiring.probed(first, true),
        [install(first, "main-runtime-1"), install(first, "show-1")]
    );
    assert_eq!(wiring.reconcile(None, |_| false, now), [retire(first, &[])]);

    // From then on, also after a restart, its client starts before its view
    // is created, and the view is routed as it is made: before its first
    // load, without waiting for an answer.
    let mut wiring = Wiring::new(Routing::Live, false, Choices::load(path));
    assert!(wiring.starts_at_creation(&target(ONE)));
    assert!(!wiring.starts_at_creation(&target(TWO)));
    let session = wiring.open(target(ONE), material(4300), None, now);
    assert_eq!(
        wiring.window_created(session, "main-runtime-2"),
        [install(session, "main-runtime-2")]
    );
    assert_eq!(wiring.window_created(session, "main-runtime-2"), []);
    // Until its window is the published instance view it waits, asks
    // nothing, and no second client is started for that view.
    assert_eq!(wiring.asks_through(session), None);
    assert_eq!(wiring.reconcile(None, |_| true, now), []);
    assert_eq!(wiring.reconcile(Some(&away(ONE, "main-runtime-2")), |_| true, now), []);
    // Its view can be on show before a look has seen it there. An app
    // window opened from it in that moment joins it and is routed as it is
    // made; one of another instance or account does not.
    assert_eq!(wiring.shared(ACCOUNT, ONE), Some(session));
    assert_eq!(
        wiring.app_window_created("show-1", ACCOUNT, ONE, None),
        [install(session, "show-1")]
    );
    assert_eq!(wiring.app_window_created("show-9", ACCOUNT, TWO, None), []);
    assert_eq!(wiring.app_window_created("show-8", "account-2", ONE, None), []);
    let published = shown(ONE, "main-runtime-2");
    assert_eq!(wiring.reconcile(Some(&published), |_| true, now), []);
    assert_eq!(wiring.asks_through(session), Some("main-runtime-2"));
    // The first answer routes nothing twice.
    assert_eq!(wiring.probed(session, true), []);
    // An app window opened from the view joins it and is routed as it is made.
    assert_eq!(
        wiring.app_window_created("show-2", ACCOUNT, ONE, None),
        [install(session, "show-2")]
    );
    // It ends as any session does, and its route comes off every window.
    assert_eq!(
        wiring.choose(ACCOUNT, ONE, Mode::Tunnel),
        [retire(session, &["main-runtime-2", "show-1", "show-2"])]
    );
    // "Cloudflare tunnel only", a configured proxy, system proxy settings
    // that rule the route out, and a system that cannot route: no client
    // before the view, as none after.
    assert!(!wiring.starts_at_creation(&target(ONE)));
    wiring.choose(ACCOUNT, ONE, Mode::Automatic);
    assert!(wiring.starts_at_creation(&target(ONE)));
    wiring.system_proxy(true);
    assert!(!wiring.starts_at_creation(&target(ONE)));
    for (routing, proxy_configured) in [(Routing::Live, true), (Routing::Unsupported, false)] {
        let mut choices = Choices::unsaved();
        choices.remember(ACCOUNT, ONE);
        assert!(!Wiring::new(routing, proxy_configured, choices).starts_at_creation(&target(ONE)));
    }
}

#[test]
fn a_view_that_is_never_shown_ends_its_early_session_and_takes_its_route_off() {
    let now = Instant::now();
    let mut choices = Choices::unsaved();
    choices.remember(ACCOUNT, ONE);
    let mut wiring = Wiring::new(Routing::Live, false, choices);
    let session = wiring.open(target(ONE), material(4300), None, now);
    wiring.window_created(session, "main-runtime-1");
    // Another instance's view is on show meanwhile: it has a session of its own.
    assert_eq!(
        wiring.reconcile(Some(&shown(TWO, "main-runtime-0")), |_| true, now),
        [open("main-runtime-0")]
    );
    assert!(wiring
        .reconcile(None, |_| true, now + Duration::from_secs(59))
        .is_empty());
    assert_eq!(
        wiring.reconcile(None, |_| true, now + Duration::from_secs(60)),
        [retire(session, &["main-runtime-1"])]
    );
}
