//! Ask Vibey: a macOS Services selection goes into the main Bot's composer on
//! the instance the main window shows. It is never sent from here.
//!
//! Delivery is native `eval` into the shown Workbench document, the channel the
//! client-state doorbell already uses. Every call is fenced to the current
//! Runtime origin and to the identity of the receiver the document installed,
//! so it needs no Tauri command or capability and reaches local and remote
//! documents alike. The page side is `ui/src/desktop/desktopComposeReceiver.ts`
//! (installed by `DesktopCompose.tsx`); the two form one contract
//! (`window.__AVIBE_DESKTOP_COMPOSE__`, version 1).
use avibe_runtime_host::selection::{ordinary_open, unchanged, Input};
use base64::Engine as _;
use serde::Serialize;
use serde_json::json;
use std::collections::VecDeque;
use std::io::Read;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Mutex;
use std::time::Duration;
use tauri::{AppHandle, Manager, Webview};

/// The composer's own per-file limit (`MAX_WORKBENCH_ATTACHMENT_BYTES`).
pub const FILE_LIMIT: u64 = 100 * 1024 * 1024;
/// Raw bytes per eval; base64 makes each script about 256 KiB.
const CHUNK: usize = 192 * 1024;
const EVAL_TIMEOUT: Duration = Duration::from_secs(5);
/// How long the page may take to open Vibey's conversation and insert.
const INSERT_TIMEOUT: Duration = Duration::from_secs(30);
const RETRY_DELAY: Duration = Duration::from_secs(2);
/// A share that reached a document but never completed is retried this often
/// (with `RETRY_DELAY` between), which also covers a Workbench still loading.
const ATTEMPTS: u8 = 8;
const KEPT: crate::AskVibeyNotice = crate::AskVibeyNotice::Kept;
const UNAVAILABLE: crate::AskVibeyNotice = crate::AskVibeyNotice::Unavailable;

/// One file as the page needs to rebuild it, or why native refused it.
#[derive(Clone, Debug)]
pub enum Part {
    File {
        path: PathBuf,
        name: String,
        mime: String,
        size: u64,
        /// The file as selected: delivery reopens it and refuses any other
        /// file, link or contents found at the path by then.
        captured: std::fs::Metadata,
    },
    Refused {
        name: String,
        reason: &'static str,
    },
}

#[derive(Clone, Debug)]
pub enum Content {
    Text(String),
    Url(String),
    Files(Vec<Part>),
    /// The selection could not be read; the page reports it with existing copy.
    Unreadable,
}

#[derive(Debug)]
struct Share {
    id: String,
    content: Content,
    attempts: u8,
    kept_notice: bool,
}

#[derive(Default)]
pub struct Queue {
    shares: Mutex<VecDeque<Share>>,
    running: AtomicBool,
    /// Share ids only need to be distinct within this process: every receiver
    /// lives in a document of it.
    next: AtomicU64,
}

#[derive(Debug, PartialEq, Eq)]
enum Outcome {
    Done,
    /// No current Workbench document: sign-in, account page, unreachable or switching.
    NotReady,
    /// The receiver could not open Vibey's conversation.
    Unavailable,
    Retry,
}

/// What Services read becomes queued content. Files are checked now, against
/// the same limit the composer applies, so an oversized file never travels.
pub fn content(input: Input) -> Content {
    match input {
        Input::Text(text) => Content::Text(text),
        Input::Url(url) => Content::Url(url),
        Input::Files(files) => Content::Files(files.into_iter().map(part).collect()),
        Input::Unreadable => Content::Unreadable,
    }
}

