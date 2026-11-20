//! The macOS Service "Ask Vibey" (zh-Hans 「问 Vibey」). This provider reads only
//! the pasteboard supplied to the service callback, never the general pasteboard
//! or the foreground selection, and hands the selection to the main Bot's
//! composer (`crate::compose`).
use avibe_runtime_host::selection::{Input, FILE_COUNT_LIMIT, TEXT_LIMIT, URL_LIMIT};
use objc2::rc::Retained;
use objc2::{define_class, msg_send, AnyThread, DefinedClass, MainThreadMarker};
use objc2_app_kit::{NSApplication, NSPasteboard};
use objc2_foundation::{NSObject, NSObjectProtocol, NSString, NSURL};
use std::{cell::RefCell, path::PathBuf};
use tauri::AppHandle;

struct State {
    app: AppHandle,
}
define_class!(
    #[unsafe(super = NSObject)]
    #[name = "AvibeAskVibeyService"]
    #[ivars = State]
    struct Provider;
    unsafe impl NSObjectProtocol for Provider {}
    impl Provider {
        // Info.plist NSServices: NSMessage "askVibey".
        #[unsafe(method(askVibey:userData:error:))]
        fn receive(&self, pasteboard: &NSPasteboard, _data: Option<&NSString>,
            _error: *mut *mut NSString)
        {
            crate::compose::enqueue(&self.ivars().app, selected(pasteboard));
        }
    }
);
thread_local! {
    static PROVIDER: RefCell<Option<Retained<Provider>>> = const { RefCell::new(None) };
}

/// One selection: files, or text, or one URL. Anything else, or anything over
/// the selection bounds, is unreadable; the composer side says so.
fn selected(pasteboard: &NSPasteboard) -> Input {
    read(pasteboard).unwrap_or(Input::Unreadable)
}

fn read(pasteboard: &NSPasteboard) -> Option<Input> {
    let items = pasteboard.pasteboardItems()?;
    if items.is_empty() || items.len() > FILE_COUNT_LIMIT {
        return None;
    }
    let file_type = NSString::from_str("public.file-url");
    let text_type = NSString::from_str("public.utf8-plain-text");
    let url_type = NSString::from_str("public.url");
    let mut files = vec![];
    let mut texts = vec![];
    let mut urls = vec![];
    let mut text_bytes = 0;
    for item in items.iter() {
        if let Some(value) = item.stringForType(&file_type) {
            files.push(file_path(&value)?);
        } else if let Some(value) = item.stringForType(&url_type) {
            if value.len() as u64 > URL_LIMIT {
                return None;
            }
            urls.push(value.to_string());
        } else if let Some(value) = item.stringForType(&text_type) {
            let value = value.to_string();
            text_bytes += value.len() as u64 + u64::from(!texts.is_empty());
            if text_bytes > TEXT_LIMIT {
                return None;
            }
            texts.push(value);
        } else {
            return None;
        }
    }
    match (files.is_empty(), texts.is_empty(), urls.is_empty()) {
        (false, true, true) => Some(Input::Files(files)),
        (true, false, true) => Some(Input::Text(texts.join("\n"))),
        (true, true, false) if urls.len() == 1 => urls.pop().map(Input::Url),
        _ => None,
    }
}

/// Finder supplies `public.file-url` as a file reference URL
/// (`file:///.file/id=…`), which is not a filesystem path. Foundation resolves
/// it to the selected file's path URL; a reference that no longer resolves
/// means the selected file moved or was deleted after selection.
fn file_path(value: &NSString) -> Option<PathBuf> {
    let mut url = NSURL::URLWithString(value)?;
    if url.isFileReferenceURL() {
        url = url.filePathURL()?;
    }
    let url = url::Url::parse(&url.absoluteString()?.to_string()).ok()?;
    if url.scheme() != "file"
        || url.query().is_some()
        || url.fragment().is_some()
        || !url.username().is_empty()
        || url.password().is_some()
    {
        return None;
    }
    url.to_file_path().ok()
}

