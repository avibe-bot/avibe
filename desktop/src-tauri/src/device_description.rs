//! Who this computer is, as the Desktop app tells another computer's instance
//! (the `device.describe` of `docs/plans/instance-device-registry.md`). The
//! instance's Web UI reports it with `POST /api/devices/self`, from a page
//! that is already signed in, so that every sign-in of this app to that
//! instance is listed as one computer. Display and grouping only: nothing
//! here confers authority.
//!
//! The description is given only to the top document of the app's committed
//! view of that instance, at a Workbench route (`admits`), when the view is
//! published and on each load; never to a candidate, frame, Show page or
//! other origin. `installation_id`
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

/// The event the Workbench hears when the description is given to it.
const EVENT: &str = "avibe:desktop-device";

/// The Workbench's own routes (`ui/src/lib/applicationRoutes.ts`, without its
/// catch-all): only a document at one of these is the Workbench. Anything
/// else at the instance's origin (a Show page, a file, a sign-in page) is not.
const WORKBENCH_ROUTES: &[&str] = &[
    "/",
    "/setup",
    "/setup/remote-access",
    "/inbox",
    "/search",
    "/agents",
    "/skills",
    "/harness",
    "/vaults",
    "/projects",
    "/more",
    "/pet",
    "/apps",
    "/apps/files",
    "/apps/terminal",
    "/apps/editor",
    "/apps/preview",
    "/apps/library",
    "/admin",
    "/admin/dashboard",
    "/admin/permissions",
    "/admin/remote-access",
    "/admin/groups",
    "/admin/users",
    "/admin/show-pages",
    "/admin/logs",
    "/admin/settings/service",
    "/admin/settings/platforms",
    "/admin/settings/backends",
    "/admin/settings/backends/opencode",
    "/admin/settings/backends/claude",
    "/admin/settings/backends/codex",
    "/admin/settings/models",
    "/admin/settings/dependencies",
    "/admin/settings/memory",
    "/admin/settings/messaging",
    "/admin/settings/diagnostics",
    "/admin/settings/logs",
    "/dashboard",
    "/groups",
    "/channels",
    "/users",
    "/logs",
    "/settings",
    "/settings/general",
    "/settings/notifications",
    "/settings/memory",
    "/settings/appearance",
    "/settings/account",
    "/settings/shortcuts",
    "/settings/microphone",
    "/settings/computer-use",
    "/settings/service",
    "/settings/platforms",
    "/settings/platforms/groups",
    "/settings/platforms/users",
    "/settings/remote-access",
    "/settings/remote-access/activity",
    "/settings/remote-access/connection",
    "/settings/remote-access/connection/address",
    "/settings/backends",
    "/settings/backends/vibey",
    "/settings/backends/opencode",
    "/settings/backends/claude",
    "/settings/backends/codex",
    "/settings/models",
    "/settings/dependencies",
    "/settings/replies",
    "/settings/messaging",
    "/settings/diagnostics",
    "/settings/diagnostics/logs",
    "/settings/logs",
    "/settings/access",
    "/remote-access",
    "/doctor",
    "/doctor/logs",
];
/// Its routes with one variable segment: `/apps/show/:sessionId`,
/// `/chat/:sessionId`, `/mobile/health/:requestId`.
const WORKBENCH_ROUTE_PREFIXES: &[&str] = &["/apps/show/", "/chat/", "/mobile/health/"];

/// A path as the Workbench's router takes it: without trailing slashes.
fn route(path: &str) -> &str {
    match path.trim_end_matches('/') {
        "" => "/",
        trimmed => trimmed,
    }
}

fn workbench_route(path: &str) -> bool {
    let path = route(path);
    WORKBENCH_ROUTES.contains(&path)
        || WORKBENCH_ROUTE_PREFIXES.iter().any(|prefix| {
            path.strip_prefix(prefix)
                .is_some_and(|rest| !rest.is_empty() && !rest.contains('/'))
        })
}

/// Whether the document at `url` may be told who this computer is: the
/// committed (`committed`: current, disposable, no account page or commit
/// in between) view of the instance at `remote`, at a Workbench route.
pub(crate) fn admits(remote: Option<&Url>, committed: bool, url: &Url) -> bool {
    committed
        && remote.is_some_and(|remote| remote.scheme() == "https" && url.origin() == remote.origin())
        && url.username().is_empty()
        && url.password().is_none()
        && workbench_route(url.path())
}

