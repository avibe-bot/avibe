//! The bootstrap state machine.
//!
//! One run answers a single question — "is there a Runtime at this origin, and
//! if not, can we get one?" — and reports every step through a [`StatusSink`].
//! The shell navigates only when a run ends in [`BootstrapPhase::Ready`].

use std::env;
use std::sync::{Arc, Mutex, MutexGuard};
use std::time::Duration;

use tokio::time::{sleep, Instant};

use crate::health::{HealthProbe, Presence};
use crate::launcher::{CliOutcome, LaunchError, LaunchExit, LaunchedRuntime, ResolvedRuntimeLauncher, RuntimeLauncher};
use crate::origin::LoopbackOrigin;
use crate::receipt::{ReceiptEvent, StartupReceiptOutcome, StartupReceiptReason};
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

/// Per-request timeout for a single presence probe.
///
/// It must outlast a refused loopback connect on Windows, which resends the SYN
/// after the RST and refuses only after about 2 s. A shorter budget turns
/// `Absent` into `Unknown`, and uninstall is then blocked.
pub const DEFAULT_PROBE_TIMEOUT: Duration = Duration::from_secs(5);

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

/// What started a bootstrap run.
///
/// Only the app's own launch, including the relaunch after an update, and the
/// user's Try again may stop a predecessor Runtime to hand over. A shell
/// recovering by itself never does: if two shells ever ran at once, each would
/// otherwise keep stopping the other's Runtime.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BootstrapTrigger {
    Launch,
    Retry,
    Recovery,
}

impl BootstrapTrigger {
    fn allows_handover(self) -> bool {
        matches!(self, Self::Launch | Self::Retry)
    }
}

/// How removing the app-private Runtime for uninstall ended.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RemovalOutcome {
    /// This app's Runtime was stopped and its private files are gone.
    Removed,
    /// The launcher owns no private files, so nothing was changed.
    NotPrivate,
    /// Something may still be running from or installing into the files: a
    /// Runtime this app did not start, one still starting, or a backend
    /// install. Every file was kept.
    Kept,
    /// The Runtime's CLI could not run, so nothing proved that its installs had
    /// stopped, and nothing was seen running either. Every file was kept; only
    /// the user can choose to delete them anyway.
    Unverified,
    /// Removal began and did not finish. Part of the files may remain.
    Failed,
}

/// Owns one Runtime origin for the lifetime of the shell process.
///
/// A host may run `bootstrap` many times — once at startup and once per retry or
/// recovery. It never overlaps launch commands. A Runtime is this shell's only
/// while its Controller carries the shell's Runtime identity, and only such a
/// Runtime is stopped by Stop, Quit, or uninstall. The one other Runtime the
/// shell ever stops is a desktop predecessor with another identity, on handover.
/// After a completed successful launch still fails the full readiness budget, a
/// later retry may re-run the idempotent start command to repair missing
/// components.
pub struct RuntimeHost {
    probe: Arc<dyn HealthProbe>,
    launcher: Arc<dyn RuntimeLauncher>,
    settings: RuntimeHostSettings,
    launched_runtime: Mutex<LaunchState>,
    /// Reserves one run's prepare-and-launch step, so concurrent runs ask the
    /// login shell once between them. Only that step takes it: stop and
    /// removal use the launch-state mutex alone and never wait on a lookup.
    launch_reservation: tokio::sync::Mutex<()>,
}

/// What the last successful bootstrap run adopted: the monitor's baseline.
#[derive(Clone)]
struct Adoption {
    presence: Presence,
    launcher: Option<Arc<dyn ResolvedRuntimeLauncher>>,
}

#[derive(Default)]
struct LaunchState {
    attempt: Option<LaunchedRuntime>,
    adoption: Option<Adoption>,
    /// The launcher whose Runtime identity the served Controller last carried.
    /// It is the only stop authority, and it is re-read from every probe.
    owner: Option<Arc<dyn ResolvedRuntimeLauncher>>,
    /// The shell has evidence that a Runtime may still be alive, whether or not
    /// it is this shell's.
    runtime_may_be_running: bool,
    stopping: bool,
    /// Advances when a bootstrap run begins and when a stop or removal ends. A
    /// probe records what it saw only while the epoch it was sent in is still
    /// current, so one that a new run or a stop overtook cannot rewrite who
    /// owns the origin.
    epoch: u64,
}

impl LaunchState {
    /// Records who serves the origin now, for a probe sent in `epoch`. An
    /// unprovable probe keeps the last owner: a slow `/ready` is not evidence
    /// that the Runtime changed hands.
    fn observe(&mut self, epoch: u64, presence: &Presence, launcher: Option<&Arc<dyn ResolvedRuntimeLauncher>>) {
        if self.stopping || self.epoch != epoch {
            return;
        }
        match presence {
            Presence::Mine { .. } => {
                self.owner = launcher.cloned();
                self.runtime_may_be_running = true;
            }
            Presence::Unknown => {}
            Presence::Foreign { .. } | Presence::Unmanaged | Presence::Absent => self.owner = None,
        }
    }

    fn launch_pending(&self) -> bool {
        self.attempt.as_ref().is_some_and(|attempt| attempt.watch.pending())
    }

    /// Starts a bootstrap run: the last adoption no longer stands, and no probe
    /// sent before now may record what it saw. Returns the run's epoch.
    fn begin_run(&mut self) -> u64 {
        self.adoption = None;
        self.epoch += 1;
        self.epoch
    }

    /// Forgets the Runtime a stop or removal ended. The fence stays up until
    /// the caller finishes, and the epoch is kept so no earlier probe can
    /// match it again.
    fn forget(&mut self) {
        *self = LaunchState {
            stopping: self.stopping,
            epoch: self.epoch,
            ..LaunchState::default()
        };
    }

    /// Ends a stop or removal. A probe still in flight saw the Runtime from
    /// before it, so it may no longer record what it saw.
    fn finish_stopping(&mut self) {
        self.stopping = false;
        self.epoch += 1;
    }

    fn retain_completed_attempt_liveness(&mut self) {
        if self.attempt.as_ref().is_some_and(|attempt| attempt.watch.succeeded()) {
            // A successful helper may have started or adopted a Runtime.
            // Completion is not proof of process absence, so releasing the
            // retry slot must retain this fence.
            self.runtime_may_be_running = true;
        }
    }
}

impl RuntimeHost {
    pub fn new(probe: Arc<dyn HealthProbe>, launcher: Arc<dyn RuntimeLauncher>, settings: RuntimeHostSettings) -> Self {
        Self {
            probe,
            launcher,
            settings,
            launched_runtime: Mutex::new(LaunchState::default()),
            launch_reservation: tokio::sync::Mutex::new(()),
        }
    }

