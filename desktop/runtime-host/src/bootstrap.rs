//! The bootstrap state machine.
//!
//! One run answers a single question — "is there a Runtime at this origin, and
//! if not, can we get one?" — and reports every step through a [`StatusSink`].
//! The shell navigates only when a run ends in [`BootstrapPhase::Ready`].

use std::env;
use std::sync::{Arc, Mutex, MutexGuard};
use std::time::Duration;

use tokio::time::{sleep, Instant};

use crate::health::{HealthProbe, RuntimeReadiness};
use crate::launcher::{LaunchError, LaunchedRuntime, ResolvedRuntimeLauncher, RuntimeLauncher, RuntimeRemovalState};
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
/// retry. It never overlaps launch commands. Normal lifecycle never stops a
/// Runtime; explicit replacement and uninstall use the Runtime's own graceful
/// CLI. After a completed successful launch still fails the full readiness
/// budget, a later retry may re-run the idempotent start command to repair
/// missing components.
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

    /// Gracefully stops and removes an app-private Runtime owned by this host.
    ///
    /// Installed/user-managed launchers return `false` and are never modified.
    pub async fn remove_private_runtime(&self, active_origin: Option<&LoopbackOrigin>) -> Result<bool, LaunchError> {
        let launched_by_host = self.has_launched();
        let state = match active_origin {
            Some(origin) => match self.probe.readiness(origin).await {
                Some(readiness) if readiness.desktop_runtime_id.is_some() => RuntimeRemovalState::Managed,
                Some(_) => RuntimeRemovalState::External,
                None if launched_by_host => RuntimeRemovalState::Managed,
                None => RuntimeRemovalState::Unknown,
            },
            None if launched_by_host => RuntimeRemovalState::Managed,
            None => RuntimeRemovalState::Inactive,
        };
        let launcher = self.launcher.clone();
        let removed = tokio::task::spawn_blocking(move || launcher.remove_private_runtime(state))
            .await
            .map_err(|_| LaunchError::RuntimeRemoval)??;
        if removed {
            *self.launched_runtime() = None;
        }
        Ok(removed)
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
        let mut handover_performed = false;
        publish(sink, BootstrapStatus::probing(&origin, attempt));

        // External Runtimes and the same desktop Runtime are adopted. A
        // desktop-managed predecessor is stopped through its own graceful CLI
        // before the successor starts.
        if let Some(readiness) = self.probe.readiness(&origin).await {
            let needs_handover = resolved_launcher
                .as_ref()
                .is_some_and(|launcher| launcher.requires_handover(&readiness));
            if needs_handover {
                if let Err(error) = handover(resolved_launcher.as_ref().expect("checked launcher").clone()).await {
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
                handover_performed = true;
            } else {
                cleanup_if_current(resolved_launcher.as_ref(), &readiness).await;
                return publish(
                    sink,
                    BootstrapStatus::ready(&origin, attempt, BootstrapNoticeCode::Adopted),
                );
            }
        }
        // `/ready` rejects a UI/Controller pair with different private Runtime
        // identities. That is intentionally not readiness, but the exact
        // mismatch response still authorizes the bundled successor to stop the
        // superseded desktop-managed service before launching.
        if !handover_performed
            && self.probe.mismatched_runtime_identity(&origin).await.is_some()
            && resolved_launcher
                .as_ref()
                .is_some_and(|launcher| launcher.expected_runtime_id().is_some())
        {
            if let Err(error) = handover(resolved_launcher.as_ref().expect("checked launcher").clone()).await {
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
            handover_performed = true;
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
        if let Err(error) = self.launch_if_needed(resolved_launcher.clone()) {
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

        let mut deadline = Instant::now() + self.settings.ready_timeout;
        while Instant::now() < deadline {
            sleep(self.settings.poll_interval).await;
            attempt += 1;
            let readiness = self.probe.readiness(&origin).await;
            let mut needs_polling_handover = false;
            if let Some(readiness) = readiness {
                if readiness_matches_launched_runtime(resolved_launcher.as_ref(), &readiness) {
                    cleanup_if_current(resolved_launcher.as_ref(), &readiness).await;
                    return publish(
                        sink,
                        BootstrapStatus::ready(&origin, attempt, BootstrapNoticeCode::Ready),
                    );
                }
                needs_polling_handover = !handover_performed
                    && resolved_launcher
                        .as_ref()
                        .is_some_and(|launcher| launcher.requires_handover(&readiness));
            } else if !handover_performed {
                needs_polling_handover = self.probe.mismatched_runtime_identity(&origin).await.is_some()
                    && resolved_launcher
                        .as_ref()
                        .is_some_and(|launcher| launcher.expected_runtime_id().is_some());
            }
            if needs_polling_handover {
                let launcher = resolved_launcher
                    .as_ref()
                    .expect("handover requires a launcher")
                    .clone();
                if let Err(error) = handover(launcher).await {
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
                handover_performed = true;
                *self.launched_runtime() = None;
                if let Err(error) = self.launch_if_needed(resolved_launcher.clone()) {
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
                // Handover may legitimately spend most of the original startup
                // budget. The newly launched successor gets its own full window.
                deadline = Instant::now() + self.settings.ready_timeout;
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

        // Do not retain a completed zero-exit helper forever when the Runtime
        // still failed the entire readiness budget. An unresolved launcher is
        // retained so Retry cannot overlap a command that is still running.
        self.clear_successful_launch();
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
        // A previous `vibe start` may have exited zero without producing a
        // ready UI. Check and replace it under one lock so a just-completed
        // helper cannot strand this Retry between inspection and launch.
        if launched.as_ref().is_some_and(|runtime| runtime.watch.succeeded()) {
            *launched = None;
        }
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

    /// Releases a completed zero-exit launcher after readiness did not follow.
    ///
    /// This does not stop a Runtime. It only allows the next bootstrap run to
    /// invoke the idempotent start command again. A launcher still running or
    /// otherwise unobservable remains retained, so attempts never overlap.
    fn clear_successful_launch(&self) -> bool {
        let mut launched = self.launched_runtime();
        if launched.as_ref().is_some_and(|runtime| runtime.watch.succeeded()) {
            *launched = None;
            return true;
        }
        false
    }
}

async fn handover(launcher: Arc<dyn ResolvedRuntimeLauncher>) -> Result<(), LaunchError> {
    tokio::task::spawn_blocking(move || launcher.handover())
        .await
        .map_err(|_| LaunchError::Handover)?
}

fn readiness_matches_launched_runtime(
    launcher: Option<&Arc<dyn ResolvedRuntimeLauncher>>,
    readiness: &RuntimeReadiness,
) -> bool {
    match launcher.and_then(|launcher| launcher.expected_runtime_id()) {
        Some(expected) => readiness.desktop_runtime_id.as_deref() == Some(expected),
        None => true,
    }
}

async fn cleanup_if_current(launcher: Option<&Arc<dyn ResolvedRuntimeLauncher>>, readiness: &RuntimeReadiness) {
    let Some(launcher) = launcher else {
        return;
    };
    if launcher.expected_runtime_id() != readiness.desktop_runtime_id.as_deref() {
        return;
    }
    let launcher = launcher.clone();
    let _ = tokio::task::spawn_blocking(move || launcher.prune_superseded()).await;
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
