//! "The direct path and the relay only", as `wiring.rs` decides it: plain
//! data in, effects out. What the effects do to real windows and a real
//! listener is the shell's, and the WebView's part is measured by
//! `examples/direct_webview_probe.rs`.
use super::{
    choices::{Choices, Mode, Reason, Reverted},
    wiring::{Effect, Material, Published, Routing, Target, Wiring},
};
use std::time::Duration;
use tokio::time::Instant;

const ACCOUNT: &str = "account-1";
const ONE: &str = "https://one-app.avibe.bot";
const TWO: &str = "https://two-app.avibe.bot";
const VIEW: &str = "main-runtime-1";

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

fn material() -> Material {
    Material {
        proxy: "127.0.0.1:4100".parse().unwrap(),
        exclusive: true,
    }
}

fn seconds(count: u64) -> Duration {
    Duration::from_secs(count)
}

fn strict(session: u64, only: bool) -> Effect {
    Effect::Strict { session, only }
}

fn reload(session: u64, windows: &[&str]) -> Effect {
    Effect::Reload {
        session,
        windows: windows.iter().map(|window| (*window).to_owned()).collect(),
    }
}

fn reopen(runtime: &str) -> Effect {
    Effect::Reopen {
        runtime: runtime.to_owned(),
    }
}

fn revert(origin: &str, runtime: &str) -> Effect {
    Effect::Revert {
        account: ACCOUNT.to_owned(),
        origin: origin.to_owned(),
        runtime: runtime.to_owned(),
    }
}

fn retire(session: u64, detach: &[&str]) -> Effect {
    Effect::Retire {
        session,
        detach: detach.iter().map(|window| (*window).to_owned()).collect(),
    }
}

/// A wiring whose choice for the first instance is "the direct path and the
/// relay only".
fn chosen(routing: Routing) -> Wiring {
    let mut wiring = Wiring::new(routing, false, Choices::unsaved());
    assert_eq!(wiring.choose(ACCOUNT, ONE, Mode::Direct), []);
    wiring
}

/// Its session, as the shell starts one for a view that is about to be
/// created, with that view made and on show.
fn connecting(wiring: &mut Wiring, runtime: &str, now: Instant) -> u64 {
    assert!(wiring.starts_at_creation(&target(ONE)));
    let session = wiring.open(target(ONE), material(), None, now);
    assert_eq!(wiring.window_created(session, runtime).len(), 1);
    assert_eq!(wiring.reconcile(Some(&shown(ONE, runtime)), |_| true, now), []);
    session
}

/// The same, with its first link up: direct-only from here.
fn linked(wiring: &mut Wiring, runtime: &str, now: Instant) -> u64 {
    let session = connecting(wiring, runtime, now);
    assert_eq!(
        wiring.path(session, true, 0, now),
        [strict(session, true), reload(session, &[runtime])]
    );
    session
}

/// A direct-only instance gets its client and its route before its view's
/// first load on its very first open, where every other instance is routed
/// only once it has answered. Its route allows no failover, and no session
/// is ever started for a view of it that is already open.
#[test]
fn a_direct_only_instance_is_routed_before_its_first_load_from_its_first_open() {
    let now = Instant::now();
    for routing in [Routing::Live, Routing::AtCreation] {
        let mut wiring = chosen(routing);
        assert!(wiring.only(&target(ONE)));
        assert!(!wiring.only(&target(TWO)));
        // It has never answered, and starts at creation all the same.
        assert!(wiring.starts_at_creation(&target(ONE)));
        assert!(!wiring.starts_at_creation(&target(TWO)));
        let session = wiring.open(target(ONE), material(), None, now);
        // Routed as it is made, whichever way this system routes.
        let routed = wiring.window_created(session, VIEW);
        match routing {
            Routing::Live => assert_eq!(
                routed,
                [Effect::Install {
                    session,
                    window: VIEW.to_owned()
                }]
            ),
            _ => assert_eq!(
                routed,
                [Effect::Answer {
                    session,
                    window: VIEW.to_owned()
                }]
            ),
        }
        assert!(wiring.routes(session, VIEW));
        assert!(wiring.route(session).is_some_and(|(_, material)| material.exclusive));
        assert_eq!(wiring.reconcile(Some(&shown(ONE, VIEW)), |_| true, now), []);
        // An app window opened from it is routed as it is made, too.
        let created_for = (routing == Routing::AtCreation).then_some(session);
        assert_eq!(wiring.app_window_created("show-1", ACCOUNT, ONE, created_for).len(), 1);
        assert!(wiring.routes(session, "show-1"));
        // Connecting: the listener serves the tunnel, as any does.
        assert!(!wiring.is_direct_only(session));
    }
    // Where nothing can be routed it is no instance for a session at all.
    for wiring in [
        Wiring::new(Routing::Unsupported, false, Choices::unsaved()),
        Wiring::new(Routing::Live, true, Choices::unsaved()),
    ] {
        let mut wiring = wiring;
        wiring.choose(ACCOUNT, ONE, Mode::Direct);
        assert!(!wiring.starts_at_creation(&target(ONE)));
    }
}

