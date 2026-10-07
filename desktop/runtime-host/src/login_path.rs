//! The `PATH` the user's terminal would give the Runtime.
//!
//! macOS starts an application from Finder, the Dock, or Launchpad with
//! launchd's minimal `PATH` (`/usr/bin:/bin:/usr/sbin:/sbin`). Version managers
//! such as nvm extend `PATH` only in shell startup files, so a Runtime started
//! with the application's own `PATH` cannot run any `#!/usr/bin/env node`
//! launcher: an npm-installed Codex or Claude Code fails on every run, while the
//! same Runtime started from a terminal works.
//!
//! The shell therefore asks the user's login shell, once per Runtime start, what
//! `PATH` it ends up with, the same way editors that start language servers do.
//! Nothing here is required for the Runtime to start: every failure leaves the
//! application's own `PATH` in place, which is exactly the behaviour before this
//! lookup existed.

use std::ffi::OsString;
#[cfg(target_os = "macos")]
use std::{
    io::Read,
    path::Path,
    process::{Command, Stdio},
    sync::mpsc,
    time::{Duration, Instant},
};

/// How long the login shell may take to print its `PATH`.
///
/// Startup files that load nvm, conda, or a prompt framework commonly take one
/// or two seconds. The budget is only spent in full when a startup file waits
/// for input or hangs, and the cost of that is the old `PATH`, not a failed
/// start.
#[cfg(target_os = "macos")]
pub const LOGIN_PATH_TIMEOUT: Duration = Duration::from_secs(10);

/// The shell macOS gives a new account, used when `SHELL` is not usable.
#[cfg(target_os = "macos")]
const DEFAULT_SHELL: &str = "/bin/zsh";

#[cfg(target_os = "macos")]
const START_MARKER: &[u8] = b"__AVIBE_LOGIN_PATH_START__";
#[cfg(target_os = "macos")]
const END_MARKER: &[u8] = b"__AVIBE_LOGIN_PATH_END__";

/// Startup files may print banners, so the value is fenced by markers. The
/// commands are absolute and the arguments need no quoting, so sh, bash, zsh,
/// fish, and nushell all run it as written.
#[cfg(target_os = "macos")]
const PRINT_PATH_SCRIPT: &str = "/usr/bin/printf %s __AVIBE_LOGIN_PATH_START__; /usr/bin/printenv PATH; \
                                 /usr/bin/printf %s __AVIBE_LOGIN_PATH_END__";

/// Startup output is not read past this; a `PATH` is far smaller.
#[cfg(target_os = "macos")]
const MAX_OUTPUT_BYTES: usize = 256 * 1024;

/// What the Runtime start will use as its inherited `PATH`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum LoginPath {
    /// The `PATH` the user's login shell ends up with.
    Resolved(OsString),
    /// Keep the application's own `PATH`, for the stated reason.
    Inherited(&'static str),
}

impl LoginPath {
    /// The stable code the bootstrap log records for this outcome.
    pub fn outcome(&self) -> &'static str {
        match self {
            Self::Resolved(_) => "resolved",
            Self::Inherited(reason) => reason,
        }
    }
}

/// The `PATH` a Runtime started now should inherit.
///
/// Only macOS gives applications a `PATH` that differs from the terminal's;
/// Windows applications receive the user's registry `PATH`. An application
/// started from a terminal (a development run) already carries that terminal's
/// environment, and an interactive shell attached to the same terminal would
/// compete with it for the foreground.
pub fn login_shell_path() -> LoginPath {
    #[cfg(target_os = "macos")]
    {
        use std::io::IsTerminal;
        if std::io::stdin().is_terminal() {
            return LoginPath::Inherited("terminal");
        }
        let shell = std::env::var_os("SHELL")
            .map(std::path::PathBuf::from)
            .filter(|shell| shell.is_absolute())
            .unwrap_or_else(|| std::path::PathBuf::from(DEFAULT_SHELL));
        resolve_with(&shell, LOGIN_PATH_TIMEOUT)
    }
    #[cfg(not(target_os = "macos"))]
    {
        LoginPath::Inherited("unsupported")
    }
}

