//! Guards on the shell's two load-bearing boundaries.
//!
//! Both are enforced by configuration and narrow ownership — a capability that
//! names no remote URL, and code that never sends raw process signals. Explicit
//! uninstall may invoke the Runtime's own graceful CLI, so the source boundary
//! is asserted directly alongside the capability contract.

use std::path::{Path, PathBuf};

use serde_json::Value;

const MAIN_WINDOW: &str = "main";

fn crate_dir() -> &'static Path {
    Path::new(env!("CARGO_MANIFEST_DIR"))
}

fn read_to_string(path: &Path) -> String {
    std::fs::read_to_string(path).unwrap_or_else(|error| panic!("{} is readable: {error}", path.display()))
}

fn read_json(path: PathBuf) -> Value {
    serde_json::from_str(&read_to_string(&path))
        .unwrap_or_else(|error| panic!("{} is valid JSON: {error}", path.display()))
}

fn capability() -> Value {
    read_json(crate_dir().join("capabilities").join("bootstrap.json"))
}

fn config() -> Value {
    read_json(crate_dir().join("tauri.conf.json"))
}

/// Source with its test module removed, so a test's own vocabulary cannot
/// satisfy a check about the shipping code.
fn shipping_source(relative: &str) -> String {
    let path = crate_dir().join(relative);
    let source = read_to_string(&path);
    match source.find("#[cfg(test)]") {
        Some(offset) => source[..offset].to_owned(),
        None => source,
    }
}

#[test]
fn the_bootstrap_capability_is_never_granted_to_remotely_loaded_pages() {
    let capability = capability();
    // A `remote` entry is the only way a Tauri capability reaches a page loaded
    // over http. The Workbench, and every Show Page inside it, is such a page.
    assert!(
        capability.get("remote").is_none(),
        "the bootstrap capability must not name remote URLs"
    );
    assert_eq!(capability["local"], Value::Bool(true));
}

#[test]
fn the_bootstrap_capability_is_scoped_to_the_shell_window() {
    assert_eq!(
        capability()["windows"],
        serde_json::json!([MAIN_WINDOW]),
        "the capability must name exactly the shell's own window"
    );
}

#[test]
fn the_bootstrap_capability_grants_only_the_three_shell_commands() {
    let capability = capability();
    let permissions: Vec<&str> = capability["permissions"]
        .as_array()
        .expect("permissions is a list")
        .iter()
        .map(|value| value.as_str().expect("permissions are strings"))
        .collect();

    assert_eq!(
        permissions,
        [
            "core:event:default",
            "allow-bootstrap-status",
            "allow-bootstrap-retry",
            "allow-open-install-docs",
        ],
        "widening this list widens what a WebView can ask the shell to do"
    );
}

#[test]
fn the_shell_enables_no_capability_beyond_bootstrap() {
    assert_eq!(
        config()["app"]["security"]["capabilities"],
        serde_json::json!(["bootstrap"])
    );
}

#[test]
fn product_bundles_have_an_explicit_private_runtime_gate() {
    let cargo = read_to_string(&crate_dir().join("Cargo.toml"));
    let source = shipping_source("src/lib.rs");
    assert!(
        cargo.contains("bundled-runtime = []"),
        "consumer packaging must select the private Runtime explicitly"
    );
    for required in [
        "#[cfg(feature = \"bundled-runtime\")]",
        "bundled_runtime_host(",
        "app.path().resource_dir()?.join(\"runtime\")",
        "app.path().app_local_data_dir()?.join(\"runtime\")",
    ] {
        assert!(
            source.contains(required),
            "the product package must install its own Runtime, missing {required:?}"
        );
    }
    assert_eq!(
        config()["bundle"]["resources"]["resources/runtime/"],
        Value::String("runtime/".to_owned())
    );
}