/// Connecting goes through the tunnel once. With the first link the
/// listener becomes direct-only and every window of the session is
/// reloaded, once: nothing follows from a link that stays, or changes its
/// kind.
#[test]
fn the_first_link_makes_the_listener_direct_only_and_reloads_its_windows_once() {
    let now = Instant::now();
    let mut wiring = chosen(Routing::Live);
    let session = connecting(&mut wiring, VIEW, now);
    assert_eq!(wiring.app_window_created("show-1", ACCOUNT, ONE, None).len(), 1);
    // No link yet, however often that is said: still the tunnel.
    assert_eq!(wiring.path(session, false, 0, now), []);
    assert_eq!(wiring.path(session, false, 0, now + seconds(3)), []);
    assert!(!wiring.is_direct_only(session));
    assert_eq!(wiring.deadline(), None);

    assert_eq!(
        wiring.path(session, true, 0, now + seconds(4)),
        [strict(session, true), reload(session, &[VIEW, "show-1"])]
    );
    assert!(wiring.is_direct_only(session));
    assert_eq!(wiring.path(session, true, 0, now + seconds(5)), []);
    assert_eq!(wiring.path(session, true, 0, now + seconds(500)), []);
    assert_eq!(
        wiring.reconcile(Some(&shown(ONE, VIEW)), |_| true, now + seconds(600)),
        []
    );

    // A session of another choice hears its path and nothing follows.
    let mut automatic = Wiring::new(Routing::Live, false, Choices::unsaved());
    let other = automatic.open(target(TWO), material(), None, now);
    assert_eq!(automatic.path(other, true, 0, now), []);
    assert_eq!(automatic.path(other, false, 9, now), []);
    assert_eq!(automatic.unlinked(other, now), []);
    assert!(!automatic.is_direct_only(other));
}

/// The link goes. For ten seconds the listener stays direct-only: requests
/// wait and fail, and nothing is let onto the tunnel. Then the session
/// connects again as it did at first: the listener serves the tunnel, the
/// rendezvous is asked at once, and the next link reloads the windows once
/// more.
#[test]
fn a_drop_fails_clearly_for_ten_seconds_and_then_connects_again_through_the_tunnel() {
    let now = Instant::now();
    let mut wiring = chosen(Routing::Live);
    let session = linked(&mut wiring, VIEW, now);
    let published = shown(ONE, VIEW);

    let dropped = now + seconds(100);
    assert_eq!(wiring.path(session, false, 0, dropped), []);
    assert!(wiring.is_direct_only(session));
    // The shell looks again exactly when that time is up.
    assert_eq!(wiring.deadline(), Some(dropped + seconds(10)));
    assert_eq!(wiring.reconcile(Some(&published), |_| true, dropped + seconds(9)), []);
    // The requests that failed meanwhile are the core's to count; they
    // change nothing here.
    assert_eq!(wiring.path(session, false, 7, dropped + seconds(9)), []);
    assert!(wiring.is_direct_only(session));

    assert_eq!(
        wiring.reconcile(Some(&published), |_| true, dropped + seconds(10)),
        [strict(session, false), Effect::Ask { session }]
    );
    assert!(!wiring.is_direct_only(session));
    assert_eq!(wiring.deadline(), None);
    // Once: the next look starts nothing again.
    assert_eq!(wiring.reconcile(Some(&published), |_| true, dropped + seconds(11)), []);
    assert_eq!(
        wiring.path(session, true, 7, dropped + seconds(13)),
        [strict(session, true), reload(session, &[VIEW])]
    );

    // A link that is back while requests were still failing: nothing was
    // let onto the tunnel, and nothing has to be reloaded.
    let again = dropped + seconds(200);
    assert_eq!(wiring.path(session, false, 7, again), []);
    assert_eq!(wiring.path(session, true, 7, again + seconds(4)), []);
    assert!(wiring.is_direct_only(session));
    assert_eq!(wiring.deadline(), None);
    assert_eq!(wiring.reconcile(Some(&published), |_| true, again + seconds(30)), []);
}

