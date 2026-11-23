//! A Show window is named after its Show. The page in it sets its document
//! title to the Show's current title (the UI's `StandaloneShowBody`), so the
//! window follows the document title: every entry (Dock, search, a deep link,
//! the `/apps/show` route) and every rename reach the title bar, the Window
//! menu and Mission Control.
//!
//! It names no crate item, so a probe can compile it from its source file.

use tauri::{Manager, Runtime, WebviewWindowBuilder};

/// Makes the window `builder` builds take its document's title as its own.
pub fn follow<'a, R: Runtime, M: Manager<R>>(
    builder: WebviewWindowBuilder<'a, R, M>,
) -> WebviewWindowBuilder<'a, R, M> {
    builder.on_document_title_changed(|window, title| {
        if let Some(title) = followed(&title) {
            let _ = window.set_title(title);
        }
    })
}

/// A document title the window takes: not blank (the blank first document
/// has one), and as bounded as a launch request's title.
fn followed(title: &str) -> Option<&str> {
    (!title.trim().is_empty() && title.len() <= 512 && !title.contains('\0')).then_some(title)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_show_window_takes_its_page_s_title_but_not_a_blank_or_oversized_one() {
        assert_eq!(followed("Q4 漏斗看板"), Some("Q4 漏斗看板"));
        assert_eq!(followed(""), None, "the blank first document");
        assert_eq!(followed("   "), None);
        assert_eq!(followed(&"x".repeat(513)), None);
        assert_eq!(followed("a\0b"), None);
    }
}