fn part(path: PathBuf) -> Part {
    let name = path
        .file_name()
        .and_then(|name| name.to_str())
        .unwrap_or_default()
        .to_owned();
    let refused = |reason| Part::Refused {
        name: name.clone(),
        reason,
    };
    // An ordinary file, never through a final symlink, unchanged while it is
    // opened (`selection::ordinary_open`).
    let Ok(metadata) = ordinary_open(&path).and_then(|opened| opened.metadata()) else {
        return refused("unreadable");
    };
    if name.is_empty() {
        return refused("unreadable");
    }
    if metadata.len() == 0 {
        return refused("empty");
    }
    if metadata.len() > FILE_LIMIT {
        return refused("too_large");
    }
    Part::File {
        mime: mime(&name).to_owned(),
        path,
        name,
        size: metadata.len(),
        captured: metadata,
    }
}

/// Enough for the composer to show images as images; the server sniffs the rest.
fn mime(name: &str) -> &'static str {
    let extension = Path::new(name)
        .extension()
        .and_then(|value| value.to_str())
        .map(str::to_ascii_lowercase)
        .unwrap_or_default();
    match extension.as_str() {
        "png" => "image/png",
        "jpg" | "jpeg" => "image/jpeg",
        "gif" => "image/gif",
        "webp" => "image/webp",
        "heic" => "image/heic",
        "pdf" => "application/pdf",
        "txt" => "text/plain",
        "md" => "text/markdown",
        "csv" => "text/csv",
        "json" => "application/json",
        _ => "",
    }
}

#[derive(Serialize)]
struct Begin<'a> {
    id: &'a str,
    kind: &'static str,
    #[serde(skip_serializing_if = "Option::is_none")]
    text: Option<&'a str>,
    files: Vec<serde_json::Value>,
}

fn begin(id: &str, content: &Content) -> String {
    let (kind, text) = match content {
        Content::Text(text) => ("text", Some(text.as_str())),
        Content::Url(url) => ("url", Some(url.as_str())),
        Content::Files(_) => ("files", None),
        Content::Unreadable => ("unreadable", None),
    };
    let files = match content {
        Content::Files(parts) => parts
            .iter()
            .map(|part| match part {
                Part::File { name, mime, size, .. } => json!({"name":name,"type":mime,"size":size}),
                Part::Refused { name, reason } => json!({"name":name,"refused":reason}),
            })
            .collect(),
        _ => vec![],
    };
    serde_json::to_string(&Begin { id, kind, text, files }).expect("compose payload")
}

/// The receiver's document identity, read only from the top document at the
/// current Runtime `origin`; empty when there is no such receiver yet.
fn identify(origin: &str) -> String {
    let origin = serde_json::to_string(origin).expect("origin");
    format!(
        "(()=>{{if(window.self!==window.top||location.origin!=={origin})return '';\
const c=window.__AVIBE_DESKTOP_COMPOSE__;return c&&c.version===1&&typeof c.document==='string'?c.document:'';}})()"
    )
}

/// A script that runs `call` against the receiver only in the top document at
/// the current Runtime `origin` whose receiver is still `document`. It answers
/// `"stale"` for any other document, `"missing"` when it has no receiver (a
/// Workbench without Ask Vibey), else the call's own string answer.
fn fenced(document: &str, origin: &str, call: &str) -> String {
    let document = serde_json::to_string(document).expect("document");
    let origin = serde_json::to_string(origin).expect("origin");
    format!(
        "(()=>{{if(window.self!==window.top||location.origin!=={origin})return 'stale';\
const c=window.__AVIBE_DESKTOP_COMPOSE__;if(!c||c.version!==1)return 'missing';if(c.document!=={document})return 'stale';\
return String({call});}})()"
    )
}

fn chunks(size: u64) -> usize {
    (size as usize).div_ceil(CHUNK)
}

pub fn enqueue(app: &AppHandle, input: Input) {
    let Some(queue) = app.try_state::<Queue>() else {
        return;
    };
    queue.shares.lock().expect("compose queue").push_back(Share {
        id: format!("share-{}", queue.next.fetch_add(1, Ordering::SeqCst) + 1),
        content: content(input),
        attempts: 0,
        kept_notice: false,
    });
    // The main window comes forward at once, also when what it shows is a
    // sign-in or an unreachable instance; delivery follows when it can.
    let handle = app.clone();
    let _ = app.run_on_main_thread(move || crate::focus_or_restore_main_window(&handle));
    pump(app);
}

