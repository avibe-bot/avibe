//! The single native owner for standalone Apps. Page input chooses bounded
//! launch metadata; the published Runtime chooses authority, origin and store.
use crate::instance_client::{self, AppRuntime};
use serde::{Deserialize, Serialize};
use std::{
    collections::BTreeMap,
    sync::{
        atomic::{AtomicBool, AtomicU64, Ordering},
        Arc, Mutex, OnceLock,
    },
};
use tauri::{AppHandle, Manager, Webview, WebviewUrl, WebviewWindow, WebviewWindowBuilder};
use url::Url;

pub(crate) static LAUNCH_LOCK: tokio::sync::Mutex<()> = tokio::sync::Mutex::const_new(());
const WINDOW_PREFIX: &str = "avibe-app-";
const NEW_WINDOW_MARKER: &str = "--new-";
const MAX_REQUEST: usize = 32768;
const SAFE_INTEGER: u64 = 9_007_199_254_740_991;
const NATIVE_APP_WINDOW_MARKER: &str =
    "if (window.self === window.top) Object.defineProperty(window, '__AVIBE_NATIVE_APP_WINDOW__', { value: true });";
/// Asks the page whether a close would discard its unsaved state. The answer is
/// a JSON string so the WebKit bridge never has to serialize a page object.
const CLOSE_GUARD_QUERY: &str = "(()=>{try{const g=window.__AVIBE_APP_WINDOW_CLOSE_GUARD__;const c=typeof g==='function'?g():null;return c?JSON.stringify({message:c.message,confirm:c.confirm,cancel:c.cancel}):''}catch(_){return ''}})()";
/// A page that does not answer in time closes as it did before it could ask.
const CLOSE_GUARD_TIMEOUT: std::time::Duration = std::time::Duration::from_secs(2);
const CLOSE_GUARD_TEXT: usize = 512;
pub(crate) const PROTOCOL_SCRIPT: &str = "if(window.self===window.top){Object.defineProperty(window,'__AVIBE_APP_LAUNCH_PROTOCOL__',{value:1});Object.defineProperty(window,'__AVIBE_APP_DOCUMENT__',{value:crypto.randomUUID()});}";

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub(crate) struct Request {
    pub app_id: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub session_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub title: Option<String>,
    pub new_window: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub source: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub mode: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub origin: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub request_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub params: Option<Params>,
}
#[derive(Clone, Debug, Default, Deserialize, Serialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub(crate) struct Params {
    #[serde(skip_serializing_if = "Option::is_none")]
    path: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    cwd: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    filename: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    name: Option<String>,
    #[serde(
        default,
        deserialize_with = "optional_mtime",
        skip_serializing_if = "Option::is_none"
    )]
    mtime: Option<Option<f64>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    new_file_dir: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    root_dir: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    mime: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    line: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    column: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    end_column: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    initial_tab: Option<String>,
}
fn optional_mtime<'de, D: serde::Deserializer<'de>>(deserializer: D) -> Result<Option<Option<f64>>, D::Error> {
    Option::<f64>::deserialize(deserializer).map(Some)
}
fn bounded(value: &str, max: usize) -> bool {
    value.len() <= max && !value.contains('\0')
}
fn request_id(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 128
        && value
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'-' | b'_'))
}
impl Request {
    fn target(&self, protocol: bool) -> Result<AppWindowTarget, String> {
        let target = parse_target(&self.app_id, self.session_id.as_deref())?;
        if self.title.as_ref().is_some_and(|s| !bounded(s, 512)) {
            return Err("invalid_app_launch".into());
        }
        let rich = protocol
            || self.source.is_some()
            || self.mode.is_some()
            || self.origin.is_some()
            || self.request_id.is_some()
            || self.params.is_some();
        if rich
            && !(self.request_id.as_deref().is_some_and(request_id)
                && self.origin.as_ref().is_some_and(|s| bounded(s, 4096))
                && self.source.as_deref().is_some_and(|s| {
                    matches!(
                        s,
                        "dock"
                            | "apps-launcher"
                            | "library"
                            | "command-palette"
                            | "dock-shortcut"
                            | "show-control"
                            | "show-route"
                            | "file-browser"
                            | "chat-attachment"
                            | "deep-link"
                    )
                })
                && self.mode.as_deref() == Some(if self.new_window { "newWindow" } else { "activate" }))
        {
            return Err("invalid_app_launch".into());
        }
        if let Some(params) = &self.params {
            params.validate(&self.app_id)?;
        }
        Ok(target)
    }
}
impl Params {
    fn validate(&self, app: &str) -> Result<(), String> {
        for value in [&self.path, &self.cwd, &self.new_file_dir, &self.root_dir]
            .into_iter()
            .flatten()
        {
            if !bounded(value, 4096) {
                return Err("invalid_app_launch".into());
            }
        }
        for value in [&self.filename, &self.name].into_iter().flatten() {
            if !bounded(value, 512) {
                return Err("invalid_app_launch".into());
            }
        }
        if self.mime.as_ref().is_some_and(|s| !bounded(s, 256))
            || self.mtime.flatten().is_some_and(|n| !n.is_finite())
            || self.line.is_some_and(|n| n == 0 || n > SAFE_INTEGER)
            || self.column.is_some_and(|n| n > SAFE_INTEGER)
            || self
                .end_column
                .is_some_and(|n| n > SAFE_INTEGER || n < self.column.unwrap_or(0))
            || self
                .initial_tab
                .as_deref()
                .is_some_and(|s| !matches!(s, "apps" | "showpages"))
        {
            return Err("invalid_app_launch".into());
        }
        let keys = serde_json::to_value(self).expect("params");
        for key in keys.as_object().expect("params object").keys() {
            let allowed = match app {
                "files" => matches!(key.as_str(), "path" | "rootDir"),
                "terminal" => key == "cwd",
                "editor" => matches!(
                    key.as_str(),
                    "path" | "filename" | "mtime" | "newFileDir" | "rootDir" | "line" | "column" | "endColumn"
                ),
                "preview" => matches!(key.as_str(), "path" | "name" | "mime" | "mtime"),
                "library" => key == "initialTab",
                _ => false,
            };
            if !allowed {
                return Err("invalid_app_launch".into());
            }
        }
        Ok(())
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) enum AppWindowTarget {
    Builtin(&'static str),
    ShowPage(String),
}
impl AppWindowTarget {
    fn base_label(&self) -> String {
        match self {
            Self::Builtin(id) => format!("{WINDOW_PREFIX}{id}"),
            Self::ShowPage(id) => format!("{WINDOW_PREFIX}show-{}", hex_encode(id.as_bytes())),
        }
    }
}
pub(crate) fn parse_target(app: &str, session: Option<&str>) -> Result<AppWindowTarget, String> {
    match (app, session) {
        ("files", None) => Ok(AppWindowTarget::Builtin("files")),
        ("terminal", None) => Ok(AppWindowTarget::Builtin("terminal")),
        ("editor", None) => Ok(AppWindowTarget::Builtin("editor")),
        ("preview", None) => Ok(AppWindowTarget::Builtin("preview")),
        ("library", None) => Ok(AppWindowTarget::Builtin("library")),
        ("showpage", Some(id)) if valid_show_session_id(id.trim()) => Ok(AppWindowTarget::ShowPage(id.trim().into())),
        _ => Err("invalid_app_launch".into()),
    }
}
fn valid_show_session_id(id: &str) -> bool {
    id.len() <= 128
        && id.bytes().next().is_some_and(|b| b.is_ascii_alphanumeric())
        && id
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'_' | b'.' | b':' | b'-'))
}
pub(crate) fn target_url(origin: &Url, target: &AppWindowTarget) -> Url {
    let mut url = origin.clone();
    match target {
        AppWindowTarget::Builtin(id) => url.set_path(&format!("/apps/{id}")),
        AppWindowTarget::ShowPage(id) => url.set_path(&format!("/apps/show/{}", id.replace(':', "%3A"))),
    }
    url.set_query(Some("standalone=1"));
    url.set_fragment(None);
    url
}
pub(crate) fn is_app_window(label: &str) -> bool {
    label.starts_with(WINDOW_PREFIX)
}
fn same_origin(origin: &Url, url: &Url) -> bool {
    matches!(url.scheme(), "http" | "https")
        && origin.origin() == url.origin()
        && url.username().is_empty()
        && url.password().is_none()
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub(crate) enum Status {
    Opened,
    Fallback,
    Stale,
}
#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct Outcome {
    pub request_id: String,
    pub status: Status,
}
#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
struct Snapshot {
    request_id: String,
    window_id: String,
    app_id: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    session_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    title: Option<String>,
    source: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    params: Option<Params>,
}
impl Snapshot {
    fn new(request: &Request, label: &str) -> Self {
        Self {
            request_id: request
                .request_id
                .clone()
                .unwrap_or_else(|| format!("legacy-{}", NEXT.fetch_add(1, Ordering::SeqCst))),
            window_id: label.into(),
            app_id: request.app_id.clone(),
            session_id: request.session_id.clone(),
            title: request.title.clone(),
            source: request.source.clone().unwrap_or_else(|| "dock".into()),
            params: request.params.clone(),
        }
    }
}
static NEXT: AtomicU64 = AtomicU64::new(1);