/// The choice is set back when the tunnel reaches the instance and no link
/// comes of it: on the instance's second such answer, or twenty seconds
/// after its first. A computer that reaches nothing keeps its choice,
/// however long: no timer alone sets it back.
#[test]
fn the_choice_is_set_back_only_when_the_instance_is_heard_from_and_no_link_comes() {
    let now = Instant::now();
    let published = shown(ONE, VIEW);

    // Two answers through the tunnel that came to nothing.
    let mut wiring = chosen(Routing::Live);
    let session = connecting(&mut wiring, VIEW, now);
    assert_eq!(wiring.unlinked(session, now + seconds(2)), []);
    assert_eq!(wiring.deadline(), Some(now + seconds(22)));
    assert_eq!(wiring.unlinked(session, now + seconds(7)), [revert(ONE, VIEW)]);

    // One answer, and twenty seconds without a link after it.
    let mut wiring = chosen(Routing::Live);
    let session = connecting(&mut wiring, VIEW, now);
    assert_eq!(wiring.unlinked(session, now + seconds(2)), []);
    assert_eq!(wiring.reconcile(Some(&published), |_| true, now + seconds(21)), []);
    assert_eq!(
        wiring.reconcile(Some(&published), |_| true, now + seconds(22)),
        [revert(ONE, VIEW)]
    );

    // One answer, and then the link: connected, nothing set back.
    let mut wiring = chosen(Routing::Live);
    let session = connecting(&mut wiring, VIEW, now);
    assert_eq!(wiring.unlinked(session, now + seconds(2)), []);
    assert_eq!(
        wiring.path(session, true, 0, now + seconds(5)),
        [strict(session, true), reload(session, &[VIEW])]
    );
    assert_eq!(wiring.deadline(), None);
    assert_eq!(wiring.reconcile(Some(&published), |_| true, now + seconds(60)), []);
    // Once linked, an answer that comes to nothing is not this rule's.
    assert_eq!(wiring.unlinked(session, now + seconds(61)), []);
    assert_eq!(wiring.unlinked(session, now + seconds(62)), []);

    // Nothing reaches the instance at all: no answer, so nothing is set
    // back, an hour on.
    let mut wiring = chosen(Routing::Live);
    let session = connecting(&mut wiring, VIEW, now);
    for minute in 1..=60 {
        let at = now + seconds(60 * minute);
        assert_eq!(wiring.path(session, false, 0, at), []);
        assert_eq!(wiring.reconcile(Some(&published), |_| true, at), []);
    }
    assert_eq!(wiring.deadline(), None);
    assert_eq!(wiring.mode(ACCOUNT, ONE), Mode::Direct);
}