/// The script that gives the description to the document `admits` let
/// through. It acts only if, when it runs, the top document is still at that
/// origin and route: a navigation can commit between the look and the run.
/// The description is kept on the document for its reporter, which may not
/// be listening yet, and announced for one that already is.
pub(crate) fn delivery(app: &AppHandle, url: &Url) -> Option<String> {
    let facts = facts(app)?;
    let origin = url.origin().ascii_serialization();
    let description = serde_json::json!({
        "kind": "desktop_app",
        "display_name": facts.name,
        "installation_id": installation_id(&facts.seed, &origin),
        "aliases": [],
        "app_version": facts.app_version,
        "system_version": facts.system_version,
    });
    Some(format!(
        "(()=>{{if(window.self!==window.top||window.location.origin!=={origin}||(window.location.pathname.replace(/\\/+$/,'')||'/')!=={route})return;if(!Object.getOwnPropertyDescriptor(window,'__AVIBE_DESKTOP_DEVICE__'))Object.defineProperty(window,'__AVIBE_DESKTOP_DEVICE__',{{value:Object.freeze({description})}});window.dispatchEvent(new Event('{EVENT}'));}})();",
        origin = serde_json::json!(origin),
        route = serde_json::json!(route(url.path())),
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
        .map(|c| if c.is_whitespace() { ' ' } else { c })
        .filter(|c| !invisible(*c))
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

    /// Only the committed view's Workbench is told. Each refusal differs from
    /// the admitted case in one respect.
    #[test]
    fn only_the_committed_view_at_a_workbench_route_is_told() {
        let remote = Url::parse("https://alex.avibe.bot/").unwrap();
        let at = |raw: &str| Url::parse(raw).unwrap();
        for raw in [
            "https://alex.avibe.bot/",
            "https://alex.avibe.bot/inbox?tab=all#x",
            "https://alex.avibe.bot/settings/remote-access/",
            "https://alex.avibe.bot/chat/ses_123",
            "https://alex.avibe.bot/apps/show/ses%3Aa",
        ] {
            assert!(admits(Some(&remote), true, &at(raw)), "{raw}");
        }
        // A candidate, a retired view, or one under an account page or a commit.
        assert!(!admits(Some(&remote), false, &at("https://alex.avibe.bot/")));
        // This computer's own Runtime, or nothing committed.
        assert!(!admits(None, true, &at("https://alex.avibe.bot/")));
        assert!(!admits(
            Some(&at("http://127.0.0.1:5123/")),
            true,
            &at("http://127.0.0.1:5123/")
        ));
        for raw in [
            // Another instance, or the same host elsewhere.
            "https://sam.avibe.bot/",
            "https://alex.avibe.bot:8443/",
            "https://user:pw@alex.avibe.bot/",
            // The instance's own documents that are not the Workbench.
            "https://alex.avibe.bot/show/ses_123/",
            "https://alex.avibe.bot/p/token/",
            "https://alex.avibe.bot/auth/login",
            "https://alex.avibe.bot/api/devices",
            "https://alex.avibe.bot/projects/report.html",
            "https://alex.avibe.bot/chat/ses_123/file.html",
            "https://alex.avibe.bot/chat/",
        ] {
            assert!(!admits(Some(&remote), true, &at(raw)), "{raw}");
        }
    }

    /// The Workbench's routes are declared in the Web UI. A route added there
    /// and not here would never report; one removed there and kept here
    /// would describe a document that is no longer the Workbench.
    #[test]
    fn the_workbench_routes_are_the_web_ui_routes() {
        let source = include_str!("../../../ui/src/lib/applicationRoutes.ts");
        let literals = |from: &str, to: &str| -> Vec<String> {
            let block = &source[source.find(from).unwrap()..];
            let block = &block[..block.find(to).unwrap()];
            block
                .split('\'')
                .skip(1)
                .step_by(2)
                .filter(|literal| literal.starts_with('/'))
                .map(str::to_owned)
                .collect()
        };
        let mut declared = literals("APPLICATION_ROUTE_PATHS = [", "] as const");
        // `...AGENT_BACKENDS.map((backend) => backend.settingsRoute)`.
        let catalog = include_str!("../../../ui/src/lib/agentBackendCatalog.generated.ts");
        declared.extend(
            catalog
                .split("\"settings_route\": \"")
                .skip(1)
                .map(|rest| rest[..rest.find('"').unwrap()].to_owned()),
        );
        declared.sort();
        declared.dedup();
        let mut ours: Vec<String> = WORKBENCH_ROUTES.iter().map(|route| (*route).to_owned()).collect();
        ours.sort();
        assert_eq!(ours, declared);
        let dynamic: Vec<String> = literals("APPLICATION_DYNAMIC_ROUTE_PATHS = [", "] as const")
            .iter()
            .map(|route| route[..route.rfind('/').unwrap() + 1].to_owned())
            .collect();
        assert_eq!(dynamic, WORKBENCH_ROUTE_PREFIXES);
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
