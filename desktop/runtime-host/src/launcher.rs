//! Starting the installed Avibe Runtime.
//!
//! Two rules shape this module:
//!
//! 1. The Runtime outlives normal shell lifecycle. Launched processes are
//!    detached and reaped; only a handover, an explicit Stop or Quit, or
//!    uninstall asks the Runtime's own CLI to stop it, and always scoped to one
//!    Runtime identity.
//! 2. No shell interpreter is involved. The executable is resolved to a real
//!    path and spawned directly, so no user-controlled string is ever parsed as
//!    a command line.

use std::env;
use std::ffi::{OsStr, OsString};
use std::io::Read;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::{mpsc, Arc, OnceLock};
use std::time::{Duration, Instant};

use serde::Deserialize;

use crate::bootstrap_log::BootstrapLog;
use crate::origin::LoopbackOrigin;
use crate::private_runtime::{InstalledPrivateRuntime, PrivateRuntimeBundle, PrivateRuntimeError};
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

/// Prevents the embedded Python environment from trying to update itself.
pub const DESKTOP_MANAGED_RUNTIME_ENV: &str = "AVIBE_DESKTOP_MANAGED_RUNTIME";

/// Identifies the app-private Runtime root to the Python process.
pub const DESKTOP_RUNTIME_ROOT_ENV: &str = "AVIBE_DESKTOP_RUNTIME_ROOT";

/// Identifies the bundled npm CLI that must be invoked through private Node.js.
pub const DESKTOP_NPM_CLI_ENV: &str = "AVIBE_DESKTOP_NPM_CLI";

/// Identifies the mutable root for app-private agent backends.
pub const DESKTOP_BACKENDS_ROOT_ENV: &str = "AVIBE_DESKTOP_BACKENDS_ROOT";

/// How the shell starts a Runtime.
///
/// `start` is idempotent: it brings up what is missing without stopping what is
/// running. `--no-open-browser` is what keeps a desktop launch from *also*
/// opening a system browser at the Workbench — plain `vibe start` honours
/// `config.ui.open_browser` and would leave the user with two windows onto the
/// same Runtime. The shell owns a WebView for exactly this purpose, so it always
/// opts out.
const START_ARGS: [&str; 2] = ["start", "--no-open-browser"];
/// Lets this start stop another desktop Runtime that serves this home. `vibe
/// start` refuses that handover without it.
const HAND_OVER_ARG: &str = "--hand-over";
const ENDPOINT_ARGS: [&str; 3] = ["desktop", "endpoint", "--json"];
const REMOVE_BACKENDS_ARGS: [&str; 2] = ["desktop", "remove-backends"];
/// How long the shell waits for the Runtime to name its own address.
///
/// Sized for a *cold* first launch, not a warm one. The bundle has just been
/// extracted, so nothing in the runtime tree has been paged in or evaluated by
/// the system yet, and the interpreter pays for all of it on this first call.
/// Measured on an M-series Mac with the exact environment `RuntimeCommand::private`
/// builds: 15.8s and 18.4s on two independent cold extractions, 20.0s for the
/// x86_64 build under Rosetta, against 2.3-4.1s once the binaries are warm. A 10s
/// budget was therefore sized for the second launch and cut off the first one,
/// which reached the user as `runtime_discovery_failed` on every fresh install.
///
/// This is the last step on the cold path that still held a warm number --
/// readiness already allows 120s (`DEFAULT_READY_TIMEOUT`). Staying under that
/// keeps discovery from dominating the launch, while 3x the worst measurement
/// leaves room for slower hardware. The budget is only spent in full when the
/// endpoint is genuinely broken, and that failure is already retryable.
const ENDPOINT_TIMEOUT: Duration = Duration::from_secs(60);

/// How much of a helper's stderr the shell keeps: the bootstrap log's
/// diagnostic, and a lifecycle verb's closing JSON verdict.
const MAX_STDERR_TAIL_BYTES: usize = 4 * 1024;

/// How long the stderr reader is given to reach EOF once the helper is over.
///
/// A grandchild that inherited the pipe can hold it open after the child is
/// gone, and a diagnostic is worth waiting a moment for and nothing more.
const STDERR_DRAIN: Duration = Duration::from_secs(2);
const MAX_ENDPOINT_BYTES: u64 = 4096;
#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

#[derive(Debug, thiserror::Error)]
pub enum LaunchError {
    #[error("installed Avibe Runtime not found")]
    ExecutableNotFound,
    #[error("failed to prepare the app-private Avibe Runtime")]
    RuntimeInstall,
    #[error("failed to remove the app-private Avibe Runtime")]
    RuntimeRemoval,
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
    #[error("the Runtime is being stopped")]
    RuntimeStop,
}

/// How one run of a Runtime lifecycle verb ended: `vibe stop
/// --expect-runtime-id` or `vibe desktop remove-backends`.
///
/// Read from the exit code and the last JSON line the verb wrote to stderr.
/// The reason and part are stable codes for the log; none of them reaches the
/// WebView.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CliOutcome {
    /// Exit 0.
    Completed,
    /// Exit 3: the verb refused and changed nothing.
    Refused { reason: String },
    /// Exit 2, any other exit, or a signal: part of the work may not have
    /// happened, and something may still be running.
    Failed { part: String },
    /// The verb could not be run at all, so it proved nothing either way.
    Unrunnable,
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
            Self::RuntimeInstall | Self::RuntimeRemoval => BootstrapNoticeCode::RuntimeInstallFailed,
            Self::EndpointSpawn(_) | Self::EndpointTimeout | Self::EndpointExit | Self::EndpointOutput => {
                BootstrapNoticeCode::RuntimeDiscoveryFailed
            }
            Self::InvalidOrigin => BootstrapNoticeCode::InvalidOrigin,
            Self::Spawn(_) | Self::RuntimeStop => BootstrapNoticeCode::RuntimeSpawnFailed,
        }
    }
}

/// Resolves one installed Avibe Runtime for a single bootstrap attempt.
///
/// Resolution is deliberately attempt-scoped. A retry after installation or
/// repair must observe the current filesystem instead of a shell-lifetime cache.
pub trait RuntimeLauncher: Send + Sync {
    fn resolve(&self) -> Result<Arc<dyn ResolvedRuntimeLauncher>, LaunchError>;

    /// Whether uninstall has app-private files to remove. Installed and
    /// user-managed launchers own nothing and are never modified.
    fn owns_private_files(&self) -> bool {
        false
    }

    /// Deletes the private Runtime tree.
    ///
    /// Runs only after the Runtime was stopped and `remove-backends` deleted
    /// the backend root under every install claim. The backend root is left
    /// alone on purpose: an install that starts afterwards owns it.
    fn remove_private_files(&self) -> Result<(), LaunchError> {
        Ok(())
    }

    /// Deletes the private Runtime tree and the backend root without the
    /// Runtime's confirmation that its installs have stopped. Only a user who
    /// chose "Delete anyway" reaches this.
    fn remove_unverified_private_files(&self) -> Result<(), LaunchError> {
        Ok(())
    }
}

/// One executable frozen for a single bootstrap attempt.
///
/// Every method uses this exact executable. `launch` returns as soon as it is
/// spawned; readiness is decided by the presence probe.
pub trait ResolvedRuntimeLauncher: Send + Sync {
    fn endpoint(&self) -> Result<LoopbackOrigin, LaunchError>;
    /// `hand_over` lets the start replace another desktop Runtime serving this
    /// home. The start owns that act and reports it in its exit.
    fn launch(&self, hand_over: bool) -> Result<LaunchedRuntime, LaunchError>;

    /// The provenance id of the Runtime this launcher starts, if it has one.
    /// Only a Runtime serving this id is this launcher's own.
    fn expected_runtime_id(&self) -> Option<&str> {
        None
    }