/// A link that has to be made again for the third time in ten minutes arms
/// the setting-back. It is done when the instance is next heard from: its
/// first answer through the tunnel, or a link coming up. Three drops for
/// want of any network set nothing back, and drops that are far apart
/// never arm anything.
#[test]
fn a_third_drop_in_ten_minutes_sets_the_choice_back_once_the_instance_is_heard_from() {
    let now = Instant::now();
    let published = shown(ONE, VIEW);
    // Drops at `at`, and connects again ten seconds on.
    let drop = |wiring: &mut Wiring, session: u64, at: Instant| {
        assert_eq!(wiring.path(session, false, 0, at), []);
        assert_eq!(
            wiring.reconcile(Some(&published), |_| true, at + seconds(10)),
            [strict(session, false), Effect::Ask { session }]
        );
    };
    let link = |wiring: &mut Wiring, session: u64, at: Instant| {
        assert_eq!(
            wiring.path(session, true, 0, at),
            [strict(session, true), reload(session, &[VIEW])]
        );
    };

    // Three in ten minutes, and then the instance answers: set back at its
    // first answer, not its second.
    let mut wiring = chosen(Routing::Live);
    let session = linked(&mut wiring, VIEW, now);
    drop(&mut wiring, session, now + seconds(60));
    link(&mut wiring, session, now + seconds(75));
    drop(&mut wiring, session, now + seconds(120));
    link(&mut wiring, session, now + seconds(135));
    drop(&mut wiring, session, now + seconds(180));
    // Armed, and nothing reaches the instance: nothing is set back.
    for minute in 4..=30 {
        assert_eq!(
            wiring.reconcile(Some(&published), |_| true, now + seconds(60 * minute)),
            []
        );
    }
    assert_eq!(wiring.mode(ACCOUNT, ONE), Mode::Direct);
    assert_eq!(wiring.unlinked(session, now + seconds(1900)), [revert(ONE, VIEW)]);

    // The same, and the link comes up instead: heard from, and set back
    // rather than connected once more.
    let mut wiring = chosen(Routing::Live);
    let session = linked(&mut wiring, VIEW, now);
    for at in [60, 120] {
        drop(&mut wiring, session, now + seconds(at));
        link(&mut wiring, session, now + seconds(at + 15));
    }
    drop(&mut wiring, session, now + seconds(180));
    assert_eq!(wiring.path(session, true, 0, now + seconds(195)), [revert(ONE, VIEW)]);

    // Far apart: every drop is connected again, and nothing is armed.
    let mut wiring = chosen(Routing::Live);
    let session = linked(&mut wiring, VIEW, now);
    for round in 1..=6 {
        let at = now + seconds(400 * round);
        drop(&mut wiring, session, at);
        link(&mut wiring, session, at + seconds(15));
    }

    // A choice made again starts the count anew.
    let mut wiring = chosen(Routing::Live);
    let first = linked(&mut wiring, VIEW, now);
    drop(&mut wiring, first, now + seconds(60));
    link(&mut wiring, first, now + seconds(75));
    drop(&mut wiring, first, now + seconds(120));
    wiring.choose(ACCOUNT, ONE, Mode::Automatic);
    wiring.choose(ACCOUNT, ONE, Mode::Direct);
    let published = shown(ONE, "main-runtime-2");
    let second = wiring.open(target(ONE), material(), None, now + seconds(130));
    assert_eq!(wiring.window_created(second, "main-runtime-2").len(), 1);
    assert_eq!(wiring.reconcile(Some(&published), |_| true, now + seconds(131)), []);
    assert_eq!(
        wiring.path(second, true, 0, now + seconds(132)),
        [strict(second, true), reload(second, &["main-runtime-2"])]
    );
    assert_eq!(wiring.path(second, false, 0, now + seconds(140)), []);
    assert_eq!(
        wiring.reconcile(Some(&published), |_| true, now + seconds(150)),
        [strict(second, false), Effect::Ask { session: second }]
    );
    assert_eq!(
        wiring.path(second, true, 0, now + seconds(155)),
        [strict(second, true), reload(second, &["main-runtime-2"])]
    );
}

/// A link is reported, and direct-only connections fail on it all the
/// same: three within a minute, and the link is taken for one that carries
/// nothing. Asking again would only renew it, so the instance is opened
/// again, with a new client. Failures counted before the session looked,
/// or spread out, or made while no link is up, are not that.
#[test]
fn a_link_that_carries_nothing_has_its_instance_opened_again() {
    let now = Instant::now();
    let mut wiring = chosen(Routing::Live);
    // The client had counted failures before this session first looked.
    let session = connecting(&mut wiring, VIEW, now);
    assert_eq!(
        wiring.path(session, true, 40, now),
        [strict(session, true), reload(session, &[VIEW])]
    );
    assert_eq!(wiring.path(session, true, 40, now + seconds(1)), []);
    // Spread out: never three within a minute.
    assert_eq!(wiring.path(session, true, 41, now + seconds(10)), []);
    assert_eq!(wiring.path(session, true, 42, now + seconds(50)), []);
    assert_eq!(wiring.path(session, true, 43, now + seconds(110)), []);
    assert_eq!(wiring.path(session, true, 44, now + seconds(170)), []);
    // Three looks with new failures within a minute.
    assert_eq!(wiring.path(session, true, 45, now + seconds(175)), []);
    assert_eq!(
        wiring.path(session, true, 50, now + seconds(178)),
        // Its route stays on the window: its client goes, and the window
        // fails rather than reach the tunnel until it is replaced.
        [retire(session, &[]), reopen(VIEW)]
    );
    // The view is being replaced: it is not opened a second time, and
    // gets no session of its own.
    assert_eq!(
        wiring.reconcile(Some(&shown(ONE, VIEW)), |_| true, now + seconds(180)),
        []
    );

    // While no link is up, failed connections are what is meant to happen.
    let mut wiring = chosen(Routing::Live);
    let session = linked(&mut wiring, VIEW, now);
    assert_eq!(wiring.path(session, false, 0, now + seconds(1)), []);
    for count in 1..=9 {
        assert_eq!(wiring.path(session, false, count, now + seconds(2)), []);
    }
}

