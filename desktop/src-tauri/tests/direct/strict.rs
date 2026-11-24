use super::{
    clock::Clock,
    credential_tests::{connect_as, echo},
    handover_tests::{echo_socket, pair},
    listener::Listener,
    quiet::{PublicLegs, SessionPolicy},
    strict, Client, SelectedPath,
};
use std::{
    future::pending,
    sync::{
        atomic::{AtomicUsize, Ordering},
        Arc,
    },
    time::Duration,
};
use tokio::{
    io::AsyncReadExt,
    sync::{watch, Notify},
    time::{sleep, timeout, Instant},
};

#[tokio::test(start_paused = true)]
async fn publication_and_dial_share_one_budget_including_relay() {
    let budget = strict::Timing::default().strict_open;
    assert_eq!(budget, Duration::from_secs(10));
    for stalled in [false, true] {
        let (tx, rx) = watch::channel(None);
        let start = Instant::now();
        let clock = Clock(Arc::new(move || start.elapsed()));
        let open = strict::open(rx, &clock, budget, |path| async move {
            assert_eq!(path, SelectedPath::Relay);
            if stalled {
                pending::<()>().await;
            }
            Some(path)
        });
        let (result, ()) = tokio::join!(open, async {
            sleep(Duration::from_secs(7)).await;
            tx.send_replace(Some(SelectedPath::Relay));
        });
        assert_eq!(result, (!stalled).then_some(SelectedPath::Relay));
        assert_eq!(start.elapsed(), if stalled { budget } else { Duration::from_secs(7) });
    }
}

#[tokio::test(start_paused = true)]
async fn sleep_expires_a_silent_wait_and_a_ready_dial() {
    let budget = Duration::from_secs(10);
    let ticks = Arc::new(AtomicUsize::new(0));
    let source = ticks.clone();
    let clock = Clock(Arc::new(move || Duration::from_secs(source.load(Ordering::SeqCst) as u64)));
    let (_tx, rx) = watch::channel(None::<()>);
    let start = Instant::now();
    let (opened, ()) = tokio::join!(strict::open(rx, &clock, budget, |_| async { Some(()) }), async {
        tokio::task::yield_now().await;
        ticks.store(60, Ordering::SeqCst);
    });
    assert_eq!(opened, None);
    assert!(start.elapsed() <= Duration::from_millis(250));

    let (_tx, rx) = watch::channel(Some(()));
    let start = Instant::now();
    let opened = strict::open(rx, &clock, budget, |_| async {
        // The machine wakes inside a successful dial poll, before the next timer tick.
        ticks.store(120, Ordering::SeqCst);
        Some(())
    }).await;
    assert_eq!(opened, None);
    assert_eq!(start.elapsed(), Duration::ZERO);
}

#[tokio::test]
async fn forced_cutover_retires_setup_before_any_payload_or_200() {
    let p = SessionPolicy::default();
    let legs = PublicLegs::default();
    let (mut client, browser) = pair().await;
    let (peer, mut server) = pair().await;
    let setup = p.sample().unwrap().commit(&legs, &browser, &peer).unwrap();
    assert_eq!(legs.census().busy, 1);
    assert_eq!(p.close_public(), 1);
    assert_eq!(legs.census().busy, 0);
    setup.serve(browser, peer, b"buffered").await;
    assert_eq!(server.read(&mut [0]).await.unwrap(), 0);
    assert_eq!(client.read(&mut [0]).await.unwrap(), 0);
}

#[tokio::test]
async fn toggling_preserves_streams_and_cutover_fences_even_a_late_public_open() {
    timeout(Duration::from_secs(20), async {
        let mut c = Client::new("one-app.avibe.bot", "one".into()).await.unwrap();
        let calls = Arc::new(AtomicUsize::new(0));
        let entered = Arc::new(Notify::new());
        let release = Arc::new(Notify::new());
        let (n, start, go) = (calls.clone(), entered.clone(), release.clone());
        c.listener = Listener::bind_via(c.host.clone(), c.links.subscribe(), c.status.clone(), move |_| {
            let nth = n.fetch_add(1, Ordering::SeqCst);
            let (start, go) = (start.clone(), go.clone());
            async move {
                if nth == 2 {
                    start.notify_one();
                    go.notified().await;
                }
                echo_socket().await
            }
        })
        .await
        .unwrap();
        let h = c.handover();
        let a = h.window_credential("a").unwrap();
        let b = h.window_credential("b").unwrap();
        let mut first = connect_as(&c, &a, 200).await;
        let mut second = connect_as(&c, &b, 200).await;
        let mut status = c.path_changes();
        let (late, ()) = tokio::join!(connect_as(&c, &a, 502), async {
            entered.notified().await;
            h.set_direct_only(true);
            echo(&mut first).await;
            echo(&mut second).await;
            assert_eq!(h.close_public(), 2);
            assert_eq!(h.close_public(), 0);
            assert_eq!(h.window_census(&a).unwrap(), Default::default());
            assert_eq!(h.window_census(&b).unwrap(), Default::default());
            release.notify_one();
        });
        drop(late);
        assert!(matches!(first.read(&mut [0]).await, Ok(0) | Err(_)));
        assert!(matches!(second.read(&mut [0]).await, Ok(0) | Err(_)));
        assert_eq!(status.borrow().strict_failures, 0);
        let mut failed = connect_as(&c, &a, 502).await;
        assert_eq!(failed.read(&mut [0]).await.unwrap(), 0);
        status.changed().await.unwrap();
        let s = *status.borrow_and_update();
        assert_eq!((s.strict_failures, s.sequence, s.closure), (1, 0, 0));
        assert_eq!(calls.load(Ordering::SeqCst), 3, "strict CONNECT must never dial public");
        h.set_direct_only(false);
        let mut reopened = connect_as(&c, &b, 200).await;
        echo(&mut reopened).await;
    })
    .await
    .unwrap();
}
