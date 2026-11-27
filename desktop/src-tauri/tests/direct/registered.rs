use super::{
    credential_tests::{connect_as, echo},
    registered::Registered,
    Client, HandoverResult, PublicCensus,
};
use tokio::{
    io::AsyncReadExt,
    net::TcpStream,
    time::{timeout, Duration},
};

const HOST: &str = "one-app.avibe.bot";

/// A client whose listener's tunnel leg is an echo, and the registrations
/// of its session's windows.
async fn session() -> (Client, Registered) {
    let mut client = Client::new(HOST, "one".into()).await.unwrap();
    // Replaces the listener with one whose public leg needs no network.
    drop(super::handover_tests::public_browser(&mut client).await);
    let registered = Registered::new(client.handover());
    (client, registered)
}

async fn closed(stream: &mut TcpStream) -> bool {
    matches!(stream.read(&mut [0]).await, Ok(0) | Err(_))
}

fn one_quiet_or_busy(census: Option<PublicCensus>) -> bool {
    census.is_some_and(|census| census.quiet + census.busy == 1)
}

#[tokio::test]
async fn each_window_presents_its_own_registration_and_has_its_own_connections() {
    timeout(Duration::from_secs(6), async {
        let (client, mut registered) = session().await;
        let handover = client.handover();
        let view = registered.register("main-runtime-1").unwrap();
        let show = registered.register("show-1").unwrap();
        // Two credentials, each its window's own.
        assert_ne!(view.native_login().1, show.native_login().1);
        assert_eq!(
            registered.of("main-runtime-1").map(|kept| kept.native_login().1),
            Some(view.native_login().1)
        );
        assert!(registered.of("editor-9").is_none() && registered.handing_over("editor-9").is_none());

        // Each window's connection is counted for that window alone.
        let mut of_view = connect_as(&client, &view, 200).await;
        echo(&mut of_view).await;
        assert!(one_quiet_or_busy(handover.window_census(&view)));
        assert_eq!(handover.window_census(&show), Some(PublicCensus::default()));
        let mut of_show = connect_as(&client, &show, 200).await;
        echo(&mut of_show).await;
        assert!(one_quiet_or_busy(handover.window_census(&show)));
        assert!(one_quiet_or_busy(handover.window_census(&view)));
        // Both in the count for the instance's log, with the one the fixture
        // made: the tunnel carried them, with no direct path. Summed, since
        // the three may fall either side of a minute's end.
        let usage = &client.listener.access.usage;
        assert_eq!(usage.total(Some(super::faults::PathWord::None), "tunnel"), 3);
        assert_eq!(usage.total(None, "tunnel"), 3);
        assert_eq!(usage.total(None, "direct") + usage.total(None, "direct_failed"), 0);

        // A page's hand-over is asked with its own window's registration.
        // With no direct link up there is nothing to move to, and nothing
        // of either window is closed.
        let (handle, own) = registered.handing_over("show-1").unwrap();
        assert_eq!(own.native_login().1, show.native_login().1);
        assert_eq!(
            handle.close_quiet_public_for_window(HOST, &own).await,
            HandoverResult::NotUsable
        );
        echo(&mut of_view).await;
        echo(&mut of_show).await;
    })
    .await
    .unwrap();
}

#[tokio::test]
async fn a_window_that_closed_is_revoked_and_the_others_are_untouched() {
    timeout(Duration::from_secs(6), async {
        let (client, mut registered) = session().await;
        let handover = client.handover();
        let view = registered.register("main-runtime-1").unwrap();
        let show = registered.register("show-1").unwrap();
        let mut of_view = connect_as(&client, &view, 200).await;
        let mut of_show = connect_as(&client, &show, 200).await;
        echo(&mut of_view).await;
        echo(&mut of_show).await;

        // The app window closed: it is not routed any more.
        registered.keep(|window| window != "show-1");
        assert!(registered.of("show-1").is_none());
        assert!(closed(&mut of_show).await);
        drop(connect_as(&client, &show, 407).await);
        assert_eq!(handover.window_census(&show), None);
        assert_eq!(
            handover.close_quiet_public_for_window(HOST, &show).await,
            HandoverResult::NotUsable
        );
        // The instance view keeps its registration and its connection.
        assert!(registered.of("main-runtime-1").is_some());
        echo(&mut of_view).await;
        assert!(one_quiet_or_busy(handover.window_census(&view)));
        // Nothing changes for a window that is still routed.
        registered.keep(|_| true);
        echo(&mut of_view).await;

        // The session ends: every registration goes, before the client does.
        registered.revoke_all();
        assert!(registered.of("main-runtime-1").is_none());
        assert!(closed(&mut of_view).await);
        drop(connect_as(&client, &view, 407).await);
    })
    .await
    .unwrap();
}

#[tokio::test]
async fn a_new_window_under_a_used_label_gets_its_own_and_the_old_one_can_do_nothing() {
    timeout(Duration::from_secs(6), async {
        let (client, mut registered) = session().await;
        let handover = client.handover();
        let old = registered.register("show-1").unwrap();
        let mut of_old = connect_as(&client, &old, 200).await;
        echo(&mut of_old).await;
        // A hand-over the old window's page asked for, still waiting.
        let (handle, held) = registered.handing_over("show-1").unwrap();

        // The label is used again for a new window.
        let new = registered.register("show-1").unwrap();
        assert_ne!(old.native_login().1, new.native_login().1);
        assert_eq!(
            registered.of("show-1").map(|kept| kept.native_login().1),
            Some(new.native_login().1)
        );
        // The old window's connection is closed and its credential opens no
        // other; the new window's does.
        assert!(closed(&mut of_old).await);
        drop(connect_as(&client, &old, 407).await);
        let mut of_new = connect_as(&client, &new, 200).await;
        echo(&mut of_new).await;

        // What the old window started does nothing to the new one: its
        // hand-over, its census, and a late teardown of it.
        assert_eq!(
            handle.close_quiet_public_for_window(HOST, &held).await,
            HandoverResult::NotUsable
        );
        assert_eq!(handover.window_census(&held), None);
        handover.revoke_window(&held);
        echo(&mut of_new).await;
        assert!(one_quiet_or_busy(handover.window_census(&new)));
        assert!(registered.of("show-1").is_some());
    })
    .await
    .unwrap();
}

#[tokio::test]
async fn a_listener_that_ended_registers_no_window() {
    let (client, mut registered) = session().await;
    assert!(registered.register("main-runtime-1").is_some());
    drop(client);
    assert!(registered.register("show-1").is_none());
    assert!(registered.of("show-1").is_none());
    // Revoking after the listener is gone is nothing to do, not a fault.
    registered.revoke_all();
    assert!(registered.of("main-runtime-1").is_none());
}
