// The shell is a GUI application; a release build must not open a console window
// alongside it on Windows.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    avibe_desktop_lib::run()
}
