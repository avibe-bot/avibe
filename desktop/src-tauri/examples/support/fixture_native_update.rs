//! Archived-assembly-only updater harness; never declared by the shipped lib.
//! Derived from instance_client_probe + fixture_shell, without account/backend
//! fixtures. Production panel commands, scheduler, verifier and installer run.
use super::*;
use async_trait::async_trait;
use avibe_runtime_host::{
    instance_client::{
        account::Passkeys,
        protocol::*,
        storage::{ConnectionStore, Credentials},
        transport::{Epoch, Request, Response, Transport},
    },
    HealthProbe, LaunchError, Presence, ResolvedRuntimeLauncher, RuntimeHostSettings, RuntimeLauncher,
};
use serde::Deserialize;
use serde_json::{json, Value};
use std::{
    collections::HashMap,
    io::Write,
    path::PathBuf,
    sync::OnceLock,
    time::{Duration, Instant},
};
use tauri::utils::config::AppDirectoriesOverride;
use tauri::{Listener, Manager, WebviewUrl, WebviewWindowBuilder};
use url::Url;

#[derive(Clone, Deserialize)]
struct Config {
    root: PathBuf,
    run_id: String,
    local: Url,
    proxy: Url,
    certificate: PathBuf,
}
static CONFIG: OnceLock<Config> = OnceLock::new();
static OUTPUT: Mutex<()> = Mutex::new(());

fn emit(value: Value) {
    let _lock = OUTPUT.lock().unwrap();
    let config = CONFIG.get().expect("fixture config");
    let mut value = value;
    value["pid"] = json!(std::process::id());
    value["unix_ms"] = json!(std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_millis());
    let mut stream = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(config.root.join("events.jsonl"))
        .unwrap();
    writeln!(stream, "{value}").unwrap();
    stream.sync_data().unwrap();
}

/// Replaces only updater client construction in the archived assembly.
/// HTTPS names/paths, signature validation and download logic stay production.
pub(crate) fn update_client() -> Result<reqwest::Client, String> {
    let config = CONFIG.get().ok_or("fixture config missing")?;
    let certificate =
        reqwest::Certificate::from_pem(&std::fs::read(&config.certificate).map_err(|error| error.to_string())?)
            .map_err(|error| error.to_string())?;
    reqwest::Client::builder()
        .no_proxy()
        .proxy(reqwest::Proxy::all(config.proxy.as_str()).map_err(|error| error.to_string())?)
        .tls_built_in_root_certs(false)
        .add_root_certificate(certificate)
        .redirect(reqwest::redirect::Policy::none())
        .https_only(true)
        .connect_timeout(Duration::from_secs(10))
        .read_timeout(Duration::from_secs(30))
        .user_agent("Avibe-desktop-updater")
        .build()
        .map_err(|error| error.to_string())
}

/// A one-shot observation/pause BEFORE the unchanged final authorization
/// callback, not a substitute for it. A timeout fails closed before mutation.
pub(crate) fn before_commit() -> Result<(), String> {
    let root = &CONFIG.get().ok_or("fixture config missing")?.root;
    let arm = root.join("gate-arm");
    match std::fs::remove_file(&arm) {
        Ok(()) => {}
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(()),
        Err(error) => return Err(error.to_string()),
    }
    emit(json!({"event":"before_commit"}));
    let deadline = Instant::now() + Duration::from_secs(60);
    while !root.join("gate-release").is_file() {
        if Instant::now() >= deadline {
            return Err("fixture final-edge pause timed out".into());
        }
        std::thread::sleep(Duration::from_millis(20));
    }
    std::fs::remove_file(root.join("gate-release")).map_err(|error| error.to_string())?;
    Ok(())
}

struct NoRuntime;
#[async_trait]
impl HealthProbe for NoRuntime {
    async fn presence(&self, _: &LoopbackOrigin, _: Option<&str>) -> Presence {
        panic!("fixture cannot bootstrap an installed Runtime")
    }
}
impl RuntimeLauncher for NoRuntime {
    fn resolve(&self) -> Result<Arc<dyn ResolvedRuntimeLauncher>, LaunchError> {
        panic!("fixture cannot resolve an installed Runtime")
    }
}
struct NoAccountNetwork;
#[async_trait]
impl Transport for NoAccountNetwork {
    async fn send(&self, _: Request) -> Result<Response, ClientError> {
        Err(ClientError::new("fixture_account_network_denied"))
    }
}
#[derive(Default)]
struct Private(Mutex<HashMap<String, Vec<u8>>>);
impl Credentials for Private {
    fn read(&self, id: &str) -> Result<Option<Vec<u8>>, ClientError> {
        Ok(self.0.lock().unwrap().get(id).cloned())
    }
    fn write(&self, id: &str, bytes: &[u8]) -> Result<(), ClientError> {
        self.0.lock().unwrap().insert(id.into(), bytes.into());
        Ok(())
    }
    fn erase(&self, id: &str) -> Result<(), ClientError> {
        self.0.lock().unwrap().remove(id);
        Ok(())
    }
}
struct NoPasskey;
#[async_trait]
impl Passkeys for NoPasskey {
    async fn assert(&self, _: Value, _: Epoch) -> Result<Value, ClientError> {
        Err(ClientError::new("fixture_passkey_unavailable"))
    }
}

