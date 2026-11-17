"""Build an immutable native fixture without changing product signing settings.

The updater HTTP client's socket/root trust and compile-time public test key
are fixture-owned. Automatic OS notification/popover nudges are recorded in
the fixture process; explicit UI CTA, native dialogs, mailbox dispatcher,
signature validation, generation checks and installer owner remain actual.
The checked-in artifact is inert test bytes, never an installable package.
"""

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--bundle-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--target-dir", type=Path, required=True)
    parser.add_argument("--shell-consumers", action="store_true")
    parser.add_argument("--account-feedback", action="store_true")
    parser.add_argument("--update-consumers", action="store_true")
    parser.add_argument("--shell-fixture", type=Path)
    args = parser.parse_args()
    assert not any(part.endswith(".app") for part in args.output.resolve().parts), "native fixture must stay outside an application bundle"
    args.output.mkdir(parents=True, exist_ok=False)
    source = args.output / "source"
    source.mkdir()
    assert subprocess.check_output(["git", "rev-parse", args.head], cwd=args.source, text=True).strip() == args.head
    archive = subprocess.check_output(
        ["git", "archive", args.head, "desktop", "ui/src/i18n/en.json", "ui/src/i18n/zh.json"], cwd=args.source,
    )
    with tarfile.open(fileobj=io.BytesIO(archive)) as stream:
        stream.extractall(source, filter="data")
    bundle = args.bundle.read_bytes()
    assert hashlib.sha256(bundle).hexdigest() == args.bundle_sha256
    assets = source / "desktop/dist"
    assets.mkdir(exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(bundle)) as stream:
        # Complete Desktop archives retain their leading dist directory.
        leading_dist = all(member.name == "dist" or member.name.startswith("dist/")
                           for member in stream.getmembers())
        stream.extractall(source / "desktop" if leading_dist else assets, filter="data")
    if args.account_feedback:
        # This archive-only adapter invokes the same menu owner as production.
        # No public product seam, alternate admission or updater change is added.
        library = source / "desktop/src-tauri/src/lib.rs"
        original_library = library.read_bytes()
        library.write_text(library.read_text() + """
pub fn fixture_account_menu(app: &tauri::AppHandle, view: &str) {
    instance_client::menu_present(app, match view {
        "account" => "account", "instances" => "instances", "sign-out" => "sign-out", "updates" => "updates",
        _ => panic!("unsupported fixture menu"),
    });
}
pub static FIXTURE_SHOW_FAILURE: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);
pub static FIXTURE_RETIRE_FAILURE: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);
pub static FIXTURE_ERROR_HOLD: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);
pub static FIXTURE_ERROR_RELEASE: tokio::sync::Notify = tokio::sync::Notify::const_new();
pub static FIXTURE_PRESENTATION_HOLD: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);
pub static FIXTURE_PRESENTATION_RELEASE: tokio::sync::Notify = tokio::sync::Notify::const_new();
pub static FIXTURE_RETIRED_HOLD: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);
pub static FIXTURE_RETIRED_RELEASE: tokio::sync::Notify = tokio::sync::Notify::const_new();
""")
        example = source / "desktop/src-tauri/examples/instance_client_probe.rs"
        text = example.read_text()
        needle = '                        "state"=>emit('
        assert text.count(needle) == 1
        text = text.replace(needle, '''                        "menu"=> avibe_desktop_lib::fixture_account_menu(&handle, command["view"].as_str().unwrap_or("sign-out")),
                        "presentation_snapshot"=> avibe_desktop_lib::instance_client::fixture_presentation_snapshot(&handle),
''' + needle)
        text = text.replace(needle, '''                        "show_failure"=> {
                            avibe_desktop_lib::FIXTURE_SHOW_FAILURE.store(true,std::sync::atomic::Ordering::SeqCst);
                            emit(json!({"event":"control_ready"}));
                        },
                        "retire_failure"=> {
                            avibe_desktop_lib::FIXTURE_RETIRE_FAILURE.store(true,std::sync::atomic::Ordering::SeqCst);
                            emit(json!({"event":"control_ready"}));
                        },
                        "hold_error_view"=> {
                            avibe_desktop_lib::FIXTURE_ERROR_HOLD.store(true,std::sync::atomic::Ordering::SeqCst);
                            emit(json!({"event":"control_ready"}));
                        },
                        "release_error_view"=> avibe_desktop_lib::FIXTURE_ERROR_RELEASE.notify_one(),
                        "hold_presentation"=> {
                            avibe_desktop_lib::FIXTURE_PRESENTATION_HOLD.store(true,std::sync::atomic::Ordering::SeqCst);
                            emit(json!({"event":"control_ready"}));
                        },
                        "release_presentation"=> avibe_desktop_lib::FIXTURE_PRESENTATION_RELEASE.notify_one(),
                        "hold_retired_runtime"=> {
                            avibe_desktop_lib::FIXTURE_RETIRED_HOLD.store(true,std::sync::atomic::Ordering::SeqCst);
                            emit(json!({"event":"control_ready"}));
                        },
                        "release_retired_runtime"=> avibe_desktop_lib::FIXTURE_RETIRED_RELEASE.notify_one(),
''' + needle)
        example.write_text(text)
        native = source / "desktop/src-tauri/src/instance_client.rs"
        native_original = native.read_bytes()
        # Read-only owner observations: assertions wait for actual dispatch or
        # replacement, never an arbitrary absence-of-navigation delay.
        text = native.read_text()
        needle = "    fn guard_result(&mut self, attempt: &GuardAttempt, result: &str) {\n"
        assert text.count(needle) == 1
        text = text.replace(needle, needle + '''
        println!("{}", serde_json::json!({"event":"guard_result","token":attempt.token(),"view":attempt.intent.view,"result":result}));
''')
        start = text.index("fn apply_presentation(")
        end = text.index("\n/// The callback reports", start)
        apply = text[start:end]
        needle = "    tauri::async_runtime::spawn(async move {\n"
        assert apply.count(needle) == 1
        apply = apply.replace(needle, needle + '''
        if crate::FIXTURE_PRESENTATION_HOLD.swap(false, std::sync::atomic::Ordering::SeqCst) {
            println!("{}", serde_json::json!({"event":"presentation_held","id":intent.id}));
            tokio::time::timeout(std::time::Duration::from_secs(20), crate::FIXTURE_PRESENTATION_RELEASE.notified())
                .await.expect("fixture presentation release");
        }
''')
        needle = "        native.changed(&client.coordinator.state());"
        assert apply.count(needle) == 1
        apply = apply.replace(needle, needle + '''
        println!("{}",serde_json::json!({"event":"presentation_finished","id":intent.id,"ok":result.is_ok(),
            "error":result.as_ref().err().map(|error|error.code.as_str())}));
''')
        text = text[:start] + apply + text[end:]
        start = text.index("    async fn replace_local(")
        end = text.index("    fn local_url(", start)
        replacement = text[start:end]
        needle = "            return result;"
        assert replacement.count(needle) == 1
        replacement = replacement.replace(needle, '''
            println!("{}",serde_json::json!({"event":"replacement_finished","owner":owner,"ok":result.is_ok(),
                "error":result.as_ref().err().map(|error|error.code.as_str())}));
''' + needle)
        text = text[:start] + replacement + text[end:]
        start = text.index("    async fn retire_current(")
        end = text.index("    async fn build_candidate(", start)
        retirement = text[start:end]
        needle = "            self.retire_runtime(role).await?;"
        assert retirement.count(needle) == 1
        retirement = retirement.replace(needle, needle + '''
            if crate::FIXTURE_RETIRED_HOLD.swap(false, std::sync::atomic::Ordering::SeqCst) {
                println!("{}",serde_json::json!({"event":"runtime_retired_held"}));
                tokio::time::timeout(std::time::Duration::from_secs(15), crate::FIXTURE_RETIRED_RELEASE.notified())
                    .await.expect("fixture retired Runtime release");
            }
''')
        text = text[:start] + retirement + text[end:]
        needle = "            if !coordinator.state().connection.committed.is_local() {\n                return;"
        assert text.count(needle) == 1
        text = text.replace(needle, '''            if !coordinator.state().connection.committed.is_local() {
                println!("{}",serde_json::json!({"event":"local_rebound_remote_preserved"}));
                return;''')
        native.write_text(text + '''
pub fn fixture_presentation_snapshot(app: &AppHandle) {
    let handle = app.clone();
    app.run_on_main_thread(move || {
        let client = handle.state::<Client>();
        let queue = client.presentations.lock().unwrap();
        let physical = handle.get_window(MAIN_WINDOW).unwrap();
        let active = active_webview(&handle);
        let mut result = serde_json::json!({"event":"presentation_snapshot",
            "owner":client.coordinator.presentation_owner(),"window":queue.window,"document":queue.document,
            "pending":queue.pending.map(|intent|intent.id),"guard":queue.guard.as_ref().map(GuardAttempt::token),
            "approval":queue.approval.as_ref().map(GuardAttempt::token),"logout":queue.logout.is_some(),
            "label":active.as_ref().map(|view|view.label()),"visible":physical.is_visible().unwrap(),
            "minimized":physical.is_minimized().unwrap(),"focused":physical.is_focused().unwrap()});
        {
            let views = client.view.views.lock().unwrap();
            result["runtime"] = views.runtime.as_ref().map(|view| serde_json::json!({
                "label":view.label,"document":view.document,"window":view.window,"destination":view.destination
            })).into();
            result["returning"] = views.account.as_ref().and_then(|view|view.returning.as_ref())
                .map(|(label,document)|serde_json::json!({"label":label,"document":document})).into();
        }
        #[cfg(target_os="macos")]
        unsafe {
            use objc2::{msg_send,runtime::AnyObject};
            let native = &*physical.ns_window().unwrap().cast::<AnyObject>();
            let key: bool = msg_send![native,isKeyWindow];
            let visible: bool = msg_send![native,isVisible];
            let mini: bool = msg_send![native,isMiniaturized];
            let application: *mut AnyObject = msg_send![objc2::class!(NSApplication),sharedApplication];
            let application_active: bool = msg_send![application,isActive];
            result["native"] = serde_json::json!({"key":key,"visible":visible,"minimized":mini,"application_active":application_active});
        }
        println!("{}",result);
    }).unwrap();
}
''')
        needle = "        let result = retired.and(removed);"
        assert native.read_text().count(needle) == 1
        native.write_text(native.read_text().replace(needle, """
        let result = retired.and(removed);
        let result = if result.is_ok() && crate::FIXTURE_RETIRE_FAILURE.swap(false, std::sync::atomic::Ordering::SeqCst) {
            Err(failure("fixture retirement result"))
        } else { result };
"""))
        needle = "                    view.set_focus().map_err(failure)?;\n                    epoch.check()"
        assert native.read_text().count(needle) == 1
        native.write_text(native.read_text().replace(needle, """
                view.set_focus().map_err(failure)?;
                if crate::FIXTURE_SHOW_FAILURE.swap(false, std::sync::atomic::Ordering::SeqCst) {
                    return Err(failure("fixture terminal show result"));
                }
                epoch.check()"""))
        needle = "    async fn signout_failed(&self, epoch: Epoch) -> Result<(), ClientError> {\n"
        if needle in native.read_text():
            native.write_text(native.read_text().replace(needle, needle + """
        if crate::FIXTURE_ERROR_HOLD.swap(false, std::sync::atomic::Ordering::SeqCst) {
            println!("{}", serde_json::json!({"event":"error_view_held"}));
            tokio::time::timeout(std::time::Duration::from_secs(20), crate::FIXTURE_ERROR_RELEASE.notified())
                .await.expect("fixture error view release");
        }
"""))
        shell_adaptations = adapt_shell(args, source) if args.shell_consumers else {}
        # The operator owns the desktop. Only the explicitly selected foreground
        # batch may activate a fixture window; all other cases stay off screen.
        # Substitute final OS effects only, never admission or the guard result.
        library.write_text(library.read_text() + """
pub fn fixture_focus(focus: impl FnOnce() -> tauri::Result<()>) -> tauri::Result<()> {
    if std::env::var("AVIBE_FIXTURE_FOREGROUND").as_deref() == Ok("1") { focus() } else { Ok(()) }
}
pub static FIXTURE_RECEIPT_RELEASE: tokio::sync::Notify = tokio::sync::Notify::const_new();
""")
        for path in (library, native):
            text = path.read_text()
            for receiver in ("physical", "window", "view"):
                text = text.replace(f"{receiver}.set_focus()", f"crate::fixture_focus(|| {receiver}.set_focus())")
            path.write_text(text)
        text = library.read_text()
        needle = "    let _ = native_frame::clamp(&window.window());"
        assert text.count(needle) == 1
        library.write_text(text.replace(needle, """
    if std::env::var("AVIBE_FIXTURE_FOREGROUND").as_deref() == Ok("1") {
        let _ = native_frame::clamp(&window.window());
    }
"""))
        native.write_text(native.read_text() + """
pub fn fixture_initial_account(app: &AppHandle) {
    let owner = app.state::<Client>().coordinator.presentation_owner();
    let locale = match std::env::var("AVIBE_FIXTURE_LOCALE").as_deref() {
        Ok("zh") => "zh", _ => "en",
    };
    present_owned(app, "account", owner, false, None, Some(locale), None);
}
""")
        text = example.read_text()
        needle = '            instance_client::present(app.handle(),"account");'
        assert text.count(needle) == 1
        text = text.replace(needle, """
            if std::env::var("AVIBE_FIXTURE_FOREGROUND").as_deref() != Ok("1") {
                app.get_window("main").unwrap().set_position(tauri::LogicalPosition::new(-20000., -20000.)).unwrap();
            }
            instance_client::fixture_initial_account(app.handle());
""")
        needle = "    let reply = instance_client::desktop_client_request(window, app, request).await;"
        assert text.count(needle) == 1
        text = text.replace(needle, """
    let receipt_observer = std::env::var("AVIBE_FIXTURE_RECEIPT_OBSERVER").as_deref() == Ok("1")
        && request.request_id != "feedback-fixture" && matches!(request.action, ClientAction::GetState);
    let fixture_action = serde_json::to_value(&request.action).unwrap()["type"].clone();
    let reply = instance_client::desktop_client_request(window, app, request).await;
    if receipt_observer && matches!(&reply, Ok(reply) if matches!(reply.result, Some(DirectResult::SignOutIntent { .. }))) {
        // The native receipt tests inspect the genuine result before AccountApp
        // consumes it. Ordinary product-consumer cases never enable this hold.
        emit(json!({"event":"receipt_result_held"}));
        let _ = tokio::time::timeout(std::time::Duration::from_secs(20),
            avibe_desktop_lib::FIXTURE_RECEIPT_RELEASE.notified()).await;
    }
""")
        needle = 'emit(json!({"event":"reply","error":match &reply'
        assert text.count(needle) == 1
        text = text.replace(needle, 'emit(json!({"event":"reply","action":fixture_action,"error":match &reply')
        needle = '"state"=>emit('
        assert text.count(needle) == 1
        text = text.replace(needle, '''"release_receipt"=> avibe_desktop_lib::FIXTURE_RECEIPT_RELEASE.notify_waiters(),
                        ''' + needle)
        example.write_text(text)
        text = example.read_text()
        needle = "                            lifecycle.change(Some(lifecycle.local.clone()));"
        assert text.count(needle) == 1
        example.write_text(text.replace(needle, '''                            let origin = command["origin"].as_str()
                                .map(|value|LoopbackOrigin::parse(value).unwrap()).unwrap_or_else(||lifecycle.local.clone());
                            lifecycle.change(Some(origin));'''))
        update_adaptations = adapt_update_consumers(source) if args.update_consumers else {}
        build(args, source, "", {
            "head":args.head, "bundle_sha256":args.bundle_sha256, "mode":"account-feedback",
            "library_original_sha256":hashlib.sha256(original_library).hexdigest(),
            "library_fixture_sha256":hashlib.sha256(library.read_bytes()).hexdigest(),
            "native_original_sha256":hashlib.sha256(native_original).hexdigest(),
            "native_fixture_sha256":hashlib.sha256(native.read_bytes()).hexdigest(),
            "updater_sha256":hashlib.sha256((source / "desktop/src-tauri/src/updater.rs").read_bytes()).hexdigest(),
            "updater_key":"empty", "shell_adaptations": shell_adaptations, "update_adaptations": update_adaptations, "adaptations":["archive-only call to existing native menu owner",
                                                 "optional one-shot failure after actual store retirement",
                                                 "bounded pause before error presentation owner check", "optional one-shot failure after actual native show/focus",
                                                 "quiet default: offscreen main and suppressed final OS focus/clamp only; foreground batch explicitly opts out",
                                                 "initial synthetic locale; optional bounded automatic receipt-result hold for native receipt observers only"],
        })
        return
    # Fail if a source change moves the transport boundary; never fuzzy-patch
    # the discovery/decision/installation code.
    updater = source / "desktop/src-tauri/src/updater.rs"
    original = updater.read_text()
    needle = 'fn client() -> Result<reqwest::Client, String> {\n    reqwest::Client::builder()\n'
    assert original.count(needle) == 1
    replacement = '''fn client() -> Result<reqwest::Client, String> {
    let config: serde_json::Value = serde_json::from_slice(
        &std::fs::read(std::env::var("AVIBE_FIXTURE_UPDATE_CONFIG").expect("fixture config"))
            .expect("fixture config file")).expect("fixture config JSON");
    let certificate = reqwest::Certificate::from_pem(
        &std::fs::read(config["certificate"].as_str().expect("fixture certificate")).expect("certificate"))
        .expect("fixture certificate PEM");
    let proxy = reqwest::Proxy::all(config["proxy"].as_str().expect("fixture proxy")).expect("proxy");
    reqwest::Client::builder()
        .proxy(proxy)
        .tls_built_in_root_certs(false)
        .add_root_certificate(certificate)
        .redirect(reqwest::redirect::Policy::none())
'''
    patched = original.replace(needle, replacement)
    legacy_peripheral_calls = [
        "super::request_and_open_desktop_updates(&app);",
        "notify_remote_request(&app);",
    ]
    # Frozen host-confirmation heads have both side effects; explicit-click
    # heads have neither. Reject partial/duplicated drift rather than silently
    # weakening the archived fixture's boundary or inventing removed effects.
    counts = tuple(patched.count(call) for call in legacy_peripheral_calls)
    assert counts in {(0, 0), (1, 1)}, ("unexpected legacy updater side effects", counts)
    peripheral_calls = legacy_peripheral_calls if counts == (1, 1) else []
    for call in peripheral_calls:
        patched = patched.replace(call, 'eprintln!("fixture: automatic update notification deferred");')
    updater.write_text(patched)
    shell_adaptations = adapt_shell(args, source) if args.shell_consumers else {}
    build(args, source, (source / "desktop/runtime-host/tests/fixtures/updater/public-key.txt").read_text().strip(), {
        "head": args.head, "bundle_sha256": args.bundle_sha256,
        "updater_original_sha256": hashlib.sha256(original.encode()).hexdigest(),
        "updater_fixture_sha256": hashlib.sha256(updater.read_bytes()).hexdigest(),
        "fixture_public_key": "desktop/runtime-host/tests/fixtures/updater/public-key.txt",
        "recorded_peripheral_calls": peripheral_calls, "shell_adaptations": shell_adaptations,
    })


