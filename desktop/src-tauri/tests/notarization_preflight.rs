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
fn entitlements_cover_what_the_hardened_runtime_denies_wkwebview() {
    let entitlements = read_to_string(&entitlements_path());
    // WKWebView's JavaScript runtime allocates writable-and-executable
    // memory; the hardened runtime denies that by default and the notary
    // rejects the bundle while the app crashes at first navigation without
    // the exception. Stated as a property over the plist text so an added
    // entitlement cannot silently drop the load-bearing one.
    assert!(
        entitlements.contains("<key>com.apple.security.cs.allow-unsigned-executable-memory</key>")
            && entitlements.contains("<true/>"),
        "entitlements must grant the fully-namespaced com.apple.security.cs.allow-unsigned-executable-memory: macOS ignores the unnamespaced spelling, so the hardened runtime would deny WKWebView's executable-memory allocation"
    );
}

#[test]
fn the_dmg_script_never_re_signs_an_identity_signed_app() {
    let script = read_to_string(&crate_dir().join("..").join("scripts").join("create-macos-dmg.sh"));
    // The script must branch on signature state: ad-hoc or unsigned inputs
    // keep the disposable-copy ad-hoc signing, while an identity signature is
    // copied verbatim. The branch condition and the re-sign must both be
    // present, and the re-sign must stay inside the ad-hoc branch — asserted
    // by ordering: the branch test appears before the unconditional-looking
    // fallback, and the ad-hoc `--sign -` appears exactly once.
    let branch = script
        .find("not set")
        .expect("the script tests for an ad-hoc/absent signature before re-signing");
    let resign = script
        .find("--sign -")
        .expect("the script re-signs disposable ad-hoc copies");
    assert!(
        branch < resign,
        "ad-hoc re-signing must only run for ad-hoc or unsigned inputs"
    );
    assert_eq!(
        script.matches("--sign -").count(),
        1,
        "exactly one ad-hoc re-sign site: an identity-signed app is never re-signed"
    );
}