    /// Stops the Runtime whose identity is `runtime_id`, and nothing else.
    fn stop(&self, _runtime_id: &str) -> CliOutcome {
        CliOutcome::Unrunnable
    }

    /// Deletes the app-private backend root under every install claim.
    fn remove_backends(&self) -> CliOutcome {
        CliOutcome::Unrunnable
    }

    fn prune_superseded(&self) {}
}

/// What the launcher's exit says about one start.
///
/// `vibe start` is short-lived by design: it brings the Runtime up and exits, so
/// the shell cannot treat "it is gone" as failure. Its exit code distinguishes
/// startup failure from completion, and for a start of this shell's own Runtime
/// the handover's verdict: stop authority comes from the Runtime's identity,
/// never from this helper.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LaunchExit {
    /// 0: the start completed.
    Started,
    /// 3 from a start of this shell's Runtime: another desktop Runtime serves
    /// this home, and the start did not replace it.
    HandoverRefused,
    /// 2 from a start asked to hand over: the Runtime it replaced may be left
    /// partly running.
    HandoverFailed,
    /// Any other exit, or a signal.
    Failed,
}

impl LaunchExit {
    fn of(code: Option<i32>, identified: bool, hand_over: bool) -> Self {
        match code {
            Some(0) => Self::Started,
            Some(3) if identified => Self::HandoverRefused,
            Some(2) if hand_over => Self::HandoverFailed,
            _ => Self::Failed,
        }
    }
}

/// Whether the launcher process itself survived long enough to do its job.
/// Empty means "still running, or not observable".
#[derive(Debug, Clone, Default)]
pub struct LaunchWatch(Arc<OnceLock<LaunchExit>>);

impl LaunchWatch {
    /// A watch that has already seen the launcher exit. For launchers that learn
    /// the outcome synchronously, and for tests.
    pub fn exited(exit: LaunchExit) -> Self {
        let watch = Self::default();
        watch.record(exit);
        watch
    }

    /// The exit seen, if the launcher has exited.
    pub fn exit(&self) -> Option<LaunchExit> {
        self.0.get().copied()
    }

    /// True only once the launcher has been *seen* to exit non-zero.
    pub fn failed(&self) -> bool {
        self.exit().is_some_and(|exit| exit != LaunchExit::Started)
    }

    /// True only once the launcher has been *seen* to exit successfully.
    pub fn succeeded(&self) -> bool {
        self.exit() == Some(LaunchExit::Started)
    }

    /// Neither outcome has been seen: the launcher may still be starting pieces.
    pub fn pending(&self) -> bool {
        self.0.get().is_none()
    }

    fn record(&self, exit: LaunchExit) {
        let _ = self.0.set(exit);
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
            command: RuntimeCommand::installed(self.resolve_executable()?),
            expected_runtime_id: None,
            cleanup: None,
            // A development shell drives an install it does not own and has no
            // application data directory to write diagnostics into.
            log: BootstrapLog::disabled(),
        }))
    }
}

/// Installs and launches the Runtime embedded in a product desktop package.
#[derive(Debug, Clone)]
pub struct BundledVibeLauncher {
    bundle: PrivateRuntimeBundle,
    backend_root: PathBuf,
    log: BootstrapLog,
}

impl BundledVibeLauncher {
    pub fn new(bundle_dir: PathBuf, install_root: PathBuf, backend_root: PathBuf, log: BootstrapLog) -> Self {
        Self {
            bundle: PrivateRuntimeBundle::new(bundle_dir, install_root),
            backend_root,
            log,
        }
    }
}

impl RuntimeLauncher for BundledVibeLauncher {
    fn resolve(&self) -> Result<Arc<dyn ResolvedRuntimeLauncher>, LaunchError> {
        let started = Instant::now();
        let prepared = self.bundle.prepare();
        record_runtime_prepare(&self.log, &prepared, started.elapsed());
        let runtime = prepared.map_err(|_| LaunchError::RuntimeInstall)?;
        let runtime_id = runtime.runtime_id.clone();
        Ok(Arc::new(ResolvedVibeExecutable {
            command: RuntimeCommand::private(
                runtime.root.clone(),
                runtime.python,
                runtime.node,
                runtime.npm_cli,
                self.backend_root.clone(),
                &runtime_id,
                env::var_os("PATH").as_deref(),
            ),
            expected_runtime_id: Some(runtime_id),
            cleanup: Some((self.bundle.clone(), runtime.root)),
            log: self.log.clone(),
        }))
    }

    fn owns_private_files(&self) -> bool {
        true
    }

    fn remove_private_files(&self) -> Result<(), LaunchError> {
        self.bundle.remove_all().map_err(|_| LaunchError::RuntimeRemoval)
    }

    fn remove_unverified_private_files(&self) -> Result<(), LaunchError> {
        // Checked before anything is deleted, so an unsafe backend root keeps
        // the Runtime tree too.
        validate_private_directory_root(&self.backend_root)?;
        self.bundle.remove_all().map_err(|_| LaunchError::RuntimeRemoval)?;
        remove_private_directory_root(&self.backend_root)
    }
}

fn validate_private_directory_root(path: &Path) -> Result<bool, LaunchError> {
    let metadata = match std::fs::symlink_metadata(path) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(false),
        Err(_) => return Err(LaunchError::RuntimeRemoval),
    };
    if metadata.file_type().is_dir() && !metadata.file_type().is_symlink() {
        Ok(true)
    } else {
        Err(LaunchError::RuntimeRemoval)
    }
}

fn remove_private_directory_root(path: &Path) -> Result<(), LaunchError> {
    if validate_private_directory_root(path)? {
        std::fs::remove_dir_all(path).map_err(|_| LaunchError::RuntimeRemoval)?;
    }
    Ok(())
}

#[derive(Debug)]
struct ResolvedVibeExecutable {
    command: RuntimeCommand,
    expected_runtime_id: Option<String>,
    cleanup: Option<(PrivateRuntimeBundle, PathBuf)>,
    log: BootstrapLog,
}

impl ResolvedRuntimeLauncher for ResolvedVibeExecutable {
    fn endpoint(&self) -> Result<LoopbackOrigin, LaunchError> {
        query_endpoint(&self.command, &self.log)
    }

    fn launch(&self, hand_over: bool) -> Result<LaunchedRuntime, LaunchError> {
        // Only this shell's own Runtime is started by a CLI of its version,
        // which knows the flag and exits by the handover contract.
        let identified = self.expected_runtime_id.is_some();
        let hand_over = hand_over && identified;
        let started = Instant::now();
        let child = spawn_detached(&self.command, hand_over).map_err(LaunchError::Spawn)?;
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
        let log = self.log.clone();
        std::thread::spawn(move || {
            let mut child = child;
            if let Ok(status) = child.wait() {
                let exit = LaunchExit::of(status.code(), identified, hand_over);
                log.record(
                    "runtime.start",
                    &[
                        ("outcome", format!("{exit:?}")),
                        ("exit", status.to_string()),
                        ("hand_over", hand_over.to_string()),
                        ("ms", started.elapsed().as_millis().to_string()),
                    ],
                );
                reaped.record(exit);
            }
        });

        Ok(LaunchedRuntime { pid, watch })
    }

    fn expected_runtime_id(&self) -> Option<&str> {
        self.expected_runtime_id.as_deref()
    }

    fn stop(&self, runtime_id: &str) -> CliOutcome {
        run_lifecycle_verb(&self.command, &stop_arguments(runtime_id), &self.log, "runtime.stop")
    }

    fn remove_backends(&self) -> CliOutcome {
        run_lifecycle_verb(
            &self.command,
            &REMOVE_BACKENDS_ARGS,
            &self.log,
            "runtime.remove_backends",
        )
    }

