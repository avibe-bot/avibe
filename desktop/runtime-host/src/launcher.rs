//! Starting the installed Avibe Runtime.
//!
//! Two rules shape this module:
//!
//! 1. The Runtime outlives the shell. Nothing here ever kills what it started —
//!    a launched process is detached, and the shell only reaps it.
//! 2. No shell interpreter is involved. The executable is resolved to a real
//!    path and spawned directly, so no user-controlled string is ever parsed as
//!    a command line.

use std::env;
use std::ffi::OsString;
use std::io::Read;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::{mpsc, Arc, OnceLock};
use std::time::{Duration, Instant};

use serde::Deserialize;

use crate::origin::LoopbackOrigin;
use crate::status::BootstrapNoticeCode;
/// Environment variable that points the shell at a specific `vibe` executable.
///
/// Desktop applications inherit a minimal `PATH` when launched from Finder or
/// the Windows shell, which usually does not contain `~/.local/bin`; this is the
/// documented escape hatch when the search below cannot find an install.
pub const VIBE_PATH_ENV: &str = "AVIBE_DESKTOP_VIBE_PATH";

/// uv's supported override for the directory containing tool executables.
pub const UV_TOOL_BIN_DIR_ENV: &str = "UV_TOOL_BIN_DIR";

/// Marks the spawned Runtime as started by the desktop shell.
pub const DESKTOP_SHELL_ENV: &str = "AVIBE_DESKTOP_SHELL";

/// How the shell starts a Runtime.
///
/// `start` is idempotent: it brings up what is missing without stopping what is
/// running. `--no-open-browser` is what keeps a desktop launch from *also*
/// opening a system browser at the Workbench — plain `vibe start` honours
/// `config.ui.open_browser` and would leave the user with two windows onto the
/// same Runtime. The shell owns a WebView for exactly this purpose, so it always
/// opts out.
const START_ARGS: [&str; 2] = ["start", "--no-open-browser"];
const ENDPOINT_ARGS: [&str; 3] = ["desktop", "endpoint", "--json"];
const ENDPOINT_TIMEOUT: Duration = Duration::from_secs(10);
const MAX_ENDPOINT_BYTES: u64 = 4096;

