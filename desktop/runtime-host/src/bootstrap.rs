//! The bootstrap state machine.
//!
//! One run answers a single question — "is there a Runtime at this origin, and
//! if not, can we get one?" — and reports every step through a [`StatusSink`].
//! The shell navigates only when a run ends in [`BootstrapPhase::Ready`].

use std::env;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;

use tokio::time::{sleep, Instant};

use crate::health::HealthProbe;
use crate::launcher::RuntimeLauncher;
use crate::origin::{LoopbackOrigin, DEFAULT_ORIGIN};
use crate::status::BootstrapStatus;

/// Overrides the origin the shell probes and navigates to. Still validated as a
/// loopback origin — this is a development and regression convenience, not a way
/// to point the shell at a remote host.
pub const ORIGIN_ENV: &str = "AVIBE_DESKTOP_ORIGIN";

/// Overrides how long the shell waits for a freshly started Runtime.
pub const READY_TIMEOUT_ENV: &str = "AVIBE_DESKTOP_READY_TIMEOUT_SECONDS";

/// Gap between readiness probes.
pub const DEFAULT_POLL_INTERVAL: Duration = Duration::from_millis(500);

/// How long a starting Runtime has to answer `/health`.
///
/// Matches `SERVICE_SLOW_START_TIMEOUT_SECONDS` in the Python service so the
/// shell does not give up before the Runtime itself would.
pub const DEFAULT_READY_TIMEOUT: Duration = Duration::from_secs(120);

/// Per-request timeout for a single readiness probe.
pub const DEFAULT_PROBE_TIMEOUT: Duration = Duration::from_secs(2);

const READY_TIMEOUT_CEILING_SECONDS: u64 = 600;

// User-facing bootstrap copy. Deliberately free of paths, command strings, and
// process output — see the contract note on `BootstrapStatus::message`.
const MESSAGE_PROBING: &str = "Looking for a running Avibe Runtime…";
const MESSAGE_ADOPTED: &str = "Connected to the Avibe Runtime already running on this machine.";
const MESSAGE_STARTING: &str = "Starting the Avibe Runtime…";
const MESSAGE_READY: &str = "The Avibe Runtime is ready.";
const MESSAGE_LAUNCHER_EXITED: &str =
    "The Avibe Runtime stopped instead of starting. An outdated Avibe is the usual cause — update it with: vibe upgrade";

fn timeout_message(timeout: Duration) -> String {
    format!(
        "The Avibe Runtime did not become ready within {} seconds.",
        timeout.as_secs()
    )
}

/// Where bootstrap progress goes. The Tauri layer implements this by storing the
/// latest status and emitting it to the bootstrap window; tests record it.
pub trait StatusSink: Send + Sync {
    fn publish(&self, status: BootstrapStatus);
}

/// Drops every intermediate status. For callers that only need the terminal one.
pub struct DiscardStatus;

impl StatusSink for DiscardStatus {
    fn publish(&self, _status: BootstrapStatus) {}
}

#[derive(Debug, Clone)]
pub struct RuntimeHostSettings {
    /// Raw origin, validated at the start of every run.
    pub origin: String,
    pub poll_interval: Duration,
    pub ready_timeout: Duration,
    pub probe_timeout: Duration,
}

impl Default for RuntimeHostSettings {
    fn default() -> Self {
        Self {
            origin: DEFAULT_ORIGIN.to_owned(),
            poll_interval: DEFAULT_POLL_INTERVAL,
            ready_timeout: DEFAULT_READY_TIMEOUT,
            probe_timeout: DEFAULT_PROBE_TIMEOUT,
        }
    }
}

impl RuntimeHostSettings {
    /// Applies the documented environment overrides to the defaults.
    pub fn from_env() -> Self {
        let mut settings = Self::default();
        if let Some(origin) = env::var(ORIGIN_ENV).ok().filter(|value| !value.trim().is_empty()) {
            settings.origin = origin;
        }
        if let Some(timeout) = env::var(READY_TIMEOUT_ENV).ok().and_then(|value| parse_timeout(&value)) {
            settings.ready_timeout = timeout;
        }
        settings
    }
}

/// Parses a ready-timeout override, ignoring values that would disable the bound.
fn parse_timeout(raw: &str) -> Option<Duration> {
    let seconds: u64 = raw.trim().parse().ok()?;
    (1..=READY_TIMEOUT_CEILING_SECONDS)
        .contains(&seconds)
        .then(|| Duration::from_secs(seconds))
}

/// Owns one Runtime origin for the lifetime of the shell process.
///
/// A host may run `bootstrap` many times — once at startup and once per user
/// retry — but it starts at most one Runtime, and it never stops one.
pub struct RuntimeHost {
    probe: Arc<dyn HealthProbe>,
    launcher: Arc<dyn RuntimeLauncher>,
    settings: RuntimeHostSettings,
    launch_started: AtomicBool,
}

impl RuntimeHost {
    pub fn new(probe: Arc<dyn HealthProbe>, launcher: Arc<dyn RuntimeLauncher>, settings: RuntimeHostSettings) -> Self {
        Self {
            probe,
            launcher,
            settings,
            launch_started: AtomicBool::new(false),
        }
    }