    fn prune_superseded(&self) {
        if let Some((bundle, active_root)) = &self.cleanup {
            let _ = bundle.prune_superseded(active_root);
        }
    }
}

#[derive(Debug)]
struct RuntimeCommand {
    executable: PathBuf,
    prefix_args: Vec<OsString>,
    environment: Vec<(OsString, OsString)>,
    /// Whether the shell's own `PYTHON*` variables are withheld from the child.
    ///
    /// Only the private Runtime sets this. `-I` protects just the interpreter
    /// that receives the flag: `os.environ` still carries the inherited values,
    /// and the Controller and UI interpreters that Runtime starts copy it and
    /// run without `-I`. An inherited `PYTHONHOME` then aborts both before they
    /// are ready, and a `PYTHONPATH` imports code from outside the verified
    /// tree. A development shell drives an install it does not own, which runs
    /// with the user's environment exactly as their terminal would.
    withholds_inherited_python: bool,
}

impl RuntimeCommand {
    fn installed(executable: PathBuf) -> Self {
        Self {
            executable,
            prefix_args: Vec::new(),
            environment: Vec::new(),
            withholds_inherited_python: false,
        }
    }

    fn private(
        runtime_root: PathBuf,
        python: PathBuf,
        node: PathBuf,
        npm_cli: PathBuf,
        backends_root: PathBuf,
        runtime_id: &str,
        inherited_path: Option<&OsStr>,
    ) -> Self {
        let tools_dir = node.parent().expect("validated private Node has a parent");
        // The builder includes a relocatable vibe entry point in bin. Expose
        // only that directory, never python/bin (which would shadow user tools).
        let cli_dir = runtime_root.join("bin");
        let mut path_entries = vec![cli_dir.clone(), tools_dir.to_owned()];
        if let Some(path) = inherited_path {
            path_entries.extend(env::split_paths(path).filter(|entry| !entry.as_os_str().is_empty()));
        }
        let private_path = env::join_paths(path_entries).unwrap_or_else(|_| cli_dir.into_os_string());
        Self {
            executable: python,
            // Isolated mode excludes the user site and PYTHONPATH. The Avibe
            // wheel lives in this interpreter's own site-packages.
            //
            // `-B` is not redundant with the PYTHONDONTWRITEBYTECODE below, and
            // removing it reintroduces a bug that cost two days: `-I` implies
            // `-E`, so this interpreter ignores every PYTHON* variable — including
            // that one. Without the flag the Runtime writes `.pyc` files into its
            // own install tree, `verify_installed_tree` then finds a tree the
            // manifest no longer describes, and `prepare()` re-extracts 358MB on
            // every single launch until both install slots are invalid.
            prefix_args: vec![
                OsString::from("-I"),
                OsString::from("-B"),
                OsString::from("-m"),
                OsString::from("vibe"),
            ],
            environment: vec![
                (OsString::from("PATH"), private_path),
                (OsString::from("VIBE_SHOW_RUNTIME_NODE_BIN"), node.into_os_string()),
                (OsString::from(DESKTOP_NPM_CLI_ENV), npm_cli.into_os_string()),
                (
                    OsString::from(DESKTOP_BACKENDS_ROOT_ENV),
                    backends_root.into_os_string(),
                ),
                (OsString::from(DESKTOP_MANAGED_RUNTIME_ENV), OsString::from("1")),
                (OsString::from(DESKTOP_RUNTIME_ROOT_ENV), runtime_root.into_os_string()),
                (OsString::from("AVIBE_DESKTOP_RUNTIME_ID"), OsString::from(runtime_id)),
                // For descendants: child interpreters the Runtime starts do not
                // all run under `-I`, and those do honour this. The `-B` above is
                // what covers this process itself.
                (OsString::from("PYTHONDONTWRITEBYTECODE"), OsString::from("1")),
            ],
            withholds_inherited_python: true,
        }
    }

    fn apply(&self, command: &mut Command) {
        command.args(&self.prefix_args);
        self.apply_environment(command, env::vars_os());
    }

    /// The environment half of `apply`, over an explicit inherited set.
    ///
    /// Production passes this process's own environment. A test passes one it
    /// built, so that it never has to mutate a variable every other test thread
    /// reads while it spawns.
    fn apply_environment(&self, command: &mut Command, inherited: impl IntoIterator<Item = (OsString, OsString)>) {
        if self.withholds_inherited_python {
            for (name, _) in inherited {
                if is_python_startup_variable(&name) {
                    command.env_remove(name);
                }
            }
        }
        // After the removal, so the one Python variable the Runtime does want
        // is ours rather than whatever the shell was started with.
        for (name, value) in &self.environment {
            command.env(name, value);
        }
    }
}