#[derive(Debug, thiserror::Error)]
pub enum LaunchError {
    #[error("installed Avibe Runtime not found")]
    ExecutableNotFound,
    #[error("failed to execute the Avibe desktop endpoint command")]
    EndpointSpawn(#[source] std::io::Error),
    #[error("Avibe desktop endpoint command timed out")]
    EndpointTimeout,
    #[error("Avibe desktop endpoint command failed")]
    EndpointExit,
    #[error("Avibe desktop endpoint descriptor is invalid")]
    EndpointOutput,
    #[error("Avibe desktop endpoint origin is invalid")]
    InvalidOrigin,
    #[error("failed to start the installed Avibe Runtime")]
    Spawn(#[source] std::io::Error),
}

impl LaunchError {
    /// Machine/install failures may be retried; an invalid origin must be fixed
    /// before the shell is allowed to contact it.
    pub fn is_retryable(&self) -> bool {
        !matches!(self, Self::InvalidOrigin)
    }

    /// Stable localized notice code; raw errors never cross into the WebView.
    pub fn notice_code(&self) -> BootstrapNoticeCode {
        match self {
            Self::ExecutableNotFound => BootstrapNoticeCode::RuntimeNotFound,
            Self::EndpointSpawn(_) | Self::EndpointTimeout | Self::EndpointExit | Self::EndpointOutput => {
                BootstrapNoticeCode::RuntimeDiscoveryFailed
            }
            Self::InvalidOrigin => BootstrapNoticeCode::InvalidOrigin,
            Self::Spawn(_) => BootstrapNoticeCode::RuntimeSpawnFailed,
        }
    }
}

/// Resolves one installed Avibe Runtime for a single bootstrap attempt.
///
/// Resolution is deliberately attempt-scoped. A retry after installation or
/// repair must observe the current filesystem instead of a shell-lifetime cache.
pub trait RuntimeLauncher: Send + Sync {
    fn resolve(&self) -> Result<Arc<dyn ResolvedRuntimeLauncher>, LaunchError>;
}

/// One executable frozen for a single bootstrap attempt.
///
/// Both methods use this exact executable. `launch` returns as soon as it is
/// spawned; readiness is decided by the readiness probe.
pub trait ResolvedRuntimeLauncher: Send + Sync {
    fn endpoint(&self) -> Result<LoopbackOrigin, LaunchError>;
    fn launch(&self) -> Result<LaunchedRuntime, LaunchError>;
}

/// Whether the launcher process itself survived long enough to do its job.
///
/// `vibe start` is short-lived by design: it brings the Runtime up and exits, so
/// the shell cannot treat "it is gone" as failure. The *exit code* is what
/// separates the two. A non-zero one means it never started anything — it
/// refused an argument, or the install is broken — and no amount of waiting will
/// produce a Runtime. Without this signal the shell polls an address nothing
/// will ever answer until the full ready timeout expires.
///
/// Empty means "still running, or not observable"; both are indistinguishable
/// from a healthy start and are treated as such.
#[derive(Debug, Clone, Default)]
pub struct LaunchWatch(Arc<OnceLock<bool>>);

impl LaunchWatch {
    /// A watch that has already seen the launcher exit. For launchers that learn
    /// the outcome synchronously, and for tests.
    pub fn exited(succeeded: bool) -> Self {
        let watch = Self::default();
        watch.record(succeeded);
        watch
    }

    /// True only once the launcher has been *seen* to exit non-zero.
    pub fn failed(&self) -> bool {
        self.0.get() == Some(&false)
    }

    fn record(&self, succeeded: bool) {
        let _ = self.0.set(succeeded);
    }
}

/// A Runtime the shell started. Held for diagnostics only — never for control.
#[derive(Debug, Clone, Default)]
pub struct LaunchedRuntime {
    pub pid: u32,
    /// Observes the launcher, not the Runtime: the Runtime's own daemons are
    /// grandchildren and are unaffected by anything reported here.
    pub watch: LaunchWatch,
}

/// Launches the `vibe` executable that is already installed on this machine.
#[derive(Debug, Default)]
pub struct InstalledVibeLauncher {
    candidates: Vec<PathBuf>,
}

impl InstalledVibeLauncher {
    /// Builds the search list from the current process environment.
    pub fn from_env() -> Self {
        Self {
            candidates: vibe_executable_candidates(
                env::var_os(VIBE_PATH_ENV).as_deref(),
                env::var_os("PATH").as_deref(),
                env::var_os(UV_TOOL_BIN_DIR_ENV).as_deref(),
                home_dir().as_deref(),
                env::var_os("APPDATA").as_deref().map(Path::new),
            ),
        }
    }

    /// The ordered locations this launcher will try.
    pub fn candidates(&self) -> &[PathBuf] {
        &self.candidates
    }

    fn resolve_executable(&self) -> Result<PathBuf, LaunchError> {
        self.candidates
            .iter()
            .find(|candidate| is_executable_file(candidate))
            .cloned()
            .ok_or(LaunchError::ExecutableNotFound)
    }
}

impl RuntimeLauncher for InstalledVibeLauncher {
    fn resolve(&self) -> Result<Arc<dyn ResolvedRuntimeLauncher>, LaunchError> {
        Ok(Arc::new(ResolvedVibeExecutable {
            executable: self.resolve_executable()?,
        }))
    }
}

#[derive(Debug)]
struct ResolvedVibeExecutable {
    executable: PathBuf,
}

impl ResolvedRuntimeLauncher for ResolvedVibeExecutable {
    fn endpoint(&self) -> Result<LoopbackOrigin, LaunchError> {
        query_endpoint(&self.executable)
    }

    fn launch(&self) -> Result<LaunchedRuntime, LaunchError> {
        let child = spawn_detached(&self.executable).map_err(LaunchError::Spawn)?;
        let pid = child.id();
        let watch = LaunchWatch::default();

        // Reap the launcher process so it cannot linger as a zombie. `vibe start`
        // returns once the Runtime daemons are up; those daemons are grandchildren
        // and are entirely unaffected by this wait. `std::process::Child` never
        // kills on drop, so the Runtime survives the shell exiting.
        //
        // The wait was already happening; recording its verdict costs nothing and
        // is the only place the shell can learn that the launcher died.
        let reaped = watch.clone();
        std::thread::spawn(move || {
            let mut child = child;
            if let Ok(status) = child.wait() {
                reaped.record(status.success());
            }
        });

        Ok(LaunchedRuntime { pid, watch })
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct EndpointDescriptor {
    schema_version: u32,
    origin: String,
}

fn query_endpoint(executable: &Path) -> Result<LoopbackOrigin, LaunchError> {
    let mut child = Command::new(executable)
        .args(ENDPOINT_ARGS)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .env(DESKTOP_SHELL_ENV, "1")
        .spawn()
        .map_err(LaunchError::EndpointSpawn)?;
    let stdout = child.stdout.take().ok_or(LaunchError::EndpointOutput)?;
    let (sender, receiver) = mpsc::sync_channel(1);
    std::thread::spawn(move || {
        let mut bytes = Vec::new();
        let result = stdout
            .take(MAX_ENDPOINT_BYTES + 1)
            .read_to_end(&mut bytes)
            .map(|_| bytes);
        let _ = sender.send(result);
    });

    let deadline = Instant::now() + ENDPOINT_TIMEOUT;
    let status = loop {
        match child.try_wait() {
            Ok(Some(status)) => break status,
            Ok(None) if Instant::now() < deadline => {
                std::thread::sleep(Duration::from_millis(10));
            }
            Ok(None) => {
                let _ = child.kill();
                let _ = child.wait();
                return Err(LaunchError::EndpointTimeout);
            }
            Err(error) => return Err(LaunchError::EndpointSpawn(error)),
        }
    };
    if !status.success() {
        return Err(LaunchError::EndpointExit);
    }

    let remaining = deadline.saturating_duration_since(Instant::now());
    let bytes = receiver
        .recv_timeout(remaining)
        .map_err(|_| LaunchError::EndpointTimeout)?
        .map_err(|_| LaunchError::EndpointOutput)?;
    parse_endpoint_descriptor(&bytes)
}

fn parse_endpoint_descriptor(bytes: &[u8]) -> Result<LoopbackOrigin, LaunchError> {
    if bytes.len() > MAX_ENDPOINT_BYTES as usize {
        return Err(LaunchError::EndpointOutput);
    }
    let descriptor: EndpointDescriptor = serde_json::from_slice(bytes).map_err(|_| LaunchError::EndpointOutput)?;
    if descriptor.schema_version != 1 {
        return Err(LaunchError::EndpointOutput);
    }
    LoopbackOrigin::parse(&descriptor.origin).map_err(|_| LaunchError::InvalidOrigin)
}

/// Every location the shell is willing to look for an installed `vibe`, in order.
///
/// Kept pure and separate from the filesystem so the ordering is testable.
/// The well-known directories mirror where `install.sh` and `install.ps1` place
/// the executable.
pub fn vibe_executable_candidates(
    override_path: Option<&std::ffi::OsStr>,
    path_var: Option<&std::ffi::OsStr>,
    uv_tool_bin_dir: Option<&std::ffi::OsStr>,
    home: Option<&Path>,
    app_data: Option<&Path>,
) -> Vec<PathBuf> {
    let executable_name = OsString::from(format!("vibe{}", env::consts::EXE_SUFFIX));
    let mut candidates: Vec<PathBuf> = Vec::new();

    let mut push = |candidate: PathBuf| {
        if !candidates.contains(&candidate) {
            candidates.push(candidate);
        }
    };

    // 1. An explicit override wins outright.
    if let Some(raw) = override_path {
        if !raw.is_empty() {
            push(PathBuf::from(raw));
        }
    }

    // 2. Whatever the inherited PATH can offer.
    if let Some(raw) = path_var {
        for directory in env::split_paths(raw) {
            if directory.as_os_str().is_empty() {
                continue;
            }
            push(directory.join(&executable_name));
        }
    }

    // 3. uv's supported custom tool-bin location.
    if let Some(raw) = uv_tool_bin_dir {
        if !raw.is_empty() {
            push(PathBuf::from(raw).join(&executable_name));
        }
    }

    // 4. Install locations a GUI process usually cannot see through PATH.
    for directory in well_known_bin_dirs(home, app_data) {
        push(directory.join(&executable_name));
    }

    candidates
}

fn well_known_bin_dirs(home: Option<&Path>, app_data: Option<&Path>) -> Vec<PathBuf> {
    let mut directories: Vec<PathBuf> = Vec::new();
    if let Some(home) = home {
        directories.push(home.join(".local").join("bin"));
        if !cfg!(windows) {
            directories.push(home.join("bin"));
            directories.push(home.join(".cargo").join("bin"));
        }
    }
    if !cfg!(windows) {
        directories.push(PathBuf::from("/opt/homebrew/bin"));
        directories.push(PathBuf::from("/usr/local/bin"));
    } else if let Some(app_data) = app_data {
        directories.push(app_data.join("Python").join("Scripts"));
    }
    directories
}

fn home_dir() -> Option<PathBuf> {
    let key = if cfg!(windows) { "USERPROFILE" } else { "HOME" };
    env::var_os(key)
        .map(PathBuf::from)
        .filter(|path| !path.as_os_str().is_empty())
}

fn is_executable_file(path: &Path) -> bool {
    let Ok(metadata) = std::fs::metadata(path) else {
        return false;
    };
    metadata.is_file() && is_executable(&metadata)
}

#[cfg(unix)]
fn is_executable(metadata: &std::fs::Metadata) -> bool {
    use std::os::unix::fs::PermissionsExt;
    metadata.permissions().mode() & 0o111 != 0
}

/// Windows derives executability from the extension, which every candidate
/// already carries through `EXE_SUFFIX`.
#[cfg(not(unix))]
fn is_executable(_metadata: &std::fs::Metadata) -> bool {
    true
}

fn spawn_detached(executable: &Path) -> std::io::Result<std::process::Child> {
    let mut command = Command::new(executable);
    command
        .args(START_ARGS)
        // Nothing the Runtime prints may reach the shell, and therefore the WebView.
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .env(DESKTOP_SHELL_ENV, "1");

    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        // Its own process group: closing the shell, or a terminal signal sent to
        // the shell's group, must not reach the Runtime.
        command.process_group(0);
    }
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const DETACHED_PROCESS: u32 = 0x0000_0008;
        const CREATE_NEW_PROCESS_GROUP: u32 = 0x0000_0200;
        command.creation_flags(DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP);
    }

    command.spawn()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::ffi::OsStr;

    fn executable_name() -> String {
        format!("vibe{}", env::consts::EXE_SUFFIX)
    }

    #[test]
    fn an_explicit_override_is_tried_first() {
        let override_path = PathBuf::from("/opt/custom/vibe-dev");
        let candidates = vibe_executable_candidates(
            Some(override_path.as_os_str()),
            Some(OsStr::new("/usr/bin")),
            None,
            Some(Path::new("/home/tester")),
            None,
        );
        assert_eq!(candidates.first(), Some(&override_path));
    }

    #[test]
    fn an_empty_override_is_ignored() {
        assert_eq!(
            vibe_executable_candidates(Some(OsStr::new("")), None, None, Some(Path::new("/home/tester")), None,),
            vibe_executable_candidates(None, None, None, Some(Path::new("/home/tester")), None),
        );
    }

    #[test]
    fn path_entries_come_before_well_known_directories() {
        let path_var = env::join_paths([PathBuf::from("/custom/bin")]).expect("joins");
        let candidates = vibe_executable_candidates(None, Some(&path_var), None, Some(Path::new("/home/tester")), None);

        let from_path = PathBuf::from("/custom/bin").join(executable_name());
        let from_home = PathBuf::from("/home/tester")
            .join(".local")
            .join("bin")
            .join(executable_name());

        let path_index = candidates.iter().position(|c| *c == from_path).expect("PATH candidate");
        let home_index = candidates.iter().position(|c| *c == from_home).expect("home candidate");
        assert!(path_index < home_index, "got {candidates:?}");
    }

    #[test]
    fn the_gui_fallback_covers_the_documented_install_location() {
        let candidates = vibe_executable_candidates(None, None, None, Some(Path::new("/home/tester")), None);
        let expected = PathBuf::from("/home/tester")
            .join(".local")
            .join("bin")
            .join(executable_name());
        assert!(candidates.contains(&expected), "got {candidates:?}");
    }

    #[test]
    fn candidates_are_deduplicated() {
        let path_var = env::join_paths([PathBuf::from("/home/tester/.local/bin")]).expect("joins");
        let candidates = vibe_executable_candidates(None, Some(&path_var), None, Some(Path::new("/home/tester")), None);
        let duplicated = candidates
            .iter()
            .filter(|candidate| candidate.ends_with(executable_name()))
            .filter(|candidate| candidate.starts_with("/home/tester/.local/bin"))
            .count();
        assert_eq!(duplicated, 1, "got {candidates:?}");
    }

    #[test]
    fn every_candidate_targets_the_platform_executable_name() {
        let path_var = env::join_paths([PathBuf::from("/custom/bin")]).expect("joins");
        let candidates = vibe_executable_candidates(None, Some(&path_var), None, Some(Path::new("/home/tester")), None);
        assert!(!candidates.is_empty());
        for candidate in &candidates {
            assert_eq!(
                candidate.file_name().and_then(|name| name.to_str()),
                Some(executable_name().as_str()),
                "candidate {candidate:?}"
            );
        }
    }

    #[test]
    fn uv_tool_bin_dir_follows_path_and_precedes_default_locations() {
        let path_var = env::join_paths([PathBuf::from("/custom/bin")]).expect("joins");
        let uv_bin = PathBuf::from("/custom/uv-tools");
        let candidates = vibe_executable_candidates(
            None,
            Some(&path_var),
            Some(uv_bin.as_os_str()),
            Some(Path::new("/home/tester")),
            None,
        );

        let path_candidate = PathBuf::from("/custom/bin").join(executable_name());
        let uv_candidate = uv_bin.join(executable_name());
        let default_candidate = PathBuf::from("/home/tester")
            .join(".local")
            .join("bin")
            .join(executable_name());
        let path_index = candidates
            .iter()
            .position(|candidate| *candidate == path_candidate)
            .unwrap();
        let uv_index = candidates
            .iter()
            .position(|candidate| *candidate == uv_candidate)
            .unwrap();
        let default_index = candidates
            .iter()
            .position(|candidate| *candidate == default_candidate)
            .unwrap();

        assert!(path_index < uv_index && uv_index < default_index, "got {candidates:?}");
    }

    #[test]
    fn an_empty_uv_tool_bin_dir_is_ignored() {
        assert_eq!(
            vibe_executable_candidates(None, None, Some(OsStr::new("")), Some(Path::new("/home/tester")), None,),
            vibe_executable_candidates(None, None, None, Some(Path::new("/home/tester")), None),
        );
    }

    #[test]
    fn windows_app_data_scripts_are_platform_gated() {
        let app_data = Path::new("C:/Users/tester/AppData/Roaming");
        let candidates = vibe_executable_candidates(None, None, None, None, Some(app_data));
        let expected = app_data.join("Python").join("Scripts").join(executable_name());

        assert_eq!(candidates.contains(&expected), cfg!(windows), "got {candidates:?}");
    }

    #[cfg(not(windows))]
    #[test]
    fn native_homebrew_precedes_the_legacy_intel_prefix() {
        let directories = well_known_bin_dirs(None, None);
        let native = directories
            .iter()
            .position(|path| path == Path::new("/opt/homebrew/bin"))
            .expect("native Homebrew fallback");
        let legacy = directories
            .iter()
            .position(|path| path == Path::new("/usr/local/bin"))
            .expect("legacy Homebrew fallback");

        assert!(native < legacy, "got {directories:?}");
    }

    #[test]
    fn a_missing_executable_reports_not_found_instead_of_spawning() {
        let launcher = InstalledVibeLauncher {
            candidates: vec![PathBuf::from("/nonexistent/avibe-desktop-test/vibe")],
        };
        let error = match launcher.resolve() {
            Ok(_) => panic!("nothing should resolve"),
            Err(error) => error,
        };
        assert!(matches!(error, LaunchError::ExecutableNotFound));
        assert!(error.is_retryable());
    }

    #[test]
    fn a_directory_is_not_mistaken_for_an_executable() {
        assert!(!is_executable_file(Path::new(env!("CARGO_MANIFEST_DIR"))));
    }

    #[test]
    fn launch_failures_map_to_typed_notices_without_exposing_errors() {
        assert_eq!(
            LaunchError::ExecutableNotFound.notice_code(),
            BootstrapNoticeCode::RuntimeNotFound
        );
        assert_eq!(
            LaunchError::EndpointOutput.notice_code(),
            BootstrapNoticeCode::RuntimeDiscoveryFailed
        );
        assert_eq!(
            LaunchError::InvalidOrigin.notice_code(),
            BootstrapNoticeCode::InvalidOrigin
        );
        assert_eq!(
            LaunchError::Spawn(std::io::Error::other("secret path")).notice_code(),
            BootstrapNoticeCode::RuntimeSpawnFailed
        );
    }

    #[test]
    fn descriptor_parser_accepts_only_the_frozen_schema_and_literal_loopback() {
        let accepted = parse_endpoint_descriptor(br#"{"schema_version":1,"origin":"http://127.0.0.1:5123"}"#)
            .expect("valid descriptor");
        assert_eq!(accepted.as_str(), "http://127.0.0.1:5123");

        for rejected in [
            br#"{"schema_version":2,"origin":"http://127.0.0.1:5123"}"#.as_slice(),
            br#"{"schema_version":1,"origin":"http://localhost:5123"}"#.as_slice(),
            br#"{"schema_version":1,"origin":"http://192.168.1.2:5123"}"#.as_slice(),
            br#"{"schema_version":1,"origin":"http://127.0.0.1:5123","extra":true}"#.as_slice(),
            br#"{"schema_version":1}"#.as_slice(),
            b"not json".as_slice(),
        ] {
            assert!(parse_endpoint_descriptor(rejected).is_err(), "{rejected:?}");
        }
    }

    #[test]
    fn descriptor_parser_rejects_oversized_output() {
        let bytes = vec![b' '; MAX_ENDPOINT_BYTES as usize + 1];
        assert!(matches!(
            parse_endpoint_descriptor(&bytes),
            Err(LaunchError::EndpointOutput)
        ));
    }

    /// A private scratch directory. Nothing here may touch a real Avibe install,
    /// so the "executable" launched below is a script this test wrote itself.
    #[cfg(unix)]
    fn scratch_dir(label: &str) -> PathBuf {
        use std::sync::atomic::{AtomicUsize, Ordering};
        static COUNTER: AtomicUsize = AtomicUsize::new(0);

        let unique = COUNTER.fetch_add(1, Ordering::SeqCst);
        let dir = env::temp_dir().join(format!("avibe-desktop-{label}-{}-{unique}", std::process::id()));
        std::fs::create_dir_all(&dir).expect("scratch directory is created");
        dir
    }

    /// Writes a runnable stand-in for `vibe` and returns its path.
    #[cfg(unix)]
    fn write_fake_runtime(dir: &Path, body: &str) -> PathBuf {
        use std::os::unix::fs::PermissionsExt;

        let executable = dir.join("vibe");
        std::fs::write(&executable, body).expect("fake runtime is written");
        std::fs::set_permissions(&executable, std::fs::Permissions::from_mode(0o755))
            .expect("fake runtime is runnable");
        executable
    }

    #[cfg(unix)]
    #[test]
    fn a_retry_discovers_a_runtime_installed_after_the_first_attempt() {
        let dir = scratch_dir("installed-after-retry");
        let executable = dir.join("vibe");
        let launcher = InstalledVibeLauncher {
            candidates: vec![executable],
        };

        assert!(matches!(launcher.resolve(), Err(LaunchError::ExecutableNotFound)));
        write_fake_runtime(&dir, "#!/bin/sh\nexit 0\n");
        assert!(launcher.resolve().is_ok(), "the retry must see the new installation");

        std::fs::remove_dir_all(&dir).ok();
    }

    #[cfg(unix)]
    #[test]
    fn one_attempt_does_not_switch_executables_between_endpoint_and_launch() {
        let first_dir = scratch_dir("attempt-first");
        let second_dir = scratch_dir("attempt-second");
        let first = write_fake_runtime(&first_dir, "#!/bin/sh\nexit 0\n");
        let second = write_fake_runtime(&second_dir, "#!/bin/sh\nexit 0\n");
        let launcher = InstalledVibeLauncher {
            candidates: vec![first.clone(), second],
        };

        let resolved = launcher.resolve().expect("the first executable resolves");
        std::fs::remove_file(first).expect("the first executable is removed");
        assert!(
            matches!(resolved.launch(), Err(LaunchError::Spawn(_))),
            "an in-flight attempt must not silently switch to the second candidate"
        );
        assert!(
            launcher.resolve().is_ok(),
            "the next attempt may resolve the remaining candidate"
        );

        std::fs::remove_dir_all(&first_dir).ok();
        std::fs::remove_dir_all(&second_dir).ok();
    }

    /// The launch is detached, so the recording appears asynchronously.
    #[cfg(unix)]
    fn wait_for_file(path: &Path) -> String {
        for _ in 0..200 {
            if let Ok(contents) = std::fs::read_to_string(path) {
                if !contents.is_empty() {
                    return contents;
                }
            }
            std::thread::sleep(std::time::Duration::from_millis(25));
        }
        panic!("{} was never written", path.display());
    }

    /// Asserts the launch contract against what the operating system actually
    /// receives, rather than against the constant the code was built from.
    #[cfg(unix)]
    #[test]
    fn the_runtime_is_started_headless_and_marked_as_shell_started() {
        let dir = scratch_dir("launch");
        let recording = dir.join("argv");
        let executable = write_fake_runtime(
            &dir,
            &format!(
                "#!/bin/sh\nprintf '%s\\n' \"$@\" \"shell=$AVIBE_DESKTOP_SHELL\" > \"{}\"\n",
                recording.display()
            ),
        );

        let launcher = InstalledVibeLauncher {
            candidates: vec![executable],
        };
        let launched = launcher
            .resolve()
            .expect("the fake runtime resolves")
            .launch()
            .expect("the fake runtime starts");
        assert!(launched.pid > 0);

        let recorded = wait_for_file(&recording);
        assert_eq!(
            recorded.lines().collect::<Vec<_>>(),
            // Without --no-open-browser the Runtime would open a second window
            // onto the Workbench, in the system browser.
            ["start", "--no-open-browser", "shell=1"],
        );

        std::fs::remove_dir_all(&dir).ok();
    }

    #[cfg(unix)]
    #[test]
    fn the_desktop_endpoint_uses_the_machine_readable_cli_contract() {
        let dir = scratch_dir("endpoint");
        let recording = dir.join("endpoint-argv");
        let executable = write_fake_runtime(
            &dir,
            &format!(
                "#!/bin/sh\nprintf '%s\\n' \"$@\" \"shell=$AVIBE_DESKTOP_SHELL\" > \"{}\"\nprintf '%s\\n' '{{\"schema_version\":1,\"origin\":\"http://127.0.0.1:6123\"}}'\n",
                recording.display()
            ),
        );
        let launcher = InstalledVibeLauncher {
            candidates: vec![executable],
        };

        let endpoint = launcher
            .resolve()
            .expect("the fake runtime resolves")
            .endpoint()
            .expect("descriptor is accepted");

        assert_eq!(endpoint.as_str(), "http://127.0.0.1:6123");
        assert_eq!(
            wait_for_file(&recording).lines().collect::<Vec<_>>(),
            ["desktop", "endpoint", "--json", "shell=1"],
        );
        std::fs::remove_dir_all(&dir).ok();
    }

    /// The bootstrap loop aborts a doomed wait on this verdict, so it has to be
    /// right in both directions: a launcher that refused its arguments must be
    /// visible, and the ordinary `vibe start` — which exits 0 once the Runtime is
    /// up — must never be mistaken for one.
    #[cfg(unix)]
    #[test]
    fn only_a_non_zero_launcher_exit_is_reported_as_a_failure() {
        let refused = scratch_dir("exit-nonzero");
        let launcher = InstalledVibeLauncher {
            candidates: vec![write_fake_runtime(&refused, "#!/bin/sh\nexit 3\n")],
        };
        let launched = launcher
            .resolve()
            .expect("the fake runtime resolves")
            .launch()
            .expect("the fake runtime starts");
        // The wait runs on a detached thread, so the verdict arrives late.
        for _ in 0..200 {
            if launched.watch.failed() {
                break;
            }
            std::thread::sleep(std::time::Duration::from_millis(25));
        }
        assert!(launched.watch.failed(), "a launcher that exited non-zero is invisible");

        let succeeded = scratch_dir("exit-zero");
        let launcher = InstalledVibeLauncher {
            candidates: vec![write_fake_runtime(&succeeded, "#!/bin/sh\nexit 0\n")],
        };
        let launched = launcher
            .resolve()
            .expect("the fake runtime resolves")
            .launch()
            .expect("the fake runtime starts");
        std::thread::sleep(std::time::Duration::from_millis(250));
        assert!(
            !launched.watch.failed(),
            "a normal start would be aborted as if it had failed"
        );

        std::fs::remove_dir_all(&refused).ok();
        std::fs::remove_dir_all(&succeeded).ok();
    }
}
