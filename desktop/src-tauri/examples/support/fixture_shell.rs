//! Included only in the archived test assembly, never the shipped library.
//! Real shell entry points run with an inert RuntimeHost and private state.
use super::*;
use avibe_runtime_host::{HealthProbe, LaunchError, Presence, ResolvedRuntimeLauncher, RuntimeHostSettings, RuntimeLauncher};
use std::path::Path;

struct NoRuntime;
#[derive(Default)]
struct LinkAdmission(Mutex<Option<(bool, Arc<tokio::sync::Notify>)>>);
#[async_trait::async_trait]
impl HealthProbe for NoRuntime {
    async fn presence(&self, _: &LoopbackOrigin, _: Option<&str>) -> Presence {
        panic!("fixture must not start the installed Runtime bootstrap");
    }
}
impl RuntimeLauncher for NoRuntime {
    fn resolve(&self) -> Result<Arc<dyn ResolvedRuntimeLauncher>, LaunchError> {
        panic!("fixture must not resolve an installed Runtime");
    }
}

pub fn enabled() -> bool {
    std::env::var("AVIBE_FIXTURE_SHELL").as_deref() == Ok("1")
}

pub fn prepare(app: &AppHandle, root: &Path) {
    if !enabled() {
        return;
    }
    let data = app.path().app_local_data_dir().unwrap();
    assert!(data.starts_with(root));
    std::fs::create_dir_all(&data).unwrap();
    if std::env::var("AVIBE_FIXTURE_AUTO_UPDATE").as_deref() == Ok("1") {
        std::fs::write(data.join("remote-install.json"), r#"{"enabled":true}"#).unwrap();
    }
}

pub fn attach(app: &AppHandle, local: LoopbackOrigin, root: &Path) -> Arc<Mutex<Option<LoopbackOrigin>>> {
    if !enabled() {
        return Arc::new(Mutex::new(Some(local)));
    }
    let host = RuntimeHost::new(Arc::new(NoRuntime), Arc::new(NoRuntime), RuntimeHostSettings::default());
    let shell = Shell::new(host, BootstrapLog::at(root.join("fixture-bootstrap.log")));
    shell.activity.store(ACTIVITY_MONITOR, Ordering::SeqCst);
    *shell.active_origin.lock().unwrap() = Some(local);
    let active = shell.active_origin.clone();
    app.manage(shell);
    app.manage(Mutex::new(DeepLinks::default()));
    app.manage(PendingDesktopUpdateOpen(AtomicBool::new(false)));
    app.manage(notifications::Notifications::new(root.join("notifications.json")));
    app.manage(pet::Pet::new(root.join("pet.json")));
    app.manage(LinkAdmission::default());
    active
}

pub fn start(app: &AppHandle) {
    if !enabled() {
        return;
    }
    let origin = app.state::<Shell>().active_origin.lock().unwrap().clone().unwrap();
    notifications::start(app, origin);
}

pub fn finished(webview: &tauri::Webview) {
    if enabled() {
        super::main_document_finished(webview);
    } else {
        instance_client::page_loaded(webview.app_handle());
    }
}

pub fn receive(app: &AppHandle, link: String) {
    assert!(enabled());
    let handle = app.clone();
    app.run_on_main_thread(move || {
        super::receive_native_deep_link(&handle, [link]);
        println!("{}", serde_json::json!({"event":"link_received"}));
    }).unwrap();
}

/// The archived fixture pauses the real task after claim and before acceptance.
/// It does not supply an owner, a result, or an alternative return path.
pub async fn before_link_return(app: &AppHandle) {
    if !enabled() {
        return;
    }
    let release = {
        let gate = app.state::<LinkAdmission>();
        let mut held = gate.0.lock().unwrap();
        held.as_mut().and_then(|(armed, release)| {
            if !*armed {
                return None;
            }
            *armed = false;
            Some(release.clone())
        })
    };
    if let Some(release) = release {
        println!("{}", serde_json::json!({"event":"link_return_held"}));
        tokio::time::timeout(std::time::Duration::from_secs(30), release.notified())
            .await
            .expect("fixture link admission hold timed out");
    }
}

pub fn status(app: &AppHandle) -> serde_json::Value {
    let shell = app.state::<Shell>();
    serde_json::json!({
        "event": "shell_status",
        "pending": app.state::<Mutex<DeepLinks>>().lock().unwrap().has_pending(),
        "origin": shell.active_origin.lock().unwrap().as_ref().map(LoopbackOrigin::as_str),
        "notifications_enabled": app.state::<notifications::Notifications>().enabled(),
        "mini_window": app.get_webview_window(pet::PET_WINDOW).is_some(),
        "data": app.path().app_data_dir().unwrap(),
    })
}

pub fn control(app: &AppHandle, command: serde_json::Value) {
    assert!(enabled());
    let handle = app.clone();
    app.run_on_main_thread(move || {
        match command["operation"].as_str().unwrap() {
            "finished" => {
                super::main_document_finished(handle.get_webview_window(MAIN_WINDOW).unwrap().as_ref());
            }
            "hold_link_return" => {
                let gate = handle.state::<LinkAdmission>();
                let mut held = gate.0.lock().unwrap();
                assert!(held.is_none());
                *held = Some((true, Arc::new(tokio::sync::Notify::new())));
            }
            "release_link_return" => {
                let (_, release) = handle.state::<LinkAdmission>().0.lock().unwrap().take().unwrap();
                release.notify_one();
            }
            "hide" => handle.get_webview_window(MAIN_WINDOW).unwrap().hide().unwrap(),
            "notifications" => {
                notifications::set_enabled(&handle, command["enabled"].as_bool().unwrap()).unwrap();
            }
            "stop" => {
                assert!(super::return_to_bootstrap(&handle));
            }
            "rebind" => {
                let origin = LoopbackOrigin::parse(command["origin"].as_str().unwrap()).unwrap();
                assert!(super::set_active_origin(&handle, Some(origin.clone())));
                notifications::start(&handle, origin);
            }
            _ => panic!("unknown shell fixture control"),
        }
        println!("{}", serde_json::json!({"event":"shell_control", "operation":command["operation"]}));
    }).unwrap();
}