#[test]
fn every_shell_command_is_declared_so_its_permission_exists() {
    // Application commands are ungated in Tauri v2 unless declared here; an
    // undeclared command has no `allow-*` permission and so no capability can
    // scope it.
    let build_rs = shipping_source("build.rs");
    let lib_rs = shipping_source("src/lib.rs");

    let declared: Vec<&str> = ["bootstrap_status", "bootstrap_retry", "open_install_docs"]
        .into_iter()
        .filter(|command| build_rs.contains(command))
        .collect();
    assert_eq!(declared, ["bootstrap_status", "bootstrap_retry", "open_install_docs"]);

    let defined = lib_rs.matches("#[tauri::command]").count();
    assert_eq!(
        defined,
        declared.len(),
        "every #[tauri::command] must be declared in build.rs and allowed by a capability"
    );
}

#[test]
fn installation_help_is_a_fixed_bootstrap_only_destination() {
    let source = shipping_source("src/lib.rs");
    assert!(
        source.contains("https://docs.avibe.bot/get-started/install"),
        "the install action must use the product installation guide"
    );
    assert!(
        source.contains("fn open_install_docs(window: WebviewWindow)"),
        "the command must accept no URL or protocol argument from the WebView"
    );
}

#[test]
fn the_window_label_is_consistent_across_config_and_code() {
    let config = config();
    let windows = config["app"]["windows"].as_array().expect("windows is a list");
    assert_eq!(windows.len(), 1, "the shell owns exactly one window");
    assert_eq!(windows[0]["label"], MAIN_WINDOW);
    assert!(
        shipping_source("src/lib.rs").contains(&format!("MAIN_WINDOW: &str = {MAIN_WINDOW:?}")),
        "the window label in tauri.conf.json and lib.rs must agree"
    );
}

#[test]
fn the_dev_server_url_matches_the_port_the_host_trusts() {
    let expected = format!("http://localhost:{}", avibe_runtime_host::DEV_SERVER_PORT);
    assert_eq!(config()["build"]["devUrl"], Value::String(expected));
}

#[test]
fn normal_shell_lifecycle_has_no_raw_process_termination_path() {
    for file in ["src/lib.rs", "src/main.rs"] {
        let source = shipping_source(file);
        for forbidden in [".kill(", "libc::kill", "taskkill", "SIGTERM", "SIGKILL"] {
            assert!(
                !source.contains(forbidden),
                "{file} must not bypass the Runtime's graceful lifecycle, found {forbidden:?}"
            );
        }
    }
}

#[test]
fn product_packages_expose_an_explicit_private_runtime_uninstall_path() {
    let source = shipping_source("src/lib.rs");
    for required in [
        "UNINSTALL_MENU_ID",
        "fn request_private_runtime_removal",
        "host.remove_private_runtime(active_origin.as_ref()).await",
        "native_uninstall_catalog()",
        "sys_locale::get_locales()",
        "../../../ui/src/i18n/en.json",
        "../../../ui/src/i18n/zh.json",
    ] {
        assert!(
            source.contains(required),
            "the desktop uninstall path must retain {required:?}"
        );
    }
    for locale in ["en", "zh"] {
        let catalog = read_json(
            crate_dir()
                .join("..")
                .join("..")
                .join("ui")
                .join("src")
                .join("i18n")
                .join(format!("{locale}.json")),
        );
        assert!(
            catalog["desktopBootstrap"]["uninstall"]["confirmMessage"]
                .as_str()
                .is_some_and(|message| message.contains("~/.avibe")),
            "{locale} uninstall copy must promise to preserve user state"
        );
    }
}

#[test]
fn post_navigation_recovery_stays_in_the_unprivileged_rust_shell() {
    let source = shipping_source("src/lib.rs");
    for required in [
        "host.is_ready(&origin).await",
        "reset_after_confirmed_runtime_loss",
        "return_to_bootstrap(&app)",
        "spawn_owned_bootstrap(app)",
    ] {
        assert!(
            source.contains(required),
            "the post-navigation recovery path must retain {required:?}"
        );
    }
    assert!(
        source.contains("compare_exchange(ACTIVITY_BOOTSTRAP, ACTIVITY_MONITOR")
            && source.contains("compare_exchange(ACTIVITY_MONITOR, ACTIVITY_BOOTSTRAP"),
        "bootstrap and monitoring must exchange one exclusive shell activity"
    );
}

