use super::Answering;

/// One answer for a label, the newest window's; taken off once; and gone
/// with a window that closed.
#[test]
fn a_label_has_one_challenge_answer_at_a_time() {
    let mut answering = Answering::new();
    assert_eq!(answering.take("show-1"), None);
    answering.put("show-1".to_owned(), 11);
    answering.put("show-2".to_owned(), 22);
    // The label is used again for a new window: its answer replaces the
    // old window's, which went with that window.
    answering.put("show-1".to_owned(), 33);
    assert_eq!(answering.take("show-1"), Some(33));
    assert_eq!(answering.take("show-1"), None);
    assert_eq!(answering.take("show-2"), Some(22));

    // A window that closed before its session ended is forgotten when it is
    // seen closed, not kept for the life of the app.
    answering.put("show-3".to_owned(), 44);
    answering.put("show-4".to_owned(), 55);
    answering.keep(|window| window != "show-3");
    assert_eq!(answering.take("show-3"), None);
    assert_eq!(answering.take("show-4"), Some(55));
}
