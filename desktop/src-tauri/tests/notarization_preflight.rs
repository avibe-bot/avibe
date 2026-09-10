//! Guards for the macOS notarization preflight contract.
//!
//! The bundle is accepted by Apple's notary service only when it is signed
//! with Developer ID, the hardened runtime is enabled, and the entitlements
//! cover what WKWebView needs. These facts live in configuration files that
//! compile-time tooling does not fully validate, so each is asserted here as
//! an invariant rather than left to review vigilance.

use std::path::{Path, PathBuf};

use serde_json::Value;

fn crate_dir() -> &'static Path {
    Path::new(env!("CARGO_MANIFEST_DIR"))
}

fn read_to_string(path: &Path) -> String {
    std::fs::read_to_string(path).unwrap_or_else(|error| panic!("{} is readable: {error}", path.display()))
}

fn config() -> Value {
    serde_json::from_str(&read_to_string(&crate_dir().join("tauri.conf.json")))
        .unwrap_or_else(|error| panic!("tauri.conf.json is valid JSON: {error}"))
}

fn entitlements_path() -> PathBuf {
    let path = crate_dir().join("Entitlements.plist");
    assert!(path.is_file(), "Entitlements.plist exists next to tauri.conf.json");
    path
}

#[test]
fn the_macos_bundle_declares_the_hardened_runtime_and_entitlements() {
    let binding = config();
    let macos = binding["bundle"]["macOS"]
        .as_object()
        .expect("bundle.macOS is configured");
    // Explicit, not inherited from tooling defaults: a future Tauri default
    // flip must be a deliberate decision, not a silent behavior change.
    assert_eq!(
        macos["hardenedRuntime"],
        Value::Bool(true),
        "hardened runtime must be explicitly enabled for notarization"
    );
    assert_eq!(
        macos["entitlements"],
        Value::String("Entitlements.plist".into()),
        "the bundle must sign with the checked-in entitlements"
    );
}

#[test]
fn entitlements_grant_no_executable_memory_exception() {
    let entitlements = read_to_string(&entitlements_path());
    // WKWebView's JavaScript engine JITs inside WebKit-owned helper
    // processes; the embedding Tauri process itself needs no
    // executable-memory exception, and the notary does not require one.
    // Granting it would weaken the hardened runtime for the whole process,
    // so its absence is the invariant: any re-add must come with evidence
    // that the app process itself needs it.
    assert!(
        !entitlements.contains("<key>"),
        "entitlements must stay empty; in particular no executable-memory exception for the embedding process"
    );
}

#[test]
fn the_dmg_script_never_re_signs_an_identity_signed_app() {
    let script = read_to_string(&crate_dir().join("..").join("scripts").join("create-macos-dmg.sh"));
    // The guard must bind to executable logic, not comment prose. The
    // ad-hoc re-sign may only run inside the branch whose `if` condition
    // tests the ad-hoc/absent signature markers; stripping that condition
    // to re-sign unconditionally must fail this test even with every
    // comment left intact.
    let if_line = script
        .lines()
        .find(|line| {
            let trimmed = line.trim_start();
            trimmed.starts_with("if ") && (trimmed.contains("adhoc") || trimmed.contains("signature"))
        })
        .expect("the script branches on signature state before re-signing");
    assert!(
        if_line.contains("adhoc") && if_line.contains("identity"),
        "the branch condition must test the ad-hoc/absent markers: {if_line}"
    );
    let if_offset = script.find(if_line.trim()).expect("if line offset");
    let resign = script
        .find("--sign -")
        .expect("the script re-signs disposable ad-hoc copies");
    assert!(
        if_offset < resign,
        "the ad-hoc re-sign must appear after (inside) the signature-state branch"
    );
    // The re-sign appears exactly once and no second signing site exists
    // outside the branch.
    assert_eq!(
        script.matches("--sign ").count(),
        1,
        "exactly one re-sign site: an identity-signed app is never re-signed"
    );
}