fn pump(app: &AppHandle) {
    let queue = app.state::<Queue>();
    if queue.running.swap(true, Ordering::SeqCst) {
        return;
    }
    let app = app.clone();
    tauri::async_runtime::spawn(async move {
        loop {
            let next = {
                let queue = app.state::<Queue>();
                let shares = queue.shares.lock().expect("compose queue");
                shares.front().map(|share| (share.id.clone(), share.content.clone()))
            };
            let Some((id, content)) = next else {
                let queue = app.state::<Queue>();
                queue.running.store(false, Ordering::SeqCst);
                // A share enqueued between the read and the store is picked up.
                if queue.shares.lock().expect("compose queue").is_empty() || queue.running.swap(true, Ordering::SeqCst)
                {
                    return;
                }
                continue;
            };
            let outcome = deliver(&app, &id, &content).await;
            let step = {
                let queue = app.state::<Queue>();
                let mut shares = queue.shares.lock().expect("compose queue");
                settle(&mut shares, &id, outcome)
            };
            if let Some(key) = step.notice {
                notify(&app, key);
            }
            if step.wait {
                tokio::time::sleep(RETRY_DELAY).await;
            }
        }
    });
}

#[derive(Debug, PartialEq, Eq)]
struct Step {
    notice: Option<crate::AskVibeyNotice>,
    wait: bool,
}

/// What one delivery attempt does to the queue: the front share leaves on
/// success or a final failure, and stays (in order) otherwise.
fn settle(shares: &mut VecDeque<Share>, id: &str, outcome: Outcome) -> Step {
    let none = Step {
        notice: None,
        wait: false,
    };
    let Some(share) = shares.front_mut().filter(|share| share.id == id) else {
        return none;
    };
    match outcome {
        Outcome::Done => {
            shares.pop_front();
            none
        }
        Outcome::Unavailable => {
            shares.pop_front();
            Step {
                notice: Some(UNAVAILABLE),
                wait: false,
            }
        }
        Outcome::NotReady => {
            let first = !std::mem::replace(&mut share.kept_notice, true);
            Step {
                notice: first.then_some(KEPT),
                wait: true,
            }
        }
        Outcome::Retry => {
            share.attempts += 1;
            if share.attempts >= ATTEMPTS {
                shares.pop_front();
                Step {
                    notice: Some(UNAVAILABLE),
                    wait: false,
                }
            } else {
                Step {
                    notice: None,
                    wait: true,
                }
            }
        }
    }
}

/// The shown Workbench document and the Runtime origin it must have, when one
/// is current.
fn current(app: &AppHandle) -> Option<(Webview, String)> {
    if !crate::instance_client::enabled(app) {
        // No instance client (development builds): only the local loopback Runtime.
        let view = app.get_webview(crate::MAIN_WINDOW)?;
        let url = view.url().ok()?;
        let origin = local_origin(&url)?;
        return Some((view, origin));
    }
    let (view, origin) = crate::instance_client::current_workbench(app)?;
    Some((view, origin.origin().ascii_serialization()))
}

fn local_origin(url: &url::Url) -> Option<String> {
    let loopback = matches!(url.host(), Some(url::Host::Ipv4(ip)) if ip.is_loopback())
        || matches!(url.host(), Some(url::Host::Ipv6(ip)) if ip.is_loopback())
        || url.host_str() == Some("localhost");
    (url.scheme() == "http" && loopback).then(|| url.origin().ascii_serialization())
}

