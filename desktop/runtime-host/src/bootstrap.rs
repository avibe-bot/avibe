//! The bootstrap state machine.
//!
//! One run answers a single question — "is there a Runtime at this origin, and
//! if not, can we get one?" — and reports every step through a [`StatusSink`].
//! The shell navigates only when a run ends in [`BootstrapPhase::Ready`].

use std::env;
use std::sync::{Arc, Mutex, MutexGuard};
use std::time::Duration;

use tokio::time::{sleep, Instant};

use crate::health::HealthProbe;
use crate::launcher::{LaunchError, LaunchedRuntime, ResolvedRuntimeLauncher, RuntimeLauncher};
use crate::origin::LoopbackOrigin;
use crate::status::{BootstrapNotice, BootstrapNoticeCode, BootstrapStatus};

/// Overrides the origin the shell probes and navigates to. Still validated as a
/// loopback origin — this is a development and regression convenience, not a way
/// to point the shell at a remote host.
pub const ORIGIN_ENV: &str = "AVIBE_DESKTOP_ORIGIN";

/// Overrides how long the shell waits for a freshly started Runtime.
pub const READY_TIMEOUT_ENV: &str = "AVIBE_DESKTOP_READY_TIMEOUT_SECONDS";

/// Gap between readiness probes.
pub const DEFAULT_POLL_INTERVAL: Duration = Duration::from_millis(500);

/// How long a starting Runtime has to answer `/ready`.
///
/// Matches `SERVICE_SLOW_START_TIMEOUT_SECONDS` in the Python service so the
/// shell does not give up before the Runtime itself would.
pub const DEFAULT_READY_TIMEOUT: Duration = Duration::from_secs(120);

/// Per-request timeout for a single readiness probe.
pub const DEFAULT_PROBE_TIMEOUT: Duration = Duration::from_secs(2);

const READY_TIMEOUT_CEILING_SECONDS: u64 = 600;

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
    /// Explicit development/test override. Production discovers the endpoint
    /// through the installed Python Runtime on every bootstrap run.
    pub origin_override: Option<String>,
    pub poll_interval: Duration,
    pub ready_timeout: Duration,
    pub probe_timeout: Duration,
}

impl Default for RuntimeHostSettings {
    fn default() -> Self {
        Self {
            origin_override: None,
            poll_interval: DEFAULT_POLL_INTERVAL,
            ready_timeout: DEFAULT_READY_TIMEOUT,
            probe_timeout: DEFAULT_PROBE_TIMEOUT,
        }
    }
}

impl RuntimeHostSettings {
    /// Applies the documented environment overrides to the defaults.
    pub fn from_env() -> Self {
        let defaults = Self::default();
        Self {
            origin_override: env::var(ORIGIN_ENV).ok().filter(|value| !value.trim().is_empty()),
            ready_timeout: env::var(READY_TIMEOUT_ENV)
                .ok()
                .and_then(|value| parse_timeout(&value))
                .unwrap_or(defaults.ready_timeout),
            ..defaults
        }
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
    launched_runtime: Mutex<Option<LaunchedRuntime>>,
}

impl RuntimeHost {
    pub fn new(probe: Arc<dyn HealthProbe>, launcher: Arc<dyn RuntimeLauncher>, settings: RuntimeHostSettings) -> Self {
        Self {
            probe,
            launcher,
            settings,
            launched_runtime: Mutex::new(None),
        }
    }

    pub fn settings(&self) -> &RuntimeHostSettings {
        &self.settings
    }

    /// Whether this host has already started a Runtime that it must not start again.
    pub fn has_launched(&self) -> bool {
        self.launched_runtime().is_some()
    }

    /// Probes the exact validated origin using the same readiness contract as bootstrap.
    pub async fn is_ready(&self, origin: &LoopbackOrigin) -> bool {
        self.probe.is_healthy(origin).await
    }

    /// Releases retained launch ownership after the shell has confirmed that
    /// the previously ready Runtime is no longer serving.
    ///
    /// This does not stop a process. It only allows the next bootstrap run to
    /// launch again if the Runtime cannot be adopted.
    pub fn reset_after_confirmed_runtime_loss(&self) {
        *self.launched_runtime() = None;
    }