    pub fn settings(&self) -> &RuntimeHostSettings {
        &self.settings
    }

    /// Whether this host has already started a Runtime that it must not start again.
    pub fn has_launched(&self) -> bool {
        self.launch_started.load(Ordering::SeqCst)
    }

    /// Runs the state machine once and returns its terminal status.
    pub async fn bootstrap(&self, sink: &dyn StatusSink) -> BootstrapStatus {
        let origin = match LoopbackOrigin::parse(&self.settings.origin) {
            Ok(origin) => origin,
            // A misconfigured origin cannot be fixed by trying again.
            Err(error) => {
                return publish(
                    sink,
                    BootstrapStatus::failed(self.settings.origin.trim(), 0, error.to_string(), false),
                )
            }
        };

        let mut attempt = 1;
        publish(sink, BootstrapStatus::probing(&origin, attempt, MESSAGE_PROBING));

        // Adoption: an already-running Runtime is used as-is and never restarted.
        if self.probe.is_healthy(&origin).await {
            return publish(sink, BootstrapStatus::ready(&origin, attempt, MESSAGE_ADOPTED));
        }

        // `swap` makes the decision atomic, so concurrent runs cannot both launch.
        let mut launched = None;
        if !self.launch_started.swap(true, Ordering::SeqCst) {
            match self.launcher.launch() {
                Ok(runtime) => launched = Some(runtime),
                Err(error) => {
                    // Nothing was spawned, so a later attempt is still the first launch.
                    self.launch_started.store(false, Ordering::SeqCst);
                    return publish(
                        sink,
                        BootstrapStatus::failed(origin.as_str(), attempt, error.to_string(), error.is_retryable()),
                    );
                }
            }
        }

        publish(sink, BootstrapStatus::starting(&origin, attempt, MESSAGE_STARTING));

        let deadline = Instant::now() + self.settings.ready_timeout;
        while Instant::now() < deadline {
            sleep(self.settings.poll_interval).await;
            attempt += 1;
            if self.probe.is_healthy(&origin).await {
                return publish(sink, BootstrapStatus::ready(&origin, attempt, MESSAGE_READY));
            }
            // A launcher that exited non-zero started nothing, so the remaining
            // wait would be spent polling an address that will never answer.
            if launched.as_ref().is_some_and(|runtime| runtime.watch.failed()) {
                // Confirmed dead, so a retry is once again the first launch.
                self.launch_started.store(false, Ordering::SeqCst);
                return publish(
                    sink,
                    BootstrapStatus::failed(origin.as_str(), attempt, MESSAGE_LAUNCHER_EXITED, true),
                );
            }
            publish(sink, BootstrapStatus::starting(&origin, attempt, MESSAGE_STARTING));
        }

        publish(
            sink,
            BootstrapStatus::failed(
                origin.as_str(),
                attempt,
                timeout_message(self.settings.ready_timeout),
                true,
            ),
        )
    }
}

fn publish(sink: &dyn StatusSink, status: BootstrapStatus) -> BootstrapStatus {
    sink.publish(status.clone());
    status
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn defaults_match_the_avibe_web_ui_origin() {
        let settings = RuntimeHostSettings::default();
        assert_eq!(settings.origin, DEFAULT_ORIGIN);
        assert!(LoopbackOrigin::parse(&settings.origin).is_ok());
    }

    #[test]
    fn the_default_wait_is_not_shorter_than_the_service_slow_start_budget() {
        // `SERVICE_SLOW_START_TIMEOUT_SECONDS` in vibe/service_manager.py.
        assert!(RuntimeHostSettings::default().ready_timeout >= Duration::from_secs(120));
    }

    #[test]
    fn a_timeout_override_must_be_a_positive_bounded_number_of_seconds() {
        assert_eq!(parse_timeout("30"), Some(Duration::from_secs(30)));
        assert_eq!(parse_timeout("  30  "), Some(Duration::from_secs(30)));
        for rejected in ["", "0", "-1", "abc", "30s", "1e3", "601", "18446744073709551616"] {
            assert_eq!(parse_timeout(rejected), None, "override {rejected:?}");
        }
    }

    #[test]
    fn user_facing_messages_carry_no_machine_detail() {
        let messages = [
            MESSAGE_PROBING,
            MESSAGE_ADOPTED,
            MESSAGE_STARTING,
            MESSAGE_READY,
            MESSAGE_LAUNCHER_EXITED,
            &timeout_message(DEFAULT_READY_TIMEOUT),
        ];
        for message in messages {
            for path_separator in ['/', '\\'] {
                assert!(
                    !message.contains(path_separator),
                    "message must not carry a path: {message:?}"
                );
            }
            assert!(
                !message.contains("vibe start"),
                "message must not carry a command line: {message:?}"
            );
            for variable in [ORIGIN_ENV, READY_TIMEOUT_ENV, crate::launcher::VIBE_PATH_ENV] {
                assert!(
                    !message.contains(variable) && !message.contains('='),
                    "message must not carry an environment variable: {message:?}"
                );
            }
        }
    }
}
