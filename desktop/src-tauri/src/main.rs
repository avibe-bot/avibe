// The shell is a GUI application; a release build must not open a console window
// alongside it on Windows.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

#[cfg(any(windows, test))]
mod prepare_runtime;

fn main() {
    #[cfg(windows)]
    if let Some(code) = prepare_runtime::run() {
        std::process::exit(code);
    }
    avibe_desktop_lib::run()
}