pub fn install(app: &AppHandle) {
    let Some(mtm) = MainThreadMarker::new() else {
        return;
    };
    PROVIDER.with(|current| {
        if current.borrow().is_some() {
            return;
        }
        let allocated = Provider::alloc().set_ivars(State { app: app.clone() });
        let provider: Retained<Provider> = unsafe { msg_send![super(allocated), init] };
        unsafe {
            NSApplication::sharedApplication(mtm).setServicesProvider(Some(&provider));
        }
        *current.borrow_mut() = Some(provider);
    });
}

pub fn remove() {
    let Some(mtm) = MainThreadMarker::new() else {
        return;
    };
    PROVIDER.with(|current| {
        if current.borrow().is_some() {
            unsafe {
                NSApplication::sharedApplication(mtm).setServicesProvider(None);
            }
            current.borrow_mut().take();
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A private pasteboard holding one item, shaped like the one AppKit hands
    /// to the provider. It never touches the general pasteboard.
    fn pasteboard(values: &[(&str, &str)]) -> Retained<NSPasteboard> {
        let pasteboard = NSPasteboard::pasteboardWithUniqueName();
        pasteboard.clearContents();
        for (kind, value) in values {
            assert!(pasteboard.setString_forType(&NSString::from_str(value), &NSString::from_str(kind)));
        }
        pasteboard
    }
    fn select(values: &[(&str, &str)]) -> Input {
        let board = pasteboard(values);
        let input = selected(&board);
        let _: () = unsafe { msg_send![&*board, releaseGlobally] };
        input
    }

    #[test]
    fn finder_file_reference_url_resolves_to_the_selected_path() {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("share-file.txt");
        std::fs::write(&path, b"synthetic").unwrap();
        let path = path.canonicalize().unwrap();
        // Finder writes public.file-url as a file reference URL, not a path URL.
        let reference = NSURL::fileURLWithPath(&NSString::from_str(path.to_str().unwrap()))
            .fileReferenceURL()
            .and_then(|url| url.absoluteString())
            .unwrap()
            .to_string();
        assert!(reference.starts_with("file:///.file/id="), "{reference}");
        assert_eq!(select(&[("public.file-url", &reference)]), Input::Files(vec![path]));
    }

    #[test]
    fn plain_file_url_still_resolves_to_its_path() {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("plain file.txt");
        std::fs::write(&path, b"synthetic").unwrap();
        let path = path.canonicalize().unwrap();
        let url = url::Url::from_file_path(&path).unwrap().to_string();
        assert_eq!(select(&[("public.file-url", &url)]), Input::Files(vec![path]));
    }

    #[test]
    fn a_dangling_reference_or_a_non_file_url_on_the_file_type_is_unreadable() {
        for value in [
            "file:///.file/id=1.1",
            "https://example.com/a",
            "file:///tmp/a?x=1",
            "not a url",
        ] {
            assert_eq!(select(&[("public.file-url", value)]), Input::Unreadable, "{value}");
        }
    }

    #[test]
    fn selected_text_and_url_text_stay_text_and_a_url_item_is_a_url() {
        for value in ["W17 plain text", "https://example.com/w17-share-smoke"] {
            assert_eq!(select(&[("public.utf8-plain-text", value)]), Input::Text(value.into()));
        }
        assert_eq!(
            select(&[
                ("public.url", "https://example.com/w17"),
                ("public.utf8-plain-text", "https://example.com/w17")
            ]),
            Input::Url("https://example.com/w17".into())
        );
    }

    #[test]
    fn text_over_the_bound_is_unreadable() {
        let long = "x".repeat(TEXT_LIMIT as usize + 1);
        assert_eq!(select(&[("public.utf8-plain-text", &long)]), Input::Unreadable);
    }
}
