//! Guards on the shell's two load-bearing boundaries.
//!
//! Both are enforced by configuration and by absence — a capability that names no
//! remote URL, and code that contains no way to stop a process. Neither shows up
//! in a behavioural test, so they are asserted directly against the files that
//! carry them.

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
fn the_shell_never_stops_a_runtime() {
    for file in ["src/lib.rs", "src/main.rs"] {
        let source = shipping_source(file);
        for forbidden in [".kill(", "libc::kill", "taskkill", "SIGTERM", "SIGKILL"] {
            assert!(
                !source.contains(forbidden),
                "{file} must never stop a Runtime the shell adopted or started, found {forbidden:?}"
            );
        }
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
