//! Installer-only preparation before Tauri, its window, or single-instance IPC.

use std::ffi::OsString;
use std::path::{Path, PathBuf};

const PREPARE_ARGUMENT: &str = "--prepare-runtime";

fn dispatch(args: impl IntoIterator<Item = OsString>, prepare: impl FnOnce() -> bool) -> Option<i32> {
    let args: Vec<_> = args.into_iter().collect();
    if !args.iter().any(|arg| arg == PREPARE_ARGUMENT) {
        return None;
    }
    if args.len() != 1 {
        return Some(2);
    }
    Some(if prepare() { 0 } else { 1 })
}

fn app_local_data_dir(local_data: &Path) -> Result<PathBuf, serde_json::Error> {
    #[derive(serde::Deserialize)]
    struct Identity {
        identifier: String,
    }
    // Use the same identifier as the normal shell, without creating a Tauri app.
    let identity: Identity = serde_json::from_str(include_str!("../tauri.conf.json"))?;
    Ok(local_data.join(identity.identifier))
}

#[cfg(windows)]
pub fn run() -> Option<i32> {
    dispatch(std::env::args_os().skip(1), || {
        let Ok(executable) = std::env::current_exe() else {
            return false;
        };
        // This is Tauri's own Windows app_local_data_dir lookup: Known Folder
        // LocalAppData plus the configured identifier, independent of cwd/env.
        let Some(local_data) = dirs::data_local_dir() else {
            return false;
        };
        let Ok(app_data) = app_local_data_dir(&local_data) else {
            return false;
        };
        // NSIS owns localized progress/failure copy; no raw paths escape here.
        avibe_runtime_host::prepare_installed_runtime(&executable, &app_data).is_ok()
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn args(values: &[&str]) -> Vec<OsString> {
        values.iter().map(OsString::from).collect()
    }

    #[test]
    fn ordinary_launches_leave_preparation_to_the_shell() {
        for values in [vec![], vec!["avibe://show/session"], vec!["--other-flag"]] {
            assert_eq!(dispatch(args(&values), || panic!("must launch the GUI")), None);
        }
    }

    #[test]
    fn helper_reports_success_or_failure_without_falling_through_to_the_gui() {
        assert_eq!(dispatch(args(&[PREPARE_ARGUMENT]), || true), Some(0));
        assert_eq!(dispatch(args(&[PREPARE_ARGUMENT]), || false), Some(1));
        for values in [
            vec![PREPARE_ARGUMENT, "/UPDATE"],
            vec!["--install-root", "elsewhere", PREPARE_ARGUMENT],
            vec![PREPARE_ARGUMENT, PREPARE_ARGUMENT],
        ] {
            assert_eq!(dispatch(args(&values), || panic!("unexpected argument")), Some(2));
        }
    }

    #[test]
    fn app_data_uses_the_shell_identifier_under_the_os_local_data_root() {
        let temporary = tempfile::tempdir().unwrap();
        let local = temporary.path().join("Local App Data 测试");
        let config: serde_json::Value = serde_json::from_str(include_str!("../tauri.conf.json")).unwrap();
        assert!(config["app"].get("appDirectoriesOverride").is_none());
        assert_eq!(
            app_local_data_dir(&local).unwrap(),
            local.join(config["identifier"].as_str().unwrap())
        );
    }
}