/// Every variable CPython reads at startup is named `PYTHON*`. Windows looks
/// environment names up without regard to case, so `PythonPath` counts there.
fn is_python_startup_variable(name: &OsStr) -> bool {
    let name = name.to_string_lossy();
    if cfg!(windows) {
        name.to_ascii_uppercase().starts_with("PYTHON")
    } else {
        name.starts_with("PYTHON")
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct EndpointDescriptor {
    schema_version: u32,
    origin: String,
}

/// One run of the endpoint helper, including everything a `LaunchError` cannot
/// carry: how long it took, what it printed on stderr, and the exit status it
/// actually had rather than the fact that it was not zero.
struct EndpointAttempt {
    result: Result<LoopbackOrigin, LaunchError>,
    status: Option<std::process::ExitStatus>,
    stderr: Option<Vec<u8>>,
    elapsed: Duration,
}

fn query_endpoint(runtime: &RuntimeCommand, log: &BootstrapLog) -> Result<LoopbackOrigin, LaunchError> {
    query_endpoint_within(runtime, log, ENDPOINT_TIMEOUT)
}

/// The timeout is a parameter so a test can drive the kill path in milliseconds
/// instead of a minute. Production has exactly one caller and it passes
/// `ENDPOINT_TIMEOUT`.
fn query_endpoint_within(
    runtime: &RuntimeCommand,
    log: &BootstrapLog,
    timeout: Duration,
) -> Result<LoopbackOrigin, LaunchError> {
    let started = Instant::now();
    let mut status = None;
    let mut stderr_reader = None;
    let result = endpoint_descriptor(runtime, timeout, &mut status, &mut stderr_reader);
    // Collected after the query returns, so a timeout that had to kill the child
    // still reports what that child said before it was killed.
    let stderr = stderr_reader.and_then(|reader| reader.recv_timeout(STDERR_DRAIN).ok());
    let attempt = EndpointAttempt {
        result,
        status,
        stderr,
        elapsed: started.elapsed(),
    };
    record_endpoint_attempt(log, &attempt);
    attempt.result
}

fn endpoint_descriptor(
    runtime: &RuntimeCommand,
    timeout: Duration,
    status_slot: &mut Option<std::process::ExitStatus>,
    stderr_slot: &mut Option<mpsc::Receiver<Vec<u8>>>,
) -> Result<LoopbackOrigin, LaunchError> {
    let mut command = Command::new(&runtime.executable);
    runtime.apply(&mut command);
    command
        .args(ENDPOINT_ARGS)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        // Discarding this is what left us with a notice code and nothing else
        // when discovery failed on a machine we could not reach.
        .stderr(Stdio::piped())
        .env(DESKTOP_SHELL_ENV, "1");
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        // The endpoint helper is a short-lived console executable. A GUI parent
        // does not suppress its console automatically, so keep discovery silent.
        command.creation_flags(CREATE_NO_WINDOW);
    }
    let mut child = command.spawn().map_err(LaunchError::EndpointSpawn)?;
    if let Some(stderr) = child.stderr.take() {
        *stderr_slot = Some(spawn_stderr_tail(stderr));
    }
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

    let deadline = Instant::now() + timeout;
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
    *status_slot = Some(status);
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

/// Drains the child's stderr on its own thread, keeping only the tail.
///
/// The pipe must be read while the child is running. A child that fills it and
/// is never drained blocks on writing its own diagnostics, which would make
/// this log the cause of the timeout it exists to explain. Keeping the tail
/// rather than the head is deliberate: a Python traceback puts the cause last.
fn spawn_stderr_tail(mut stderr: std::process::ChildStderr) -> mpsc::Receiver<Vec<u8>> {
    let (sender, receiver) = mpsc::channel();
    std::thread::spawn(move || {
        let mut tail: Vec<u8> = Vec::new();
        let mut chunk = [0_u8; 4096];
        loop {
            match stderr.read(&mut chunk) {
                Ok(0) | Err(_) => break,
                Ok(read) => {
                    tail.extend_from_slice(&chunk[..read]);
                    if tail.len() > MAX_STDERR_TAIL_BYTES {
                        tail.drain(..tail.len() - MAX_STDERR_TAIL_BYTES);
                    }
                }
            }
        }
        let _ = sender.send(tail);
    });
    receiver
}

/// The one place a failed discovery becomes something a person can read.
fn record_endpoint_attempt(log: &BootstrapLog, attempt: &EndpointAttempt) {
    let mut fields = vec![
        ("outcome", endpoint_outcome(&attempt.result).to_owned()),
        ("ms", attempt.elapsed.as_millis().to_string()),
    ];
    if let Some(status) = attempt.status {
        fields.push((
            "exit",
            match status.code() {
                Some(code) => code.to_string(),
                // A signalled child has no code, and on Unix the signal is the
                // whole story.
                None => status.to_string(),
            },
        ));
    }
    if let Err(LaunchError::EndpointSpawn(error)) = &attempt.result {
        fields.push(("error", error.to_string()));
    }
    if let Some(stderr) = attempt.stderr.as_ref().filter(|bytes| !bytes.is_empty()) {
        fields.push(("stderr", String::from_utf8_lossy(stderr).into_owned()));
    }
    log.record("endpoint.query", &fields);
}

fn endpoint_outcome(result: &Result<LoopbackOrigin, LaunchError>) -> &'static str {
    match result {
        Ok(_) => "ok",
        Err(LaunchError::EndpointSpawn(_)) => "spawn_failed",
        Err(LaunchError::EndpointTimeout) => "timeout",
        Err(LaunchError::EndpointExit) => "exit_failed",
        Err(LaunchError::EndpointOutput) => "unreadable_output",
        Err(LaunchError::InvalidOrigin) => "invalid_origin",
        Err(_) => "failed",
    }
}

/// Records what `prepare()` did, which is the fact that separates a slow first
/// launch from an install that silently re-extracts on every launch.
fn record_runtime_prepare(
    log: &BootstrapLog,
    prepared: &Result<InstalledPrivateRuntime, PrivateRuntimeError>,
    elapsed: Duration,
) {
    let (outcome, detail) = match prepared {
        Ok(runtime) => (
            if runtime.reused { "reused" } else { "extracted" },
            ("root", runtime.root.display().to_string()),
        ),
        // The variant, with its inner I/O error: "could not be installed" alone
        // does not distinguish a full disk from a permission denial.
        Err(error) => ("failed", ("error", format!("{error:?}"))),
    };
    log.record(
        "runtime.prepare",
        &[
            ("outcome", outcome.to_owned()),
            ("ms", elapsed.as_millis().to_string()),
            detail,
        ],
    );
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

fn spawn_detached(runtime: &RuntimeCommand, hand_over: bool) -> std::io::Result<std::process::Child> {
    let mut command = lifecycle_command(runtime, &START_ARGS);
    if hand_over {
        command.arg(HAND_OVER_ARG);
    }
    command.spawn()
}

/// `vibe stop`, scoped to one Runtime identity. There is no unscoped form: a
/// refusal is final, never a cue to stop whatever holds the service.
fn stop_arguments(runtime_id: &str) -> [&str; 3] {
    ["stop", "--expect-runtime-id", runtime_id]
}

/// Runs one lifecycle verb to completion and reads its verdict.
///
/// The verb owns its graceful and forced-stop budgets. An outer deadline would
/// kill this coordinator while the Runtime is still shutting down, and the
/// caller would then act against services that are still alive.
fn run_lifecycle_verb(runtime: &RuntimeCommand, args: &[&str], log: &BootstrapLog, event: &str) -> CliOutcome {
    let started = Instant::now();
    let mut command = lifecycle_command(runtime, args);
    // The verdict is on stderr. It stays in this process and the log.
    command.stderr(Stdio::piped());
    let mut child = match command.spawn() {
        Ok(child) => child,
        Err(error) => {
            log.record(
                event,
                &[("outcome", "unrunnable".to_owned()), ("error", error.to_string())],
            );
            return CliOutcome::Unrunnable;
        }
    };
    let stderr = child.stderr.take().map(spawn_stderr_tail);
    let status = child.wait();
    let stderr = stderr
        .and_then(|reader| reader.recv_timeout(STDERR_DRAIN).ok())
        .unwrap_or_default();
    let code = status.as_ref().ok().and_then(|status| status.code());
    let outcome = cli_outcome(code, &stderr);
    let mut fields = vec![
        ("outcome", format!("{outcome:?}")),
        ("ms", started.elapsed().as_millis().to_string()),
    ];
    match &status {
        Ok(status) => fields.push(("exit", status.to_string())),
        Err(error) => fields.push(("error", error.to_string())),
    }
    if !stderr.is_empty() {
        fields.push(("stderr", String::from_utf8_lossy(&stderr).into_owned()));
    }
    log.record(event, &fields);
    outcome
}

/// The frozen exit contract: 0 completed, 3 refused, 2 failed. Anything else,
/// including a signal, is a failure whose part is unknown.
fn cli_outcome(code: Option<i32>, stderr: &[u8]) -> CliOutcome {
    match code {
        Some(0) => CliOutcome::Completed,
        Some(3) => CliOutcome::Refused {
            reason: last_json_string(stderr, "reason"),
        },
        Some(2) => CliOutcome::Failed {
            part: last_json_string(stderr, "failed"),
        },
        _ => CliOutcome::Failed {
            part: "unknown".to_owned(),
        },
    }
}

/// A string field of the last JSON line on stderr. The verb writes a localized
/// line for people first, then this one for the shell.
fn last_json_string(stderr: &[u8], field: &str) -> String {
    String::from_utf8_lossy(stderr)
        .lines()
        .rev()
        .find_map(|line| serde_json::from_str::<serde_json::Value>(line.trim()).ok())
        .and_then(|verdict| verdict.get(field)?.as_str().map(str::to_owned))
        .unwrap_or_else(|| "unknown".to_owned())
}

fn lifecycle_command(runtime: &RuntimeCommand, args: &[&str]) -> Command {
    let mut command = Command::new(&runtime.executable);
    runtime.apply(&mut command);
    command
        .args(args)
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

    command
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::ffi::OsStr;

    /// The lifecycle verbs read the frozen CLI contract from a real process:
    /// the exit code decides, and the last JSON line on stderr names the reason
    /// or the failed part. Every verb runs the resolved executable as a desktop
    /// shell child, and a stop always names one Runtime identity.
    #[cfg(unix)]
    #[test]
    fn lifecycle_verbs_read_the_frozen_exit_contract_from_the_runtime_cli() {
        let runtime_id = "a".repeat(64);
        let failed = |part: &str| CliOutcome::Failed { part: part.to_owned() };
        for (body, expected) in [
            ("exit 0", CliOutcome::Completed),
            (
                r#"printf '%s\n' '{"reason":"service_runtime_id_mismatch"}' >&2; exit 3"#,
                CliOutcome::Refused {
                    reason: "service_runtime_id_mismatch".to_owned(),
                },
            ),
            (
                r#"printf '%s\n' '无法停止远程访问' '{"failed":"remote_access","remaining":[]}' >&2; exit 2"#,
                failed("remote_access"),
            ),
            ("echo 'no verdict' >&2; exit 2", failed("unknown")),
            ("exit 1", failed("unknown")),
            ("kill -9 $$", failed("unknown")),
        ] {
            let dir = scratch_dir("lifecycle-verb");
            let recording = dir.join("argv");
            let executable = write_fake_runtime(
                &dir,
                &format!(
                    "#!/bin/sh\nprintf '%s\\n' \"$@\" \"shell=$AVIBE_DESKTOP_SHELL\" >> \"{}\"\n{body}\n",
                    recording.display()
                ),
            );
            let resolved = InstalledVibeLauncher {
                candidates: vec![executable],
            }
            .resolve()
            .expect("fake resolve");
            assert_eq!(resolved.stop(&runtime_id), expected, "{body}");
            assert_eq!(resolved.remove_backends(), expected, "{body}");
            assert_eq!(
                std::fs::read_to_string(&recording)
                    .expect("recorded argv")
                    .lines()
                    .collect::<Vec<_>>(),
                [
                    "stop",
                    "--expect-runtime-id",
                    &runtime_id,
                    "shell=1",
                    "desktop",
                    "remove-backends",
                    "shell=1",
                ],
                "{body}"
            );
            std::fs::remove_dir_all(dir).expect("remove test-owned state");
        }

        let dir = scratch_dir("lifecycle-unrunnable");
        let executable = write_fake_runtime(&dir, "#!/bin/sh\nexit 0\n");
        let resolved = InstalledVibeLauncher {
            candidates: vec![executable.clone()],
        }
        .resolve()
        .expect("fake resolve");
        std::fs::remove_file(executable).expect("the Runtime disappears after resolution");
        assert_eq!(resolved.stop(&runtime_id), CliOutcome::Unrunnable);
        assert_eq!(resolved.remove_backends(), CliOutcome::Unrunnable);
        std::fs::remove_dir_all(dir).expect("remove test-owned state");
    }

    #[test]
    fn stop_arguments_preserve_the_frozen_interpreter_and_private_environment() {
        let runtime = RuntimeCommand {
            executable: PathBuf::from("/test-owned/Runtime Root/python"),
            prefix_args: vec![OsString::from("-m"), OsString::from("vibe")],
            environment: vec![(OsString::from("AVIBE_DESKTOP_MANAGED_RUNTIME"), OsString::from("1"))],
            withholds_inherited_python: true,
        };
        let runtime_id = "b".repeat(64);
        let command = lifecycle_command(&runtime, &stop_arguments(&runtime_id));
        assert_eq!(command.get_program(), runtime.executable.as_os_str());
        assert_eq!(
            command.get_args().collect::<Vec<_>>(),
            ["-m", "vibe", "stop", "--expect-runtime-id", runtime_id.as_str()]
        );
        let environment: Vec<_> = command.get_envs().collect();
        assert!(environment.contains(&(OsStr::new(DESKTOP_SHELL_ENV), Some(OsStr::new("1")))));
        assert!(environment.contains(&(OsStr::new("AVIBE_DESKTOP_MANAGED_RUNTIME"), Some(OsStr::new("1")))));
    }

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
            LaunchError::RuntimeInstall.notice_code(),
            BootstrapNoticeCode::RuntimeInstallFailed
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

    #[cfg(windows)]
    #[test]
    fn the_endpoint_helper_uses_the_no_console_creation_flag() {
        assert_eq!(CREATE_NO_WINDOW, 0x0800_0000);
    }

    /// A private scratch directory. Nothing here may touch a real Avibe install,
    /// so the "executable" launched below is a script this test wrote itself.
    fn scratch_dir(label: &str) -> PathBuf {
        use std::sync::atomic::{AtomicUsize, Ordering};
        static COUNTER: AtomicUsize = AtomicUsize::new(0);

        let unique = COUNTER.fetch_add(1, Ordering::SeqCst);
        let dir = env::temp_dir().join(format!("avibe-desktop-{label}-{}-{unique}", std::process::id()));
        std::fs::create_dir_all(&dir).expect("scratch directory is created");
        dir
    }

    /// A verified removal leaves the backend root alone: `remove-backends`
    /// already deleted it under every install claim, and an install that began
    /// afterwards owns what is there now. Only "Delete anyway" deletes it here,
    /// and neither path touches user state.
    #[test]
    fn only_an_unverified_removal_deletes_the_backend_root_itself() {
        let root = scratch_dir("remove-private-files");
        let install_root = root.join("application-data").join("runtime");
        let backend_root = root.join("application-data").join("backends");
        let user_state = root.join("user-state");
        std::fs::create_dir_all(&install_root).expect("broken private Runtime root");
        std::fs::create_dir_all(&backend_root).expect("private backend root");
        std::fs::create_dir_all(&user_state).expect("user state root");
        std::fs::write(install_root.join("corrupt"), b"not a valid Runtime").expect("broken private Runtime file");
        std::fs::write(backend_root.join("codex"), b"private backend").expect("private backend file");
        std::fs::write(user_state.join("config.json"), b"user state").expect("user state file");
        let launcher = BundledVibeLauncher::new(
            root.join("missing-bundle"),
            install_root.clone(),
            backend_root.clone(),
            BootstrapLog::disabled(),
        );
        assert!(launcher.owns_private_files());

        launcher
            .remove_private_files()
            .expect("the private Runtime tree is removed");
        assert!(!install_root.exists());
        assert!(backend_root.join("codex").is_file());

        std::fs::create_dir_all(&install_root).expect("broken private Runtime root");
        launcher
            .remove_unverified_private_files()
            .expect("an inactive broken Runtime is removable without preparing the bundle");
        assert!(!install_root.exists());
        assert!(!backend_root.exists());
        assert_eq!(
            std::fs::read(user_state.join("config.json")).expect("preserved user state"),
            b"user state"
        );

        std::fs::remove_dir_all(root).ok();
    }

    #[test]
    fn a_non_directory_backend_root_blocks_all_private_removal() {
        let root = scratch_dir("remove-backend-file");
        let install_root = root.join("application-data").join("runtime");
        let backend_root = root.join("application-data").join("backends");
        std::fs::create_dir_all(&install_root).expect("private Runtime root");
        std::fs::write(install_root.join("runtime"), b"private Runtime").expect("private Runtime file");
        std::fs::write(&backend_root, b"not a directory").expect("unsafe backend root");
        let launcher = BundledVibeLauncher::new(
            root.join("missing-bundle"),
            install_root.clone(),
            backend_root.clone(),
            BootstrapLog::disabled(),
        );

        assert!(matches!(
            launcher.remove_unverified_private_files(),
            Err(LaunchError::RuntimeRemoval)
        ));
        assert!(install_root.is_dir());
        assert!(backend_root.is_file());

        std::fs::remove_dir_all(root).ok();
    }

    #[cfg(unix)]
    #[test]
    fn a_symlink_backend_root_blocks_all_private_removal() {
        use std::os::unix::fs::symlink;

        let root = scratch_dir("remove-backend-symlink");
        let install_root = root.join("application-data").join("runtime");
        let backend_root = root.join("application-data").join("backends");
        let external = root.join("external-backends");
        std::fs::create_dir_all(&install_root).expect("private Runtime root");
        std::fs::write(install_root.join("runtime"), b"private Runtime").expect("private Runtime file");
        std::fs::create_dir_all(&external).expect("external backend root");
        std::fs::write(external.join("preserve"), b"external backend").expect("external backend file");
        symlink(&external, &backend_root).expect("unsafe backend symlink");
        let launcher = BundledVibeLauncher::new(
            root.join("missing-bundle"),
            install_root.clone(),
            backend_root,
            BootstrapLog::disabled(),
        );

        assert!(matches!(
            launcher.remove_unverified_private_files(),
            Err(LaunchError::RuntimeRemoval)
        ));
        assert!(install_root.is_dir());
        assert_eq!(
            std::fs::read(external.join("preserve")).expect("external backend preserved"),
            b"external backend"
        );

        std::fs::remove_dir_all(root).ok();
    }

    /// Writes a runnable stand-in for `vibe` and returns its path.
    ///
    /// A child process writes it, so this test process never holds a writable
    /// descriptor on a file it is about to exec. Tests run on parallel threads,
    /// and a child that another thread forks between our open and close
    /// inherits that descriptor until its own exec; exec'ing the file then
    /// fails with ETXTBSY. Writing to a temporary name and renaming does not
    /// help, because the renamed file is the same inode.
    #[cfg(unix)]
    fn write_fake_runtime(dir: &Path, body: &str) -> PathBuf {
        let executable = dir.join("vibe");
        let status = Command::new("/bin/sh")
            .args(["-c", "printf '%s' \"$2\" > \"$1\" && chmod 755 \"$1\"", "sh"])
            .arg(&executable)
            .arg(body)
            .status()
            .expect("the fake runtime writer runs");
        assert!(status.success(), "the fake runtime is written");
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
            matches!(resolved.launch(false), Err(LaunchError::Spawn(_))),
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

    /// A launcher of this shell's own Runtime, whose CLI is of the shell's version.
    #[cfg(unix)]
    fn identified(executable: PathBuf) -> ResolvedVibeExecutable {
        ResolvedVibeExecutable {
            command: RuntimeCommand::installed(executable),
            expected_runtime_id: Some("a".repeat(64)),
            cleanup: None,
            log: BootstrapLog::disabled(),
        }
    }

    /// Asserts the launch contract against what the operating system actually
    /// receives, rather than against the constant the code was built from.
    #[cfg(unix)]
    #[test]
    fn the_runtime_is_started_headless_and_marked_as_shell_started() {
        // Only this shell's own Runtime is asked to hand over: an installed
        // CLI may predate the flag, and would refuse the start.
        for (identified_launcher, hand_over, expected) in [
            (false, true, &["start", "--no-open-browser", "shell=1"][..]),
            (true, false, &["start", "--no-open-browser", "shell=1"][..]),
            (
                true,
                true,
                &["start", "--no-open-browser", "--hand-over", "shell=1"][..],
            ),
        ] {
            let dir = scratch_dir("launch");
            let recording = dir.join("argv");
            let executable = write_fake_runtime(
                &dir,
                &format!(
                    "#!/bin/sh\nprintf '%s\\n' \"$@\" \"shell=$AVIBE_DESKTOP_SHELL\" > \"{}\"\n",
                    recording.display()
                ),
            );

            let resolved: Arc<dyn ResolvedRuntimeLauncher> = if identified_launcher {
                Arc::new(identified(executable))
            } else {
                InstalledVibeLauncher {
                    candidates: vec![executable],
                }
                .resolve()
                .expect("the fake runtime resolves")
            };
            let launched = resolved.launch(hand_over).expect("the fake runtime starts");
            assert!(launched.pid > 0);

            let recorded = wait_for_file(&recording);
            // Without --no-open-browser the Runtime would open a second window
            // onto the Workbench, in the system browser.
            assert_eq!(recorded.lines().collect::<Vec<_>>(), expected);

            std::fs::remove_dir_all(&dir).ok();
        }
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

    /// Longer than the 10s budget that cut off cold first launches, short enough
    /// that the desktop-shell job barely notices. The number only has to sit
    /// between the old budget and the new one; the assertion is that a slow
    /// endpoint is waited for, not that it answers at any particular moment.
    #[cfg(unix)]
    const SLOW_ENDPOINT_SECONDS: u64 = 12;

    /// A first launch extracts the runtime tree and then immediately asks it for
    /// its address, so the interpreter pays for a whole cold tree on that one
    /// call -- measured at 15.8s to 20.0s, against 2.3s once warm. The old budget
    /// killed the child at 10s and the user met `runtime_discovery_failed` on
    /// every fresh install, while a second launch worked and hid it from us.
    ///
    /// This drives the real `query_endpoint` deadline with a command that genuinely
    /// outlives the old budget, so it fails on the constant rather than on a value
    /// read back from it.
    #[cfg(unix)]
    #[test]
    fn a_slow_cold_endpoint_is_waited_for_rather_than_killed() {
        let dir = scratch_dir("endpoint-cold-start");
        let executable = write_fake_runtime(
            &dir,
            &format!(
                "#!/bin/sh\nsleep {}\nprintf '%s\\n' '{{\"schema_version\":1,\"origin\":\"http://127.0.0.1:6123\"}}'\n",
                SLOW_ENDPOINT_SECONDS
            ),
        );
        let launcher = InstalledVibeLauncher {
            candidates: vec![executable],
        };

        let endpoint = launcher
            .resolve()
            .expect("the fake runtime resolves")
            .endpoint()
            .expect("an endpoint slower than the old budget still answers");

        assert_eq!(endpoint.as_str(), "http://127.0.0.1:6123");
        std::fs::remove_dir_all(&dir).ok();
    }

    /// Every `.pyc` file and `__pycache__` directory under a tree.
    #[cfg(unix)]
    fn tree_bytecode(root: &Path) -> Vec<PathBuf> {
        let mut found = Vec::new();
        let mut pending = vec![root.to_owned()];
        while let Some(directory) = pending.pop() {
            let Ok(entries) = std::fs::read_dir(&directory) else {
                continue;
            };
            for entry in entries.flatten() {
                let path = entry.path();
                if path.is_dir() {
                    if path.file_name() == Some(OsStr::new("__pycache__")) {
                        found.push(path);
                    } else {
                        pending.push(path);
                    }
                } else if path.extension() == Some(OsStr::new("pyc")) {
                    found.push(path);
                }
            }
        }
        found
    }

    /// The bug that invalidated an install on every launch, and the one test
    /// that could have caught it.
    ///
    /// `RuntimeCommand::private` asks for no bytecode twice: once by flag and
    /// once by environment variable. Only the flag works — `-I` implies `-E`,
    /// which makes the interpreter ignore every PYTHON* variable, including the
    /// one set right beside it. Nothing about that is visible in the code; it is
    /// only visible in what a real interpreter leaves on disk. So this runs the
    /// real interpreter with the real prefix arguments and the real environment,
    /// substituting only the module to import, and counts the files written.
    #[cfg(unix)]
    #[test]
    fn the_private_interpreter_writes_no_bytecode_into_its_own_install_tree() {
        let python = env::split_paths(&env::var_os("PATH").unwrap_or_default())
            .map(|entry| entry.join("python3"))
            .find(|candidate| is_executable_file(candidate))
            .expect("a python3 interpreter to run the private command against");
        let dir = scratch_dir("no-bytecode");
        let tree = dir.join("runtime");
        let package = tree.join("avibe_bytecode_probe");
        std::fs::create_dir_all(&package).expect("probe package directory");
        std::fs::write(package.join("__init__.py"), "VALUE = 1\n").expect("probe package");
        std::fs::write(package.join("module.py"), "from . import VALUE\n").expect("probe module");

        let command = RuntimeCommand::private(
            tree.clone(),
            python,
            tree.join("tools/bin/node"),
            tree.join("tools/npm/bin/npm-cli.js"),
            dir.join("backends"),
            &"a".repeat(64),
            env::var_os("PATH").as_deref(),
        );
        let mut process = Command::new(&command.executable);
        for (name, value) in &command.environment {
            process.env(name, value);
        }
        // Everything up to `-m vibe`, which is the part that cannot resolve here.
        for argument in command.prefix_args.iter().take_while(|argument| *argument != "-m") {
            process.arg(argument);
        }
        // `-I` also drops PYTHONPATH, so the probe package is put on the path
        // the only way an isolated interpreter still accepts.
        let status = process
            .arg("-c")
            .arg(format!(
                "import sys; sys.path.insert(0, {:?}); import avibe_bytecode_probe.module",
                tree.display().to_string()
            ))
            .status()
            .expect("the private interpreter runs");
        assert!(status.success(), "the probe import itself must succeed: {status}");

        let bytecode = tree_bytecode(&tree);
        assert!(
            bytecode.is_empty(),
            "the Runtime wrote bytecode into the tree `verify_installed_tree` checks: {bytecode:?}"
        );

        std::fs::remove_dir_all(&dir).ok();
    }

    /// `-I` shields the one interpreter it is passed to, not the ones that
    /// interpreter starts. The Controller and the UI copy `os.environ` and run
    /// without it, so a `PYTHONHOME` the shell happened to be launched with
    /// aborted both before readiness.
    ///
    /// The shell's own environment is modelled rather than mutated: the
    /// variables are set on the child the way inheritance would deliver them,
    /// and passed as the inherited set, so no other test thread sees a change to
    /// this process's environment while it spawns.
    #[cfg(unix)]
    #[test]
    fn the_private_runtime_does_not_hand_the_shells_python_variables_to_its_descendants() {
        let python = env::split_paths(&env::var_os("PATH").unwrap_or_default())
            .map(|entry| entry.join("python3"))
            .find(|candidate| is_executable_file(candidate))
            .expect("a python3 interpreter to run the private command against");
        let dir = scratch_dir("python-environment");
        let tree = dir.join("runtime");
        std::fs::create_dir_all(&tree).expect("runtime directory");
        let command = RuntimeCommand::private(
            tree.clone(),
            python,
            tree.join("tools/bin/node"),
            tree.join("tools/npm/bin/npm-cli.js"),
            dir.join("backends"),
            &"a".repeat(64),
            env::var_os("PATH").as_deref(),
        );
        let inherited = [
            (
                OsString::from("PYTHONHOME"),
                dir.join("no-such-python-home").into_os_string(),
            ),
            (
                OsString::from("PYTHONPATH"),
                dir.join("outside-the-verified-tree").into_os_string(),
            ),
            (OsString::from("AVIBE_UNRELATED_INHERITED"), OsString::from("kept")),
        ];

        let mut process = Command::new(&command.executable);
        for (name, value) in &inherited {
            process.env(name, value);
        }
        command.apply_environment(&mut process, env::vars_os().chain(inherited.iter().cloned()));
        for argument in command.prefix_args.iter().take_while(|argument| *argument != "-m") {
            process.arg(argument);
        }
        // What the Runtime would see, and whether a descendant started the way
        // `cmd_start` starts the Controller -- no `-I`, `os.environ` copied --
        // gets as far as running a statement.
        let output = process
            .arg("-c")
            .arg(concat!(
                "import json, os, subprocess, sys; ",
                "child = subprocess.run([sys.executable, '-c', 'pass'], env=dict(os.environ)); ",
                "print(json.dumps({'python': {k: v for k, v in os.environ.items() if k.startswith('PYTHON')}, ",
                "'unrelated': os.environ.get('AVIBE_UNRELATED_INHERITED'), 'descendant': child.returncode}))",
            ))
            .stderr(Stdio::null())
            .output()
            .expect("the private interpreter runs");
        assert!(
            output.status.success(),
            "the private interpreter itself must run: {output:?}"
        );
        let report: serde_json::Value =
            serde_json::from_slice(&output.stdout).expect("the child reports its environment");

        assert_eq!(
            report["python"],
            serde_json::json!({"PYTHONDONTWRITEBYTECODE": "1"}),
            "only the Runtime's own Python variable may reach it"
        );
        assert_eq!(
            report["unrelated"], "kept",
            "only Python startup variables are withheld"
        );
        assert_eq!(
            report["descendant"], 0,
            "a descendant interpreter without -I must start"
        );

        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn python_startup_variables_are_recognised_by_prefix() {
        assert!(is_python_startup_variable(OsStr::new("PYTHONHOME")));
        assert!(is_python_startup_variable(OsStr::new("PYTHONSTARTUP")));
        assert!(!is_python_startup_variable(OsStr::new("PATH")));
        assert!(!is_python_startup_variable(OsStr::new("VIRTUAL_ENV")));
        assert_eq!(is_python_startup_variable(OsStr::new("PythonPath")), cfg!(windows));
    }

    /// The stderr this used to send to the null device is where the cause lives.
    #[cfg(unix)]
    #[test]
    fn a_failed_endpoint_query_records_its_stderr_and_exit_status() {
        let dir = scratch_dir("endpoint-stderr");
        let executable = write_fake_runtime(
            &dir,
            "#!/bin/sh\nprintf '%s\\n' 'Traceback (most recent call last):' 'ModuleNotFoundError: vibe' >&2\nexit 3\n",
        );
        let log = BootstrapLog::at(dir.join("bootstrap.log"));

        let failure = query_endpoint(&RuntimeCommand::installed(executable), &log);

        assert!(matches!(failure, Err(LaunchError::EndpointExit)));
        let written = std::fs::read_to_string(dir.join("bootstrap.log")).expect("the attempt is recorded");
        assert_eq!(written.lines().count(), 1, "one attempt is one record: {written}");
        assert!(written.contains("endpoint.query"), "{written}");
        assert!(written.contains("outcome=\"exit_failed\""), "{written}");
        assert!(written.contains("exit=\"3\""), "{written}");
        assert!(
            written.contains("ModuleNotFoundError: vibe"),
            "the discarded stderr is the whole point: {written}"
        );

        std::fs::remove_dir_all(&dir).ok();
    }

    /// A timeout is the failure we could least explain, so it must not be the
    /// one that records nothing: the child is killed, and what it said before
    /// that is still collected.
    #[cfg(unix)]
    #[test]
    fn an_endpoint_that_never_answers_records_the_timeout_rather_than_nothing() {
        let dir = scratch_dir("endpoint-timeout");
        // The fixture runs twice. The first run takes the `exit 0` branch and
        // only leaves the flag behind; the second finds it and blocks forever,
        // which is the child this test is about.
        //
        // The warm-up is not decoration. Measured under a full `cargo test`, the
        // FIRST execution of a just-written executable takes ~4.8s to reach its
        // own first line -- macOS evaluates a new binary before it runs -- while
        // every later one takes ~10-25ms. Inside a sub-second deadline that cost
        // lands on the wrong side of the kill, and the test then proves nothing
        // while looking like a timing flake. Paying it through a run that exits
        // on its own moves it outside the window by construction rather than by
        // choosing a budget large enough to hide it.
        //
        // `exec` so the sleep replaces the shell: one process to kill, and the
        // stderr pipe closes with it rather than being held open by a survivor.
        // The message goes through the external `echo` because `exec` replaces
        // the image without flushing whatever the shell still holds in its own
        // stdio buffers.
        let warmed = dir.join("warmed");
        let executable = write_fake_runtime(
            &dir,
            &format!(
                "#!/bin/sh\n/bin/echo 'still importing' >&2\nif [ -e {flag} ]; then exec sleep 30; fi\n: > {flag}\nexit 0\n",
                flag = warmed.display()
            ),
        );
        let warm_up = Command::new(&executable)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status()
            .expect("the fixture runs");
        assert!(
            warm_up.success() && warmed.exists(),
            "the warm-up run must complete: {warm_up}"
        );
        let log = BootstrapLog::at(dir.join("bootstrap.log"));

        let failure = query_endpoint_within(
            &RuntimeCommand::installed(executable),
            &log,
            Duration::from_millis(1_500),
        );

        assert!(matches!(failure, Err(LaunchError::EndpointTimeout)));
        let written = std::fs::read_to_string(dir.join("bootstrap.log")).expect("the attempt is recorded");
        assert!(written.contains("outcome=\"timeout\""), "{written}");
        assert!(
            !written.contains(" exit="),
            "a child that was killed has no exit status to report: {written}"
        );
        assert!(
            written.contains("still importing"),
            "output from before the kill is still worth having: {written}"
        );

        std::fs::remove_dir_all(&dir).ok();
    }

    /// Reused against extracted is the line that settles whether an install is
    /// quietly reinstalling itself on every launch.
    #[test]
    fn a_prepare_records_whether_it_reused_an_install_or_extracted_one() {
        let dir = scratch_dir("prepare-record");
        let log = BootstrapLog::at(dir.join("bootstrap.log"));
        let installed = |reused: bool| InstalledPrivateRuntime {
            root: PathBuf::from("/installs/3.1.1/20aa5822aa3595e1"),
            python: PathBuf::from("/installs/3.1.1/20aa5822aa3595e1/python/bin/python3"),
            node: PathBuf::from("/installs/3.1.1/20aa5822aa3595e1/tools/bin/node"),
            npm_cli: PathBuf::from("/installs/3.1.1/20aa5822aa3595e1/tools/npm/bin/npm-cli.js"),
            runtime_id: "a".repeat(64),
            reused,
        };

        record_runtime_prepare(&log, &Ok(installed(true)), Duration::from_millis(5_123));
        record_runtime_prepare(&log, &Ok(installed(false)), Duration::from_millis(41_000));
        record_runtime_prepare(
            &log,
            &Err(PrivateRuntimeError::ArchiveVerification),
            Duration::from_millis(12),
        );

        let written = std::fs::read_to_string(dir.join("bootstrap.log")).expect("the attempts are recorded");
        let records: Vec<&str> = written.lines().collect();
        assert_eq!(records.len(), 3, "{written}");
        assert!(
            records[0].contains("outcome=\"reused\"")
                && records[0].contains("ms=\"5123\"")
                // The install directory is named for the payload digest, which is
                // what makes a user-sent log identify its own build.
                && records[0].contains("20aa5822aa3595e1"),
            "{}",
            records[0]
        );
        assert!(records[1].contains("outcome=\"extracted\""), "{}", records[1]);
        assert!(
            records[2].contains("outcome=\"failed\"") && records[2].contains("ArchiveVerification"),
            "{}",
            records[2]
        );

        std::fs::remove_dir_all(&dir).ok();
    }

    /// The bootstrap loop aborts a doomed wait on this verdict, so it has to be
    /// right in every direction: a launcher that refused or failed must be
    /// visible, the ordinary `vibe start` — which exits 0 once the Runtime is
    /// up — must never be mistaken for one, and a handover verdict is read only
    /// from a CLI that speaks the handover contract.
    #[cfg(unix)]
    #[test]
    fn a_launcher_exit_is_read_by_the_start_contract_it_was_launched_under() {
        for (identified_launcher, hand_over, code, expected) in [
            (true, true, 0, LaunchExit::Started),
            (true, true, 3, LaunchExit::HandoverRefused),
            (true, false, 3, LaunchExit::HandoverRefused),
            (true, true, 2, LaunchExit::HandoverFailed),
            // Without a handover, a 2 is an ordinary failure, such as a usage error.
            (true, false, 2, LaunchExit::Failed),
            (true, true, 1, LaunchExit::Failed),
            // An installed CLI never hands over, so its 3 and 2 are failures.
            (false, true, 3, LaunchExit::Failed),
            (false, true, 2, LaunchExit::Failed),
            (false, false, 0, LaunchExit::Started),
        ] {
            let dir = scratch_dir("launch-exit");
            let executable = write_fake_runtime(&dir, &format!("#!/bin/sh\nexit {code}\n"));
            let resolved: Arc<dyn ResolvedRuntimeLauncher> = if identified_launcher {
                Arc::new(identified(executable))
            } else {
                InstalledVibeLauncher {
                    candidates: vec![executable],
                }
                .resolve()
                .expect("the fake runtime resolves")
            };
            let launched = resolved.launch(hand_over).expect("the fake runtime starts");
            // The wait runs on a detached thread, so the verdict arrives late.
            for _ in 0..200 {
                if !launched.watch.pending() {
                    break;
                }
                std::thread::sleep(std::time::Duration::from_millis(25));
            }
            assert_eq!(
                launched.watch.exit(),
                Some(expected),
                "identified={identified_launcher} hand_over={hand_over} exit={code}"
            );
            std::fs::remove_dir_all(&dir).ok();
        }
    }

    #[test]
    fn private_runtime_commands_ignore_user_python_and_prepend_managed_tools() {
        let runtime_root = PathBuf::from("/private/runtime");
        let python = PathBuf::from("/private/runtime/python/bin/python3");
        let node = PathBuf::from("/private/runtime/tools/bin/node");
        let npm_cli = PathBuf::from("/private/runtime/tools/npm/bin/npm-cli.js");
        let backends_root = PathBuf::from("/private/backends");
        let inherited = env::join_paths([PathBuf::from("/usr/bin")]).expect("PATH");
        let expected_node = node.clone().into_os_string();
        let expected_npm_cli = npm_cli.clone().into_os_string();
        let expected_backends_root = backends_root.clone().into_os_string();

        let command = RuntimeCommand::private(
            runtime_root.clone(),
            python.clone(),
            node.clone(),
            npm_cli,
            backends_root,
            &"a".repeat(64),
            Some(&inherited),
        );

        assert_eq!(command.executable, python);
        assert_eq!(
            command.prefix_args,
            [
                OsString::from("-I"),
                OsString::from("-B"),
                OsString::from("-m"),
                OsString::from("vibe")
            ]
        );
        let environment: std::collections::HashMap<_, _> = command.environment.into_iter().collect();
        let path_entries: Vec<_> =
            env::split_paths(environment.get(OsStr::new("PATH")).expect("private PATH")).collect();
        assert_eq!(
            path_entries,
            [
                runtime_root.join("bin"),
                node.parent().unwrap().to_owned(),
                PathBuf::from("/usr/bin")
            ]
        );
        assert_eq!(
            environment.get(OsStr::new("VIBE_SHOW_RUNTIME_NODE_BIN")),
            Some(&expected_node)
        );
        assert_eq!(
            environment.get(OsStr::new(DESKTOP_NPM_CLI_ENV)),
            Some(&expected_npm_cli)
        );
        assert_eq!(
            environment.get(OsStr::new(DESKTOP_BACKENDS_ROOT_ENV)),
            Some(&expected_backends_root)
        );
        assert_eq!(
            environment.get(OsStr::new(DESKTOP_MANAGED_RUNTIME_ENV)),
            Some(&OsString::from("1"))
        );
        assert_eq!(
            environment.get(OsStr::new(DESKTOP_RUNTIME_ROOT_ENV)),
            Some(&runtime_root.into_os_string())
        );
        assert_eq!(
            environment.get(OsStr::new("AVIBE_DESKTOP_RUNTIME_ID")),
            Some(&OsString::from("a".repeat(64)))
        );
        assert_eq!(
            environment.get(OsStr::new("PYTHONDONTWRITEBYTECODE")),
            Some(&OsString::from("1"))
        );
    }
}