/// Reopens every selected file and keeps only those still the file that was
/// selected; any other is refused as unreadable, with the existing copy.
fn reopen(content: &Content) -> (Content, Vec<Option<std::fs::File>>) {
    let Content::Files(parts) = content else {
        return (content.clone(), vec![]);
    };
    let mut handles = vec![];
    let parts = parts
        .iter()
        .map(|part| match part {
            Part::File {
                path, name, captured, ..
            } => {
                match ordinary_open(path)
                    .ok()
                    .filter(|file| file.metadata().is_ok_and(|now| unchanged(captured, &now)))
                {
                    Some(file) => {
                        handles.push(Some(file));
                        part.clone()
                    }
                    None => {
                        handles.push(None);
                        Part::Refused {
                            name: name.clone(),
                            reason: "unreadable",
                        }
                    }
                }
            }
            Part::Refused { .. } => {
                handles.push(None);
                part.clone()
            }
        })
        .collect();
    (Content::Files(parts), handles)
}

async fn eval(view: &Webview, script: String) -> Option<String> {
    let (tx, rx) = tokio::sync::oneshot::channel();
    let tx = Mutex::new(Some(tx));
    view.eval_with_callback(script, move |raw| {
        if let Some(tx) = tx.lock().expect("compose eval").take() {
            let _ = tx.send(serde_json::from_str::<String>(&raw).ok());
        }
    })
    .ok()?;
    tokio::time::timeout(EVAL_TIMEOUT, rx).await.ok()?.ok()?
}

async fn deliver(app: &AppHandle, id: &str, content: &Content) -> Outcome {
    let Some((view, origin)) = current(app) else {
        return Outcome::NotReady;
    };
    let label = view.label().to_owned();
    let live = || current(app).is_some_and(|(now, now_origin)| now.label() == label && now_origin == origin);
    // No receiver yet: still loading, or a Workbench without Ask Vibey.
    let token = match eval(&view, identify(&origin)).await {
        Some(token) if !token.is_empty() => token,
        Some(_) => return Outcome::Retry,
        None => return Outcome::NotReady,
    };
    let fence = |call: &str| fenced(&token, &origin, call);
    let id_json = serde_json::to_string(id).expect("id");
    let (content, mut files) = reopen(content);
    // The content goes out only while this document is still the current Runtime's.
    if !live() {
        return Outcome::NotReady;
    }
    let started = eval(&view, fence(&format!("c.begin({})", begin(id, &content)))).await;
    match started.as_deref() {
        Some("ok") => {}
        // Already committed in this document (a status poll was lost).
        Some("done") => return Outcome::Done,
        Some("unavailable") => return Outcome::Unavailable,
        Some("inserting") => return poll(&view, &fence(&format!("c.status({id_json})"))).await,
        Some("stale") | None => return Outcome::NotReady,
        // "missing": no receiver yet (still loading) or a Workbench without
        // Ask Vibey; retries end in the unavailable notice.
        Some(_) => return Outcome::Retry,
    }
    if let Content::Files(parts) = &content {
        for (index, part) in parts.iter().enumerate() {
            let (Part::File { size, captured, .. }, Some(file)) = (part, files[index].as_mut()) else {
                continue;
            };
            let mut buffer = vec![0; CHUNK];
            let mut sent = 0_u64;
            for _ in 0..chunks(*size) {
                let wanted = CHUNK.min((*size - sent) as usize);
                if file.read_exact(&mut buffer[..wanted]).is_err() || !live() {
                    return Outcome::Retry;
                }
                sent += wanted as u64;
                let data = base64::engine::general_purpose::STANDARD.encode(&buffer[..wanted]);
                if eval(&view, fence(&format!("c.chunk({id_json},{index},'{data}')")))
                    .await
                    .as_deref()
                    != Some("ok")
                {
                    return Outcome::Retry;
                }
            }
            // Changed while it was read: the next attempt refuses it.
            if !file.metadata().is_ok_and(|now| unchanged(captured, &now)) {
                return Outcome::Retry;
            }
        }
    }
    if !live() || eval(&view, fence(&format!("c.commit({id_json})"))).await.as_deref() != Some("ok") {
        return Outcome::Retry;
    }
    poll(&view, &fence(&format!("c.status({id_json})"))).await
}