    /// Runs the state machine once and returns its terminal status.
    pub async fn bootstrap(&self, sink: &dyn StatusSink) -> BootstrapStatus {
        let (origin, resolved_launcher) = match self.resolve_origin().await {
            Ok(resolved) => resolved,
            Err(error) => {
                return publish(
                    sink,
                    BootstrapStatus::rejected(error.notice_code(), error.is_retryable()),
                )
            }
        };

        let mut attempt = 1;
        publish(sink, BootstrapStatus::probing(&origin, attempt));

        // Adoption: an already-running Runtime is used as-is and never restarted.
        if self.probe.is_healthy(&origin).await {
            return publish(
                sink,
                BootstrapStatus::ready(&origin, attempt, BootstrapNoticeCode::Adopted),
            );
        }

        // A launcher may report its non-zero exit after an earlier bootstrap
        // timed out. Re-check the retained watch before deciding this run is
        // already waiting on a viable launch.
        if self.clear_failed_launch() {
            return publish(
                sink,
                BootstrapStatus::failed(
                    &origin,
                    attempt,
                    BootstrapNotice::new(BootstrapNoticeCode::LauncherExited),
                    true,
                ),
            );
        }

        // A development override points at another local Runtime and is treated
        // as externally managed. The shell must not start a differently
        // configured default Runtime and then wait on the override forever.
        if resolved_launcher.is_none() {
            return publish(
                sink,
                BootstrapStatus::failed(
                    &origin,
                    attempt,
                    BootstrapNotice::new(BootstrapNoticeCode::RuntimeNotFound),
                    true,
                ),
            );
        }

        // The lock makes the decision and launch atomic, so concurrent runs
        // cannot both start the Runtime.
        if let Err(error) = self.launch_if_needed(resolved_launcher) {
            return publish(
                sink,
                BootstrapStatus::failed(
                    &origin,
                    attempt,
                    BootstrapNotice::new(error.notice_code()),
                    error.is_retryable(),
                ),
            );
        }

        publish(sink, BootstrapStatus::starting(&origin, attempt));

        let deadline = Instant::now() + self.settings.ready_timeout;
        while Instant::now() < deadline {
            sleep(self.settings.poll_interval).await;
            attempt += 1;
            if self.probe.is_healthy(&origin).await {
                return publish(
                    sink,
                    BootstrapStatus::ready(&origin, attempt, BootstrapNoticeCode::Ready),
                );
            }
            // A launcher that exited non-zero started nothing, so the remaining
            // wait would be spent polling an address that will never answer.
            if self.clear_failed_launch() {
                return publish(
                    sink,
                    BootstrapStatus::failed(
                        &origin,
                        attempt,
                        BootstrapNotice::new(BootstrapNoticeCode::LauncherExited),
                        true,
                    ),
                );
            }
            publish(sink, BootstrapStatus::starting(&origin, attempt));
        }

        publish(
            sink,
            BootstrapStatus::failed(
                &origin,
                attempt,
                BootstrapNotice::timeout(self.settings.ready_timeout.as_secs()),
                true,
            ),
        )
    }

    async fn resolve_origin(&self) -> Result<(LoopbackOrigin, Option<Arc<dyn ResolvedRuntimeLauncher>>), LaunchError> {
        if let Some(origin) = self.settings.origin_override.as_deref() {
            return LoopbackOrigin::parse(origin)
                .map(|origin| (origin, None))
                .map_err(|_| LaunchError::InvalidOrigin);
        }
        let launcher = self.launcher.clone();
        tokio::task::spawn_blocking(move || {
            let resolved = launcher.resolve()?;
            let origin = resolved.endpoint()?;
            Ok((origin, Some(resolved)))
        })
        .await
        .map_err(|_| LaunchError::EndpointOutput)?
    }

    fn launched_runtime(&self) -> MutexGuard<'_, Option<LaunchedRuntime>> {
        self.launched_runtime
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }

    fn launch_if_needed(&self, resolved_launcher: Option<Arc<dyn ResolvedRuntimeLauncher>>) -> Result<(), LaunchError> {
        let mut launched = self.launched_runtime();
        if launched.is_none() {
            let resolved_launcher = match resolved_launcher {
                Some(resolved) => resolved,
                None => self.launcher.resolve()?,
            };
            *launched = Some(resolved_launcher.launch()?);
        }
        Ok(())
    }

    /// Clears a launch only after its retained watch proves that it failed.
    ///
    /// A successful short-lived launcher and an unobservable long-running
    /// launcher both remain owned by this host, preserving the at-most-one
    /// launch contract across retries.
    fn clear_failed_launch(&self) -> bool {
        let mut launched = self.launched_runtime();
        if launched.as_ref().is_some_and(|runtime| runtime.watch.failed()) {
            *launched = None;
            return true;
        }
        false
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
    fn production_discovers_the_origin_from_the_installed_runtime() {
        let settings = RuntimeHostSettings::default();
        assert!(settings.origin_override.is_none());
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
}
