//! Who this computer is, as the Desktop app tells another computer's instance
//! (the `device.describe` of `docs/plans/instance-device-registry.md`). The
//! instance's Web UI reports it with `POST /api/devices/self`, from a page
//! that is already signed in, so that every sign-in of this app to that
//! instance is listed as one computer. Display and grouping only: nothing
//! here confers authority.
//!
//! The description is given to the top document of the app's view of that
//! instance, and to no frame, Show page or other origin. `installation_id`
//! is this installation's and outlives a run: a random seed in the app's
//! local data (not the credential store), from which each instance gets its
//! own identifier, so two instances cannot match one computer by it.
use sha2::{Digest, Sha256};
use std::{
    io::{ErrorKind, Write},
    path::Path,
    sync::OnceLock,
};
use tauri::{AppHandle, Manager};
use url::Url;

const SEED_FILE: &str = "device-installation";
/// The instance's own bounds (`vibe/device_metadata.py`).
const NAME_LIMIT: usize = 120;

struct Facts {
    name: String,
    app_version: String,
    system_version: String,
    seed: [u8; 32],
}

static FACTS: OnceLock<Option<Facts>> = OnceLock::new();

/// Reads the facts once, off the main thread, before a view needs them.
pub(crate) fn prepare(app: &AppHandle) {
    let app = app.clone();
    std::thread::spawn(move || {
        facts(&app);
    });
}

fn facts(app: &AppHandle) -> Option<&'static Facts> {
    FACTS
        .get_or_init(|| {
            Some(Facts {
                name: display_name(&computer_name()?)?,
                app_version: app.package_info().version.to_string(),
                system_version: system_version()?,
                seed: seed(&app.path().app_local_data_dir().ok()?)?,
            })
        })
        .as_ref()
}

/// The script that gives the description to the top document of the
/// instance at `destination`, outside its Show pages. `None` where this
/// computer cannot say who it is.
pub(crate) fn script(app: &AppHandle, destination: &Url) -> Option<String> {
    if destination.scheme() != "https" {
        return None;
    }
    let facts = facts(app)?;
    let origin = destination.origin().ascii_serialization();
    let description = serde_json::json!({
        "kind": "desktop_app",
        "display_name": facts.name,
        "installation_id": installation_id(&facts.seed, &origin),
        "aliases": [],
        "app_version": facts.app_version,
        "system_version": facts.system_version,
    });
    Some(format!(
        "if(window.self===window.top&&window.location.origin==={}&&!window.location.pathname.startsWith('/show/'))Object.defineProperty(window,'__AVIBE_DESKTOP_DEVICE__',{{value:Object.freeze({description})}});",
        serde_json::json!(origin)
    ))
}

/// This installation's identifier for the instance at `origin`.
fn installation_id(seed: &[u8; 32], origin: &str) -> String {
    let mut hash = Sha256::new();
    hash.update(b"avibe-desktop-installation:v1\0");
    hash.update(seed);
    hash.update(origin.as_bytes());
    hex(&hash.finalize())
}

/// The seed kept in `directory`; made the first time. A seed that cannot be
/// read is replaced: the computer is then listed anew, never as another one.
fn seed(directory: &Path) -> Option<[u8; 32]> {
    let path = directory.join(SEED_FILE);
    let replace = match std::fs::read_to_string(&path) {
        Ok(text) => match parse_seed(&text) {
            Some(seed) => return Some(seed),
            None => true,
        },
        Err(error) if error.kind() == ErrorKind::NotFound => false,
        Err(_) => return None,
    };
    std::fs::create_dir_all(directory).ok()?;
    let mut seed = [0u8; 32];
    getrandom::fill(&mut seed).ok()?;
    let mut file = tempfile::NamedTempFile::new_in(directory).ok()?;
    file.write_all(hex(&seed).as_bytes()).ok()?;
    file.as_file().sync_all().ok()?;
    let written = if replace {
        file.persist(&path).is_ok()
    } else {
        file.persist_noclobber(&path).is_ok()
    };
    if written {
        Some(seed)
    } else {
        // Another run made it first: theirs is the one.
        parse_seed(&std::fs::read_to_string(&path).ok()?)
    }
}

fn parse_seed(text: &str) -> Option<[u8; 32]> {
    let text = text.as_bytes();
    if text.len() != 64 {
        return None;
    }
    let mut seed = [0u8; 32];
    for (byte, pair) in seed.iter_mut().zip(text.chunks(2)) {
        *byte = u8::from_str_radix(std::str::from_utf8(pair).ok()?, 16).ok()?;
    }
    Some(seed)
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|byte| format!("{byte:02x}")).collect()
}

/// The computer's name as the instance takes it: one line of printable
/// characters, at most `NAME_LIMIT` of them.
fn display_name(raw: &str) -> Option<String> {
    let line: String = raw
        .chars()
        .filter(|c| !invisible(*c))
        .map(|c| if c.is_whitespace() { ' ' } else { c })
        .collect();
    let name: String = line.trim().chars().take(NAME_LIMIT).collect();
    let name = name.trim_end().to_owned();
    (!name.is_empty()).then_some(name)
}