/// Runs `shell` as an interactive login shell and reads the `PATH` it prints.
///
/// Interactive as well as login: nvm's installer writes to `.zshrc` and
/// `.bashrc`, which only interactive shells read.
#[cfg(target_os = "macos")]
pub(crate) fn resolve_with(shell: &Path, timeout: Duration) -> LoginPath {
    use std::os::unix::process::CommandExt;

    let mut command = Command::new(shell);
    command
        .args(["-l", "-i", "-c", PRINT_PATH_SCRIPT])
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        // Out of the shell's own process group, so nothing a startup file does
        // with job control or signals reaches the desktop shell.
        .process_group(0);
    let Ok(mut child) = command.spawn() else {
        return LoginPath::Inherited("spawn_failed");
    };
    let Some(mut stdout) = child.stdout.take() else {
        let _ = child.kill();
        let _ = child.wait();
        return LoginPath::Inherited("spawn_failed");
    };

    // A process a startup file leaves behind can hold stdout open after the
    // shell is gone, so the reader is never joined: the value is taken as soon
    // as both markers have arrived.
    let (sender, receiver) = mpsc::channel::<Vec<u8>>();
    std::thread::spawn(move || {
        let mut chunk = [0_u8; 4096];
        loop {
            match stdout.read(&mut chunk) {
                Ok(0) | Err(_) => break,
                Ok(read) => {
                    if sender.send(chunk[..read].to_vec()).is_err() {
                        break;
                    }
                }
            }
        }
    });

    let deadline = Instant::now() + timeout;
    let mut output = Vec::new();
    let outcome = loop {
        if let Some(path) = parse_marked_path(&output) {
            break path;
        }
        if output.len() > MAX_OUTPUT_BYTES {
            break LoginPath::Inherited("output_too_large");
        }
        let remaining = deadline.saturating_duration_since(Instant::now());
        match receiver.recv_timeout(remaining) {
            Ok(chunk) => output.extend_from_slice(&chunk),
            Err(mpsc::RecvTimeoutError::Timeout) => break LoginPath::Inherited("timeout"),
            Err(mpsc::RecvTimeoutError::Disconnected) => break LoginPath::Inherited("no_path"),
        }
    };
    end_process_group(&mut child);
    outcome
}

/// Ends the login shell and everything its startup files started.
///
/// A startup file that blocks in a subprocess (`sleep`, a prompt that waits for
/// input) would otherwise outlive the shell and hold stdout, and with it the
/// reader thread, open. The group is signalled before the shell is reaped: until
/// then its leader's pid, and so the group id, cannot be reused by an unrelated
/// process. The crate forbids `unsafe`, so the signal goes through `kill(1)`.
#[cfg(target_os = "macos")]
fn end_process_group(child: &mut std::process::Child) {
    let _ = Command::new("/bin/kill")
        .args(["-KILL", "--", &format!("-{}", child.id())])
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status();
    let _ = child.kill();
    let _ = child.wait();
}

/// The value between the markers, or `None` until the end marker has arrived.
#[cfg(target_os = "macos")]
fn parse_marked_path(output: &[u8]) -> Option<LoginPath> {
    use std::os::unix::ffi::OsStringExt;

    let start = find(output, START_MARKER)? + START_MARKER.len();
    let length = find(&output[start..], END_MARKER)?;
    let mut value = &output[start..start + length];
    while let Some(rest) = value.strip_suffix(b"\n").or_else(|| value.strip_suffix(b"\r")) {
        value = rest;
    }
    if value.is_empty() || value.contains(&0) {
        return Some(LoginPath::Inherited("no_path"));
    }
    Some(LoginPath::Resolved(OsString::from_vec(value.to_vec())))
}

#[cfg(target_os = "macos")]
fn find(haystack: &[u8], needle: &[u8]) -> Option<usize> {
    haystack.windows(needle.len()).position(|window| window == needle)
}

#[cfg(all(test, target_os = "macos"))]
mod tests {
    use std::os::unix::fs::PermissionsExt;
    use std::path::PathBuf;

    use super::*;