/// A direct-only instance's view is never left without a session's route:
/// it is opened again with one, once, and never given a session late.
/// Where no window of it can be routed now, the choice is set back.
#[test]
fn a_direct_only_view_without_a_session_is_opened_again_or_its_choice_set_back() {
    let now = Instant::now();
    let mut wiring = chosen(Routing::Live);
    let published = shown(ONE, VIEW);
    // Its view is there and no session holds it (its client could not be
    // started when the view was made): never `Open`, which would route it
    // only after it has connections on the tunnel.
    assert!(wiring.awaits(Some(&published)));
    assert_eq!(wiring.reconcile(Some(&published), |_| true, now), [reopen(VIEW)]);
    // Once: while the new view is on its way, nothing more.
    for second in [1, 30, 59] {
        assert_eq!(wiring.reconcile(Some(&published), |_| true, now + seconds(second)), []);
    }
    // It did not come: asked again.
    assert_eq!(
        wiring.reconcile(Some(&published), |_| true, now + seconds(60)),
        [reopen(VIEW)]
    );
    // The new view, with its session from before it was made: held.
    let session = wiring.open(target(ONE), material(), None, now + seconds(61));
    assert_eq!(wiring.window_created(session, "main-runtime-2").len(), 1);
    let replaced = shown(ONE, "main-runtime-2");
    assert_eq!(wiring.reconcile(Some(&replaced), |_| true, now + seconds(62)), []);
    assert!(!wiring.awaits(Some(&replaced)));

    // Its listener ends for good. The route is not taken off the view,
    // and the instance is opened again.
    assert_eq!(
        wiring.listener_ended(session, now + seconds(100)),
        [retire(session, &[])]
    );
    assert_eq!(
        wiring.reconcile(Some(&replaced), |_| true, now + seconds(100)),
        [reopen("main-runtime-2")]
    );

    // The instance was opened again, the tunnel carried its sign-in, and
    // the new view came without a session all the same (no client could be
    // started for it): it is not opened again and again. The choice is
    // set back.
    let mut wiring = chosen(Routing::Live);
    assert_eq!(wiring.reconcile(Some(&published), |_| true, now), [reopen(VIEW)]);
    assert_eq!(
        wiring.reconcile(Some(&replaced), |_| true, now + seconds(3)),
        [revert(ONE, "main-runtime-2")]
    );
    // A view that came with its session, and lost it later, is opened
    // again like any other.
    let mut wiring = chosen(Routing::Live);
    assert_eq!(wiring.reconcile(Some(&published), |_| true, now), [reopen(VIEW)]);
    let session = wiring.open(target(ONE), material(), None, now + seconds(1));
    assert_eq!(wiring.window_created(session, "main-runtime-2").len(), 1);
    assert_eq!(wiring.reconcile(Some(&replaced), |_| true, now + seconds(2)), []);
    assert_eq!(
        wiring.listener_ended(session, now + seconds(90)),
        [retire(session, &[])]
    );
    assert_eq!(
        wiring.reconcile(Some(&replaced), |_| true, now + seconds(90)),
        [reopen("main-runtime-2")]
    );

    // The system's proxy settings rule the route out: no window of it can
    // be routed, so the choice is set back instead.
    let mut wiring = chosen(Routing::Live);
    let session = linked(&mut wiring, VIEW, now);
    wiring.system_proxy(true);
    assert_eq!(
        wiring.reconcile(Some(&published), |_| true, now + seconds(5)),
        [retire(session, &[]), revert(ONE, VIEW)]
    );

    // Where a route is given only at creation, its windows keep their
    // answer to the listener until the session ends, as any session's do:
    // their route names the listener alone and fails by itself.
    let mut wiring = chosen(Routing::AtCreation);
    let session = wiring.open(target(ONE), material(), None, now);
    assert_eq!(wiring.window_created(session, VIEW).len(), 1);
    assert_eq!(wiring.reconcile(Some(&published), |_| true, now), []);
    assert_eq!(
        wiring.listener_ended(session, now + seconds(1)),
        [retire(session, &[VIEW])]
    );
    assert_eq!(
        wiring.reconcile(Some(&published), |_| true, now + seconds(1)),
        [reopen(VIEW)]
    );
}