/// Control and format characters, which the instance refuses in a name.
fn invisible(c: char) -> bool {
    c.is_control()
        || matches!(c, '\u{00AD}' | '\u{200B}'..='\u{200F}' | '\u{202A}'..='\u{202E}' | '\u{2060}'..='\u{2064}' | '\u{FEFF}')
}

#[cfg(target_os = "macos")]
fn computer_name() -> Option<String> {
    // The name the user gave this Mac (System Settings › General › Sharing).
    let output = std::process::Command::new("/usr/sbin/scutil")
        .args(["--get", "ComputerName"])
        .output()
        .ok()?;
    output
        .status
        .success()
        .then(|| String::from_utf8_lossy(&output.stdout).into_owned())
}

#[cfg(windows)]
fn computer_name() -> Option<String> {
    use windows_sys::Win32::System::SystemInformation::{ComputerNamePhysicalDnsHostname, GetComputerNameExW};
    let mut size = 0u32;
    // SAFETY: a null buffer of size 0 asks only for the size it needs.
    unsafe { GetComputerNameExW(ComputerNamePhysicalDnsHostname, std::ptr::null_mut(), &mut size) };
    if size == 0 {
        return None;
    }
    let mut buffer = vec![0u16; size as usize];
    // SAFETY: `buffer` holds `size` UTF-16 units, as `size` says.
    if unsafe { GetComputerNameExW(ComputerNamePhysicalDnsHostname, buffer.as_mut_ptr(), &mut size) } == 0 {
        return None;
    }
    buffer.truncate(size as usize);
    String::from_utf16(&buffer).ok()
}

#[cfg(not(any(target_os = "macos", windows)))]
fn computer_name() -> Option<String> {
    None
}

#[cfg(target_os = "macos")]
fn system_version() -> Option<String> {
    let version = objc2_foundation::NSProcessInfo::processInfo().operatingSystemVersion();
    Some(match version.patchVersion {
        0 => format!("macOS {}.{}", version.majorVersion, version.minorVersion),
        patch => format!("macOS {}.{}.{patch}", version.majorVersion, version.minorVersion),
    })
}

#[cfg(windows)]
fn system_version() -> Option<String> {
    use windows_sys::{Wdk::System::SystemServices::RtlGetVersion, Win32::System::SystemInformation::OSVERSIONINFOW};
    let mut info = OSVERSIONINFOW {
        dwOSVersionInfoSize: std::mem::size_of::<OSVERSIONINFOW>() as u32,
        ..Default::default()
    };
    // SAFETY: `info` is a sized OSVERSIONINFOW, as the call requires.
    if unsafe { RtlGetVersion(&mut info) } != 0 {
        return None;
    }
    // Windows 11 still says 10.0; its builds start at 22000.
    let name = match (info.dwMajorVersion, info.dwBuildNumber) {
        (10, build) if build >= 22000 => "11".to_owned(),
        (major, _) => major.to_string(),
    };
    Some(format!("Windows {name} ({})", info.dwBuildNumber))
}

#[cfg(not(any(target_os = "macos", windows)))]
fn system_version() -> Option<String> {
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    /// One computer stays one row across runs: the seed is kept, and read
    /// back, not made anew. A damaged seed is replaced, not trusted.
    #[test]
    fn the_seed_outlives_a_run_and_a_damaged_one_is_replaced() {
        let directory = tempfile::tempdir().unwrap();
        let first = seed(directory.path()).unwrap();
        assert_eq!(seed(directory.path()), Some(first));
        std::fs::write(directory.path().join(SEED_FILE), "not a seed").unwrap();
        let replaced = seed(directory.path()).unwrap();
        assert_ne!(replaced, first);
        assert_eq!(seed(directory.path()), Some(replaced));
    }

    #[test]
    fn each_instance_gets_its_own_installation_id() {
        let seed = [7u8; 32];
        let one = installation_id(&seed, "https://alex.avibe.bot");
        assert_eq!(one, installation_id(&seed, "https://alex.avibe.bot"));
        assert_ne!(one, installation_id(&seed, "https://sam.avibe.bot"));
        assert_ne!(one, installation_id(&[8u8; 32], "https://alex.avibe.bot"));
        assert_eq!(one.len(), 64);
    }

    /// `str.isprintable()` on the instance refuses control and format
    /// characters and any space but U+0020; such a name would be refused
    /// with the whole report.
    #[test]
    fn a_name_is_one_printable_line_within_the_instance_limit() {
        let long = "M".repeat(130);
        for (raw, expected) in [
            ("Alex’s MacBook Pro\n", Some("Alex’s MacBook Pro")),
            ("Studio\u{00A0}Mac\tmini", Some("Studio Mac mini")),
            ("Dev\u{200D}Box\u{FEFF}", Some("DevBox")),
            ("  \n", None),
            (long.as_str(), Some(&long[..NAME_LIMIT])),
        ] {
            assert_eq!(display_name(raw).as_deref(), expected, "{raw:?}");
        }
    }
}
