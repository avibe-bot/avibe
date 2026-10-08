fn main() {
    println!("cargo:rerun-if-env-changed=AVIBE_DESKTOP_UPDATER_PUBLIC_KEY");
    // Application commands are ungated by default in Tauri v2: any page loaded in
    // any window could invoke them. Declaring them here makes `tauri-build`
    // generate per-command permissions, so the capability files become the only
    // thing that can hand them out: `bootstrap.json` gives the bootstrap
    // commands only to the shell's own local page, `pet.json` gives the pet
    // commands only to the pet window, and `main-pet-bind.json` gives the
    // Workbench `pet_bind` alone.
    let manifest = tauri_build::AppManifest::new().commands(&[
        "bootstrap_status",
        "bootstrap_retry",
        "open_install_docs",
        "pet_ready",
        "pet_set_expanded",
        "pet_bind",
        "pet_unbind",
        "pet_open",
    ]);
    let mut attributes = tauri_build::Attributes::new().app_manifest(manifest);
    // tauri-build embeds its Windows application manifest, which binds Common
    // Controls v6, into the app binary only. A test binary that builds a Tauri
    // app then fails to load with STATUS_ENTRYPOINT_NOT_FOUND. The linker embeds
    // a verbatim copy into every binary this package links instead.
    if std::env::var("CARGO_CFG_TARGET_OS").as_deref() == Ok("windows")
        && std::env::var("CARGO_CFG_TARGET_ENV").as_deref() == Ok("msvc")
    {
        let windows_manifest = std::path::Path::new(&std::env::var("CARGO_MANIFEST_DIR").expect("manifest dir"))
            .join("windows-app-manifest.xml");
        println!("cargo:rerun-if-changed={}", windows_manifest.display());
        println!("cargo:rustc-link-arg=/MANIFEST:EMBED");
        println!("cargo:rustc-link-arg=/MANIFESTINPUT:{}", windows_manifest.display());
        attributes = attributes.windows_attributes(tauri_build::WindowsAttributes::new_without_app_manifest());
    }
    tauri_build::try_build(attributes).expect("failed to run tauri-build");
}