/// A choice to or from "the direct path and the relay only" ends the
/// instance's sessions, whichever way, and needs the instance opened
/// again: a window's route is of one kind for as long as it lives. Between
/// the two other choices nothing changes from before.
#[test]
fn choosing_or_leaving_direct_only_ends_the_sessions_and_reopens_the_instance() {
    use Mode::{Automatic, Direct, Tunnel};
    for (before, after, reopens) in [
        (Automatic, Direct, true),
        (Tunnel, Direct, true),
        (Direct, Automatic, true),
        (Direct, Tunnel, true),
        (Automatic, Tunnel, false),
        (Tunnel, Automatic, false),
        (Direct, Direct, false),
        (Automatic, Automatic, false),
    ] {
        assert_eq!(Wiring::reopens(before, after), reopens, "{before:?} to {after:?}");
    }

    let now = Instant::now();
    // From automatic, with a session that routes its view: the session
    // ends and its route comes off, as when the tunnel is chosen.
    let mut wiring = Wiring::new(Routing::Live, false, Choices::unsaved());
    let published = shown(ONE, VIEW);
    assert_eq!(
        wiring.reconcile(Some(&published), |_| true, now),
        [Effect::Open {
            runtime: VIEW.to_owned()
        }]
    );
    let automatic = wiring.open(
        target(ONE),
        Material {
            exclusive: false,
            ..material()
        },
        Some(VIEW),
        now,
    );
    assert_eq!(wiring.probed(automatic, true).len(), 1);
    assert_eq!(wiring.choose(ACCOUNT, ONE, Direct), [retire(automatic, &[VIEW])]);
    // The shell opens the instance again and says so: the view that is
    // going gets no session and is not opened a second time.
    wiring.reopening(VIEW, now);
    assert_eq!(wiring.reconcile(Some(&published), |_| true, now + seconds(1)), []);

    // To automatic, from a session that is direct-only: it ends, its
    // route stays on the view, and the view that is going gets no late
    // session under the automatic choice either.
    let mut wiring = chosen(Routing::Live);
    let session = linked(&mut wiring, VIEW, now);
    assert_eq!(wiring.choose(ACCOUNT, ONE, Automatic), [retire(session, &[])]);
    wiring.reopening(VIEW, now);
    assert_eq!(wiring.reconcile(Some(&published), |_| true, now + seconds(1)), []);
    // Its new view is routed like any automatic one.
    let replaced = shown(ONE, "main-runtime-2");
    assert_eq!(
        wiring.reconcile(Some(&replaced), |_| true, now + seconds(2)),
        [Effect::Open {
            runtime: "main-runtime-2".to_owned()
        }]
    );

    // To the same choice again: nothing ends.
    let mut wiring = chosen(Routing::Live);
    let _session = linked(&mut wiring, VIEW, now);
    assert_eq!(wiring.choose(ACCOUNT, ONE, Direct), []);
}

/// The app sets the choice back itself: it is automatic from then on, it
/// says when and from what until someone chooses again, every reading of
/// it is newer than the ones before, and the instance's sessions end.
#[test]
fn a_choice_the_app_set_back_is_automatic_and_says_so_until_the_next_choice() {
    let now = Instant::now();
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("direct-paths.json");
    let mut wiring = Wiring::new(Routing::Live, false, Choices::load(path.clone()));
    wiring.choose(ACCOUNT, ONE, Mode::Direct);
    assert_eq!(wiring.choice(ACCOUNT, ONE), (Mode::Direct, None, 1));
    let session = linked(&mut wiring, VIEW, now);

    let reverted = Reverted {
        at: 1_795_000_000_000,
        reason: Reason::Unreachable,
        from: Mode::Direct,
    };
    assert_eq!(wiring.revert(ACCOUNT, ONE, reverted), [retire(session, &[])]);
    assert_eq!(wiring.choice(ACCOUNT, ONE), (Mode::Automatic, Some(reverted), 2));
    assert_eq!(wiring.choice(ACCOUNT, TWO), (Mode::Automatic, None, 2));
    assert!(!wiring.only(&target(ONE)));
    // Kept across a restart of the app.
    let restarted = Wiring::new(Routing::Live, false, Choices::load(path));
    assert_eq!(restarted.choice(ACCOUNT, ONE), (Mode::Automatic, Some(reverted), 0));

    wiring.choose(ACCOUNT, ONE, Mode::Tunnel);
    assert_eq!(wiring.choice(ACCOUNT, ONE), (Mode::Tunnel, None, 3));
}