    pub fn settings(&self) -> &RuntimeHostSettings {
        &self.settings
    }

    /// Whether this host retains a launch attempt for launch deduplication.
    pub fn has_launched(&self) -> bool {
        self.launched_runtime().attempt.is_some()
    }

    /// Whether the served Controller last carried this shell's Runtime identity.
    pub fn has_owned_runtime(&self) -> bool {
        self.launched_runtime().owner.is_some()
    }

    /// Stops this shell's Runtime, scoped to its identity, for Stop and Quit.
    ///
    /// `Refused` with `not_owned` means the shell has no Runtime to stop, and
    /// `Failed` with `launch_pending` that a start or stop is still running.
    /// Neither runs the CLI.
    pub async fn stop_owned_runtime(&self) -> CliOutcome {
        let (launcher, runtime_id) = {
            let mut state = self.launched_runtime();
            let Some((owner, runtime_id)) = state
                .owner
                .clone()
                .and_then(|owner| Some((owner.clone(), owner.expected_runtime_id()?.to_owned())))
            else {
                return CliOutcome::Refused {
                    reason: "not_owned".to_owned(),
                };
            };
            if state.stopping || state.launch_pending() {
                return CliOutcome::Failed {
                    part: "launch_pending".to_owned(),
                };
            }
            state.stopping = true;
            (owner, runtime_id)
        };
        let outcome = tokio::task::spawn_blocking(move || launcher.stop(&runtime_id))
            .await
            .unwrap_or_else(|_| CliOutcome::Failed {
                part: "unknown".to_owned(),
            });
        let mut state = self.launched_runtime();
        match &outcome {
            CliOutcome::Completed => state.forget(),
            CliOutcome::Refused { .. } => {
                // The service now carries another identity. That revokes stop
                // authority but does not prove this shell's Runtime is gone, so
                // the uninstall fence stays.
                state.retain_completed_attempt_liveness();
                state.attempt = None;
                state.owner = None;
            }
            CliOutcome::Failed { .. } | CliOutcome::Unrunnable => {}
        }
        state.finish_stopping();
        outcome
    }

    /// Whether the origin is still served by the Runtime the last bootstrap
    /// adopted, judged by the same presence contract. Every probe also records
    /// who owns the origin now, unless a bootstrap run began or a stop or
    /// removal ended while it was in flight.
    pub async fn is_serving(&self, origin: &LoopbackOrigin) -> bool {
        let (adoption, epoch) = {
            let state = self.launched_runtime();
            let Some(adoption) = state.adoption.clone() else {
                return false;
            };
            (adoption, state.epoch)
        };
        let presence = self.observe_presence(epoch, origin, adoption.launcher.as_ref()).await;
        presence == adoption.presence
    }

    /// Releases only completed helper retry state after readiness loss.
    ///
    /// Readiness loss is not process-loss evidence. A pending helper remains
    /// retained so recovery cannot overlap it. This transition never changes
    /// stopping state or clears the liveness fence.
    pub fn release_after_readiness_loss(&self) {
        let mut state = self.launched_runtime();
        if state.stopping {
            return;
        }
        if state
            .attempt
            .as_ref()
            .is_some_and(|attempt| attempt.watch.succeeded() || attempt.watch.failed())
        {
            state.retain_completed_attempt_liveness();
            state.attempt = None;
        }
    }

    /// Stops this app's Runtime and removes its private files, for uninstall.
    ///
    /// Proceeds only while the origin is served by this app's Runtime or by
    /// nothing, and only after the Runtime's own CLI stopped it and deleted the
    /// backend root under every install claim. Installed and user-managed
    /// launchers are never modified.
    pub async fn remove_private_runtime(&self, active_origin: Option<&LoopbackOrigin>) -> RemovalOutcome {
        if !self.launcher.owns_private_files() {
            return RemovalOutcome::NotPrivate;
        }
        if !self.begin_removal() {
            return RemovalOutcome::Kept;
        }
        let outcome = self.remove_verified(active_origin).await;
        self.finish_removal(outcome);
        outcome
    }

    /// Deletes the private files without the Runtime's confirmation, after the
    /// user chose "Delete anyway" for [`RemovalOutcome::Unverified`].
    ///
    /// The origin is checked again first: anything answering there now keeps
    /// every file.
    pub async fn remove_unverified_private_runtime(&self, active_origin: Option<&LoopbackOrigin>) -> RemovalOutcome {
        if !self.launcher.owns_private_files() {
            return RemovalOutcome::NotPrivate;
        }
        if !self.begin_removal() {
            return RemovalOutcome::Kept;
        }
        let (_, presence) = self.removal_presence(active_origin).await;
        let outcome = if presence == Presence::Absent {
            let launcher = self.launcher.clone();
            match tokio::task::spawn_blocking(move || launcher.remove_unverified_private_files()).await {
                Ok(Ok(())) => RemovalOutcome::Removed,
                _ => RemovalOutcome::Failed,
            }
        } else {
            RemovalOutcome::Kept
        };
        self.finish_removal(outcome);
        outcome
    }

