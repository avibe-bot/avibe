//! Included only by build_native_probe.py's finite App-window archive adapter.
use super::*;
static HOLD: Mutex<Option<String>> = Mutex::new(None);
static RELEASE: tokio::sync::Notify = tokio::sync::Notify::const_new();
static FAIL: Mutex<Option<String>> = Mutex::new(None);

/// Archive-only boundary: role/receipt owners execute normally, but physical
/// presentation never reaches AppKit. Hidden WebViews still run real scripts.
pub fn physical(window: &tauri::Window, effect: &str) -> tauri::Result<()> {
    let visible = window.is_visible()?;
    let focused = window.is_focused()?;
    println!(
        "{}",
        serde_json::json!({"event":"app_physical_effect_suppressed",
            "label":window.label(),"effect":effect,"visible":visible,"focused":focused})
    );
    if visible || focused {
        return Err(std::io::Error::other("fixture physical window escaped hidden policy").into());
    }
    Ok(())
}

/// A native popup's parent attachment can order it visible during build.
/// Popup presentation is outside this hidden-only fixture, so fail before the
/// builder reaches that OS side effect; do not substitute another parent.
pub fn popup_build<T>() -> Result<T, &'static str> {
    println!("{}", serde_json::json!({"event":"app_popup_build_excluded"}));
    Err("popup construction excluded by hidden-only fixture")
}

pub fn failure(phase: &str) -> bool {
    let mut fault = FAIL.lock().unwrap();
    if fault.as_deref() == Some(phase) {
        fault.take();
        true
    } else {
        false
    }
}
pub async fn pause(phase: &str) {
    let pause = {
        let mut hold = HOLD.lock().unwrap();
        if hold.as_deref() == Some(phase) {
            hold.take();
            true
        } else {
            false
        }
    };
    if pause {
        println!("{}", serde_json::json!({"event":"app_held","phase":phase}));
        tokio::time::timeout(std::time::Duration::from_secs(20), RELEASE.notified())
            .await
            .expect("App fixture hold");
    }
}
pub fn main(app: &AppHandle) {
    crate::build_main_window(app, None, false, None).expect("actual main builder");
}
pub fn navigation(view: &Webview, url: &Url) -> Option<bool> {
    super::navigation(view, url).or_else(|| {
        is_app_window(view.label()).then(|| {
            let allowed = child_navigation(view, url);
            println!(
                "{}",
                serde_json::json!({"event":"app_navigation","label":view.label(),"url":url,"allowed":allowed})
            );
            allowed
        })
    })
}
pub fn invoke(invoke: tauri::ipc::Invoke) -> bool {
    let handler: Box<dyn Fn(tauri::ipc::Invoke) -> bool + Send + Sync> =
        Box::new(tauri::generate_handler![crate::open_app_window]);
    handler(invoke)
}
pub fn control(app: &AppHandle, command: serde_json::Value) {
    match command["operation"].as_str().unwrap() {
        "hold" => {
            *HOLD.lock().unwrap() = Some(command["phase"].as_str().unwrap().into());
        }
        "release" => RELEASE.notify_one(),
        "fail" => {
            *FAIL.lock().unwrap() = Some(command["phase"].as_str().unwrap().into());
        }
        "close" => {
            app.get_window(command["label"].as_str().unwrap())
                .unwrap()
                .close()
                .unwrap();
        }
        "cleanup" => {
            let app = app.clone();
            tauri::async_runtime::spawn(async move {
                let entries: Vec<_> = registry(&app)
                    .0
                    .lock()
                    .unwrap()
                    .windows
                    .iter()
                    .map(|(label, entry)| (label.clone(), entry.clone()))
                    .collect();
                let mut results = Vec::new();
                for (label, entry) in entries {
                    results.push((label.clone(), retire(&app, &label, &entry).await));
                }
                println!("{}", serde_json::json!({"event":"app_cleanup","results":results}));
            });
            return;
        }
        "snapshot" => {
            let entries: Vec<_> = registry(app)
                .0
                .lock()
                .unwrap()
                .windows
                .iter()
                .map(|(label, entry)| (label.clone(), entry.clone()))
                .collect();
            let values: Vec<_> = entries.into_iter().map(|(label,entry)| {
                let physical = app.get_window(&label);
                let view = app.get_webview(&label);
                serde_json::json!({"label":label,"snapshot":*entry.snapshot.lock().unwrap(),
                    "retired":entry.retired.load(Ordering::SeqCst),"directory":entry.directory.lock().unwrap().as_ref().map(|d|d.path().to_owned()),
                    "document":entry.document.generation.load(Ordering::SeqCst),"live":view.is_some(),
                    "title":physical.as_ref().and_then(|w|w.title().ok()),
                    "visible":physical.as_ref().and_then(|w|w.is_visible().ok()),"focused":physical.as_ref().and_then(|w|w.is_focused().ok()),
                    "position":physical.as_ref().and_then(|w|w.outer_position().ok()).map(|p|[p.x,p.y])})
            }).collect();
            println!("{}", serde_json::json!({"event":"app_snapshot","windows":values}));
            return;
        }
        "cookies" => {
            let view = app.get_webview(command["label"].as_str().unwrap()).unwrap();
            let url: Url = command["url"].as_str().unwrap().parse().unwrap();
            tauri::async_runtime::spawn_blocking(move || {
                let cookies = view.cookies_for_url(url).unwrap();
                println!(
                    "{}",
                    serde_json::json!({"event":"app_cookies","names":cookies.iter().map(|c|c.name()).collect::<Vec<_>>()})
                );
            });
            return;
        }
        _ => panic!("unsupported App fixture operation"),
    }
    println!("{}", serde_json::json!({"event":"app_control_ready"}));
}
