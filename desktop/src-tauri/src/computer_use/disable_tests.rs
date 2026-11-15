//! Hermetic request/reply tests shared by both production Controllers. No
//! AppHandle, desktop, driver, permissions or user state is accessed here.
use super::*;

fn controller() -> (Controller, mpsc::UnboundedReceiver<Event>) {
    let (sender, receiver) = mpsc::unbounded_channel();
    let view = MenuView {
        #[cfg(target_os = "windows")]
        available: true,
        ..MenuView::default()
    };
    (
        Controller {
            sender,
            view: Arc::new(Mutex::new(view)),
            shutdown_requested: Arc::new(AtomicBool::new(false)),
            #[cfg(target_os = "macos")]
            shutdown_started: AtomicBool::new(false),
        },
        receiver,
    )
}

#[tokio::test]
async fn disable_waits_for_transition_reply_and_propagates_failure() {
    for result in [Ok(()), Err("write/teardown failed".to_owned())] {
        let (controller, mut events) = controller();
        let mut request = Box::pin(controller.disable());
        assert!(tokio::time::timeout(Duration::from_millis(10), &mut request)
            .await
            .is_err());
        let Event::Disable(reply) = events.recv().await.unwrap() else {
            panic!("wrong event")
        };
        // Merely dequeueing is not acknowledgement either.
        assert!(tokio::time::timeout(Duration::from_millis(10), &mut request)
            .await
            .is_err());
        reply.send(result.clone()).unwrap();
        assert_eq!(request.await, result);
    }
}

#[tokio::test]
async fn unavailable_or_terminated_lifecycle_rejects_disable() {
    let (controller, events) = controller();
    drop(events);
    assert!(controller.disable().await.is_err());
    let (controller, mut events) = self::controller();
    let mut request = Box::pin(controller.disable());
    assert!(tokio::time::timeout(Duration::from_millis(10), &mut request)
        .await
        .is_err());
    let Event::Disable(reply) = events.recv().await.unwrap() else {
        panic!("wrong event")
    };
    drop(reply);
    assert!(request.await.is_err());
}

#[tokio::test]
async fn enable_disable_requests_are_fifo_and_disable_is_not_a_toggle() {
    let (controller, mut events) = controller();
    controller.turn_on().unwrap();
    let mut first_off = Box::pin(controller.disable());
    assert!(tokio::time::timeout(Duration::from_millis(10), &mut first_off)
        .await
        .is_err());
    controller.turn_on().unwrap();
    let mut last_off = Box::pin(controller.disable());
    assert!(tokio::time::timeout(Duration::from_millis(10), &mut last_off)
        .await
        .is_err());
    assert!(matches!(events.recv().await, Some(Event::Enable)));
    let Some(Event::Disable(first)) = events.recv().await else {
        panic!("expected first off")
    };
    assert!(matches!(events.recv().await, Some(Event::Enable)));
    let Some(Event::Disable(last)) = events.recv().await else {
        panic!("expected last off")
    };
    first.send(Ok(())).unwrap();
    last.send(Ok(())).unwrap();
    assert_eq!(first_off.await, Ok(()));
    assert_eq!(last_off.await, Ok(()));
}

#[tokio::test]
async fn ui_wait_timeout_does_not_retract_an_accepted_disable() {
    let (controller, mut events) = controller();
    assert!(tokio::time::timeout(Duration::from_millis(10), controller.disable())
        .await
        .is_err());
    let Event::Disable(reply) = events.recv().await.unwrap() else {
        panic!("request must remain queued")
    };
    // The lifecycle still executes the accepted transition; a vanished page
    // only drops delivery of its result, not the requested safety action.
    assert!(reply.send(Ok(())).is_err());
}