    /// Runs the state machine once and returns its terminal status.
    pub async fn bootstrap(&self, sink: &dyn StatusSink, trigger: BootstrapTrigger) -> BootstrapStatus {
        let run = self.launched_runtime().begin_run();
        publish(sink, BootstrapStatus::preparing());
        let (origin, resolved_launcher) = match self.resolve_origin().await {
            Ok(resolved) => resolved,
            Err(error) => {
                return publish(
                    sink,
                    BootstrapStatus::rejected(error.notice_code(), error.is_retryable()),
                )
            }
        };
        let identified = resolved_launcher
            .as_ref()
            .is_some_and(|launcher| launcher.expected_runtime_id().is_some());

        let mut attempt = 1;
        publish(sink, BootstrapStatus::probing(&origin, attempt));

        // This app's Runtime and any Runtime that is not a desktop predecessor
        // are adopted. A predecessor with another desktop identity is replaced
        // only by this app's `vibe start`, and only when this run may hand over.
        let presence = self.observe_presence(run, &origin, resolved_launcher.as_ref()).await;
        match presence {
            Presence::Mine { ready: true } | Presence::Unmanaged => {
                return self
                    .adopt(
                        sink,
                        &origin,
                        attempt,
                        presence,
                        resolved_launcher.as_ref(),
                        BootstrapNoticeCode::Adopted,
                    )
                    .await;
            }
            Presence::Foreign { ready: true, .. } if !identified => {
                return self
                    .adopt(
                        sink,
                        &origin,
                        attempt,
                        presence,
                        resolved_launcher.as_ref(),
                        BootstrapNoticeCode::Adopted,
                    )
                    .await;
            }
            Presence::Foreign { .. } if identified && !trigger.allows_handover() => {
                return publish(sink, ownership_lost(&origin, attempt));
            }
            // A predecessor to hand over, starting, not provable, or nothing
            // there. `vibe start` is idempotent and decides for itself whether
            // a service already runs, and whether it may replace a predecessor.
            _ => {}
        }

        // A launcher may report its non-zero exit after an earlier bootstrap
        // timed out. Re-check the retained watch before deciding this run is
        // already waiting on a viable launch.
        if let Some(code) = self.clear_failed_launch() {
            return publish(
                sink,
                BootstrapStatus::failed(&origin, attempt, BootstrapNotice::new(code), true),
            );
        }
        // A development override points at another local Runtime and is treated
        // as externally managed. The shell must not start a differently
        // configured default Runtime and then wait on the override forever.
        let Some(launcher) = resolved_launcher.clone() else {
            return publish(
                sink,
                BootstrapStatus::failed(
                    &origin,
                    attempt,
                    BootstrapNotice::new(BootstrapNoticeCode::RuntimeNotFound),
                    true,
                ),
            );
        };

        // The login-shell PATH lookup can wait 10 s on a hanging startup file.
        // It runs here, on a blocking thread, before the launch-state mutex is
        // taken, so a concurrent recovery or stop is not held for that budget.
        // A run that finds an earlier launch still in flight starts nothing, so
        // it does not ask the login shell either. The reservation makes a
        // concurrent run wait here and then find this run's launch in flight.
        let reservation = self.launch_reservation.lock().await;
        let prepared = !self.launched_runtime().launch_pending();
        if prepared {
            let preparing = launcher.clone();
            let _ = tokio::task::spawn_blocking(move || preparing.prepare_launch()).await;
        }
        // The lock makes the decision and launch atomic, so concurrent runs
        // cannot both start the Runtime.
        let launched = self.launch_if_needed(launcher.clone(), trigger.allows_handover(), prepared);
        drop(reservation);
        if let Err(error) = launched {
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

        let receipt_watch = self
            .launched_runtime()
            .attempt
            .as_ref()
            .map(|attempt| attempt.watch.clone())
            .filter(|watch| watch.receipt_capable());
        if let Some(watch) = receipt_watch {
            return self
                .wait_for_startup_receipt(sink, run, origin, launcher, identified, attempt, watch)
                .await;
        }

        let deadline = Instant::now() + self.settings.ready_timeout;
        while Instant::now() < deadline {
            sleep(self.settings.poll_interval).await;
            attempt += 1;
            // Read before the probe, so a predecessor judged below was seen
            // after this app's helper had exited.
            let settled = !self.launched_runtime().launch_pending();
            let presence = self.observe_presence(run, &origin, Some(&launcher)).await;
            match presence {
                Presence::Mine { ready: true } => {
                    return self
                        .adopt(
                            sink,
                            &origin,
                            attempt,
                            presence,
                            Some(&launcher),
                            BootstrapNoticeCode::Ready,
                        )
                        .await;
                }
                Presence::Unmanaged => {
                    return self
                        .adopt(
                            sink,
                            &origin,
                            attempt,
                            presence,
                            Some(&launcher),
                            BootstrapNoticeCode::Adopted,
                        )
                        .await;
                }
                Presence::Foreign { ready: true, .. } if !identified => {
                    return self
                        .adopt(
                            sink,
                            &origin,
                            attempt,
                            presence,
                            Some(&launcher),
                            BootstrapNoticeCode::Adopted,
                        )
                        .await;
                }
                _ => {}
            }
            // A launcher that exited non-zero started nothing, so the remaining
            // wait would be spent polling an address that will never answer.
            // Its exit also says whether it declined or failed a handover.
            if let Some(code) = self.clear_failed_launch() {
                return publish(
                    sink,
                    BootstrapStatus::failed(&origin, attempt, BootstrapNotice::new(code), true),
                );
            }
            // While the helper runs, a predecessor may be the one it is
            // replacing. Once it has exited, one still serving was not replaced.
            if identified && settled && matches!(presence, Presence::Foreign { .. }) {
                return publish(sink, ownership_lost(&origin, attempt));
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

    async fn wait_for_startup_receipt(
        &self,
        sink: &dyn StatusSink,
        run: u64,
        origin: LoopbackOrigin,
        launcher: Arc<dyn ResolvedRuntimeLauncher>,
        identified: bool,
        mut attempt: u32,
        watch: crate::launcher::LaunchWatch,
    ) -> BootstrapStatus {
        let deadline = Instant::now() + self.settings.ready_timeout;
        let mut eof_seen = false;
        loop {
            let mut receipt = None;
            let mut invalid_before_acceptance = false;
            while let Some(event) = watch.receipt_event() {
                match event {
                    ReceiptEvent::Receipt(next) => {
                        if receipt.replace(next).is_some() {
                            invalid_before_acceptance = true;
                        }
                    }
                    ReceiptEvent::Invalid | ReceiptEvent::TransportError => invalid_before_acceptance = true,
                    ReceiptEvent::Eof => {
                        eof_seen = true;
                    }
                }
            }
            if invalid_before_acceptance {
                return publish(sink, receipt_invalid(&origin, attempt));
            }
            if let Some(receipt) = receipt {
                if watch.failed() || receipt.origin != origin.as_str() {
                    return publish(sink, receipt_invalid(&origin, attempt));
                }
                if receipt.outcome == StartupReceiptOutcome::Failed {
                    return publish(
                        sink,
                        BootstrapStatus::failed(
                            &origin,
                            attempt,
                            BootstrapNotice::new(receipt_failure_notice(receipt.reason)),
                            true,
                        ),
                    );
                }
                // Receipt acceptance is the startup commit observation. The
                // final probe below is still authoritative for the exact
                // endpoint and Runtime identity, but helper output and exit
                // events observed after this point belong to the monitor.
                launcher.record_bootstrap_event("runtime.start.receipt.accepted");
                attempt += 1;
                let presence = self.observe_presence(run, &origin, Some(&launcher)).await;
                launcher.record_bootstrap_event("runtime.start.final_presence");
                return match presence {
                    Presence::Mine { ready: true } => {
                        self.adopt(
                            sink,
                            &origin,
                            attempt,
                            presence,
                            Some(&launcher),
                            BootstrapNoticeCode::Ready,
                        )
                        .await
                    }
                    Presence::Unmanaged => {
                        self.adopt(
                            sink,
                            &origin,
                            attempt,
                            presence,
                            Some(&launcher),
                            BootstrapNoticeCode::Adopted,
                        )
                        .await
                    }
                    Presence::Foreign { ready: true, .. } if !identified => {
                        self.adopt(
                            sink,
                            &origin,
                            attempt,
                            presence,
                            Some(&launcher),
                            BootstrapNoticeCode::Adopted,
                        )
                        .await
                    }
                    Presence::Foreign { .. } if identified => publish(sink, ownership_lost(&origin, attempt)),
                    _ => publish(sink, receipt_invalid(&origin, attempt)),
                };
            }
            if let Some(code) = self.clear_failed_launch() {
                return publish(
                    sink,
                    BootstrapStatus::failed(&origin, attempt, BootstrapNotice::new(code), true),
                );
            }
            if eof_seen
                && self
                    .launched_runtime()
                    .attempt
                    .as_ref()
                    .is_some_and(|attempt| attempt.watch.succeeded())
            {
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
            if Instant::now() >= deadline {
                self.clear_successful_launch();
                return publish(
                    sink,
                    BootstrapStatus::failed(
                        &origin,
                        attempt,
                        BootstrapNotice::timeout(self.settings.ready_timeout.as_secs()),
                        true,
                    ),
                );
            }
            sleep(self.settings.poll_interval).await;
        }
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

    /// Probes the origin, and records the answer only if `epoch`, read when
    /// the probe was sent, is still current.
    async fn observe_presence(
        &self,
        epoch: u64,
        origin: &LoopbackOrigin,
        launcher: Option<&Arc<dyn ResolvedRuntimeLauncher>>,
    ) -> Presence {
        let expected = launcher.and_then(|launcher| launcher.expected_runtime_id());
        let presence = self.probe.presence(origin, expected).await;
        self.launched_runtime().observe(epoch, &presence, launcher);
        presence
    }

    async fn adopt(
        &self,
        sink: &dyn StatusSink,
        origin: &LoopbackOrigin,
        attempt: u32,
        presence: Presence,
        launcher: Option<&Arc<dyn ResolvedRuntimeLauncher>>,
        notice: BootstrapNoticeCode,
    ) -> BootstrapStatus {
        let mine = matches!(presence, Presence::Mine { .. });
        self.launched_runtime().adoption = Some(Adoption {
            presence,
            launcher: launcher.cloned(),
        });
        // Only this app's own Runtime proves which bundle is current.
        if let Some(launcher) = launcher.filter(|_| mine).cloned() {
            let _ = tokio::task::spawn_blocking(move || launcher.prune_superseded()).await;
        }
        publish(sink, BootstrapStatus::ready(origin, attempt, notice))
    }

    /// Fences removal against a start or stop that is still running.
    fn begin_removal(&self) -> bool {
        let mut state = self.launched_runtime();
        if state.stopping || state.launch_pending() {
            return false;
        }
        state.stopping = true;
        true
    }

    fn finish_removal(&self, outcome: RemovalOutcome) {
        let mut state = self.launched_runtime();
        if outcome == RemovalOutcome::Removed {
            state.forget();
        }
        state.finish_stopping();
    }

    async fn remove_verified(&self, active_origin: Option<&LoopbackOrigin>) -> RemovalOutcome {
        let (resolved, presence) = self.removal_presence(active_origin).await;
        if !matches!(presence, Presence::Mine { .. } | Presence::Absent) {
            return RemovalOutcome::Kept;
        }
        let Some(resolved) = resolved else {
            return RemovalOutcome::Unverified;
        };
        let Some(runtime_id) = resolved.expected_runtime_id().map(str::to_owned) else {
            return RemovalOutcome::Kept;
        };
        let stopping = resolved.clone();
        match run_blocking(move || stopping.stop(&runtime_id)).await {
            CliOutcome::Completed => {}
            CliOutcome::Unrunnable if presence == Presence::Absent => return RemovalOutcome::Unverified,
            _ => return RemovalOutcome::Kept,
        }
        self.launched_runtime().forget();
        match run_blocking(move || resolved.remove_backends()).await {
            CliOutcome::Completed => {}
            CliOutcome::Refused { .. } => return RemovalOutcome::Kept,
            CliOutcome::Failed { .. } => return RemovalOutcome::Failed,
            CliOutcome::Unrunnable => return RemovalOutcome::Unverified,
        }
        let launcher = self.launcher.clone();
        match tokio::task::spawn_blocking(move || launcher.remove_private_files()).await {
            Ok(Ok(())) => RemovalOutcome::Removed,
            _ => RemovalOutcome::Failed,
        }
    }

    /// Who serves the origin, judged against the bundle's identity. Without an
    /// origin to ask, any sign of a Runtime this shell started is enough to be
    /// unsure.
    async fn removal_presence(
        &self,
        active_origin: Option<&LoopbackOrigin>,
    ) -> (Option<Arc<dyn ResolvedRuntimeLauncher>>, Presence) {
        let launcher = self.launcher.clone();
        let resolved = tokio::task::spawn_blocking(move || launcher.resolve())
            .await
            .ok()
            .and_then(Result::ok);
        let origin = match (active_origin, &resolved) {
            (Some(origin), _) => Some(origin.clone()),
            (None, Some(resolved)) => {
                let resolved = resolved.clone();
                tokio::task::spawn_blocking(move || resolved.endpoint())
                    .await
                    .ok()
                    .and_then(Result::ok)
            }
            (None, None) => None,
        };
        let presence = match origin {
            Some(origin) => {
                let expected = resolved.as_ref().and_then(|resolved| resolved.expected_runtime_id());
                self.probe.presence(&origin, expected).await
            }
            None => {
                let state = self.launched_runtime();
                if state.runtime_may_be_running || state.attempt.is_some() {
                    Presence::Unknown
                } else {
                    Presence::Absent
                }
            }
        };
        (resolved, presence)
    }

    fn launched_runtime(&self) -> MutexGuard<'_, LaunchState> {
        self.launched_runtime
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }

    /// Starts the Runtime unless an attempt is still in flight. Only a
    /// launcher this run `prepared` off the lock may start: launching another
    /// would run the login-shell lookup under this mutex. An attempt seen in
    /// flight that has finished since is what such a run waits for, and a
    /// later run prepares before it launches again.
    fn launch_if_needed(
        &self,
        launcher: Arc<dyn ResolvedRuntimeLauncher>,
        hand_over: bool,
        prepared: bool,
    ) -> Result<(), LaunchError> {
        let mut state = self.launched_runtime();
        if state.stopping {
            return Err(LaunchError::RuntimeStop);
        }
        if state.attempt.as_ref().is_some_and(|attempt| attempt.watch.succeeded()) {
            state.retain_completed_attempt_liveness();
            state.attempt = None;
        }
        if state.attempt.is_none() && prepared {
            // Spawning the helper is evidence that a Runtime may exist even
            // when the helper later reports failure. Keep that fact separate
            // from retry deduplication and stop authority.
            state.attempt = Some(launcher.launch(hand_over)?);
            state.runtime_may_be_running = true;
        }
        Ok(())
    }

    /// A failed helper releases its retry slot and nothing else. Returns the
    /// notice its exit calls for.
    fn clear_failed_launch(&self) -> Option<BootstrapNoticeCode> {
        let mut state = self.launched_runtime();
        let code = match state.attempt.as_ref()?.watch.exit()? {
            LaunchExit::Started => return None,
            LaunchExit::HandoverRefused => BootstrapNoticeCode::RuntimeOwnershipLost,
            LaunchExit::HandoverFailed => BootstrapNoticeCode::RuntimeStopFailed,
            LaunchExit::Failed => BootstrapNoticeCode::LauncherExited,
        };
        state.attempt = None;
        Some(code)
    }

    /// Readiness timeout releases only a completed helper, never the liveness
    /// fence. Pending helpers remain retained so Retry cannot overlap a live
    /// launch.
    fn clear_successful_launch(&self) -> bool {
        let mut state = self.launched_runtime();
        if state.attempt.as_ref().is_some_and(|attempt| attempt.watch.succeeded()) {
            state.retain_completed_attempt_liveness();
            state.attempt = None;
            return true;
        }
        false
    }
}

async fn run_blocking(verb: impl FnOnce() -> CliOutcome + Send + 'static) -> CliOutcome {
    tokio::task::spawn_blocking(verb)
        .await
        .unwrap_or_else(|_| CliOutcome::Failed {
            part: "unknown".to_owned(),
        })
}

fn ownership_lost(origin: &LoopbackOrigin, attempt: u32) -> BootstrapStatus {
    BootstrapStatus::failed(
        origin,
        attempt,
        BootstrapNotice::new(BootstrapNoticeCode::RuntimeOwnershipLost),
        true,
    )
}

fn receipt_invalid(origin: &LoopbackOrigin, attempt: u32) -> BootstrapStatus {
    BootstrapStatus::failed(
        origin,
        attempt,
        BootstrapNotice::new(BootstrapNoticeCode::RuntimeDiscoveryFailed),
        true,
    )
}

fn receipt_failure_notice(reason: Option<StartupReceiptReason>) -> BootstrapNoticeCode {
    match reason {
        Some(StartupReceiptReason::HandoverRefused | StartupReceiptReason::RuntimeClaimRefused) => {
            BootstrapNoticeCode::RuntimeOwnershipLost
        }
        Some(StartupReceiptReason::HandoverFailed) => BootstrapNoticeCode::RuntimeStopFailed,
        Some(StartupReceiptReason::RuntimeNotReady) => BootstrapNoticeCode::RuntimeDiscoveryFailed,
        Some(StartupReceiptReason::StartupFailed) | None => BootstrapNoticeCode::RuntimeSpawnFailed,
    }
}

fn publish(sink: &dyn StatusSink, status: BootstrapStatus) -> BootstrapStatus {
    sink.publish(status.clone());
    status
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::launcher::LaunchWatch;
    use crate::receipt::{ReceiptEvent, StartupReceipt, StartupReceiptOutcome, StartupReceiptStream};
    use crate::status::BootstrapPhase;
    use std::collections::VecDeque;
    use std::sync::atomic::{AtomicUsize, Ordering};

    const ORIGIN: &str = "http://127.0.0.1:5123";

    /// This app's Runtime, ready.
    struct MineProbe;

    /// Nothing serving the origin: bootstrap will try to start one.
    struct AbsentProbe;

    #[async_trait::async_trait]
    impl HealthProbe for MineProbe {
        async fn presence(&self, _origin: &LoopbackOrigin, _expected: Option<&str>) -> Presence {
            Presence::Mine { ready: true }
        }
    }

    #[async_trait::async_trait]
    impl HealthProbe for AbsentProbe {
        async fn presence(&self, _origin: &LoopbackOrigin, _expected: Option<&str>) -> Presence {
            Presence::Absent
        }
    }

    #[derive(Default)]
    struct CountingLauncher {
        launches: AtomicUsize,
        stops: AtomicUsize,
        prepares: AtomicUsize,
    }

    impl RuntimeLauncher for CountingLauncher {
        fn resolve(&self) -> Result<Arc<dyn ResolvedRuntimeLauncher>, LaunchError> {
            unreachable!("the tests hand the resolved launcher over directly")
        }
    }

    impl ResolvedRuntimeLauncher for Arc<CountingLauncher> {
        fn endpoint(&self) -> Result<LoopbackOrigin, LaunchError> {
            Ok(LoopbackOrigin::parse(ORIGIN).expect("test origin"))
        }

        fn prepare_launch(&self) {
            self.prepares.fetch_add(1, Ordering::SeqCst);
        }

        fn launch(&self, _hand_over: bool) -> Result<LaunchedRuntime, LaunchError> {
            self.launches.fetch_add(1, Ordering::SeqCst);
            Ok(LaunchedRuntime {
                pid: 1,
                watch: LaunchWatch::exited(LaunchExit::Started),
            })
        }

        fn expected_runtime_id(&self) -> Option<&str> {
            Some("b")
        }

        fn stop(&self, _runtime_id: &str) -> CliOutcome {
            self.stops.fetch_add(1, Ordering::SeqCst);
            CliOutcome::Completed
        }
    }

    /// While a Stop, Quit, or uninstall is running, nothing starts a Runtime,
    /// no second stop overlaps it, and no probe hands stop authority to or away
    /// from anyone.
    #[tokio::test]
    async fn a_running_stop_fences_launch_stop_and_ownership() {
        let counting = Arc::new(CountingLauncher::default());
        let resolved: Arc<dyn ResolvedRuntimeLauncher> = Arc::new(counting.clone());
        let host = RuntimeHost::new(Arc::new(MineProbe), counting.clone(), RuntimeHostSettings::default());
        {
            let mut state = host.launched_runtime();
            state.owner = Some(resolved.clone());
            state.stopping = true;
        }

        assert!(matches!(
            host.launch_if_needed(resolved.clone(), false, true),
            Err(LaunchError::RuntimeStop)
        ));
        assert_eq!(counting.launches.load(Ordering::SeqCst), 0);
        assert_eq!(
            host.stop_owned_runtime().await,
            CliOutcome::Failed {
                part: "launch_pending".to_owned()
            }
        );
        assert_eq!(counting.stops.load(Ordering::SeqCst), 0);

        let origin = LoopbackOrigin::parse(ORIGIN).expect("test origin");
        host.launched_runtime().owner = None;
        let epoch = host.launched_runtime().epoch;
        host.observe_presence(epoch, &origin, Some(&resolved)).await;
        assert!(
            !host.has_owned_runtime(),
            "a probe during a stop must not restore stop authority"
        );
        assert!(!host.launched_runtime().runtime_may_be_running);
    }

    /// Resolves to itself so a bootstrap run can reach `prepare_launch`.
    struct PreparingLauncher {
        inner: Arc<CountingLauncher>,
    }

    #[derive(Clone)]
    struct ReceiptLauncher {
        state: Arc<ReceiptLauncherState>,
    }

    struct ReceiptLauncherState {
        events: Vec<ReceiptEvent>,
        launches: AtomicUsize,
        runtime_id: String,
        exit: Option<LaunchExit>,
    }

    impl ReceiptLauncher {
        fn new(events: impl IntoIterator<Item = ReceiptEvent>) -> Self {
            Self {
                state: Arc::new(ReceiptLauncherState {
                    events: events.into_iter().collect(),
                    launches: AtomicUsize::new(0),
                    runtime_id: "b".repeat(64),
                    exit: None,
                }),
            }
        }

        fn ready(events: impl IntoIterator<Item = ReceiptEvent>) -> Self {
            Self::new(events)
        }

        fn failed_before_acceptance(events: impl IntoIterator<Item = ReceiptEvent>) -> Self {
            let mut launcher = Self::new(events);
            Arc::get_mut(&mut launcher.state)
                .expect("unique receipt launcher state")
                .exit = Some(LaunchExit::Failed);
            launcher
        }

        fn launches(&self) -> usize {
            self.state.launches.load(Ordering::SeqCst)
        }
    }

    impl RuntimeLauncher for ReceiptLauncher {
        fn resolve(&self) -> Result<Arc<dyn ResolvedRuntimeLauncher>, LaunchError> {
            Ok(Arc::new(self.clone()))
        }
    }

    impl ResolvedRuntimeLauncher for ReceiptLauncher {
        fn endpoint(&self) -> Result<LoopbackOrigin, LaunchError> {
            Ok(LoopbackOrigin::parse(ORIGIN).expect("test origin"))
        }

        fn launch(&self, _hand_over: bool) -> Result<LaunchedRuntime, LaunchError> {
            self.state.launches.fetch_add(1, Ordering::SeqCst);
            let watch = if let Some(exit) = self.state.exit {
                LaunchWatch::with_receipt_and_exit(StartupReceiptStream::from_events(self.state.events.clone()), exit)
            } else {
                LaunchWatch::with_receipt(StartupReceiptStream::from_events(self.state.events.clone()))
            };
            Ok(LaunchedRuntime { pid: 4242, watch })
        }

        fn expected_runtime_id(&self) -> Option<&str> {
            Some(&self.state.runtime_id)
        }
    }

    fn ready_receipt(origin: &str) -> ReceiptEvent {
        ReceiptEvent::Receipt(StartupReceipt {
            schema_version: 1,
            outcome: StartupReceiptOutcome::Ready,
            origin: origin.to_owned(),
            reason: None,
        })
    }

    struct ReceiptProbe {
        calls: AtomicUsize,
    }

    impl ReceiptProbe {
        fn calls(&self) -> usize {
            self.calls.load(Ordering::SeqCst)
        }
    }

    struct SequenceProbe {
        responses: Mutex<VecDeque<Presence>>,
    }

    #[async_trait::async_trait]
    impl HealthProbe for SequenceProbe {
        async fn presence(&self, _origin: &LoopbackOrigin, _expected: Option<&str>) -> Presence {
            self.responses
                .lock()
                .expect("sequence probe lock")
                .pop_front()
                .unwrap_or(Presence::Unknown)
        }
    }

    #[async_trait::async_trait]
    impl HealthProbe for ReceiptProbe {
        async fn presence(&self, _origin: &LoopbackOrigin, expected: Option<&str>) -> Presence {
            if self.calls.fetch_add(1, Ordering::SeqCst) == 0 {
                return Presence::Absent;
            }
            Presence::Mine {
                ready: expected == Some("b".repeat(64).as_str()),
            }
        }
    }

    impl RuntimeLauncher for PreparingLauncher {
        fn resolve(&self) -> Result<Arc<dyn ResolvedRuntimeLauncher>, LaunchError> {
            Ok(Arc::new(self.inner.clone()))
        }
    }

    /// The login-shell PATH lookup lives in `prepare_launch`, which bootstrap
    /// runs on a blocking thread before taking the launch-state mutex. A Stop
    /// that already holds that mutex therefore still lets the lookup finish,
    /// and still fences the spawn itself.
    #[tokio::test]
    async fn prepare_launch_runs_before_the_launch_state_mutex_is_taken() {
        let counting = Arc::new(CountingLauncher::default());
        let host = RuntimeHost::new(
            Arc::new(AbsentProbe),
            Arc::new(PreparingLauncher {
                inner: counting.clone(),
            }),
            RuntimeHostSettings {
                origin_override: None,
                ready_timeout: Duration::from_millis(10),
                poll_interval: Duration::from_millis(1),
                probe_timeout: Duration::from_millis(10),
            },
        );
        host.launched_runtime().stopping = true;

        let status = host.bootstrap(&DiscardStatus, BootstrapTrigger::Launch).await;

        assert_eq!(counting.prepares.load(Ordering::SeqCst), 1);
        assert_eq!(counting.launches.load(Ordering::SeqCst), 0);
        assert_eq!(status.phase, BootstrapPhase::Failed);
        assert_eq!(status.notice.code, BootstrapNoticeCode::RuntimeSpawnFailed);
    }

    /// A Retry or recovery run that finds an earlier launch still in flight
    /// starts nothing, so it must not spend the login-shell lookup either.
    #[tokio::test]
    async fn a_run_that_finds_a_launch_in_flight_does_not_prepare_another() {
        let counting = Arc::new(CountingLauncher::default());
        let host = RuntimeHost::new(
            Arc::new(AbsentProbe),
            Arc::new(PreparingLauncher {
                inner: counting.clone(),
            }),
            RuntimeHostSettings {
                origin_override: None,
                ready_timeout: Duration::from_millis(10),
                poll_interval: Duration::from_millis(1),
                probe_timeout: Duration::from_millis(10),
            },
        );
        host.launched_runtime().attempt = Some(LaunchedRuntime {
            pid: 1,
            watch: LaunchWatch::default(),
        });

        host.bootstrap(&DiscardStatus, BootstrapTrigger::Retry).await;

        assert_eq!(counting.prepares.load(Ordering::SeqCst), 0);
        assert_eq!(counting.launches.load(Ordering::SeqCst), 0);
    }

    #[tokio::test(start_paused = true)]
    async fn a_ready_receipt_is_followed_by_one_final_presence_check() {
        let probe = Arc::new(ReceiptProbe {
            calls: AtomicUsize::new(0),
        });
        let launcher = ReceiptLauncher::ready([ready_receipt(ORIGIN), ReceiptEvent::Eof]);
        let host = RuntimeHost::new(
            probe.clone(),
            Arc::new(launcher.clone()),
            RuntimeHostSettings {
                ready_timeout: Duration::from_secs(2),
                poll_interval: Duration::from_secs(1),
                ..RuntimeHostSettings::default()
            },
        );

        let status = host.bootstrap(&DiscardStatus, BootstrapTrigger::Launch).await;

        assert_eq!(status.phase, BootstrapPhase::Ready);
        assert_eq!(status.notice.code, BootstrapNoticeCode::Ready);
        assert_eq!(probe.calls(), 2, "initial adoption plus one final receipt check");
        assert_eq!(launcher.launches(), 1);
    }

    #[tokio::test(start_paused = true)]
    async fn a_duplicate_receipt_before_acceptance_refuses_navigation() {
        let probe = Arc::new(ReceiptProbe {
            calls: AtomicUsize::new(0),
        });
        let launcher = ReceiptLauncher::ready([ready_receipt(ORIGIN), ready_receipt(ORIGIN), ReceiptEvent::Eof]);
        let host = RuntimeHost::new(
            probe,
            Arc::new(launcher),
            RuntimeHostSettings {
                ready_timeout: Duration::from_secs(2),
                poll_interval: Duration::from_secs(1),
                ..RuntimeHostSettings::default()
            },
        );

        let status = host.bootstrap(&DiscardStatus, BootstrapTrigger::Launch).await;

        assert_eq!(status.phase, BootstrapPhase::Failed);
        assert_eq!(status.notice.code, BootstrapNoticeCode::RuntimeDiscoveryFailed);
    }

    #[tokio::test(start_paused = true)]
    async fn a_receipt_for_another_origin_refuses_navigation() {
        let probe = Arc::new(ReceiptProbe {
            calls: AtomicUsize::new(0),
        });
        let launcher = ReceiptLauncher::ready([ready_receipt("http://127.0.0.1:5124")]);
        let host = RuntimeHost::new(
            probe.clone(),
            Arc::new(launcher),
            RuntimeHostSettings {
                ready_timeout: Duration::from_secs(2),
                poll_interval: Duration::from_secs(1),
                ..RuntimeHostSettings::default()
            },
        );

        let status = host.bootstrap(&DiscardStatus, BootstrapTrigger::Launch).await;

        assert_eq!(status.phase, BootstrapPhase::Failed);
        assert_eq!(status.notice.code, BootstrapNoticeCode::RuntimeDiscoveryFailed);
        assert_eq!(probe.calls(), 1, "wrong origin precedes the final probe");
    }

    #[tokio::test(start_paused = true)]
    async fn a_receipt_transport_failure_before_publication_refuses_navigation() {
        let probe = Arc::new(ReceiptProbe {
            calls: AtomicUsize::new(0),
        });
        let launcher = ReceiptLauncher::ready([ReceiptEvent::TransportError]);
        let host = RuntimeHost::new(
            probe.clone(),
            Arc::new(launcher),
            RuntimeHostSettings {
                ready_timeout: Duration::from_secs(2),
                poll_interval: Duration::from_secs(1),
                ..RuntimeHostSettings::default()
            },
        );

        let status = host.bootstrap(&DiscardStatus, BootstrapTrigger::Launch).await;

        assert_eq!(status.phase, BootstrapPhase::Failed);
        assert_eq!(status.notice.code, BootstrapNoticeCode::RuntimeDiscoveryFailed);
        assert_eq!(probe.calls(), 1, "transport failure precedes the final probe");
    }

    #[tokio::test(start_paused = true)]
    async fn a_failed_helper_before_receipt_acceptance_refuses_navigation() {
        let launcher = ReceiptLauncher::failed_before_acceptance([ready_receipt(ORIGIN)]);
        let host = RuntimeHost::new(
            Arc::new(ReceiptProbe {
                calls: AtomicUsize::new(0),
            }),
            Arc::new(launcher),
            RuntimeHostSettings {
                ready_timeout: Duration::from_secs(2),
                poll_interval: Duration::from_secs(1),
                ..RuntimeHostSettings::default()
            },
        );

        let status = host.bootstrap(&DiscardStatus, BootstrapTrigger::Launch).await;

        assert_eq!(status.phase, BootstrapPhase::Failed);
        assert_eq!(status.notice.code, BootstrapNoticeCode::RuntimeDiscoveryFailed);
    }

    #[tokio::test(start_paused = true)]
    async fn the_final_presence_probe_remains_authoritative_after_a_ready_receipt() {
        for final_presence in [
            Presence::Absent,
            Presence::Unknown,
            Presence::Mine { ready: false },
            Presence::Foreign {
                runtime_id: "c".repeat(64),
                ready: true,
            },
        ] {
            let probe = Arc::new(SequenceProbe {
                responses: Mutex::new(VecDeque::from([Presence::Absent, final_presence])),
            });
            let host = RuntimeHost::new(
                probe,
                Arc::new(ReceiptLauncher::ready([ready_receipt(ORIGIN)])),
                RuntimeHostSettings {
                    ready_timeout: Duration::from_secs(2),
                    poll_interval: Duration::from_secs(1),
                    ..RuntimeHostSettings::default()
                },
            );

            let status = host.bootstrap(&DiscardStatus, BootstrapTrigger::Launch).await;

            assert_eq!(status.phase, BootstrapPhase::Failed);
            assert_ne!(status.notice.code, BootstrapNoticeCode::Ready);
        }
    }

    #[tokio::test(start_paused = true)]
    async fn an_in_flight_receipt_helper_is_retained_after_observation_timeout() {
        struct NeverReadyProbe;

        #[async_trait::async_trait]
        impl HealthProbe for NeverReadyProbe {
            async fn presence(&self, _origin: &LoopbackOrigin, _expected: Option<&str>) -> Presence {
                Presence::Absent
            }
        }

        let launcher = ReceiptLauncher::ready([]);
        let host = RuntimeHost::new(
            Arc::new(NeverReadyProbe),
            Arc::new(launcher.clone()),
            RuntimeHostSettings {
                ready_timeout: Duration::ZERO,
                poll_interval: Duration::from_secs(1),
                ..RuntimeHostSettings::default()
            },
        );

        let first = host.bootstrap(&DiscardStatus, BootstrapTrigger::Launch).await;
        let second = host.bootstrap(&DiscardStatus, BootstrapTrigger::Retry).await;

        assert_eq!(first.notice.code, BootstrapNoticeCode::ReadyTimeout);
        assert_eq!(second.notice.code, BootstrapNoticeCode::ReadyTimeout);
        assert_eq!(launcher.launches(), 1, "Retry must not start a duplicate helper");
        assert!(host.has_launched());
    }

    #[tokio::test]
    async fn a_slow_controller_receipt_wait_removes_the_legacy_probe_loop() {
        struct CountingAbsentProbe {
            calls: AtomicUsize,
        }

        #[async_trait::async_trait]
        impl HealthProbe for CountingAbsentProbe {
            async fn presence(&self, _origin: &LoopbackOrigin, _expected: Option<&str>) -> Presence {
                self.calls.fetch_add(1, Ordering::SeqCst);
                Presence::Absent
            }
        }

        let receipt_probe = Arc::new(CountingAbsentProbe {
            calls: AtomicUsize::new(0),
        });
        let receipt_host = RuntimeHost::new(
            receipt_probe.clone(),
            Arc::new(ReceiptLauncher::ready([])),
            RuntimeHostSettings {
                ready_timeout: Duration::from_millis(30),
                poll_interval: Duration::from_millis(5),
                ..RuntimeHostSettings::default()
            },
        );
        let receipt_status = receipt_host.bootstrap(&DiscardStatus, BootstrapTrigger::Launch).await;

        let legacy_probe = Arc::new(CountingAbsentProbe {
            calls: AtomicUsize::new(0),
        });
        let legacy_launcher = Arc::new(CountingLauncher::default());
        let legacy_host = RuntimeHost::new(
            legacy_probe.clone(),
            Arc::new(PreparingLauncher { inner: legacy_launcher }),
            RuntimeHostSettings {
                ready_timeout: Duration::from_millis(30),
                poll_interval: Duration::from_millis(5),
                ..RuntimeHostSettings::default()
            },
        );
        let legacy_status = legacy_host.bootstrap(&DiscardStatus, BootstrapTrigger::Launch).await;

        assert_eq!(receipt_status.notice.code, BootstrapNoticeCode::ReadyTimeout);
        assert_eq!(legacy_status.notice.code, BootstrapNoticeCode::ReadyTimeout);
        let receipt_calls = receipt_probe.calls.load(Ordering::SeqCst);
        let legacy_calls = legacy_probe.calls.load(Ordering::SeqCst);
        assert_eq!(receipt_calls, 1, "receipt waiting does not poll /ready");
        assert!(
            legacy_calls > receipt_calls,
            "legacy Rust polling should exceed the receipt path"
        );
    }

    /// A launcher whose login-shell lookup blocks until the test releases it.
    struct BlockedLookup {
        started: std::sync::mpsc::SyncSender<()>,
        release: Mutex<std::sync::mpsc::Receiver<()>>,
    }

    struct ResolvesToBlockedLookup(Arc<BlockedLookup>);

    impl RuntimeLauncher for ResolvesToBlockedLookup {
        fn resolve(&self) -> Result<Arc<dyn ResolvedRuntimeLauncher>, LaunchError> {
            Ok(Arc::new(self.0.clone()))
        }

        fn owns_private_files(&self) -> bool {
            true
        }
    }

    impl ResolvedRuntimeLauncher for Arc<BlockedLookup> {
        fn endpoint(&self) -> Result<LoopbackOrigin, LaunchError> {
            Ok(LoopbackOrigin::parse(ORIGIN).expect("test origin"))
        }

        fn prepare_launch(&self) {
            let _ = self.started.send(());
            let _ = self.release.lock().expect("release channel").recv();
        }

        fn launch(&self, _hand_over: bool) -> Result<LaunchedRuntime, LaunchError> {
            Ok(LaunchedRuntime {
                pid: 1,
                watch: LaunchWatch::exited(LaunchExit::Started),
            })
        }
    }

    /// The launch reservation covers only preparing and launching: a stop or
    /// a removal requested while a lookup holds it answers without waiting.
    #[tokio::test]
    async fn stop_and_removal_never_wait_on_a_launch_reservation() {
        let (started_tx, started_rx) = std::sync::mpsc::sync_channel(1);
        let (release_tx, release_rx) = std::sync::mpsc::channel();
        let blocked = Arc::new(BlockedLookup {
            started: started_tx,
            release: Mutex::new(release_rx),
        });
        let host = Arc::new(RuntimeHost::new(
            Arc::new(AbsentProbe),
            Arc::new(ResolvesToBlockedLookup(blocked)),
            RuntimeHostSettings {
                origin_override: None,
                ready_timeout: Duration::from_millis(10),
                poll_interval: Duration::from_millis(1),
                probe_timeout: Duration::from_millis(10),
            },
        ));
        let running = tokio::spawn({
            let host = host.clone();
            async move { host.bootstrap(&DiscardStatus, BootstrapTrigger::Launch).await }
        });
        tokio::task::spawn_blocking(move || started_rx.recv())
            .await
            .expect("lookup watcher")
            .expect("the lookup started and holds the reservation");

        let stop = tokio::time::timeout(Duration::from_secs(5), host.stop_owned_runtime()).await;
        assert!(stop.is_ok(), "a stop waited on the launch reservation");
        let removal = tokio::time::timeout(Duration::from_secs(5), host.remove_private_runtime(None)).await;
        assert!(removal.is_ok(), "a removal waited on the launch reservation");

        release_tx.send(()).expect("release the lookup");
        running.await.expect("the bootstrap run finishes");
    }

    /// An attempt seen in flight may finish before the lock is taken. The run
    /// that saw it did not prepare, so it must not launch: launching would run
    /// the login-shell lookup under the launch-state mutex.
    #[test]
    fn only_a_prepared_run_launches_after_an_in_flight_attempt_finishes() {
        let counting = Arc::new(CountingLauncher::default());
        let resolved: Arc<dyn ResolvedRuntimeLauncher> = Arc::new(counting.clone());
        let host = RuntimeHost::new(Arc::new(AbsentProbe), counting.clone(), RuntimeHostSettings::default());
        host.launched_runtime().attempt = Some(LaunchedRuntime {
            pid: 1,
            watch: LaunchWatch::exited(LaunchExit::Started),
        });

        host.launch_if_needed(resolved.clone(), false, false)
            .expect("an unprepared run waits instead");
        assert_eq!(counting.launches.load(Ordering::SeqCst), 0);
        assert_eq!(counting.prepares.load(Ordering::SeqCst), 0);

        host.launch_if_needed(resolved, false, true)
            .expect("a prepared run launches");
        assert_eq!(counting.launches.load(Ordering::SeqCst), 1);
    }

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