/// Waits for the page to open Vibey's conversation and insert the share.
async fn poll(view: &Webview, status: &str) -> Outcome {
    let deadline = tokio::time::Instant::now() + INSERT_TIMEOUT;
    while tokio::time::Instant::now() < deadline {
        tokio::time::sleep(Duration::from_millis(250)).await;
        match eval(view, status.to_owned()).await.as_deref() {
            Some("done") => return Outcome::Done,
            Some("unavailable") => return Outcome::Unavailable,
            Some("inserting") => {}
            _ => return Outcome::Retry,
        }
    }
    Outcome::Retry
}

fn notify(app: &AppHandle, notice: crate::AskVibeyNotice) {
    use tauri_plugin_notification::NotificationExt;
    if app
        .try_state::<crate::notifications::Notifications>()
        .is_some_and(|notifications| !notifications.enabled())
    {
        return;
    }
    let body = crate::ask_vibey_notice(notice);
    let _ = app.notification().builder().title("Avibe").body(body).show();
}

#[cfg(test)]
mod tests {
    use super::*;

    fn selected(path: PathBuf) -> PathBuf {
        path
    }

    #[test]
    fn files_are_checked_against_the_composer_limit_before_they_travel() {
        let directory = tempfile::tempdir().unwrap();
        let ok = directory.path().join("报价单.pdf");
        std::fs::write(&ok, b"%PDF-1.7").unwrap();
        let empty = directory.path().join("empty.txt");
        std::fs::write(&empty, b"").unwrap();
        let large = directory.path().join("large.bin");
        std::fs::File::create(&large).unwrap().set_len(FILE_LIMIT + 1).unwrap();
        let folder = directory.path().join("folder");
        std::fs::create_dir(&folder).unwrap();
        let gone = directory.path().join("gone.txt");
        let parts = match content(Input::Files(
            [&ok, &empty, &large, &folder, &gone]
                .map(|p| selected(p.clone()))
                .into(),
        )) {
            Content::Files(parts) => parts,
            other => panic!("{other:?}"),
        };
        assert!(
            matches!(&parts[0], Part::File { path, name, mime, size: 8, .. }
                if *path == ok && name == "报价单.pdf" && mime == "application/pdf"),
            "{:?}",
            parts[0]
        );
        let reasons: Vec<_> = parts[1..]
            .iter()
            .map(|part| match part {
                Part::Refused { reason, .. } => *reason,
                other => panic!("{other:?}"),
            })
            .collect();
        assert_eq!(reasons, ["empty", "too_large", "unreadable", "unreadable"]);
    }

    #[test]
    fn a_file_at_the_limit_is_accepted() {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("edge.bin");
        std::fs::File::create(&path).unwrap().set_len(FILE_LIMIT).unwrap();
        assert!(matches!(part(selected(path)), Part::File { size: FILE_LIMIT, .. }));
    }

    #[test]
    fn the_begin_payload_carries_content_without_paths() {
        let text: serde_json::Value = serde_json::from_str(&begin("a", &Content::Text("「引」\n'x'".into()))).unwrap();
        assert_eq!(text, json!({"id":"a","kind":"text","text":"「引」\n'x'","files":[]}));
        let url: serde_json::Value =
            serde_json::from_str(&begin("b", &Content::Url("https://example.com/a".into()))).unwrap();
        assert_eq!(url["kind"], "url");
        let directory = tempfile::tempdir().unwrap();
        std::fs::write(directory.path().join("a"), b"abc").unwrap();
        let files = Content::Files(vec![
            Part::File {
                path: "/private/secret/a.png".into(),
                name: "a.png".into(),
                mime: "image/png".into(),
                size: 3,
                captured: std::fs::metadata(directory.path().join("a")).unwrap(),
            },
            Part::Refused {
                name: "big.mov".into(),
                reason: "too_large",
            },
        ]);
        let raw = begin("c", &files);
        assert!(!raw.contains("/private/secret"), "paths never reach the page: {raw}");
        let files: serde_json::Value = serde_json::from_str(&raw).unwrap();
        assert_eq!(
            files["files"],
            json!([{"name":"a.png","type":"image/png","size":3},{"name":"big.mov","refused":"too_large"}])
        );
        assert_eq!(
            serde_json::from_str::<serde_json::Value>(&begin("d", &Content::Unreadable)).unwrap()["kind"],
            "unreadable"
        );
    }

