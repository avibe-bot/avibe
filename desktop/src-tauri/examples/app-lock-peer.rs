//! Isolated native peer for desktop-client App Lock tests. No Runtime, account,
//! Keychain, network, real preference store or real authentication is used.
fn main() {
    #[cfg(target_os = "macos")]
    avibe_desktop_lib::app_lock::run_e2e();
    #[cfg(not(target_os = "macos"))]
    panic!("The native App Lock peer requires macOS");
}
