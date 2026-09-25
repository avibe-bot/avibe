//! Replacement is a filesystem transaction; user state is never an input.
use tauri::AppHandle;

#[cfg(target_os = "macos")]
pub fn install(_app: &AppHandle, bytes: &[u8], version: &str) -> Result<(), String> {
    use std::{fs, io::Cursor, process::Command};
    let executable = std::env::current_exe().map_err(|e| e.to_string())?;
    let current = executable
        .parent()
        .and_then(|p| p.parent())
        .and_then(|p| p.parent())
        .filter(|p| p.extension().is_some_and(|e| e == "app"))
        .ok_or("not running from an application bundle")?;
    let parent = current.parent().ok_or("missing application directory")?;
    // Same filesystem as the installation. Permission/capacity errors occur
    // before moving the running app. No privileged rm/mv fallback.
    let staging = tempfile::Builder::new()
        .prefix(".avibe-update-")
        .tempdir_in(parent)
        .map_err(|e| e.to_string())?;
    let mut archive = tar::Archive::new(flate2::read::GzDecoder::new(Cursor::new(bytes)));
    archive.unpack(staging.path()).map_err(|e| e.to_string())?;
    let replacement = staging.path().join("Avibe.app");
    let plist = replacement.join("Contents/Info.plist");
    for (field, expected) in [
        ("CFBundleIdentifier", "bot.avibe.desktop"),
        ("CFBundleShortVersionString", version),
    ] {
        let output = Command::new("/usr/bin/plutil")
            .args(["-extract", field, "raw", "-o", "-"])
            .arg(&plist)
            .output()
            .map_err(|e| e.to_string())?;
        if !output.status.success() || String::from_utf8_lossy(&output.stdout).trim() != expected {
            return Err("replacement application identity mismatch".into());
        }
    }
    let binary = replacement.join("Contents/MacOS/avibe-desktop");
    let arch = if cfg!(target_arch = "aarch64") {
        "arm64"
    } else {
        "x86_64"
    };
    let output = Command::new("/usr/bin/lipo")
        .arg("-archs")
        .arg(&binary)
        .output()
        .map_err(|e| e.to_string())?;
    if !output.status.success() || String::from_utf8_lossy(&output.stdout).trim() != arch {
        return Err("replacement architecture mismatch".into());
    }
    if !Command::new("/usr/bin/codesign")
        .args(["--verify", "--deep", "--strict"])
        .arg(&replacement)
        .status()
        .map_err(|e| e.to_string())?
        .success()
    {
        return Err("replacement app seal is invalid".into());
    }
    let backup = staging.path().join("previous.app");
    match replace_with_rollback(current, &replacement, &backup, |a, b| fs::rename(a, b)) {
        Ok(()) => Ok(()),
        Err(error) => {
            // Never let TempDir cleanup erase a backup that restoration could
            // not move back (for example a filesystem failure).
            if backup.exists() {
                let _ = staging.keep();
            }
            Err(error)
        }
    }
}

#[cfg(any(target_os = "macos", test))]
fn replace_with_rollback(
    current: &std::path::Path,
    replacement: &std::path::Path,
    backup: &std::path::Path,
    mut rename: impl FnMut(&std::path::Path, &std::path::Path) -> std::io::Result<()>,
) -> Result<(), String> {
    rename(current, backup).map_err(|e| format!("application retained: {e}"))?;
    if let Err(error) = rename(replacement, current) {
        rename(backup, current).map_err(|restore| {
            format!(
                "replacement failed: {error}; previous app retained at {}: {restore}",
                backup.display()
            )
        })?;
        return Err(format!("replacement failed; previous app restored: {error}"));
    }
    Ok(())
}

#[cfg(target_os = "windows")]
pub fn install(app: &AppHandle, bytes: &[u8], version: &str) -> Result<(), String> {
    use std::{
        fs,
        os::windows::process::CommandExt,
        process::Command,
        time::{Duration, Instant},
    };
    let current = std::env::current_exe().map_err(|e| e.to_string())?;
    let directory = current.parent().ok_or("missing installation directory")?;
    let staging = tempfile::Builder::new()
        .prefix("avibe-update-")
        .tempdir()
        .map_err(|e| e.to_string())?;
    let installer = staging.path().join("update.exe");
    fs::write(&installer, bytes).map_err(|e| e.to_string())?;
    let script = staging.path().join("install.ps1");
    fs::write(&script, include_str!("update_windows.ps1")).map_err(|e| e.to_string())?;
    let request = staging.path().join("request.json");
    fs::write(
        &request,
        serde_json::to_vec(&serde_json::json!({
            "pid": std::process::id(), "directory": directory, "executable": current,
            "installer": installer, "version": version, "staging": staging.path(),
            "errorTitle": super::native_catalog_for_locales(sys_locale::get_locales()).updater.title,
            "errorMessage": super::native_catalog_for_locales(sys_locale::get_locales()).updater.install_failed,
        }))
        .map_err(|e| e.to_string())?,
    )
    .map_err(|e| e.to_string())?;
    let system = std::env::var_os("SystemRoot").ok_or("missing Windows system directory")?;
    let powershell = std::path::Path::new(&system).join("System32/WindowsPowerShell/v1.0/powershell.exe");
    let mut child = Command::new(powershell)
        .creation_flags(0x08000000) // CREATE_NO_WINDOW; errors use the native dialog.
        .args(["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File"])
        .arg(script)
        .arg("-Request")
        .arg(request)
        .spawn()
        .map_err(|e| e.to_string())?;
    let deadline = Instant::now() + Duration::from_secs(600);
    while !staging.path().join("ready").exists() {
        if child.try_wait().map_err(|e| e.to_string())?.is_some() {
            return Err("installer preparation failed; current application retained".into());
        }
        if Instant::now() > deadline {
            let _ = child.kill();
            let _ = child.wait();
            return Err("installer preparation timed out".into());
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    // The helper has copied the previous install and now waits for this PID.
    // Ownership transfers to it; it reports failures and relaunches the old app.
    let _ = staging.keep();
    super::exit_shell(app);
    Ok(())
}

#[cfg(not(any(target_os = "macos", target_os = "windows")))]
pub fn install(_app: &AppHandle, _bytes: &[u8], _version: &str) -> Result<(), String> {
    Err("unsupported update platform".into())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn replacement_failure_restores_the_current_runnable_bytes() {
        let root = tempfile::tempdir().unwrap();
        let current = root.path().join("Avibe.app");
        let next = root.path().join("next.app");
        let backup = root.path().join("old.app");
        std::fs::write(&current, b"current runnable application").unwrap();
        std::fs::write(&next, b"new application").unwrap();
        let mut calls = 0;
        let result = replace_with_rollback(&current, &next, &backup, |a, b| {
            calls += 1;
            if calls == 2 {
                return Err(std::io::Error::other("injected disk failure"));
            }
            std::fs::rename(a, b)
        });
        assert!(result.is_err());
        assert_eq!(std::fs::read(&current).unwrap(), b"current runnable application");
        assert!(!backup.exists());
    }
    #[test]
    fn failed_restore_keeps_the_backup_for_recovery() {
        let root = tempfile::tempdir().unwrap();
        let current = root.path().join("app");
        let next = root.path().join("next");
        let backup = root.path().join("backup");
        std::fs::write(&current, b"old").unwrap();
        let result = replace_with_rollback(&current, &next, &backup, |a, b| {
            if a == current {
                std::fs::rename(a, b)
            } else {
                Err(std::io::Error::other("disk unavailable"))
            }
        });
        assert!(result.is_err());
        assert_eq!(std::fs::read(&backup).unwrap(), b"old");
    }
}