#[tauri::command]
async fn desktop_client_request(
    window: tauri::WebviewWindow,
    app: AppHandle,
    request: ClientRequest,
) -> Result<ClientReply, String> {
    let action = serde_json::to_value(&request.action).unwrap();
    let result = instance_client::desktop_client_request(window, app, request).await;
    emit(json!({"event":"panel_command", "action":action,
        "ok":result.as_ref().is_ok_and(|reply| reply.ok)}));
    result
}

pub fn run() {
    // LaunchServices does not promise inherited environment. Locate persistent
    // config beside the owned installation, and bind it to this job's binary.
    let native_executable = std::env::current_exe().unwrap();
    let native_installation = native_executable.parent().unwrap().to_path_buf();
    let executable = native_executable.canonicalize().unwrap();
    let root = executable.ancestors().nth(if cfg!(windows) { 2 } else { 5 }).unwrap();
    let path = root.join("config.json");
    let mut config: Config = serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
    config.root = config.root.canonicalize().unwrap();
    let compiled_root = PathBuf::from(env!("AVIBE_ACCEPTANCE_ROOT")).canonicalize().unwrap();
    assert_eq!(config.run_id, env!("AVIBE_ACCEPTANCE_RUN_ID"));
    assert_eq!(config.root, root);
    assert!(config.root.starts_with(compiled_root.join("cases")));
    assert!(path.canonicalize().unwrap().starts_with(&config.root));
    assert!(executable.starts_with(config.root.join("installation")));
    // Restore only known fixture-owned path variables, never arbitrary saved
    // environment, production credentials, PATH or global proxy/browser flags.
    for key in [
        "HOME",
        "USERPROFILE",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
        "APPDATA",
        "LOCALAPPDATA",
        "TMP",
        "TEMP",
        "TMPDIR",
        "AVIBE_HOME",
    ] {
        let owned = config
            .root
            .join("environment")
            .join(key.to_ascii_lowercase())
            .canonicalize()
            .unwrap();
        assert!(owned.starts_with(&config.root));
        std::env::set_var(key, owned);
    }
    std::env::set_var("AVIBE_COMPUTER_USE_STATE_DIR", config.root.join("native-state"));
    // Windows production passes the native current_exe().parent() spelling to
    // NSIS, while config.root is canonicalized for the ownership boundary.
    // Keep the installer mailbox in that same native spelling; the canonical
    // assertion above still proves it is exactly the owned installation.
    let installation = if cfg!(windows) {
        native_installation
    } else {
        config.root.join("installation")
    };
    assert!(installation
        .canonicalize()
        .unwrap()
        .starts_with(config.root.join("installation")));
    std::env::set_var("AVIBE_NATIVE_ACCEPTANCE_INSTALLATION", installation);
    for url in [&config.local, &config.proxy] {
        assert_eq!(url.scheme(), "http");
        assert_eq!(url.host_str(), Some("127.0.0.1"));
    }
    assert!(config.certificate.canonicalize().unwrap().starts_with(&config.root));
    CONFIG.set(config.clone()).ok().expect("one fixture instance");
    let data = config.root.join("native-state");
    std::fs::create_dir_all(&data).unwrap();
    let mut context = tauri::generate_context!();
    context.config_mut().build.dev_url = None;
    context.config_mut().app.app_directories_override = Some(AppDirectoriesOverride::Root(data.clone()));
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .invoke_handler(tauri::generate_handler![desktop_client_request])
        .plugin(tauri::plugin::Builder::<_, ()>::new("native-update-fixture")
            .on_navigation(|view, url| {
                if url.scheme() == "avibe-acceptance-result" {
                    let payload = url.query_pairs().find(|(key, _)| key == "value").unwrap().1;
                    emit(json!({"event":"javascript", "value":serde_json::from_str::<Value>(&payload).unwrap()}));
                    return false;
                }
                instance_client::navigation(view.app_handle(), view.label(), url).unwrap_or_else(|| {
                    url.scheme() == "tauri" || url.host_str() == Some("tauri.localhost")
                })
            })
            .on_page_load(|view, payload| {
                if view.label() == MAIN_WINDOW && payload.event() == tauri::webview::PageLoadEvent::Finished {
                    instance_client::page_loaded(view.app_handle());
                    emit(json!({"event":"loaded", "url":payload.url().as_str()}));
                }
            }).build())
        .setup(move |app| {
            for resolved in [app.path().app_data_dir()?, app.path().app_local_data_dir()?, app.path().app_config_dir()?] {
                std::fs::create_dir_all(&resolved)?;
                assert_eq!(resolved.canonicalize()?, data.canonicalize()?);
            }
            let local = LoopbackOrigin::parse(config.local.as_str())?;
            let host = RuntimeHost::new(Arc::new(NoRuntime), Arc::new(NoRuntime), RuntimeHostSettings::default());
            let shell = Shell::new(host, BootstrapLog::at(config.root.join("bootstrap.log")));
            shell.activity.store(ACTIVITY_MONITOR, Ordering::SeqCst);
            *shell.active_origin.lock().unwrap() = Some(local.clone());
            let active = shell.active_origin.clone();
            app.manage(shell);
            app.manage(notifications::Notifications::new(data.join("notifications.json")));
            app.manage(Mutex::new(DeepLinks::default()));
            app.manage(PendingDesktopUpdateOpen(AtomicBool::new(false)));
            updater::init(app.handle())?;
            app.listen_any(updater::STATE_EVENT, |event| {
                emit(json!({"event":"update", "state":serde_json::from_str::<Value>(event.payload()).unwrap()}));
            });
            let bundle: Url = if cfg!(windows) { "http://tauri.localhost/" } else { "tauri://localhost/" }.parse()?;
            let store = ConnectionStore::new(data.join("connection.json"));
            let record = store.load("Acceptance runner").map_err(|e| std::io::Error::other(e.code))?;
            let coordinator = instance_client::attach(app.handle(), instance_client::NativeClientConfig {
                bundle:bundle.clone(), state:ClientState::new("Acceptance runner".into(), app.package_info().version.to_string(), updater::client_snapshot(app.handle())),
                record, store, local_origin:active,
            }, Arc::new(NoAccountNetwork), Arc::new(Private::default()), Arc::new(NoPasskey));
            coordinator.local_origin(Some(local));
            let mut window = app.config().app.windows[0].clone();
            window.url = WebviewUrl::External(bundle);
            WebviewWindowBuilder::from_config(app, &window)?
                .initialization_script("localStorage.setItem('i18nextLng', 'en');")
                .initialization_script(instance_client::initial_script(app.handle()).unwrap())
                .visible(false).build()?;
            assert!(instance_client::present(app.handle(), "updates"));
            // Like open_desktop_updates, accept the real presentation first,
            // then use the production main-window show/focus lifecycle. The
            // existing window prevents recreation/bootstrap; present alone
            // only queues navigation and cannot make a hidden WebView visible.
            super::focus_or_restore_main_window(app.handle());
            emit(json!({"event":"native_ready", "version":app.package_info().version.to_string(),
                "executable":executable, "data":data, "source_sha":env!("AVIBE_ACCEPTANCE_SOURCE_SHA")}));
            let handle = app.handle().clone();
            std::thread::spawn(move || {
                let mut seen = 0;
                loop {
                    let bytes = std::fs::read_to_string(config.root.join("commands.jsonl")).unwrap_or_default();
                    let mut lines: Vec<_> = bytes.lines().collect();
                    if !bytes.is_empty() && !bytes.ends_with('\n') { lines.pop(); }
                    for line in lines.iter().skip(seen) {
                        let Ok(command) = serde_json::from_str::<Value>(line) else { continue };
                        if command["pid"].as_u64() != Some(std::process::id() as u64) { continue; }
                        match command["action"].as_str().unwrap() {
                            "eval" => handle.get_webview_window(MAIN_WINDOW).unwrap().eval(command["script"].as_str().unwrap()).unwrap(),
                            "state" => emit(json!({"event":"state", "state":coordinator.state(),
                                "visible":handle.get_webview_window(MAIN_WINDOW).is_some_and(|window| window.is_visible().unwrap_or(false))})),
                            "stop" => { super::exit_shell(&handle); return; },
                            _ => panic!("unsupported fixture command"),
                        }
                    }
                    seen = lines.len();
                    std::thread::sleep(Duration::from_millis(50));
                }
            });
            Ok(())
        }).build(context).expect("native fixture build");
    app.run(|app, event| {
        if let tauri::RunEvent::ExitRequested { api, code, .. } = event {
            if instance_client::replacing_window_exit(app, code) {
                api.prevent_exit();
            }
        }
    });
}