#[test]
fn macos_reopen_recreates_or_refocuses_the_main_window() {
    let source = shipping_source("src/lib.rs");
    for required in [
        "fn ensure_main_window",
        "fn focus_or_restore_main_window",
        "RunEvent::Reopen",
        "claim_recreated_window_bootstrap(&activity)",
        "spawn_owned_bootstrap(app.clone())",
        "WebviewWindowBuilder::from_config",
    ] {
        assert!(
            source.contains(required),
            "the shell must keep a path to recreate or refocus the main window, missing {required:?}"
        );
    }
}

#[test]
fn recreated_windows_transfer_monitor_ownership_before_bootstrapping() {
    let source = shipping_source("src/lib.rs");
    for required in [
        "compare_exchange(current, ACTIVITY_BOOTSTRAP",
        "activity.load(Ordering::SeqCst) != ACTIVITY_MONITOR",
        "claim_recreated_window_bootstrap(&activity)",
    ] {
        assert!(
            source.contains(required),
            "window recreation must retire a stale monitor before bootstrapping, missing {required:?}"
        );
    }
    let awaited_probe = source
        .find("let ready = host.is_ready(&origin).await;")
        .expect("the monitor awaits readiness");
    let ownership_recheck = source[awaited_probe..]
        .find("activity.load(Ordering::SeqCst) != ACTIVITY_MONITOR")
        .map(|offset| awaited_probe + offset)
        .expect("the monitor rechecks ownership after readiness");
    let state_mutation = source[ownership_recheck..]
        .find("if readiness_loss.observe(ready)")
        .map(|offset| ownership_recheck + offset)
        .expect("the monitor mutates state only after the ownership recheck");
    assert!(
        awaited_probe < ownership_recheck && ownership_recheck < state_mutation,
        "the superseded monitor must retire after its await before mutating recovery state"
    );
    for required in [
        "window_generation.fetch_add(1, Ordering::SeqCst)",
        "complete_workbench_handoff(&activity, &window_generation, observed_generation)",
        "WorkbenchHandoff::RetryCurrentWindow => continue",
    ] {
        assert!(
            source.contains(required),
            "window recreation during the bootstrap handoff must retain {required:?}"
        );
    }
}

#[test]
fn every_navigation_stays_on_the_shell_or_the_proved_runtime_listener() {
    let source = shipping_source("src/lib.rs");
    for required in [
        ".on_navigation(|webview, url|",
        "fn navigation_is_allowed",
        "origin.matches_url_origin(url)",
        "active_origin",
        "set_active_origin(app, Some(origin.clone()))",
        "is_runtime_rebind_target(url)",
        "rediscover_after_runtime_rebind(webview.app_handle().clone(), origin)",
    ] {
        assert!(
            source.contains(required),
            "native navigation confinement is missing {required:?}"
        );
    }
}

#[test]
fn native_navigation_failures_return_to_a_retryable_bootstrap_state() {
    let source = shipping_source("src/lib.rs");
    for required in [
        "window.navigate(origin.navigation_url()).is_err()",
        "workbench_navigation_failure_status(ready, &origin)",
        "BootstrapNoticeCode::WorkbenchNavigationFailed",
    ] {
        assert!(
            source.contains(required),
            "native navigation failures must remain recoverable, missing {required:?}"
        );
    }
}

#[test]
fn a_dropped_retry_is_reported_to_the_bootstrap_page() {
    let source = shipping_source("src/lib.rs");
    for required in [
        "fn bootstrap_retry(window: WebviewWindow, app: AppHandle) -> Result<bool, String>",
        "Ok(spawn_bootstrap(app))",
        "fn spawn_bootstrap(app: AppHandle) -> bool",
        "return false",
    ] {
        assert!(
            source.contains(required),
            "a retry command must report whether it acquired bootstrap ownership, missing {required:?}"
        );
    }

    let frontend = shipping_source("../src/main.ts");
    for required in [
        "invoke<boolean>('bootstrap_retry')",
        "if (!scheduled)",
        "retryEl.disabled = false",
    ] {
        assert!(
            frontend.contains(required),
            "the bootstrap page must preserve a retry the shell did not schedule, missing {required:?}"
        );
    }
    assert!(
        !frontend.contains("retryEl.hidden = scheduled"),
        "the invoke response must not overwrite a newer terminal status event"
    );
}