    #[test]
    fn every_call_is_fenced_to_one_top_document_of_the_runtime_origin_and_receiver() {
        let script = fenced("doc-\"1", "https://work.example", "c.commit(\"x\")");
        assert!(script.contains(r#"c.document!=="doc-\"1""#), "{script}");
        assert!(
            script.contains(r#"location.origin!=="https://work.example""#),
            "{script}"
        );
        assert!(script.contains("window.self!==window.top"));
        assert!(script.contains("return 'stale'") && script.contains("return 'missing'"));
        assert!(script.contains("c.version!==1"));
        assert!(script.ends_with("return String(c.commit(\"x\"));})()"));
        let identify = identify("https://work.example");
        assert!(
            identify.contains(r#"location.origin!=="https://work.example""#),
            "{identify}"
        );
        assert!(identify.contains("window.self!==window.top") && identify.contains("c.version===1"));
    }

    #[test]
    fn without_an_instance_client_only_the_local_loopback_runtime_is_a_target() {
        let origin = |raw: &str| local_origin(&url::Url::parse(raw).unwrap());
        assert_eq!(
            origin("http://127.0.0.1:5173/chat/ses_main"),
            Some("http://127.0.0.1:5173".into())
        );
        assert_eq!(origin("http://localhost:8080/"), Some("http://localhost:8080".into()));
        assert_eq!(origin("http://[::1]:8080/"), Some("http://[::1]:8080".into()));
        assert_eq!(origin("https://127.0.0.1/"), None);
        assert_eq!(origin("http://work.example/"), None);
        assert_eq!(origin("tauri://localhost/"), None);
    }

    fn reopened(content: &Content) -> Vec<String> {
        match reopen(content).0 {
            Content::Files(parts) => parts
                .iter()
                .map(|part| match part {
                    Part::File { name, .. } => format!("file:{name}"),
                    Part::Refused { name, reason } => format!("{reason}:{name}"),
                })
                .collect(),
            other => panic!("{other:?}"),
        }
    }

    #[test]
    fn a_symlinked_selection_is_refused_like_the_d1_import() {
        let directory = tempfile::tempdir().unwrap();
        let target = directory.path().join("target.txt");
        std::fs::write(&target, b"secret").unwrap();
        let link = directory.path().join("link.txt");
        std::os::unix::fs::symlink(&target, &link).unwrap();
        assert!(matches!(
            part(selected(link)),
            Part::Refused {
                reason: "unreadable",
                ..
            }
        ));
    }

    #[test]
    fn delivery_refuses_a_file_that_is_no_longer_the_one_selected() {
        let directory = tempfile::tempdir().unwrap();
        let path = |name: &str| directory.path().join(name);
        for name in ["same.txt", "grown.txt", "replaced.txt", "linked.txt", "gone.txt"] {
            std::fs::write(path(name), b"selected").unwrap();
        }
        let content = content(Input::Files(
            ["same.txt", "grown.txt", "replaced.txt", "linked.txt", "gone.txt"]
                .map(|name| selected(path(name)))
                .into(),
        ));
        // While queued: one file grows, one is replaced by another file at the
        // same path, one becomes a symlink, one is deleted.
        std::fs::OpenOptions::new()
            .append(true)
            .open(path("grown.txt"))
            .unwrap()
            .set_len(20)
            .unwrap();
        std::fs::remove_file(path("replaced.txt")).unwrap();
        std::fs::write(path("other.txt"), b"selected").unwrap();
        std::fs::rename(path("other.txt"), path("replaced.txt")).unwrap();
        std::fs::remove_file(path("linked.txt")).unwrap();
        std::os::unix::fs::symlink(path("same.txt"), path("linked.txt")).unwrap();
        std::fs::remove_file(path("gone.txt")).unwrap();
        assert_eq!(
            reopened(&content),
            [
                "file:same.txt",
                "unreadable:grown.txt",
                "unreadable:replaced.txt",
                "unreadable:linked.txt",
                "unreadable:gone.txt"
            ]
        );
        let (_, handles) = reopen(&content);
        assert_eq!(
            handles.iter().map(Option::is_some).collect::<Vec<_>>(),
            [true, false, false, false, false]
        );
    }

    fn queue(ids: &[&str]) -> VecDeque<Share> {
        ids.iter()
            .map(|id| Share {
                id: (*id).into(),
                content: Content::Text((*id).into()),
                attempts: 0,
                kept_notice: false,
            })
            .collect()
    }
    fn ids(shares: &VecDeque<Share>) -> Vec<&str> {
        shares.iter().map(|share| share.id.as_str()).collect()
    }

    #[test]
    fn while_no_instance_is_reachable_shares_stay_in_order_and_say_so_once() {
        let mut shares = queue(&["a", "b"]);
        assert_eq!(
            settle(&mut shares, "a", Outcome::NotReady),
            Step {
                notice: Some(KEPT),
                wait: true
            }
        );
        assert_eq!(
            settle(&mut shares, "a", Outcome::NotReady),
            Step {
                notice: None,
                wait: true
            }
        );
        assert_eq!(ids(&shares), ["a", "b"]);
        // Connected again: delivered in arrival order.
        assert_eq!(
            settle(&mut shares, "a", Outcome::Done),
            Step {
                notice: None,
                wait: false
            }
        );
        assert_eq!(
            settle(&mut shares, "b", Outcome::Done),
            Step {
                notice: None,
                wait: false
            }
        );
        assert!(shares.is_empty());
    }

    #[test]
    fn consecutive_shares_never_overtake_one_another() {
        let mut shares = queue(&["a", "b", "c"]);
        // A stale result for a share that is not at the front changes nothing.
        assert_eq!(
            settle(&mut shares, "b", Outcome::Done),
            Step {
                notice: None,
                wait: false
            }
        );
        assert_eq!(ids(&shares), ["a", "b", "c"]);
        settle(&mut shares, "a", Outcome::Retry);
        assert_eq!(ids(&shares), ["a", "b", "c"]);
    }

    #[test]
    fn an_instance_without_vibey_ends_the_share_with_the_unavailable_notice() {
        let mut shares = queue(&["a", "b"]);
        assert_eq!(
            settle(&mut shares, "a", Outcome::Unavailable),
            Step {
                notice: Some(UNAVAILABLE),
                wait: false
            }
        );
        assert_eq!(ids(&shares), ["b"]);
        for _ in 1..ATTEMPTS {
            assert_eq!(
                settle(&mut shares, "b", Outcome::Retry),
                Step {
                    notice: None,
                    wait: true
                }
            );
        }
        assert_eq!(
            settle(&mut shares, "b", Outcome::Retry),
            Step {
                notice: Some(UNAVAILABLE),
                wait: false
            }
        );
        assert!(shares.is_empty());
    }

    #[test]
    fn chunk_count_covers_every_byte() {
        assert_eq!(chunks(1), 1);
        assert_eq!(chunks(CHUNK as u64), 1);
        assert_eq!(chunks(CHUNK as u64 + 1), 2);
        assert_eq!(chunks(FILE_LIMIT), (FILE_LIMIT as usize).div_ceil(CHUNK));
    }
}