def adapt_update_consumers(source):
    """Observe real popup/Workbench commands; delay results, never fabricate them.

    The popup's initial focus and final placement are additional OS effects not
    covered by the older main-only quiet adapter. This adapter keeps the actual
    parent, receiver, navigation, presentation and updater admission.
    """
    library = source / "desktop/src-tauri/src/lib.rs"
    example = source / "desktop/src-tauri/examples/instance_client_probe.rs"
    popup = source / "desktop/src-tauri/src/update_popover.rs"
    native = source / "desktop/src-tauri/src/instance_client.rs"
    paths = (library, example, popup, native)
    before = {str(path.relative_to(source)): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
    needle = ".eval_with_callback(open_updates_script(id, legacy_fallback), move |result| {\n"
    assert library.read_text().count(needle) == 1
    library.write_text(library.read_text().replace(needle, needle + '''
            println!("{}",serde_json::json!({"event":"update_delivery","id":id,"result":result}));
'''))
    library.write_text(library.read_text() + """
pub mod fixture_updates {
    use super::{instance_client, update_popover, updater};
    use tauri::{AppHandle, Manager, Webview};
    use url::Url;
    use std::sync::atomic::{AtomicBool, Ordering};
    static HOLDS: [AtomicBool; 4] = [const { AtomicBool::new(false) }; 4];
    static RELEASES: [tokio::sync::Notify; 4] = [const { tokio::sync::Notify::const_new() }; 4];
    static STATE_EXCLUDE: std::sync::Mutex<Option<String>> = std::sync::Mutex::new(None);
    static STATE_SKIP: std::sync::atomic::AtomicUsize = std::sync::atomic::AtomicUsize::new(0);
    fn index(name: &str) -> usize {
        match name { "build"=>0, "finished"=>1, "state"=>2, "client"=>3, _=>panic!("unknown update hold") }
    }
    pub async fn pause(name: &str) {
        let index = index(name);
        if HOLDS[index].swap(false, Ordering::SeqCst) {
            println!("{}",serde_json::json!({"event":"update_held","phase":name}));
            tokio::time::timeout(std::time::Duration::from_secs(30), RELEASES[index].notified())
                .await.expect("fixture update release");
            println!("{}",serde_json::json!({"event":"update_released","phase":name}));
        }
    }
    pub fn navigation(view: &Webview, url: &Url) -> bool {
        instance_client::bundle(view.app_handle())
            .is_some_and(|bundle| update_popover::navigation_allowed(url, &bundle))
    }
    pub fn snapshot(app: &AppHandle) -> serde_json::Value { updater::client_snapshot(app) }
    pub fn control(app: &AppHandle, command: serde_json::Value) {
        let operation = command["operation"].as_str().unwrap();
        match operation {
            "hold" => {
                if command["phase"] == "state" {
                    *STATE_EXCLUDE.lock().unwrap()=command["exclude"].as_str().map(str::to_owned);
                    STATE_SKIP.store(command["skip"].as_u64().unwrap_or(0) as usize, Ordering::SeqCst);
                }
                HOLDS[index(command["phase"].as_str().unwrap())].store(true, Ordering::SeqCst);
            },
            "release" => RELEASES[index(command["phase"].as_str().unwrap())].notify_one(),
            "close" => { app.get_webview_window("update-popover").expect("actual popup").destroy().unwrap(); },
            "snapshot" => {
                let handle=app.clone();
                app.run_on_main_thread(move || {
                    let popup=handle.get_webview_window("update-popover");
                    println!("{}",serde_json::json!({"event":"popup_snapshot","popup":popup.map(|view|
                        serde_json::json!({"label":view.label(),"physical":view.as_ref().window().label(),
                        "url":view.url().unwrap(),"visible":view.is_visible().unwrap(),
                        "focused":view.is_focused().unwrap(),"position":view.outer_position().unwrap()}))}));
                }).unwrap();
            },
            _=>panic!("unsupported update control"),
        }
        println!("{}",serde_json::json!({"event":"update_control","operation":operation}));
    }
    #[tauri::command]
    async fn desktop_update_state(window: Webview, app: AppHandle) -> Result<updater::DesktopUpdateState,String> {
        let label=window.label().to_owned();
        let result=updater::desktop_update_state(window, app);
        println!("{}",serde_json::json!({"event":"updater_probe","label":label,"result":result}));
        let excluded=STATE_EXCLUDE.lock().unwrap().as_deref()==Some(&label);
        if !excluded && STATE_SKIP.fetch_update(Ordering::SeqCst,Ordering::SeqCst,|n|n.checked_sub(1)).is_err() {
            pause("state").await;
        }
        result
    }
    #[tauri::command]
    fn desktop_update_auto_update_set(window: Webview, app: AppHandle, enabled: bool) -> Result<bool,String> {
        let label=window.label().to_owned();
        let result=updater::desktop_update_auto_update_set(window, app, enabled);
        println!("{}",serde_json::json!({"event":"updater_setting","label":label,"result":result}));
        result
    }
    #[tauri::command]
    fn desktop_update_panel_ready(window: Webview) -> Result<(),String> {
        let label=window.label().to_owned();
        let result=super::desktop_update_panel_ready(window);
        println!("{}",serde_json::json!({"event":"updater_ready","label":label,"result":result}));
        result
    }
    pub fn invoke(invoke: tauri::ipc::Invoke) -> bool {
        let handler: Box<dyn Fn(tauri::ipc::Invoke)->bool+Send+Sync> = Box::new(tauri::generate_handler![
            self::desktop_update_state,self::desktop_update_auto_update_set,self::desktop_update_panel_ready,
            super::bootstrap_status,super::notifications::desktop_notifications_state,
            super::notifications::desktop_notifications_set_enabled
        ]);
        handler(invoke)
    }
}
""")
    text = example.read_text()
    needle = 'json!({"phase":"idle","channel":"stable","prereleases":false,"interactive":false})'
    # Initial client state must use the actual initialized updater snapshot,
    # like production. A minimal historical fixture value lacks its actions.
    start = text.index("record,store,local_origin:")
    prefix, suffix = text[:start], text[start:]
    assert prefix.count(needle) == 1
    text = prefix.replace(needle, "avibe_desktop_lib::fixture_updates::snapshot(app.handle())") + suffix
    needle = '    let fixture_action = serde_json::to_value(&request.action)'
    assert text.count(needle) == 1
    text = text.replace(needle, '    let fixture_label = window.label().to_owned();\n' + needle)
    text = text.replace('"event":"reply","action":fixture_action', '"event":"reply","label":fixture_label,"action":fixture_action')
    needle = ".invoke_handler(tauri::generate_handler![desktop_client_request])"
    assert text.count(needle) == 1
    text = text.replace(needle, """.invoke_handler(|invoke| {
        if invoke.message.command() == "desktop_client_request" {
            let handler: Box<dyn Fn(tauri::ipc::Invoke)->bool+Send+Sync> =
                Box::new(tauri::generate_handler![desktop_client_request]);
            handler(invoke)
        } else { avibe_desktop_lib::fixture_updates::invoke(invoke) }
    })""")
    needle = '                        "state"=>emit('
    assert text.count(needle) == 1
    text = text.replace(needle, '''                        "update_control"=>avibe_desktop_lib::fixture_updates::control(&handle,command.clone()),
''' + needle)
    needle = "                if instance_client::presentation_request("
    assert text.count(needle) == 1
    text = text.replace(needle, '''                if view.label()=="update-popover" {
                    return avibe_desktop_lib::fixture_updates::navigation(view,url);
                }
''' + needle)
    # A missing explicit receiver must never fall back to the active Account.
    needle = 'command["label"].as_str().and_then(|label|handle.get_webview(label)).or_else(||instance_client::active_webview(&handle))'
    assert text.count(needle) == 1
    text = text.replace(needle, 'match command["label"].as_str() { Some(label)=>handle.get_webview(label), None=>instance_client::active_webview(&handle) }')
    example.write_text(text)
    text = popup.read_text()
    start = text.index("pub(crate) fn open(")
    end = text.index("    let window = app.get_webview_window(UPDATE_POPOVER);", start)
    admission = text[start:end]
    assert admission.count("return false;") == 1
    admission = admission.replace("return false;", '''
        println!("{}",serde_json::json!({"event":"popup_open_rejected",
            "physical_main":app.get_window(crate::MAIN_WINDOW).is_some(),
            "webview_window_main":app.get_webview_window(crate::MAIN_WINDOW).is_some()}));
        return false;''')
    text = text[:start] + admission + text[end:]
    assert text.count(".focused(true)") == 1
    text = text.replace(".focused(true)", ".focused(false)")
    text = text.replace("window.set_focus()", "crate::fixture_focus(|| window.set_focus())")
    assert text.count(".position(x, y)") == 1
    text = text.replace(".position(x, y)", ".position(-20000.0, -20000.0)")
    # The real window remains parented. Suppress only the non-macOS placement.
    text = text.replace('if !cfg!(target_os = "macos") {', 'if false {')
    if "struct Popup {" in text:
        for function, phase in [("fn spawn_build(", "build"), ("fn page_loaded(", "finished")]:
            start = text.index(function)
            point = text.index("tauri::async_runtime::spawn(async move {", start) + len("tauri::async_runtime::spawn(async move {")
            text = text[:point] + f'\n        crate::fixture_updates::pause("{phase}").await;\n' + text[point:]
        needle = "        let _ = window.show();"
        assert text.count(needle) == 1
        text = text.replace(needle, needle + '\n        println!("{}",serde_json::json!({"event":"popup_shown"}));')
        needle = "            retire(&app, &bundle, &popup);\n        }\n    });"
        assert text.count(needle) == 1
        text = text.replace(needle, needle.replace("\n    });", '\n        println!("{}",serde_json::json!({"event":"popup_build_settled"}));\n    });'))
        needle = "            let _ = window.destroy();\n            return;"
        assert text.count(needle) == 1
        text = text.replace(needle, '            println!("{}",serde_json::json!({"event":"popup_stale_finished"}));\n' + needle)
    popup.write_text(text)
    text = native.read_text()
    needle = "        let reply = client.coordinator.request(request).await;"
    if needle in text:
        assert text.count(needle) == 1
        text = text.replace(needle, needle + '\n        crate::fixture_updates::pause("client").await;')
    native.write_text(text)
    support = source / "desktop/src-tauri/examples/support/fixture_shell.rs"
    text = support.read_text()
    needle = '"pending": app.state::<Mutex<DeepLinks>>().lock().unwrap().has_pending(),'
    assert text.count(needle) == 1
    text = text.replace(needle, needle + '\n        "pending_update": app.state::<PendingDesktopUpdateOpen>().current(),')
    needle = '            "updates" => assert!(super::open_desktop_updates(&handle)),'
    assert text.count(needle) == 1
    support.write_text(text.replace(needle, needle + '\n            "queue_updates" => super::request_desktop_updates(&handle),'))
    return {"before": before, "after": {
        str(path.relative_to(source)): hashlib.sha256(path.read_bytes()).hexdigest() for path in (*paths, support)
    }}


def adapt_shell(args, source):
    shell_adaptations = {}
    support = source / "desktop/src-tauri/examples/support/fixture_shell.rs"
    if args.shell_fixture:
        shutil.copy2(args.shell_fixture, support)
    support_text = support.read_text()
    needle = '            "hide" => handle.get_window(MAIN_WINDOW).unwrap().hide().unwrap(),'
    assert support_text.count(needle) == 1
    support.write_text(support_text.replace(needle, needle + '''
            "toggle_notifications" => notifications::toggle(&handle),
            "minimize" => handle.get_window(MAIN_WINDOW).unwrap().minimize().unwrap(),
            #[cfg(target_os="macos")]
            "background" => unsafe {
                use objc2::{msg_send,runtime::AnyObject};
                let application: *mut AnyObject = msg_send![objc2::class!(NSApplication),sharedApplication];
                let _: () = msg_send![application,deactivate];
            },
'''))
    shell_adaptations["support_sha256"] = hashlib.sha256(support.read_bytes()).hexdigest()
    library = source / "desktop/src-tauri/src/lib.rs"
    library_source = library.read_text()
    shell_adaptations["library_original_sha256"] = hashlib.sha256(library.read_bytes()).hexdigest()
    if "fn main_document_finished(" not in library_source:
        # Frozen pre-fix red assembly: mechanically extract the actual
        # callback bytes, exactly as in the production refactor.
        start = library_source.index("                        // The native frame is independent of the Runtime UI version;")
        end = library_source.index("\n                    }\n                })\n                .on_navigation", start)
        body = library_source[start:end]
        helper = "fn main_document_finished(webview: &tauri::Webview) {\n" + "".join(
            line[20:] + "\n" for line in body.splitlines()
        ) + "}\n\n"
        library_source = library_source[:start] + "                        main_document_finished(webview);" + library_source[end:]
        library_source = library_source.replace("fn receive_native_deep_link(", helper + "fn receive_native_deep_link(", 1)
        shell_adaptations["extracted_frozen_finished_callback"] = True
    event_start = library_source.index("                .on_event(|app, event| {\n") + len("                .on_event(|app, event| {\n")
    event_end = library_source.index("\n                })", event_start)
    events = library_source[event_start:event_end]
    shell_adaptations["shell_event_source_sha256"] = hashlib.sha256(events.encode()).hexdigest()
    # Execute the actual main event handler for WindowEvent/Reopen only. Test
    # teardown owns process exit; it must not start the inert RuntimeHost.
    event_wrapper = "\npub fn fixture_main_event(app: &AppHandle, event: &RunEvent) {\nif matches!(event, RunEvent::ExitRequested{..}) { return; }\n" + events + "\n}\n"
    opener = 'if tauri_plugin_opener::open_url(url.as_str(), None::<&str>).is_err() {'
    assert library_source.count(opener) == 1
    library_source = library_source.replace(opener, '''println!("{}", serde_json::json!({"event":"external_browser_request","url":url.as_str()}));
            if false {''')
    library.write_text(library_source + event_wrapper + '\n#[path = "../examples/support/fixture_shell.rs"]\npub mod fixture_shell;\n')
    native_client = source / "desktop/src-tauri/src/instance_client.rs"
    client_source = native_client.read_text()
    shell_adaptations["instance_client_original_sha256"] = hashlib.sha256(native_client.read_bytes()).hexdigest()
    start = client_source.index("pub(crate) fn return_local_for_link(")
    end = client_source.index("\n}\n", start) + 3
    return_source = client_source[start:end]
    needle = "    tauri::async_runtime::spawn(async move {\n"
    assert return_source.count(needle) == 1
    return_source = return_source.replace(
        needle, needle + "        crate::fixture_shell::before_link_return(&app).await;\n",
    )
    client_source = client_source[:start] + return_source + client_source[end:]
    needle = "        crate::apply_pending_deep_link(&app);\n    });\n}"
    assert client_source.count(needle) == 1
    client_source = client_source.replace(
        needle, '        println!("{}", serde_json::json!({"event":"link_return_finished"}));\n' + needle,
    )
    native_client.write_text(client_source)
    shell_adaptations["instance_client_fixture_sha256"] = hashlib.sha256(native_client.read_bytes()).hexdigest()
    shell_adaptations["link_admission_instrumentation"] = "bounded pause after the actual claim, before the actual coordinator call"
    example = source / "desktop/src-tauri/examples/instance_client_probe.rs"
    example_source = example.read_text()
    replacements = {
        "avibe_desktop_lib::updater::init(app.handle())?;":
            "avibe_desktop_lib::fixture_shell::prepare(app.handle(), &setup.root);\n            avibe_desktop_lib::updater::init(app.handle())?;",
        "let local_origin=Arc::new(Mutex::new(Some(local.clone())));":
            "let local_origin=avibe_desktop_lib::fixture_shell::attach(app.handle(), local.clone(), &setup.root);",
        "instance_client::page_loaded(view.app_handle());":
            "avibe_desktop_lib::fixture_shell::finished(view);",
        'instance_client::present(app.handle(),"account");':
            'instance_client::present(app.handle(),"account");\n            avibe_desktop_lib::fixture_shell::start(app.handle());',
        '                        "state"=>emit(':
            '''                        "native_link"=> avibe_desktop_lib::fixture_shell::receive(&handle, command["url"].as_str().unwrap().into()),
                    "shell_control"=> avibe_desktop_lib::fixture_shell::control(&handle, command.clone()),
                    "shell_status"=> emit(avibe_desktop_lib::fixture_shell::status(&handle)),
                    "state"=>emit(''',
        "    app.run(|app, event| {": """    app.run(|app, event| {
        avibe_desktop_lib::fixture_main_event(app, &event);
        if avibe_desktop_lib::fixture_shell::enabled() {
            if let tauri::RunEvent::ExitRequested { api, code: None, .. } = &event { api.prevent_exit(); }
        }""",
    }
    if args.account_feedback:
        # Lifecycle-only shell fixtures do not start notification/control polling.
        replacements.pop('instance_client::present(app.handle(),"account");')
    for needle, replacement in replacements.items():
        assert example_source.count(needle) == 1, needle
        example_source = example_source.replace(needle, replacement)
    example.write_text(example_source)
    # Keep the production output gates. Only the final OS effects are
    # recorded; no notification, badge or global shortcut reaches the user.
    notifications = source / "desktop/src-tauri/src/notifications.rs"
    notification_source = notifications.read_text()
    notification_source = notification_source.replace("use tauri_plugin_notification::NotificationExt;\n", "")
    needle = """            if sink
                .app
                .notification()
                .builder()
                .title(copy.title)
                .body(copy.body)
                .show()
                .is_err()
            {
                eprintln!("The desktop notification could not be submitted to the operating system.");
            }"""
    if needle in notification_source:
        assert notification_source.count(needle) == 1
        notification_source = notification_source.replace(needle, '''            println!("{}", serde_json::json!({"event":"notification_output","title":copy.title,"body":copy.body}));''')
    else:
        needle = "native::show(&sink.app, &receipt, copy, sink.registry.clone())"
        assert notification_source.count(needle) == 1
        notification_source = notification_source.replace(needle, '''{
            println!("{}", serde_json::json!({"event":"notification_output","title":copy.title,"body":copy.body}));
            Ok::<(), String>(())
        }''')
    needle = "let _ = window.set_badge_count(badge_count(total));"
    assert notification_source.count(needle) == 1
    notification_source = notification_source.replace(needle, '''let _ = window;\n        println!("{}", serde_json::json!({"event":"badge_output","count":badge_count(total)}));''')
    needle = "                    poll_sink.desktop_update_requested();"
    assert notification_source.count(needle) == 1
    notification_source = notification_source.replace(needle, '''                    println!("{}", serde_json::json!({"event":"mailbox_poll"}));
                poll_sink.desktop_update_requested();''')
    notifications.write_text(notification_source)
    notification_source = notifications.read_text()
    needle = "fn dispatch(window: &Webview, enabled: bool) {\n"
    assert notification_source.count(needle) == 1
    notification_source = notification_source.replace(needle, needle + '''
    println!("{}",serde_json::json!({"event":"notification_preference_dispatched","label":window.label(),"enabled":enabled}));
''')
    needle = "    let saved = app.state::<Notifications>().set_enabled(enabled)?;"
    assert notification_source.count(needle) == 1
    notifications.write_text(notification_source.replace(needle, needle + '''
    println!("{}",serde_json::json!({"event":"notification_preference_saved","enabled":saved}));
'''))
    stream = source / "desktop/runtime-host/src/notifications.rs"
    stream_source = stream.read_text()
    needle = '                    if frame.event == "desktop.update.requested" {\n                        sink.desktop_update_requested();'
    assert stream_source.count(needle) == 1
    stream.write_text(stream_source.replace(needle, '''                    if frame.event == "desktop.update.requested" {
                    println!("{}", serde_json::json!({"event":"sse_update_nudge"}));
                    sink.desktop_update_requested();'''))
    pet = source / "desktop/src-tauri/src/pet.rs"
    pet_source = pet.read_text()
    start = pet_source.index("pub fn apply_shortcuts(")
    end = pet_source.index("\nfn set_enabled(", start)
    shortcuts_source = pet_source[start:end]
    # Preserve the actual reveal/settings producer; replace only registration
    # effects, never dispatch_settings or its current-Workbench admission.
    for needle, replacement in [
        ("    let shortcuts = app.global_shortcut();\n", ""),
        ("|shortcut| shortcuts.register(shortcut).map_err(|error| error.to_string())",
         '|shortcut| { println!("fixture register shortcut: {shortcut}"); Ok::<(), String>(()) }'),
        ("|shortcut| shortcuts.unregister(shortcut).map_err(|error| error.to_string())",
         '|shortcut| { println!("fixture unregister shortcut: {shortcut}"); Ok::<(), String>(()) }'),
    ]:
        assert shortcuts_source.count(needle) == 1, needle
        shortcuts_source = shortcuts_source.replace(needle, replacement)
    pet_source = pet_source[:start] + shortcuts_source + pet_source[end:]
    needle = "    dispatch_view(\n        &window,\n        SETTINGS_EVENT,"
    assert pet_source.count(needle) == 1
    pet_source = pet_source.replace(needle,
        '    println!("{}",serde_json::json!({"event":"mini_settings_dispatched","label":window.label()}));\n' + needle)
    pet.write_text(pet_source)
    shell_adaptations["recorded_peripheral_calls"] = ["notification.show", "window.set_badge_count", "shortcut.register", "shortcut.unregister", "open_url"]
    return shell_adaptations


def build(args, source, public_key, record):
    environment = {key: os.environ[key] for key in ("PATH", "LANG", "CARGO_HOME", "RUSTUP_HOME") if key in os.environ}
    environment.setdefault("CARGO_HOME", str(Path.home() / ".cargo"))
    environment.setdefault("RUSTUP_HOME", str(Path.home() / ".rustup"))
    for key in ("HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "TMPDIR"):
        path = args.output / key.lower()
        path.mkdir()
        environment[key] = str(path)
    environment["CARGO_TARGET_DIR"] = str(args.target_dir.resolve())
    environment["AVIBE_DESKTOP_UPDATER_PUBLIC_KEY"] = public_key
    command = [
        "cargo", "build", "--manifest-path", "desktop/Cargo.toml", "-p", "avibe-desktop",
        "--example", "instance_client_probe", "--locked", "--offline",
        "--features", "tauri/custom-protocol,tauri/macos-proxy",
    ]
    with (args.output / "build.log").open("w") as log:
        subprocess.run(command, cwd=source, env=environment, stdout=log, stderr=subprocess.STDOUT, check=True)
    executable = args.output / "instance_client_probe"
    shutil.copy2(args.target_dir / "debug/examples/instance_client_probe", executable)
    assert not any(part.endswith(".app") for part in executable.resolve().parts)
    record.update({"command":command, "binary_sha256":hashlib.sha256(executable.read_bytes()).hexdigest(),
                   "binary":str(executable), "source":str(source),
                   "builder_sha256":hashlib.sha256(Path(__file__).read_bytes()).hexdigest()})
    (args.output / "provenance.json").write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps(record))


if __name__ == "__main__":
    main()