/// Resource-table identity fences native handles even if Tauri reuses a label.
#[derive(Default)]
pub(crate) struct Document {
    resource: OnceLock<tauri::ResourceId>,
    generation: AtomicU64,
    outcomes: Mutex<BTreeMap<(u64, String), Status>>,
}
impl tauri::Resource for Document {}
impl Document {
    fn bind(self: &Arc<Self>, view: &Webview) {
        self.resource
            .get_or_init(|| view.resources_table().add_arc(self.clone()));
    }
    fn owns(self: &Arc<Self>, view: &Webview) -> bool {
        self.resource.get().is_some_and(|id| {
            view.resources_table()
                .get::<Self>(*id)
                .is_ok_and(|v| Arc::ptr_eq(&v, self))
        })
    }
}
#[derive(Default)]
struct Registry {
    documents: BTreeMap<String, Arc<Document>>,
    windows: BTreeMap<String, Arc<AppWindow>>,
}
#[derive(Default)]
struct Windows(Mutex<Registry>);
fn registry(app: &AppHandle) -> tauri::State<'_, Windows> {
    app.manage(Windows::default());
    app.state::<Windows>()
}
pub(crate) fn document(app: &AppHandle, label: &str) -> Arc<Document> {
    let doc = Arc::new(Document::default());
    let reg = registry(app);
    let mut reg = reg.0.lock().expect("app windows");
    // Closed Runtime resources have no future receiver. In-flight work keeps
    // its own Arc and still has to pass the native resource/document fence.
    reg.documents.retain(|label, _| app.get_webview(label).is_some());
    reg.documents.insert(label.into(), doc.clone());
    doc
}
pub(crate) fn document_load(view: &Webview, doc: &Arc<Document>, started: bool) {
    doc.bind(view);
    if started {
        doc.generation.fetch_add(1, Ordering::SeqCst);
        doc.outcomes.lock().expect("outcomes").clear();
    }
}
struct AppWindow {
    runtime: AppRuntime,
    document: Arc<Document>,
    target: AppWindowTarget,
    snapshot: Mutex<Snapshot>,
    directory: Mutex<Option<tempfile::TempDir>>,
    retired: AtomicBool,
    /// A close or quit is asking this window's page; one question at a time.
    asking: AtomicBool,
    cleanup: tokio::sync::Mutex<Option<Result<(), String>>>,
}
#[derive(Clone)]
struct Source {
    runtime: AppRuntime,
    view: Webview,
    document: Arc<Document>,
    generation: u64,
    delivery: Option<avibe_runtime_host::deep_link::DeepLinkNavigation>,
    child: Option<Arc<AppWindow>>,
}
impl Source {
    fn capture(view: &Webview) -> Option<Self> {
        let app = view.app_handle();
        let reg = registry(app);
        let reg = reg.0.lock().ok()?;
        let doc = reg.documents.get(view.label())?.clone();
        if !doc.owns(view) {
            return None;
        }
        let child = reg.windows.get(view.label()).cloned();
        drop(reg);
        let runtime = if let Some(child) = &child {
            if child.retired.load(Ordering::SeqCst)
                || !child.runtime.current(app)
                || !view.url().is_ok_and(|url| same_origin(&child.runtime.origin, &url))
            {
                return None;
            }
            child.runtime.clone()
        } else {
            instance_client::app_runtime(view)?
        };
        Some(Self {
            runtime,
            view: view.clone(),
            generation: doc.generation.load(Ordering::SeqCst),
            document: doc,
            delivery: None,
            child,
        })
    }
    fn current(&self) -> bool {
        if self
            .child
            .as_ref()
            .is_some_and(|entry| entry.retired.load(Ordering::SeqCst))
        {
            return false;
        }
        self.delivery.as_ref().is_none_or(|delivery| {
            self.view
                .app_handle()
                .state::<Mutex<crate::DeepLinks>>()
                .lock()
                .is_ok_and(|links| {
                    links.owns_app_launch(delivery)
                        && links.pending_for_owner(instance_client::presentation_owner(self.view.app_handle()))
                })
        }) && self.runtime.current(self.view.app_handle())
            && self.document.owns(&self.view)
            && self.document.generation.load(Ordering::SeqCst) == self.generation
            && self.view.url().is_ok_and(|url| same_origin(&self.runtime.origin, &url))
    }
}