    fn scratch_dir(label: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!(
            "avibe-login-path-{label}-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .expect("clock")
                .as_nanos()
        ));
        std::fs::create_dir_all(&dir).expect("scratch dir");
        dir
    }

    /// A stand-in login shell: `body` runs first, then the script the shell
    /// was given, the way a startup file runs before `-c`.
    fn fake_shell(dir: &Path, body: &str) -> PathBuf {
        let shell = dir.join("shell");
        std::fs::write(&shell, format!("#!/bin/sh\n{body}\nshift 3\neval \"$1\"\n")).expect("fake shell");
        std::fs::set_permissions(&shell, std::fs::Permissions::from_mode(0o755)).expect("chmod");
        shell
    }

    #[test]
    fn the_path_a_startup_file_exports_is_returned_without_its_banner() {
        let dir = scratch_dir("resolved");
        let shell = fake_shell(
            &dir,
            "echo 'Welcome back'\nPATH=\"/home/user/.nvm/versions/node/v22/bin:$PATH\"; export PATH",
        );

        let resolved = resolve_with(&shell, Duration::from_secs(10));

        let LoginPath::Resolved(path) = resolved else {
            panic!("expected a resolved PATH, got {resolved:?}");
        };
        let path = path.into_string().expect("utf-8 PATH");
        assert!(path.starts_with("/home/user/.nvm/versions/node/v22/bin:"), "{path}");
        assert!(!path.contains('\n'));
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn the_shell_is_started_as_an_interactive_login_shell() {
        let dir = scratch_dir("flags");
        let recording = dir.join("argv");
        let shell = fake_shell(
            &dir,
            &format!("printf '%s\\n' \"$1\" \"$2\" \"$3\" > '{}'", recording.display()),
        );

        assert!(matches!(
            resolve_with(&shell, Duration::from_secs(10)),
            LoginPath::Resolved(_)
        ));
        assert_eq!(
            std::fs::read_to_string(&recording).expect("recorded argv"),
            "-l\n-i\n-c\n"
        );
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn a_startup_file_that_never_finishes_falls_back_after_the_timeout() {
        let dir = scratch_dir("timeout");
        let shell = fake_shell(&dir, "exec sleep 30");

        let started = Instant::now();
        let resolved = resolve_with(&shell, Duration::from_millis(300));

        assert_eq!(resolved, LoginPath::Inherited("timeout"));
        assert!(started.elapsed() < Duration::from_secs(5));
        std::fs::remove_dir_all(&dir).ok();
    }

    /// `.zshrc` running `sleep 30` without `exec` blocks in a subprocess, not
    /// in the shell; the fallback must not leave that subprocess behind.
    #[test]
    fn a_fallback_ends_what_the_startup_files_started() {
        let dir = scratch_dir("group");
        let pid_file = dir.join("pid");
        let shell = fake_shell(&dir, &format!("sleep 30 &\necho $! > '{}'\nwait", pid_file.display()));

        // Long enough for the fake shell to start and record its child while
        // the rest of the suite is spawning processes too.
        assert_eq!(
            resolve_with(&shell, Duration::from_secs(3)),
            LoginPath::Inherited("timeout")
        );

        let pid = std::fs::read_to_string(&pid_file).expect("startup file ran");
        let alive = || {
            Command::new("/bin/kill")
                .args(["-0", pid.trim()])
                .stderr(Stdio::null())
                .status()
                .is_ok_and(|status| status.success())
        };
        let deadline = Instant::now() + Duration::from_secs(5);
        while alive() && Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(20));
        }
        assert!(!alive(), "the startup file's subprocess outlived the fallback");
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn a_shell_that_exits_without_printing_falls_back() {
        let dir = scratch_dir("exit");
        let shell = fake_shell(&dir, "exit 1");

        assert_eq!(
            resolve_with(&shell, Duration::from_secs(10)),
            LoginPath::Inherited("no_path")
        );
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn a_missing_shell_falls_back() {
        assert_eq!(
            resolve_with(Path::new("/nonexistent/avibe-test-shell"), Duration::from_secs(1)),
            LoginPath::Inherited("spawn_failed")
        );
    }

    #[test]
    fn an_empty_value_between_the_markers_is_not_a_path() {
        assert_eq!(
            parse_marked_path(b"__AVIBE_LOGIN_PATH_START__\n__AVIBE_LOGIN_PATH_END__"),
            Some(LoginPath::Inherited("no_path"))
        );
        assert_eq!(parse_marked_path(b"__AVIBE_LOGIN_PATH_START__/usr/bin"), None);
    }
}
