fn main() {
    // Application commands are ungated by default in Tauri v2: any page loaded in
    // any window could invoke them. Declaring them here makes `tauri-build`
    // generate per-command permissions, so `capabilities/bootstrap.json`
    // becomes the only thing that can hand them out — and it hands them only to
    // the shell's own local page.
    let manifest =
        tauri_build::AppManifest::new().commands(&["bootstrap_status", "bootstrap_retry", "open_install_docs"]);
    tauri_build::try_build(tauri_build::Attributes::new().app_manifest(manifest)).expect("failed to run tauri-build");
}