fn snapshot_script(snapshot: &Snapshot, event: bool) -> String {
    let json = serde_json::to_string(snapshot).expect("snapshot");
    format!("if(window.self===window.top){{const value={json};window.__AVIBE_APP_LAUNCH__=value;try{{sessionStorage.setItem('__avibe_app_launch',JSON.stringify(value))}}catch{{}}{} }}",if event {"window.dispatchEvent(new CustomEvent('avibe:app-launch',{detail:value}));"} else {""})
}
fn initial_snapshot(snapshot: &Snapshot) -> String {
    let json = serde_json::to_string(snapshot).expect("snapshot");
    format!("if(window.self===window.top){{let value={json};try{{const saved=JSON.parse(sessionStorage.getItem('__avibe_app_launch')||'null');if(saved?.windowId===value.windowId)value=saved;}}catch{{}}window.__AVIBE_APP_LAUNCH__=value;}}")
}
fn parse_navigation(url: &Url) -> Option<Request> {
    if url.scheme() != "avibe-app"
        || url.host_str() != Some("launch")
        || !url.path().is_empty()
        || !url.username().is_empty()
        || url.password().is_some()
        || url.port().is_some()
        || url.fragment().is_some()
        || url.as_str().len() > MAX_REQUEST * 3
    {
        return None;
    }
    let pairs: Vec<_> = url.query_pairs().collect();
    if pairs.len() != 1 || pairs[0].0 != "request" || pairs[0].1.len() > MAX_REQUEST {
        return None;
    }
    serde_json::from_str(&pairs[0].1).ok()
}
/// Both per-WebView and plugin paths call this; the first consumes the scheme.
/// Native WebView identity, not a page marker, admits the request.
pub(crate) fn navigation(view: &Webview, url: &Url) -> Option<bool> {
    if url.scheme() != "avibe-app" {
        return None;
    }
    let Some(request) = parse_navigation(url) else {
        return Some(false);
    };
    let Some(id) = request
        .request_id
        .as_deref()
        .filter(|id| request_id(id))
        .map(str::to_owned)
    else {
        return Some(false);
    };
    let Some(source) = Source::capture(view) else {
        return Some(false);
    };
    let source_for_callback = source.clone();
    // Correlate the result to the JS document too: queued eval cannot deliver
    // fallback into an exact-URL reload between native inspection and execution.
    let _=view.eval_with_callback("window.__AVIBE_APP_DOCUMENT__",move |raw| {
        let Some(token)=document_token_result(&raw) else {return};
        if !source_for_callback.current() {return;}
        let source_for_callback = source_for_callback.clone();
        let request = request.clone();
        let id = id.clone();
        tauri::async_runtime::spawn(async move {
            let status=launch(&source_for_callback,&request,true).await;
            if !source_for_callback.current() {return;}
            let value=serde_json::to_string(&Outcome{request_id:id,status}).expect("outcome");
            let token=serde_json::to_string(&token).expect("token");
            let _=source_for_callback.view.eval(format!("if(window.self===window.top&&window.__AVIBE_APP_DOCUMENT__==={token})window.dispatchEvent(new CustomEvent('avibe:app-launch-result',{{detail:{value}}}));"));
        });
    });
    Some(false)
}
pub(crate) fn navigation_for(app: &AppHandle, label: &str, doc: &Arc<Document>, url: &Url) -> bool {
    if url.scheme() != "avibe-app" {
        return true;
    }
    if let Some(view) = app.get_webview(label).filter(|view| doc.owns(view)) {
        navigation(&view, url);
    }
    false
}
pub(crate) fn child_navigation(view: &Webview, url: &Url) -> bool {
    let entry = registry(view.app_handle())
        .0
        .lock()
        .expect("app windows")
        .windows
        .get(view.label())
        .cloned();
    entry.is_some_and(|entry| {
        !entry.retired.load(Ordering::SeqCst)
            && entry.document.owns(view)
            && entry.runtime.current(view.app_handle())
            && (url.as_str() == "about:blank" || same_origin(&entry.runtime.origin, url))
    })
}
pub(crate) async fn open_legacy(view: &Webview, request: Request) -> Result<(), String> {
    let source = Source::capture(view).ok_or_else(|| "stale_app_launch".to_owned())?;
    match launch(&source, &request, false).await {
        Status::Fallback => Err("invalid_app_launch".into()),
        // Legacy callers treat any rejection as fallback. A stale request must
        // be consumed instead of reopening against a successor Runtime.
        Status::Opened | Status::Stale => Ok(()),
    }
}
pub(crate) struct ShowLaunch {
    source: Source,
    request: Request,
}
pub(crate) fn prepare_show(
    view: &Webview,
    session: &str,
    delivery: avibe_runtime_host::deep_link::DeepLinkNavigation,
) -> Option<ShowLaunch> {
    let mut source = Source::capture(view)?;
    source.delivery = Some(delivery);
    let request = Request {
        app_id: "showpage".into(),
        session_id: Some(session.into()),
        title: None,
        new_window: false,
        source: Some("deep-link".into()),
        mode: Some("activate".into()),
        origin: Some(source.runtime.origin.origin().ascii_serialization()),
        request_id: Some(format!("deep-link-{}", NEXT.fetch_add(1, Ordering::SeqCst))),
        params: None,
    };
    Some(ShowLaunch { source, request })
}
pub(crate) async fn open_show(launch_request: ShowLaunch) -> Status {
    let ShowLaunch { source, request } = launch_request;
    let token = document_token(&source.view).await;
    if !source.current() {
        return Status::Stale;
    }
    let status = launch(&source, &request, true).await;
    if !source.current() {
        return Status::Stale;
    }
    if status == Status::Fallback {
        let Some(_windows) = instance_client::lock_app_runtime(source.view.app_handle()).await else {
            return Status::Stale;
        };
        if !source.current() {
            return Status::Stale;
        }
        if let Some(token) = token {
            let snapshot = Snapshot::new(&request, source.view.label());
            let script = snapshot_script(&snapshot, true);
            let token = serde_json::to_string(&token).expect("token");
            let (tx, rx) = tokio::sync::oneshot::channel();
            let tx = Mutex::new(Some(tx));
            let submitted = source
                .view
                .eval_with_callback(
                    format!(
                        "(()=>{{if(window.__AVIBE_APP_DOCUMENT__!=={token})return false;{script};return true;}})()"
                    ),
                    move |raw| {
                        if let Some(tx) = tx.lock().expect("fallback deposit").take() {
                            let _ = tx.send(raw == "true");
                        }
                    },
                )
                .is_ok();
            let deposited = submitted
                && tokio::time::timeout(std::time::Duration::from_secs(5), rx)
                    .await
                    .is_ok_and(|value| value == Ok(true));
            if !deposited || !source.current() {
                return Status::Stale;
            }
        } else {
            return Status::Stale;
        }
    }
    status
}
async fn document_token(view: &Webview) -> Option<String> {
    let (tx, rx) = tokio::sync::oneshot::channel();
    let tx = Mutex::new(Some(tx));
    view.eval_with_callback("window.__AVIBE_APP_DOCUMENT__", move |raw| {
        if let Some(tx) = tx.lock().expect("document token").take() {
            let _ = tx.send(document_token_result(&raw));
        }
    })
    .ok()?;
    tokio::time::timeout(std::time::Duration::from_secs(5), rx)
        .await
        .ok()?
        .ok()?
}
fn document_token_result(raw: &str) -> Option<String> {
    serde_json::from_str::<String>(raw)
        .ok()
        .filter(|token| !token.is_empty())
}
async fn launch(source: &Source, request: &Request, protocol: bool) -> Status {
    let status = launch_serialized(source, request, protocol).await;
    // A link received during an App build remains with the existing pending
    // owner. Wake it after both serializers are released, on every exit.
    crate::apply_pending_deep_link(source.view.app_handle());
    status
}
async fn launch_serialized(source: &Source, request: &Request, protocol: bool) -> Status {
    let _launch = LAUNCH_LOCK.lock().await;
    if !source.current() {
        return Status::Stale;
    }
    if let Some(id) = &request.request_id {
        if let Some(status) = source
            .document
            .outcomes
            .lock()
            .expect("outcomes")
            .get(&(source.generation, id.clone()))
        {
            return *status;
        }
    }
    let status = launch_owned(source, request, protocol).await;
    if let Some(id) = &request.request_id {
        let mut outcomes = source.document.outcomes.lock().expect("outcomes");
        if source.document.generation.load(Ordering::SeqCst) == source.generation {
            outcomes.insert((source.generation, id.clone()), status);
        }
    }
    status
}
async fn launch_owned(source: &Source, request: &Request, protocol: bool) -> Status {
    let Ok(target) = request.target(protocol) else {
        return Status::Fallback;
    };
    if request
        .origin
        .as_deref()
        .is_some_and(|expected| expected != source.runtime.origin.origin().ascii_serialization())
    {
        return Status::Fallback;
    }
    let app = source.view.app_handle();
    let Some(_windows) = instance_client::lock_app_runtime(app).await else {
        return Status::Stale;
    };
    if !source.current() {
        return Status::Stale;
    }
    let existing = if request.new_window {
        None
    } else {
        let reg = registry(app);
        let reg = reg.0.lock().expect("app windows");
        let mut matches: Vec<_> = reg
            .windows
            .iter()
            .filter(|(_, e)| {
                !e.retired.load(Ordering::SeqCst) && e.target == target && e.runtime.same_document(&source.runtime)
            })
            .map(|(label, e)| (label.clone(), e.clone()))
            .collect();
        matches.sort_by_key(|(label, _)| (label != &target.base_label(), label.clone()));
        matches
            .into_iter()
            .find(|(label, _)| app.get_webview_window(label).is_some())
    };
    if let Some((label, entry)) = existing {
        let window = app.get_webview_window(&label).expect("registered app window");
        let snapshot = Snapshot::new(request, &label);
        *entry.snapshot.lock().expect("snapshot") = snapshot.clone();
        if let Some(title) = request.title.as_deref().filter(|title| !title.trim().is_empty()) {
            let _ = window.set_title(title);
        }
        // The destination stays unchanged: repeated Editor/Terminal requests
        // update their consumer without replacing the live document or buffers.
        let _ = window.eval(snapshot_script(&snapshot, true));
        activate(&window);
        return if source.current() {
            Status::Opened
        } else {
            Status::Stale
        };
    }
    let base = target.base_label();
    let occupied = registry(app).0.lock().expect("app windows").windows.contains_key(&base);
    let label = if request.new_window || occupied || app.get_window(&base).is_some() {
        format!("{base}{NEW_WINDOW_MARKER}{}", NEXT.fetch_add(1, Ordering::SeqCst))
    } else {
        base
    };
    let directory = if source.runtime.disposable {
        let Ok(root) = app.path().app_local_data_dir().map(|path| path.join("instance-stores")) else {
            return Status::Fallback;
        };
        if std::fs::create_dir_all(&root).is_err() {
            return Status::Fallback;
        }
        match tempfile::tempdir_in(root) {
            Ok(dir) => Some(dir),
            Err(_) => return Status::Fallback,
        }
    } else {
        None
    };
    let path = directory.as_ref().map(|dir| dir.path().to_owned());
    let doc = document(app, &label);
    let snapshot = Snapshot::new(request, &label);
    let entry = Arc::new(AppWindow {
        runtime: source.runtime.clone(),
        document: doc.clone(),
        target: target.clone(),
        snapshot: Mutex::new(snapshot.clone()),
        directory: Mutex::new(directory),
        retired: AtomicBool::new(false),
        asking: AtomicBool::new(false),
        cleanup: tokio::sync::Mutex::new(None),
    });
    registry(app)
        .0
        .lock()
        .expect("app windows")
        .windows
        .insert(label.clone(), entry.clone());
    let app_for_nav = app.clone();
    let label_for_nav = label.clone();
    let doc_for_nav = doc.clone();
    let loaded_entry = entry.clone();
    let mut builder =
        WebviewWindowBuilder::new(app, &label, WebviewUrl::External("about:blank".parse().expect("blank")))
            .title(
                request
                    .title
                    .as_deref()
                    .filter(|s| !s.trim().is_empty())
                    .unwrap_or("Avibe"),
            )
            .inner_size(default_size(&target).0, default_size(&target).1)
            .min_inner_size(default_size(&target).0.min(720.), default_size(&target).1.min(480.))
            .center()
            .resizable(true)
            .decorations(true)
            .disable_drag_drop_handler()
            .visible(false)
            .focused(false)
            .incognito(source.runtime.disposable)
            .initialization_script(NATIVE_APP_WINDOW_MARKER)
            .initialization_script(PROTOCOL_SCRIPT)
            .initialization_script(initial_snapshot(&snapshot))
            .on_navigation(move |url| navigation_for(&app_for_nav, &label_for_nav, &doc_for_nav, url))
            .on_new_window(|url, _| crate::handle_new_window_request(url))
            .on_page_load(move |view, event| {
                document_load(
                    view.as_ref(),
                    &loaded_entry.document,
                    event.event() == tauri::webview::PageLoadEvent::Started,
                );
                if event.event() == tauri::webview::PageLoadEvent::Finished
                    && same_origin(&loaded_entry.runtime.origin, event.url())
                    && loaded_entry.runtime.current(view.app_handle())
                {
                    let snapshot = loaded_entry.snapshot.lock().expect("snapshot").clone();
                    let _ = view.eval(snapshot_script(&snapshot, true));
                }
            });
    if let Some(path) = path {
        builder = builder.data_directory(path);
    }
    if let Some(proxy) = app
        .config()
        .app
        .windows
        .iter()
        .find(|window| window.label == crate::MAIN_WINDOW)
        .and_then(|window| window.proxy_url.clone())
    {
        builder = builder.proxy_url(proxy);
    }

    let window = match builder.build() {
        Ok(window) => window,
        Err(_) => {
            // A failed builder may still leave a native window. Its lifetime
            // must complete before the caller can take Workbench fallback.
            if let Some(window) = app.get_webview_window(&label) {
                entry.document.bind(window.as_ref());
                return discard(app, &label, entry, Status::Fallback).await;
            }
            if app.get_window(&label).is_some() {
                entry.retired.store(true, Ordering::SeqCst);
                return Status::Opened;
            }
            let reg = registry(app);
            let mut reg = reg.0.lock().expect("app windows");
            reg.windows.remove(&label);
            reg.documents.remove(&label);
            return Status::Fallback;
        }
    };
    entry.document.bind(window.as_ref());
    let cleanup_app = app.clone();
    let cleanup_label = label.clone();
    let cleanup_entry = entry.clone();
    window.on_window_event(move |event| {
        if let tauri::WindowEvent::CloseRequested { api, .. } = event {
            // User close uses the same store-completion owner as replacement,
            // after the page agrees to lose any unsaved state.
            api.prevent_close();
            let app = cleanup_app.clone();
            let label = cleanup_label.clone();
            let entry = cleanup_entry.clone();
            tauri::async_runtime::spawn(async move {
                settle_close(
                    &entry.retired,
                    &entry.asking,
                    async {
                        match app.get_webview_window(&label) {
                            Some(window) => confirm_discard(&app, &window).await,
                            None => true,
                        }
                    },
                    || async {
                        let _ = retire(&app, &label, &entry).await;
                    },
                )
                .await;
            });
        }
        if matches!(event, tauri::WindowEvent::Destroyed) {
            // Destruction is not evidence of cookie erasure. Keep the receipt
            // and directory until the existing retirement operation completes.
            cleanup_entry.retired.store(true, Ordering::SeqCst);
        }
    });
    let projected = if source.runtime.disposable {
        let runtime = source.runtime.webview(app);
        let destination = target_url(&source.runtime.origin, &target);
        let child = window.as_ref().clone();
        let epoch = source.runtime.epoch.clone();
        let projected_source = source.clone();
        tokio::task::spawn_blocking(move || {
            let parent = runtime.ok_or_else(|| "stale".to_owned())?;
            epoch.check().map_err(|_| "stale".to_owned())?;
            let cookies = parent.cookies_for_url(destination).map_err(|_| "cookies".to_owned())?;
            if !projected_source.current() {
                return Err("stale".to_owned());
            }
            crate::instance_store::project(&child, &cookies, &epoch).map_err(|_| "projection".to_owned())
        })
        .await
        .is_ok_and(|result| result.is_ok())
    } else {
        true
    };
    let current = source.current();
    if !projected || !current {
        return discard(
            app,
            &label,
            entry,
            if current { Status::Fallback } else { Status::Stale },
        )
        .await;
    }
    if window.navigate(target_url(&source.runtime.origin, &target)).is_err() {
        return discard(app, &label, entry, Status::Fallback).await;
    }
    if !source.current() {
        return discard(app, &label, entry, Status::Stale).await;
    }
    activate(&window);
    Status::Opened
}
async fn discard(app: &AppHandle, label: &str, entry: Arc<AppWindow>, status: Status) -> Status {
    entry.retired.store(true, Ordering::SeqCst);
    let removed = retire(app, label, &entry).await.is_ok();
    if !removed && status == Status::Fallback {
        Status::Opened
    } else {
        status
    }
}
async fn retire(app: &AppHandle, label: &str, entry: &Arc<AppWindow>) -> Result<(), String> {
    entry.retired.store(true, Ordering::SeqCst);
    let mut cleanup = entry.cleanup.lock().await;
    if let Some(result) = &*cleanup {
        return result.clone();
    }
    let app_owned = app.clone();
    let label_owned = label.to_owned();
    let owned = entry.clone();
    let result = tokio::task::spawn_blocking(move || {
        let window = app_owned
            .get_webview_window(&label_owned)
            .ok_or("app_window_retirement_unobserved")?;
        if !owned.document.owns(window.as_ref()) {
            return Err("app_window_retirement_stale".to_owned());
        }
        let _ = window.hide();
        let erased = if owned.runtime.disposable {
            crate::instance_store::retire(window.as_ref())
        } else {
            crate::instance_store::destroy_completed(window.as_ref())
        };
        let destroyed = crate::instance_store::destroy_window_completed(&window);
        erased.and(destroyed)
    })
    .await
    .map_err(|error| error.to_string())
    .and_then(|result| result);
    *cleanup = Some(result.clone());
    if result.is_ok() {
        entry.directory.lock().expect("directory").take();
        let reg = registry(app);
        let mut reg = reg.0.lock().expect("app windows");
        if reg
            .windows
            .get(label)
            .is_some_and(|current| Arc::ptr_eq(current, entry))
        {
            reg.windows.remove(label);
            reg.documents.remove(label);
        }
    }
    result
}
#[derive(Debug, Deserialize, PartialEq)]
struct CloseConfirmation {
    message: String,
    confirm: String,
    cancel: String,
}
/// The page's confirmation, or `None` when it has nothing to lose or answered
/// with something that cannot be shown as two distinct buttons.
fn close_confirmation_result(raw: &str) -> Option<CloseConfirmation> {
    let answer = serde_json::from_str::<String>(raw).ok()?;
    let confirmation = serde_json::from_str::<CloseConfirmation>(&answer).ok()?;
    let shown = |text: &str| !text.trim().is_empty() && bounded(text, CLOSE_GUARD_TEXT);
    (shown(&confirmation.message)
        && shown(&confirmation.confirm)
        && shown(&confirmation.cancel)
        && confirmation.confirm != confirmation.cancel)
        .then_some(confirmation)
}
async fn close_confirmation(window: &WebviewWindow) -> Option<CloseConfirmation> {
    let (tx, rx) = tokio::sync::oneshot::channel();
    let tx = Mutex::new(Some(tx));
    window
        .as_ref()
        .eval_with_callback(CLOSE_GUARD_QUERY, move |raw| {
            if let Some(tx) = tx.lock().expect("close guard").take() {
                let _ = tx.send(close_confirmation_result(&raw));
            }
        })
        .ok()?;
    tokio::time::timeout(CLOSE_GUARD_TIMEOUT, rx).await.ok()?.ok()?
}
/// Whether this window's page state may be discarded. A page with unsaved
/// state gets a native dialog in front of its own window; only its confirm
/// button discards. No answer, or nothing unsaved, discards as before.
async fn confirm_discard(app: &AppHandle, window: &WebviewWindow) -> bool {
    use tauri_plugin_dialog::{DialogExt, MessageDialogButtons, MessageDialogKind};
    let Some(confirmation) = close_confirmation(window).await else {
        return true;
    };
    activate(window);
    let (tx, rx) = tokio::sync::oneshot::channel();
    app.dialog()
        .message(confirmation.message)
        .title(window.title().unwrap_or_default())
        .kind(MessageDialogKind::Warning)
        .parent(window)
        .buttons(MessageDialogButtons::OkCancelCustom(
            confirmation.confirm,
            confirmation.cancel,
        ))
        .show(move |confirmed| {
            let _ = tx.send(confirmed);
        });
    rx.await.unwrap_or(false)
}
/// Whether any App window is open, so a quit has page state it might discard.
pub(crate) fn any_open(app: &AppHandle) -> bool {
    registry(app)
        .0
        .lock()
        .expect("app windows")
        .windows
        .values()
        .any(|entry| !entry.retired.load(Ordering::SeqCst))
}
/// A quit asks every App window the same question its own close does, one
/// window at a time. A single cancel keeps every window and ends the quit;
/// a window already asking for its own close also ends it, since that answer
/// is still the user's to give.
pub(crate) async fn confirm_discard_all(app: &AppHandle) -> bool {
    let entries: Vec<_> = registry(app)
        .0
        .lock()
        .expect("app windows")
        .windows
        .iter()
        .filter(|(_, entry)| !entry.retired.load(Ordering::SeqCst))
        .filter_map(|(label, entry)| Some((entry.clone(), app.get_webview_window(label)?)))
        .collect();
    confirm_in_turn(
        &entries,
        |(entry, _)| &entry.asking,
        |(_, window)| {
            let app = app.clone();
            let window = window.clone();
            async move { confirm_discard(&app, &window).await }
        },
    )
    .await
}
/// One user close at a time. Only a confirmed discard retires the window; a
/// cancel leaves it live, and a later close asks again.
async fn settle_close<R>(
    retired: &AtomicBool,
    asking: &AtomicBool,
    discard: impl std::future::Future<Output = bool>,
    retire: impl FnOnce() -> R,
) where
    R: std::future::Future<Output = ()>,
{
    if retired.load(Ordering::SeqCst) || asking.swap(true, Ordering::SeqCst) {
        return;
    }
    if discard.await {
        retire().await;
    }
    asking.store(false, Ordering::SeqCst);
}
/// Asks each window in turn. A cancel, or a window still answering its own
/// close, ends the round with every later window unasked.
async fn confirm_in_turn<W, F>(
    windows: &[W],
    asking: impl Fn(&W) -> &AtomicBool,
    mut discard: impl FnMut(&W) -> F,
) -> bool
where
    F: std::future::Future<Output = bool>,
{
    for window in windows {
        let gate = asking(window);
        if gate.swap(true, Ordering::SeqCst) {
            return false;
        }
        let confirmed = discard(window).await;
        gate.store(false, Ordering::SeqCst);
        if !confirmed {
            return false;
        }
    }
    true
}
pub(crate) fn close_all(app: &AppHandle) {
    let entries: Vec<_> = registry(app)
        .0
        .lock()
        .expect("app windows")
        .windows
        .iter()
        .map(|(label, entry)| (label.clone(), entry.clone()))
        .collect();
    for (label, entry) in entries {
        entry.retired.store(true, Ordering::SeqCst);
        if let Some(window) = app.get_webview_window(&label) {
            let _ = window.hide();
        }
        let app = app.clone();
        tauri::async_runtime::spawn(async move {
            let _ = retire(&app, &label, &entry).await;
        });
    }
}
/// Replacement waits for all of the parent's stores. Failed native completion
/// stays owned and fails the replacement; no timer or automatic retry erases it.
pub(crate) async fn close_runtime_completed(app: &AppHandle, parent: &str, physical: u64) -> Result<(), String> {
    let entries: Vec<_> = registry(app)
        .0
        .lock()
        .expect("app windows")
        .windows
        .iter()
        .filter(|(_, entry)| entry.runtime.belongs_to(parent, physical))
        .map(|(label, entry)| (label.clone(), entry.clone()))
        .collect();
    let mut result = Ok(());
    for (label, entry) in entries {
        if let Err(error) = retire(app, &label, &entry).await {
            result = Err(error);
        }
    }
    result
}
fn activate(window: &WebviewWindow) {
    // OS presentation refusal leaves the same owned result, so it never
    // authorizes a competing Workbench fallback.
    if window.unminimize().and_then(|_| window.show()).is_ok() {
        let _ = window.set_focus();
    }
}
fn default_size(target: &AppWindowTarget) -> (f64, f64) {
    match target {
        AppWindowTarget::Builtin("terminal") => (820., 540.),
        AppWindowTarget::Builtin("editor") => (1000., 640.),
        AppWindowTarget::Builtin("preview") => (900., 640.),
        AppWindowTarget::Builtin("library") => (940., 640.),
        AppWindowTarget::ShowPage(_) => (1040., 720.),
        _ => (920., 600.),
    }
}
fn hex_encode(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn request(app: &str, params: serde_json::Value) -> Request {
        serde_json::from_value(
            json!({"appId":app,"sessionId":if app=="showpage"{Some("legacy:session.1")}else{None},
            "source":"dock","mode":"activate","newWindow":false,"origin":"https://runtime.example.test",
            "requestId":"action_1","params":params}),
        )
        .unwrap()
    }
    // The old owner accepted only four targets and silently discarded state.
    // This table pins the shared native data boundary, not UI resolution policy.
    #[test]
    fn six_targets_preserve_typed_state_without_path_urls() {
        for (app, params) in [
            ("files", json!({"path":"/tmp/project","rootDir":"/tmp"})),
            ("terminal", json!({"cwd":"/tmp/project"})),
            (
                "editor",
                json!({"path":"/tmp/file","filename":"file","mtime":null,"line":1,"column":0,"endColumn":0}),
            ),
            (
                "preview",
                json!({"path":"/tmp/image","name":"image","mime":"image/png","mtime":12.5}),
            ),
            ("showpage", json!({})),
            ("library", json!({"initialTab":"showpages"})),
        ] {
            let input = request(app, params.clone());
            let target = input.target(true).unwrap();
            let destination = target_url(&"https://runtime.example.test/".parse().unwrap(), &target);
            assert_eq!(
                destination.origin().ascii_serialization(),
                "https://runtime.example.test"
            );
            assert_eq!(destination.query(), Some("standalone=1"));
            assert!(!destination.as_str().contains("/tmp/"));
            assert_eq!(
                serde_json::to_value(Snapshot::new(&input, &target.base_label())).unwrap()["params"],
                params
            );
        }
        assert_eq!(
            target_url(
                &"http://127.0.0.1:5123/".parse().unwrap(),
                &parse_target("showpage", Some("legacy:session.1")).unwrap()
            )
            .as_str(),
            "http://127.0.0.1:5123/apps/show/legacy%3Asession.1?standalone=1"
        );
    }
    #[test]
    fn closed_params_and_target_applicability_reject_foreign_authority() {
        for (app, params) in [
            ("showpage", json!({"mtime":null})),
            ("library", json!({"path":"/tmp"})),
            ("terminal", json!({"path":"/tmp"})),
            ("files", json!({"cwd":"/tmp"})),
            ("preview", json!({"line":1})),
            ("editor", json!({"initialTab":"apps"})),
            ("editor", json!({"line":0})),
            ("editor", json!({"column":SAFE_INTEGER+1})),
            ("editor", json!({"column":4,"endColumn":3})),
            ("library", json!({"initialTab":"other"})),
        ] {
            assert!(request(app, params).target(true).is_err(), "{app}");
        }
        for params in [
            json!({"command":"sh"}),
            json!({"path":22}),
            json!({"column":-1}),
            json!({"column":0.5}),
            json!({"mtime":"today"}),
        ] {
            assert!(serde_json::from_value::<Params>(params).is_err());
        }
        let mut input = request("files", json!({"path":"é".repeat(2048)}));
        assert!(input.target(true).is_ok());
        input.params.as_mut().unwrap().path.as_mut().unwrap().push('x');
        assert!(input.target(true).is_err());
        for id in ["", "bad.id", "bad/id", &"x".repeat(129)] {
            input.request_id = Some(id.into());
            assert!(input.target(true).is_err());
        }
    }
    #[tokio::test]
    async fn a_cancelled_close_keeps_the_window_live_and_askable() {
        let (retired, asking) = (AtomicBool::new(false), AtomicBool::new(false));
        let retires = AtomicU64::new(0);
        let retire = || async {
            retires.fetch_add(1, Ordering::SeqCst);
            retired.store(true, Ordering::SeqCst);
        };
        settle_close(&retired, &asking, async { false }, retire).await;
        assert_eq!(retires.load(Ordering::SeqCst), 0);
        assert!(!retired.load(Ordering::SeqCst) && !asking.load(Ordering::SeqCst));

        // A close while another is still asking does not ask or retire again.
        asking.store(true, Ordering::SeqCst);
        settle_close(&retired, &asking, async { unreachable!("asked twice") }, retire).await;
        assert_eq!(retires.load(Ordering::SeqCst), 0);
        asking.store(false, Ordering::SeqCst);

        settle_close(&retired, &asking, async { true }, retire).await;
        assert_eq!(retires.load(Ordering::SeqCst), 1);
        assert!(retired.load(Ordering::SeqCst) && !asking.load(Ordering::SeqCst));
        settle_close(
            &retired,
            &asking,
            async { unreachable!("asked after retiring") },
            retire,
        )
        .await;
        assert_eq!(retires.load(Ordering::SeqCst), 1);
    }
    #[tokio::test]
    async fn a_quit_stops_at_the_first_window_that_keeps_its_state() {
        let windows: Vec<(bool, AtomicBool)> = [true, false, true]
            .into_iter()
            .map(|answer| (answer, AtomicBool::new(false)))
            .collect();
        let asked = Mutex::new(Vec::new());
        let ask = |window: &(bool, AtomicBool)| {
            asked.lock().unwrap().push(window.0);
            let answer = window.0;
            async move { answer }
        };
        assert!(!confirm_in_turn(&windows, |window| &window.1, ask).await);
        assert_eq!(*asked.lock().unwrap(), vec![true, false]);
        assert!(windows.iter().all(|window| !window.1.load(Ordering::SeqCst)));

        // A window still answering its own close ends the quit unasked.
        windows[0].1.store(true, Ordering::SeqCst);
        asked.lock().unwrap().clear();
        assert!(!confirm_in_turn(&windows, |window| &window.1, ask).await);
        assert!(asked.lock().unwrap().is_empty());
        windows[0].1.store(false, Ordering::SeqCst);

        assert!(confirm_in_turn(&windows[..1], |window| &window.1, ask).await);
        assert!(confirm_in_turn(&windows[..0], |window| &window.1, ask).await);
    }
    #[test]
    fn close_asks_only_for_a_showable_page_confirmation() {
        let answer = |value: serde_json::Value| serde_json::to_string(&value.to_string()).unwrap();
        assert_eq!(
            close_confirmation_result(&answer(
                json!({"message":"Discard unsaved changes and close this window?","confirm":"Discard and Close","cancel":"Cancel"})
            )),
            Some(CloseConfirmation {
                message: "Discard unsaved changes and close this window?".into(),
                confirm: "Discard and Close".into(),
                cancel: "Cancel".into(),
            })
        );
        // Nothing unsaved, no answer, a throwing guard and malformed answers
        // all close the window as before.
        for raw in [
            "\"\"".to_owned(),
            String::new(),
            "null".to_owned(),
            "{\"message\":\"m\",\"confirm\":\"c\",\"cancel\":\"x\"}".to_owned(),
            answer(json!({"message":"m","confirm":"Close"})),
            answer(json!({"message":"m","confirm":"Same","cancel":"Same"})),
            answer(json!({"message":" ","confirm":"c","cancel":"x"})),
            answer(json!({"message":"m","confirm":1,"cancel":"x"})),
            answer(json!({"message":"m".repeat(CLOSE_GUARD_TEXT + 1),"confirm":"c","cancel":"x"})),
        ] {
            assert_eq!(close_confirmation_result(&raw), None, "{raw}");
        }
    }
    #[test]
    fn legacy_shape_remains_accepted_but_partial_protocol_and_new_tab_do_not() {
        let mut old: Request =
            serde_json::from_value(json!({"appId":"editor","newWindow":false,"sessionId":null})).unwrap();
        assert!(old.target(false).is_ok());
        assert!(old.target(true).is_err());
        old.params = Some(Params::default());
        assert!(old.target(false).is_err());
        let mut rich = request("files", json!({}));
        rich.mode = Some("newTab".into());
        assert!(rich.target(true).is_err());
        rich.mode = Some("newWindow".into());
        assert!(rich.target(true).is_err());
        rich.new_window = true;
        assert!(rich.target(true).is_ok());
    }
    #[test]
    fn document_token_requires_an_actual_nonempty_callback_value() {
        for invalid in ["", "null", "undefined", "\"\"", "{}", "42", "false", "\"unterminated"] {
            assert!(document_token_result(invalid).is_none(), "{invalid}");
        }
        assert_eq!(
            document_token_result("\"native-document\"").as_deref(),
            Some("native-document")
        );
    }
    #[test]
    fn only_exact_bounded_navigation_envelope_is_consumed_as_a_request() {
        let json = serde_json::to_string(&request("terminal", json!({"cwd":"/tmp/a b"}))).unwrap();
        let mut uri: Url = "avibe-app://launch".parse().unwrap();
        uri.query_pairs_mut().append_pair("request", &json);
        assert_eq!(
            parse_navigation(&uri).unwrap().params.unwrap().cwd.as_deref(),
            Some("/tmp/a b")
        );
        for raw in [
            uri.to_string().replace("launch?", "launch/?"),
            uri.to_string().replace("launch?", "user@launch?"),
            format!("{uri}&extra=1"),
            format!("{uri}#fragment"),
            uri.to_string().replace("avibe-app:", "https:"),
        ] {
            assert!(parse_navigation(&raw.parse().unwrap()).is_none(), "{raw}");
        }
        uri.query_pairs_mut().append_pair("request", &json);
        assert!(parse_navigation(&uri).is_none());
    }
    #[test]
    fn show_ids_and_canonical_identity_do_not_include_launch_state() {
        for bad in [
            "",
            "..",
            ".session",
            "session/other",
            "session?query",
            "session#fragment",
            &"a".repeat(129),
        ] {
            assert!(parse_target("showpage", Some(bad)).is_err());
        }
        assert!(
            parse_target("showpage", Some(&"a".repeat(128)))
                .unwrap()
                .base_label()
                .len()
                <= 512
        );
        let mut input = request("editor", json!({"path":"/tmp/a"}));
        let first = input.target(true).unwrap().base_label();
        input.params.as_mut().unwrap().path = Some("/tmp/b".into());
        assert_eq!(input.target(true).unwrap().base_label(), first);
        assert_ne!(
            parse_target("showpage", Some("a:b")).unwrap().base_label(),
            parse_target("showpage", Some("a-b")).unwrap().base_label()
        );
        assert!(!same_origin(
            &"https://runtime.example.test/".parse().unwrap(),
            &"https://user@runtime.example.test/apps/files".parse().unwrap()
        ));
    }
    // Execute the native producer scripts as JS. Real React/native consumer and
    // lifecycle observations belong to the separate finite archive probe.
    #[test]
    fn snapshot_before_event_reuse_and_reload_preserve_the_live_document() {
        let initial = Snapshot::new(&request("editor", json!({"path":"/tmp/first"})), "avibe-app-editor");
        let mut repeated = initial.clone();
        repeated.request_id = "second".into();
        repeated.params.as_mut().unwrap().path = Some("/tmp/second".into());
        let script = format!(
            r#"
const assert=require('node:assert/strict');const values=new Map();
global.sessionStorage={{getItem:k=>values.get(k),setItem:(k,v)=>values.set(k,v)}};
global.CustomEvent=class{{constructor(type,options){{this.type=type;this.detail=options.detail;}}}};
global.window={{buffer:'unsaved',dispatchEvent(event){{assert.equal(this.__AVIBE_APP_LAUNCH__,event.detail);this.events=(this.events||0)+1;}}}};window.self=window;window.top=window;
{}
assert.equal(window.__AVIBE_APP_LAUNCH__.params.path,'/tmp/first');
{}
assert.equal(window.buffer,'unsaved');assert.equal(window.events,1);assert.equal(window.__AVIBE_APP_LAUNCH__.requestId,'second');
delete window.__AVIBE_APP_LAUNCH__;
{}
assert.equal(window.__AVIBE_APP_LAUNCH__.requestId,'second');
"#,
            initial_snapshot(&initial),
            snapshot_script(&repeated, true),
            initial_snapshot(&initial)
        );
        let result = std::process::Command::new("node")
            .arg("-e")
            .arg(script)
            .output()
            .unwrap();
        assert!(result.status.success(), "{}", String::from_utf8_lossy(&result.stderr));
    }
}
