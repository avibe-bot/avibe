//! Whom an answer that took time is still for (`Pages::asker`): the window
//! whose page asked, and no window that has its label since. The document
//! inside that window is held to by its mark, in the window itself
//! (`page_delivery.js`, `ui/src/lib/native.direct.desktop.test.ts`).
use super::{Asker, Live, Page, Pages};
use crate::direct::{bridge::Ledger, handover_tests::public_browser, registered::Registered, Client};
use std::time::Instant;
use tokio::time::{timeout, Duration};

const HOST: &str = "one-app.avibe.bot";

fn page(token: &str) -> Page {
    Page {
        token: token.to_owned(),
        account: "account-1".to_owned(),
        origin: format!("https://{HOST}"),
        ledger: Ledger::default(),
        made: Instant::now(),
    }
}

/// A session whose listener's tunnel leg needs no network.
async fn live() -> (Client, Live) {
    let mut client = Client::new(HOST, "one".into()).await.unwrap();
    drop(public_browser(&mut client).await);
    let live = Live {
        paths: client.path_changes(),
        registered: Registered::new(client.handover()),
        handover: client.handover(),
    };
    (client, live)
}

#[tokio::test]
async fn an_answer_is_for_the_window_whose_page_asked_and_as_it_was_registered() {
    timeout(Duration::from_secs(6), async {
        let (_client, mut session) = live().await;
        let asked_as = session.registered.register("show-1").unwrap();
        let mut pages = Pages::default();
        pages.windows.insert("show-1".to_owned(), page("token-of-the-first"));
        pages.sessions.insert(7, session);

        // As it was: the answer is its own. With no registration to hold
        // it to (a window that had no session when it asked), the window
        // alone decides.
        assert_eq!(
            pages.asker("show-1", "token-of-the-first", Some(7), Some(&asked_as)),
            Asker::Same
        );
        assert_eq!(pages.asker("show-1", "token-of-the-first", None, None), Asker::Same);
        assert_eq!(pages.asker("show-1", "token-of-the-first", Some(7), None), Asker::Same);

        // No session routes it any more, or another one does: the window is
        // the same, and what was measured through its registration is no
        // longer its path.
        assert_eq!(
            pages.asker("show-1", "token-of-the-first", None, Some(&asked_as)),
            Asker::Reregistered
        );
        assert_eq!(
            pages.asker("show-1", "token-of-the-first", Some(8), Some(&asked_as)),
            Asker::Reregistered
        );
        // Registered anew under the same session (the label's registration
        // is another one now), likewise.
        let anew = pages
            .sessions
            .get_mut(&7)
            .unwrap()
            .registered
            .register("show-1")
            .unwrap();
        assert_eq!(
            pages.asker("show-1", "token-of-the-first", Some(7), Some(&asked_as)),
            Asker::Reregistered
        );
        assert_eq!(
            pages.asker("show-1", "token-of-the-first", Some(7), Some(&anew)),
            Asker::Same
        );

        // The window closed and another was made under its label: that one
        // has its own token, and nothing of the first one's answer is its.
        pages.windows.insert("show-1".to_owned(), page("token-of-the-second"));
        assert_eq!(
            pages.asker("show-1", "token-of-the-first", Some(7), Some(&anew)),
            Asker::Gone
        );
        assert_eq!(pages.asker("show-1", "token-of-the-first", None, None), Asker::Gone);
        // No window under the label at all.
        pages.windows.remove("show-1");
        assert_eq!(
            pages.asker("show-1", "token-of-the-first", Some(7), Some(&anew)),
            Asker::Gone
        );
        // A label never known.
        assert_eq!(pages.asker("editor-9", "token-of-the-first", None, None), Asker::Gone);
    })
    .await
    .expect("in time");
}
