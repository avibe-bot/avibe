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
    if !runs_on_this_mac(&replacement.join("Contents/MacOS/avibe-desktop")).map_err(|e| e.to_string())? {
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

/// `<mach/machine.h>` CPU types: the base type with the 64-bit ABI flag.
#[cfg(any(target_os = "macos", test))]
const CPU_TYPE_ARM64: u32 = 0x0100_000c;
#[cfg(any(target_os = "macos", test))]
const CPU_TYPE_X86_64: u32 = 0x0100_0007;

/// Read in-process, not with `/usr/bin/lipo`: without the Command Line Tools
/// that is only the xcrun shim, which fails and prompts to install them (#2254).
#[cfg(target_os = "macos")]
fn runs_on_this_mac(binary: &std::path::Path) -> std::io::Result<bool> {
    use std::io::Read;
    let cputype = if cfg!(target_arch = "aarch64") {
        CPU_TYPE_ARM64
    } else {
        CPU_TYPE_X86_64
    };
    // The Mach-O header and any fat arch table fit in the first page.
    let mut header = Vec::new();
    std::fs::File::open(binary)?.take(4096).read_to_end(&mut header)?;
    Ok(macho_runs_as(&header, cputype))
}

/// Whether a Mach-O image, given by its leading bytes, runs as `cputype`: a
/// thin 64-bit image of that type, or a fat image whose complete arch table
/// lists it. Anything else, including a truncated table, is rejected.
#[cfg(any(target_os = "macos", test))]
fn macho_runs_as(header: &[u8], cputype: u32) -> bool {
    let word = |at: usize| header.get(at..at + 4).and_then(|b| <[u8; 4]>::try_from(b).ok());
    // Fat headers are big-endian on disk.
    let be = |at: usize| word(at).map(u32::from_be_bytes);
    let fat = |entry: usize| {
        let count = be(4).unwrap_or(0) as usize;
        count
            .checked_mul(entry)
            .and_then(|table| table.checked_add(8))
            .is_some_and(|end| end <= header.len())
            && (0..count).any(|i| be(8 + i * entry) == Some(cputype))
    };
    match be(0) {
        // MH_MAGIC_64 stored little-endian, the byte order of both shipped arches.
        Some(0xcffa_edfe) => word(4).map(u32::from_le_bytes) == Some(cputype),
        Some(0xcafe_babe) => fat(20), // FAT_MAGIC, 20-byte `fat_arch`
        Some(0xcafe_babf) => fat(32), // FAT_MAGIC_64, 32-byte `fat_arch_64`
        _ => false,
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

    const CPU_TYPE_I386: u32 = 7;
    const CPU_TYPE_ARM: u32 = 12;

    /// A 32-byte `mach_header_64` with `magic` written little-endian.
    fn thin(magic: u32, cputype: u32) -> Vec<u8> {
        [magic, cputype, 0, 2, 0, 0, 0, 0]
            .iter()
            .flat_map(|w| w.to_le_bytes())
            .collect()
    }

    /// A big-endian fat header and arch table; slice offsets are not read.
    fn fat(magic: u32, cputypes: &[u32]) -> Vec<u8> {
        let words = if magic == 0xcafe_babf { 8 } else { 5 };
        let mut table = vec![magic, cputypes.len() as u32];
        for &cputype in cputypes {
            table.extend([cputype, 0]);
            table.extend(std::iter::repeat_n(0, words - 2));
        }
        table.iter().flat_map(|w| w.to_be_bytes()).collect()
    }

    #[test]
    fn only_an_image_that_runs_as_the_arch_passes_the_architecture_check() {
        let (arm64, x86_64) = (CPU_TYPE_ARM64, CPU_TYPE_X86_64);
        let both = fat(0xcafe_babe, &[x86_64, arm64]);
        let mut truncated_table = both.clone();
        truncated_table.pop();
        let mut overlong_count = fat(0xcafe_babe, &[arm64]);
        overlong_count[4..8].copy_from_slice(&u32::MAX.to_be_bytes());
        let cases: [(&str, Vec<u8>, &[u32]); 13] = [
            ("thin arm64", thin(0xfeed_facf, arm64), &[arm64]),
            ("thin x86_64", thin(0xfeed_facf, x86_64), &[x86_64]),
            ("fat with both", both.clone(), &[arm64, x86_64]),
            ("fat with x86_64 only", fat(0xcafe_babe, &[x86_64]), &[x86_64]),
            ("fat_64 with both", fat(0xcafe_babf, &[arm64, x86_64]), &[arm64, x86_64]),
            ("fat_64 with arm64 only", fat(0xcafe_babf, &[arm64]), &[arm64]),
            ("fat with no arches", fat(0xcafe_babe, &[]), &[]),
            (
                "fat with 32-bit arches only",
                fat(0xcafe_babe, &[CPU_TYPE_I386, CPU_TYPE_ARM]),
                &[],
            ),
            ("thin 32-bit", thin(0xfeed_face, arm64), &[]),
            (
                "thin 64-bit in big-endian order",
                thin(0xcffa_edfe, arm64.swap_bytes()),
                &[],
            ),
            ("truncated thin header", thin(0xfeed_facf, arm64)[..6].to_vec(), &[]),
            ("truncated arch table", truncated_table, &[]),
            ("arch count beyond the table", overlong_count, &[]),
        ];
        for (name, bytes, runs_as) in cases {
            for cputype in [arm64, x86_64] {
                assert_eq!(
                    macho_runs_as(&bytes, cputype),
                    runs_as.contains(&cputype),
                    "{name} as {cputype:#x}"
                );
            }
        }
        for garbage in [&b""[..], b"\xca\xfe", b"#!/bin/sh\nexit 0\n", &[0xff; 64]] {
            assert!(!macho_runs_as(garbage, arm64) && !macho_runs_as(garbage, x86_64));
        }
    }

    /// The test binary is real toolchain output for the running arch.
    #[cfg(target_os = "macos")]
    #[test]
    fn this_toolchains_own_binary_passes_the_architecture_check() {
        assert!(runs_on_this_mac(&std::env::current_exe().unwrap()).unwrap());
    }
}
