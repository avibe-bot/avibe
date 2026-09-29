//! Behavioural coverage for the bootstrap state machine.
//!
//! Every test drives a real `RuntimeHost` through fake collaborators, so the
//! guarantees under test are the ones the shell actually relies on: adopt what
//! is already running, start what is not, never start twice, never navigate
//! before the Runtime answers, and never stop or delete a Runtime this app did
//! not start.
//!
//! Time is virtual (`start_paused`), so the 120-second production wait is
//! exercised in microseconds.

use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use async_trait::async_trait;
use avibe_runtime_host::deep_link::DeepLinks;
use avibe_runtime_host::{
    bundled_runtime_host, BootstrapLog, BootstrapNoticeCode, BootstrapPhase, BootstrapStatus, BootstrapTrigger,
    CliOutcome, HealthProbe, LaunchError, LaunchExit, LaunchWatch, LaunchedRuntime, LoopbackOrigin, Presence,
    RemovalOutcome, ResolvedRuntimeLauncher, RuntimeHost, RuntimeHostSettings, RuntimeLauncher, StatusSink,
};
use tokio::sync::Notify;

use BootstrapTrigger::{Launch, Recovery, Retry};

const TEST_ORIGIN: &str = "http://127.0.0.1:5123";

fn predecessor() -> String {
    "a".repeat(64)
}

fn bundle_id() -> String {
    "b".repeat(64)
}

fn origin() -> LoopbackOrigin {
    LoopbackOrigin::parse(TEST_ORIGIN).expect("test origin")
}

/// What the origin serves on one probe call.
#[derive(Clone)]
enum Served {
    /// Nothing accepts the connection.
    Nothing,
    /// Something answers, but readiness cannot be proven.
    Starting,
    /// A ready Controller carrying this desktop identity, or none.
    Ready(Option<String>),
    /// The Controller carries this identity, but its UI runs another one.
    UiMismatch(String),
}

/// Serves `script[n]` on probe call `n`, repeating the last entry, and
/// classifies it against the caller's expected identity as `/ready` does.
struct FakeProbe {
    script: Vec<Served>,
    calls: AtomicUsize,
}

impl FakeProbe {
    fn serving(script: impl IntoIterator<Item = Served>) -> Arc<Self> {
        let script: Vec<Served> = script.into_iter().collect();
        assert!(!script.is_empty(), "a probe script serves something");
        Arc::new(Self {
            script,
            calls: AtomicUsize::new(0),
        })
    }

    /// Nothing proves readiness until call `call`, then an untagged Runtime.
    fn healthy_from(call: usize) -> Arc<Self> {
        let mut script = vec![Served::Starting; call.saturating_sub(1)];
        script.push(Served::Ready(None));
        Self::serving(script)
    }

    fn never_healthy() -> Arc<Self> {
        Self::serving([Served::Starting])
    }

    fn calls(&self) -> usize {
        self.calls.load(Ordering::SeqCst)
    }
}

#[async_trait]
impl HealthProbe for FakeProbe {
    async fn presence(&self, _origin: &LoopbackOrigin, expected: Option<&str>) -> Presence {
        let call = self.calls.fetch_add(1, Ordering::SeqCst);
        let served = self.script[call.min(self.script.len() - 1)].clone();
        let classify = |runtime_id: String, ready: bool| {
            if Some(runtime_id.as_str()) == expected {
                Presence::Mine { ready }
            } else {
                Presence::Foreign { runtime_id, ready }
            }
        };
        match served {
            Served::Nothing => Presence::Absent,
            Served::Starting => Presence::Unknown,
            Served::Ready(None) => Presence::Unmanaged,
            Served::Ready(Some(runtime_id)) => classify(runtime_id, true),
            Served::UiMismatch(runtime_id) => classify(runtime_id, false),
        }
    }
}

/// Answers like `inner`, except that probe call `held` answers only once the
/// test releases it: a probe still in flight while the host moves on.
struct HeldProbe {
    inner: Arc<FakeProbe>,
    held: usize,
    in_flight: Notify,
    release: Notify,
}

impl HeldProbe {
    fn holding(held: usize, script: impl IntoIterator<Item = Served>) -> Arc<Self> {
        Arc::new(Self {
            inner: FakeProbe::serving(script),
            held,
            in_flight: Notify::new(),
            release: Notify::new(),
        })
    }
}

#[async_trait]
impl HealthProbe for HeldProbe {
    async fn presence(&self, origin: &LoopbackOrigin, expected: Option<&str>) -> Presence {
        let call = self.inner.calls();
        let presence = self.inner.presence(origin, expected).await;
        if call == self.held {
            self.in_flight.notify_one();
            self.release.notified().await;
        }
        presence
    }
}

/// A lifecycle verb the host ran through the launcher.
#[derive(Debug, Clone, PartialEq, Eq)]
enum Verb {
    Stop { runtime_id: String },
    RemoveBackends,
    RemoveFiles,
    RemoveUnverifiedFiles,
}

struct LauncherState {
    runtime_id: Option<String>,
    private: bool,
    /// The first `failures` launches report no executable.
    failures: usize,
    /// `None` keeps the launch helper running; `Some` has already seen it exit.
    exit: Option<LaunchExit>,
    resolvable: AtomicBool,
    launches: AtomicUsize,
    /// Whether each launch asked `vibe start` to hand over a predecessor.
    handover_requests: Mutex<Vec<bool>>,
    prunes: AtomicUsize,
    verbs: Mutex<Vec<Verb>>,
    stop_outcome: Mutex<CliOutcome>,
    remove_backends_outcome: Mutex<CliOutcome>,
}

#[derive(Clone)]
struct FakeLauncher(Arc<LauncherState>);

impl FakeLauncher {
    fn new(runtime_id: Option<String>, private: bool, failures: usize, exit: Option<LaunchExit>) -> Arc<Self> {
        Arc::new(Self(Arc::new(LauncherState {
            runtime_id,
            private,
            failures,
            exit,
            resolvable: AtomicBool::new(true),
            launches: AtomicUsize::new(0),
            handover_requests: Mutex::new(Vec::new()),
            prunes: AtomicUsize::new(0),
            verbs: Mutex::new(Vec::new()),
            stop_outcome: Mutex::new(CliOutcome::Completed),
            remove_backends_outcome: Mutex::new(CliOutcome::Completed),
        })))
    }

    /// An installed `vibe`: no Runtime identity and no private files.
    fn working() -> Arc<Self> {
        Self::new(None, false, 0, None)
    }

    /// This app's bundled Runtime.
    fn bundled() -> Arc<Self> {
        Self::new(Some(bundle_id()), true, 0, None)
    }

    /// This app's bundled Runtime, whose start helper has already exited.
    fn bundled_exiting(exit: LaunchExit) -> Arc<Self> {
        Self::new(Some(bundle_id()), true, 0, Some(exit))
    }

    fn failing_first(failures: usize) -> Arc<Self> {
        Self::new(None, false, failures, None)
    }

    /// Spawns successfully, then exits non-zero without starting a Runtime —
    /// what an installed `vibe` too old for the shell's arguments does.
    fn dying() -> Arc<Self> {
        Self::new(None, false, 0, Some(LaunchExit::Failed))
    }

    /// The start helper exits zero, but no Runtime ever answers `/ready`.
    fn zero_exit_without_runtime() -> Arc<Self> {
        Self::new(None, false, 0, Some(LaunchExit::Started))
    }

    fn calls(&self) -> usize {
        self.0.launches.load(Ordering::SeqCst)
    }

    fn handover_requests(&self) -> Vec<bool> {
        self.0.handover_requests.lock().expect("launch recorder").clone()
    }

    fn prunes(&self) -> usize {
        self.0.prunes.load(Ordering::SeqCst)
    }

    fn verbs(&self) -> Vec<Verb> {
        self.0.verbs.lock().expect("verb recorder").clone()
    }

    fn stops(&self) -> Vec<String> {
        self.verbs()
            .into_iter()
            .filter_map(|verb| match verb {
                Verb::Stop { runtime_id } => Some(runtime_id),
                _ => None,
            })
            .collect()
    }

    fn answer_stop(&self, outcome: CliOutcome) {
        *self.0.stop_outcome.lock().expect("stop outcome") = outcome;
    }

    fn answer_remove_backends(&self, outcome: CliOutcome) {
        *self.0.remove_backends_outcome.lock().expect("removal outcome") = outcome;
    }

    /// The bundle can no longer be prepared, so no CLI verb can run.
    fn stop_resolving(&self) {
        self.0.resolvable.store(false, Ordering::SeqCst);
    }

    fn record(&self, verb: Verb) {
        self.0.verbs.lock().expect("verb recorder").push(verb);
    }
}

impl RuntimeLauncher for FakeLauncher {
    fn resolve(&self) -> Result<Arc<dyn ResolvedRuntimeLauncher>, LaunchError> {
        if !self.0.resolvable.load(Ordering::SeqCst) {
            return Err(LaunchError::RuntimeInstall);
        }
        Ok(Arc::new(self.clone()))
    }

    fn owns_private_files(&self) -> bool {
        self.0.private
    }

    fn remove_private_files(&self) -> Result<(), LaunchError> {
        self.record(Verb::RemoveFiles);
        Ok(())
    }

    fn remove_unverified_private_files(&self) -> Result<(), LaunchError> {
        self.record(Verb::RemoveUnverifiedFiles);
        Ok(())
    }
}

impl ResolvedRuntimeLauncher for FakeLauncher {
    fn endpoint(&self) -> Result<LoopbackOrigin, LaunchError> {
        Ok(origin())
    }

    fn launch(&self, hand_over: bool) -> Result<LaunchedRuntime, LaunchError> {
        let call = self.0.launches.fetch_add(1, Ordering::SeqCst) + 1;
        self.0
            .handover_requests
            .lock()
            .expect("launch recorder")
            .push(hand_over);
        if call <= self.0.failures {
            return Err(LaunchError::ExecutableNotFound);
        }
        Ok(LaunchedRuntime {
            pid: 4242,
            watch: match self.0.exit {
                Some(exit) => LaunchWatch::exited(exit),
                None => LaunchWatch::default(),
            },
        })
    }

    fn expected_runtime_id(&self) -> Option<&str> {
        self.0.runtime_id.as_deref()
    }

    fn stop(&self, runtime_id: &str) -> CliOutcome {
        self.record(Verb::Stop {
            runtime_id: runtime_id.to_owned(),
        });
        self.0.stop_outcome.lock().expect("stop outcome").clone()
    }

    fn remove_backends(&self) -> CliOutcome {
        self.record(Verb::RemoveBackends);
        self.0.remove_backends_outcome.lock().expect("removal outcome").clone()
    }

    fn prune_superseded(&self) {
        self.0.prunes.fetch_add(1, Ordering::SeqCst);
    }
}

#[derive(Default)]
struct Recorder {
    statuses: Mutex<Vec<BootstrapStatus>>,
}

impl Recorder {
    fn statuses(&self) -> Vec<BootstrapStatus> {
        self.statuses.lock().expect("recorder is not poisoned").clone()
    }

    fn phases(&self) -> Vec<BootstrapPhase> {
        self.statuses().iter().map(|status| status.phase).collect()
    }
}

impl StatusSink for Recorder {
    fn publish(&self, status: BootstrapStatus) {
        self.statuses.lock().expect("recorder is not poisoned").push(status);
    }
}

/// Production defaults, minus the wall-clock wait: the shell polls every 500ms
/// for two seconds, which under virtual time is five probes.
fn fast_settings() -> RuntimeHostSettings {
    RuntimeHostSettings {
        ready_timeout: Duration::from_secs(2),
        ..RuntimeHostSettings::default()
    }
}

fn immediate_timeout_settings() -> RuntimeHostSettings {
    RuntimeHostSettings {
        ready_timeout: Duration::ZERO,
        ..RuntimeHostSettings::default()
    }
}

fn runtime_host(probe: Arc<FakeProbe>, launcher: Arc<FakeLauncher>, settings: RuntimeHostSettings) -> RuntimeHost {
    RuntimeHost::new(probe, launcher, settings)
}

async fn boot(host: &RuntimeHost, trigger: BootstrapTrigger) -> BootstrapStatus {
    host.bootstrap(&Recorder::default(), trigger).await
}

#[tokio::test(start_paused = true)]
async fn a_cold_link_is_consumed_only_after_ready_navigation_commits_and_never_replayed() {
    let launcher = FakeLauncher::working();
    let host = runtime_host(FakeProbe::healthy_from(3), launcher.clone(), fast_settings());
    let recorder = Recorder::default();
    let mut links = DeepLinks::default();
    links.receive(["avibe", "avibe://show/cold.session"]);
    let ready = host.bootstrap(&recorder, Launch).await;
    for status in recorder
        .statuses()
        .iter()
        .filter(|status| status.phase != BootstrapPhase::Ready)
    {
        assert!(links.bootstrap_navigation(status).is_none());
    }
    assert_eq!(ready.phase, BootstrapPhase::Ready);
    let destination = links.bootstrap_navigation(&ready).unwrap();
    assert_eq!(
        destination.url().as_str(),
        format!("{}/apps/show/cold.session", ready.origin)
    );
    assert_eq!(links.bootstrap_navigation(&ready).unwrap().url(), destination.url());
    assert!(links.commit_navigation(&destination, true, 1, 1));
    assert_eq!(links.bootstrap_navigation(&ready).unwrap().url().path(), "/");
    assert_eq!(launcher.calls(), 1);
}

#[tokio::test(start_paused = true)]
async fn bootstrap_failure_discards_the_link_without_navigating_or_replaying_on_retry() {
    let host = runtime_host(FakeProbe::never_healthy(), FakeLauncher::working(), fast_settings());
    let recorder = Recorder::default();
    let mut links = DeepLinks::default();
    links.receive(["avibe://session/failed"]);
    let failed = host.bootstrap(&recorder, Launch).await;
    assert_eq!(failed.phase, BootstrapPhase::Failed);
    assert!(links.bootstrap_navigation(&failed).is_none());
    links.receive(["avibe://session/arrived-after-failure"]);
    let healthy = RuntimeHost::new(FakeProbe::healthy_from(1), FakeLauncher::working(), fast_settings());
    let ready = healthy.bootstrap(&recorder, Retry).await;
    assert_eq!(links.bootstrap_navigation(&ready).unwrap().url().path(), "/");
}

#[tokio::test(start_paused = true)]
async fn a_ready_link_survives_failed_handoffs_until_the_current_window_accepts_it() {
    let host = runtime_host(FakeProbe::healthy_from(1), FakeLauncher::working(), fast_settings());
    let mut links = DeepLinks::default();
    links.receive(["avibe://session/retry"]);
    let ready = boot(&host, Launch).await;
    let origin = LoopbackOrigin::parse(&ready.origin).unwrap();
    let first = links.bootstrap_navigation(&ready).unwrap();
    assert!(!links.commit_navigation(&first, false, 1, 1));
    let handoff_failed = BootstrapStatus::failed(
        &origin,
        ready.attempt,
        avibe_runtime_host::BootstrapNotice::new(BootstrapNoticeCode::WorkbenchNavigationFailed),
        true,
    );
    assert!(links.bootstrap_navigation(&handoff_failed).is_none());
    let retried = boot(&host, Retry).await;
    let replacement = links.bootstrap_navigation(&retried).unwrap();
    assert_eq!(replacement.url(), first.url());
    assert!(!links.commit_navigation(&replacement, true, 1, 2));
    let current = links.bootstrap_navigation(&retried).unwrap();
    assert_eq!(current.url(), first.url());
    assert!(links.commit_navigation(&current, true, 2, 2));
    assert_eq!(links.bootstrap_navigation(&retried).unwrap().url().path(), "/");
}

#[tokio::test(start_paused = true)]
async fn an_already_running_runtime_is_adopted_without_starting_anything() {
    let probe = FakeProbe::healthy_from(1);
    let launcher = FakeLauncher::bundled();
    let host = runtime_host(probe.clone(), launcher.clone(), fast_settings());
    let recorder = Recorder::default();

    let status = host.bootstrap(&recorder, Launch).await;

    assert_eq!(status.phase, BootstrapPhase::Ready);
    assert_eq!(status.notice.code, BootstrapNoticeCode::Adopted);
    assert_eq!(status.origin, TEST_ORIGIN);
    assert_eq!(status.attempt, 1);
    assert_eq!(launcher.calls(), 0, "an adopted Runtime must never be re-started");
    assert_eq!(probe.calls(), 1);
    assert_eq!(recorder.phases(), vec![BootstrapPhase::Probing, BootstrapPhase::Ready]);
    assert!(!host.has_launched());
    assert!(!host.has_owned_runtime(), "an untagged Runtime is never this app's");
    assert!(launcher.verbs().is_empty(), "an untagged Runtime is never stopped");
    assert_eq!(launcher.prunes(), 0);
}

#[tokio::test(start_paused = true)]
async fn this_apps_running_runtime_is_adopted_as_its_own_and_superseded_installs_are_pruned() {
    let launcher = FakeLauncher::bundled();
    let host = runtime_host(
        FakeProbe::serving([Served::Ready(Some(bundle_id()))]),
        launcher.clone(),
        fast_settings(),
    );

    let status = boot(&host, Launch).await;

    assert_eq!(status.phase, BootstrapPhase::Ready);
    assert_eq!(status.notice.code, BootstrapNoticeCode::Adopted);
    assert_eq!(launcher.calls(), 0);
    assert!(launcher.verbs().is_empty());
    assert_eq!(launcher.prunes(), 1);
    assert!(
        host.has_owned_runtime(),
        "a Runtime carrying this app's identity is stoppable even when an earlier shell started it"
    );
}

#[tokio::test(start_paused = true)]
async fn this_apps_runtime_with_a_stale_ui_is_restarted_rather_than_handed_over() {
    let launcher = FakeLauncher::bundled();
    let host = runtime_host(
        FakeProbe::serving([Served::UiMismatch(bundle_id()), Served::Ready(Some(bundle_id()))]),
        launcher.clone(),
        fast_settings(),
    );

    let status = boot(&host, Launch).await;

    assert_eq!(status.phase, BootstrapPhase::Ready);
    assert_eq!(status.notice.code, BootstrapNoticeCode::Ready);
    assert_eq!(launcher.calls(), 1, "`vibe start` replaces the stale UI");
    assert!(
        launcher.verbs().is_empty(),
        "this app's own Controller is never handed over"
    );
}

#[tokio::test(start_paused = true)]
async fn launch_and_retry_ask_this_apps_start_to_hand_over_a_desktop_predecessor() {
    for trigger in [Launch, Retry] {
        for predecessor_state in [Served::Ready(Some(predecessor())), Served::UiMismatch(predecessor())] {
            let launcher = FakeLauncher::bundled();
            let host = runtime_host(
                FakeProbe::serving([predecessor_state, Served::Starting, Served::Ready(Some(bundle_id()))]),
                launcher.clone(),
                fast_settings(),
            );

            let status = boot(&host, trigger).await;

            assert_eq!(status.phase, BootstrapPhase::Ready, "{trigger:?}");
            assert_eq!(status.notice.code, BootstrapNoticeCode::Ready);
            assert_eq!(
                launcher.handover_requests(),
                [true],
                "`vibe start` replaces the predecessor"
            );
            assert!(launcher.verbs().is_empty(), "the host itself never stops a predecessor");
            assert_eq!(launcher.prunes(), 1);
            assert!(host.has_owned_runtime());
        }
    }
}

#[tokio::test(start_paused = true)]
async fn a_predecessor_still_serving_after_this_apps_start_exited_is_never_stopped() {
    // Found on the first probe, and while polling after `vibe start` settled.
    for script in [
        vec![Served::Ready(Some(predecessor()))],
        vec![Served::Nothing, Served::Ready(Some(predecessor()))],
    ] {
        let launcher = FakeLauncher::bundled_exiting(LaunchExit::Started);
        let host = runtime_host(FakeProbe::serving(script), launcher.clone(), fast_settings());

        let status = boot(&host, Launch).await;

        assert_eq!(status.phase, BootstrapPhase::Failed);
        assert_eq!(status.notice.code, BootstrapNoticeCode::RuntimeOwnershipLost);
        assert!(status.retryable);
        assert!(launcher.verbs().is_empty());
        assert_eq!(
            launcher.calls(),
            1,
            "a predecessor that stayed is not a reason to start again"
        );
        assert!(!host.has_owned_runtime());
    }
}

#[tokio::test(start_paused = true)]
async fn a_predecessor_is_never_judged_while_this_apps_start_helper_still_runs() {
    let launcher = FakeLauncher::bundled();
    let host = runtime_host(
        FakeProbe::serving([Served::Nothing, Served::Ready(Some(predecessor()))]),
        launcher.clone(),
        fast_settings(),
    );

    // Found while polling, and again on the Retry's first probe.
    for trigger in [Launch, Retry] {
        let status = boot(&host, trigger).await;

        assert_eq!(status.phase, BootstrapPhase::Failed, "{trigger:?}");
        assert_eq!(status.notice.code, BootstrapNoticeCode::ReadyTimeout);
        assert!(status.retryable);
        assert!(
            launcher.verbs().is_empty(),
            "the running helper may still be replacing the predecessor"
        );
        assert_eq!(launcher.calls(), 1, "a second helper must never overlap the first");
    }
}

#[tokio::test(start_paused = true)]
async fn recovery_never_asks_to_hand_over_a_foreign_runtime() {
    let launcher = FakeLauncher::bundled();
    let host = runtime_host(
        FakeProbe::serving([Served::Ready(Some(predecessor()))]),
        launcher.clone(),
        fast_settings(),
    );

    let status = boot(&host, Recovery).await;

    assert_eq!(status.phase, BootstrapPhase::Failed);
    assert_eq!(status.notice.code, BootstrapNoticeCode::RuntimeOwnershipLost);
    assert!(status.retryable, "the user's Try again may hand over");
    assert_eq!(launcher.calls(), 0);
    assert!(launcher.verbs().is_empty());

    // Nothing answered, so recovery starts this app's Runtime, but its `vibe
    // start` may not replace a predecessor it then finds, and declines.
    let launcher = FakeLauncher::bundled_exiting(LaunchExit::HandoverRefused);
    let host = runtime_host(
        FakeProbe::serving([Served::Nothing, Served::Ready(Some(predecessor()))]),
        launcher.clone(),
        fast_settings(),
    );

    let status = boot(&host, Recovery).await;

    assert_eq!(status.phase, BootstrapPhase::Failed);
    assert_eq!(status.notice.code, BootstrapNoticeCode::RuntimeOwnershipLost);
    assert!(status.retryable);
    assert_eq!(
        launcher.handover_requests(),
        [false],
        "recovery must not let `vibe start` stop another shell's Runtime"
    );
    assert!(launcher.verbs().is_empty());
    assert!(!host.has_owned_runtime());
}

#[tokio::test(start_paused = true)]
async fn a_start_that_did_not_replace_its_predecessor_fails_closed_by_its_exit() {
    for (exit, notice) in [
        (LaunchExit::HandoverRefused, BootstrapNoticeCode::RuntimeOwnershipLost),
        (LaunchExit::HandoverFailed, BootstrapNoticeCode::RuntimeStopFailed),
        (LaunchExit::Failed, BootstrapNoticeCode::LauncherExited),
    ] {
        let launcher = FakeLauncher::bundled_exiting(exit);
        let host = runtime_host(
            FakeProbe::serving([Served::Ready(Some(predecessor()))]),
            launcher.clone(),
            fast_settings(),
        );

        let status = boot(&host, Launch).await;

        assert_eq!(status.phase, BootstrapPhase::Failed, "{exit:?}");
        assert_eq!(status.notice.code, notice, "{exit:?}");
        assert!(status.retryable);
        assert_eq!(launcher.handover_requests(), [true]);
        assert!(launcher.verbs().is_empty(), "{exit:?}");
        assert!(!host.has_launched(), "{exit:?} releases the retry slot");
    }
}

#[tokio::test(start_paused = true)]
async fn a_retry_that_finds_a_failed_start_reports_it_and_leaves_the_predecessor_alone() {
    // The helper's non-zero exit landed after the first run timed out.
    let launcher = FakeLauncher::bundled_exiting(LaunchExit::Failed);
    let host = runtime_host(
        FakeProbe::serving([Served::Nothing, Served::Ready(Some(predecessor()))]),
        launcher.clone(),
        immediate_timeout_settings(),
    );
    assert_eq!(boot(&host, Launch).await.notice.code, BootstrapNoticeCode::ReadyTimeout);
    assert!(host.has_launched());

    let status = boot(&host, Retry).await;

    assert_eq!(status.phase, BootstrapPhase::Failed);
    assert_eq!(status.notice.code, BootstrapNoticeCode::LauncherExited);
    assert!(
        launcher.verbs().is_empty(),
        "a predecessor is never stopped on the way to a failure"
    );
    assert_eq!(launcher.calls(), 1);
}

#[tokio::test(start_paused = true)]
async fn a_shell_without_a_runtime_identity_adopts_a_desktop_runtime_it_can_never_stop() {
    let launcher = FakeLauncher::working();
    let host = runtime_host(
        FakeProbe::serving([Served::Ready(Some(predecessor()))]),
        launcher.clone(),
        fast_settings(),
    );

    let status = boot(&host, Launch).await;

    assert_eq!(status.phase, BootstrapPhase::Ready);
    assert_eq!(status.notice.code, BootstrapNoticeCode::Adopted);
    assert!(!host.has_owned_runtime());
    assert_eq!(
        host.stop_owned_runtime().await,
        CliOutcome::Refused {
            reason: "not_owned".to_owned()
        }
    );
    assert!(launcher.verbs().is_empty());
    assert_eq!(launcher.calls(), 0);
}

#[tokio::test(start_paused = true)]
async fn an_absent_runtime_is_started_and_adopted_once_it_answers() {
    let probe = FakeProbe::healthy_from(3);
    let launcher = FakeLauncher::working();
    let host = runtime_host(probe.clone(), launcher.clone(), fast_settings());
    let recorder = Recorder::default();

    let status = host.bootstrap(&recorder, Launch).await;

    assert_eq!(status.phase, BootstrapPhase::Ready);
    assert_eq!(launcher.calls(), 1, "exactly one Runtime is started");
    assert!(host.has_launched());
    assert_eq!(
        recorder.phases(),
        vec![
            BootstrapPhase::Probing,
            BootstrapPhase::Starting,
            BootstrapPhase::Starting,
            BootstrapPhase::Ready,
        ]
    );
}

#[tokio::test(start_paused = true)]
async fn a_helper_adopts_untagged_readiness_after_launch_without_handover_pruning_or_removal() {
    // The first probe misses, so the bundled helper is started and exits. The
    // next probe is an external Controller without a desktop identity.
    let launcher = FakeLauncher::bundled_exiting(LaunchExit::Started);
    let host = runtime_host(
        FakeProbe::serving([Served::Nothing, Served::Ready(None)]),
        launcher.clone(),
        fast_settings(),
    );

    let status = boot(&host, Launch).await;

    assert_eq!(status.phase, BootstrapPhase::Ready);
    assert_eq!(status.notice.code, BootstrapNoticeCode::Adopted);
    assert_eq!(launcher.calls(), 1);
    assert_eq!(launcher.prunes(), 0);
    assert!(!host.has_owned_runtime());

    // The helper may have created the UI from the private tree even though the
    // adopted Controller is external, so its files are kept.
    assert_eq!(host.remove_private_runtime(Some(&origin())).await, RemovalOutcome::Kept);
    assert!(launcher.verbs().is_empty());
}

#[tokio::test(start_paused = true)]
async fn the_monitor_serves_only_the_adopted_presence_and_rereads_the_owner_on_every_probe() {
    // Another shell's Runtime replaced this app's: serving ends, and so does
    // the authority to stop anything.
    let launcher = FakeLauncher::bundled();
    let host = runtime_host(
        FakeProbe::serving([
            Served::Ready(Some(bundle_id())),
            Served::Ready(Some(bundle_id())),
            Served::Ready(Some(predecessor())),
        ]),
        launcher.clone(),
        fast_settings(),
    );
    boot(&host, Launch).await;
    assert!(host.is_serving(&origin()).await);
    assert!(host.has_owned_runtime());
    assert!(!host.is_serving(&origin()).await);
    assert!(!host.has_owned_runtime());
    assert_eq!(
        host.stop_owned_runtime().await,
        CliOutcome::Refused {
            reason: "not_owned".to_owned()
        }
    );
    assert!(launcher.verbs().is_empty());

    // A probe that proves nothing is not a change of hands.
    let launcher = FakeLauncher::bundled();
    let host = runtime_host(
        FakeProbe::serving([
            Served::Ready(Some(bundle_id())),
            Served::Starting,
            Served::Ready(Some(bundle_id())),
        ]),
        launcher.clone(),
        fast_settings(),
    );
    boot(&host, Launch).await;
    assert!(!host.is_serving(&origin()).await);
    assert!(host.has_owned_runtime());
    assert_eq!(host.stop_owned_runtime().await, CliOutcome::Completed);
    assert_eq!(launcher.stops(), [bundle_id()]);
}

#[tokio::test(start_paused = true)]
async fn a_monitor_probe_overtaken_by_a_new_run_or_a_stop_never_rewrites_ownership() {
    // Window recreation: the monitor's probe found nothing, and a new run
    // adopted this app's Runtime before that answer landed.
    let launcher = FakeLauncher::bundled();
    let probe = HeldProbe::holding(
        1,
        [
            Served::Ready(Some(bundle_id())),
            Served::Nothing,
            Served::Ready(Some(bundle_id())),
        ],
    );
    let host = Arc::new(RuntimeHost::new(probe.clone(), launcher.clone(), fast_settings()));
    boot(&host, Launch).await;
    let monitor = tokio::spawn({
        let host = host.clone();
        async move { host.is_serving(&origin()).await }
    });
    probe.in_flight.notified().await;
    assert_eq!(boot(&host, Recovery).await.notice.code, BootstrapNoticeCode::Adopted);
    probe.release.notify_one();
    monitor.await.expect("monitor probe");
    assert!(
        host.has_owned_runtime(),
        "a stale Absent must not revoke the new run's stop authority"
    );

    // Stop: the monitor's probe found this app's Runtime serving, and the stop
    // completed before that answer landed.
    let launcher = FakeLauncher::bundled();
    let probe = HeldProbe::holding(1, [Served::Ready(Some(bundle_id()))]);
    let host = Arc::new(RuntimeHost::new(probe.clone(), launcher.clone(), fast_settings()));
    boot(&host, Launch).await;
    let monitor = tokio::spawn({
        let host = host.clone();
        async move { host.is_serving(&origin()).await }
    });
    probe.in_flight.notified().await;
    assert_eq!(host.stop_owned_runtime().await, CliOutcome::Completed);
    probe.release.notify_one();
    monitor.await.expect("monitor probe");
    assert!(
        !host.has_owned_runtime(),
        "a stale Mine must not restore stop authority over a stopped Runtime"
    );
    // Nor the evidence that a Runtime may still live: with no origin to ask,
    // uninstall leaves the choice to the user instead of keeping every file.
    launcher.stop_resolving();
    assert_eq!(host.remove_private_runtime(None).await, RemovalOutcome::Unverified);
    assert_eq!(launcher.stops(), [bundle_id()]);
}

#[tokio::test(start_paused = true)]
async fn a_host_without_an_adoption_is_never_serving() {
    let host = runtime_host(FakeProbe::healthy_from(1), FakeLauncher::working(), fast_settings());
    assert!(!host.is_serving(&origin()).await);
}

#[tokio::test(start_paused = true)]
async fn stop_reaches_only_this_apps_runtime_and_takes_its_tunnel_down() {
    let launcher = FakeLauncher::bundled();
    let host = runtime_host(
        FakeProbe::serving([Served::Ready(Some(bundle_id()))]),
        launcher.clone(),
        fast_settings(),
    );
    boot(&host, Launch).await;

    assert_eq!(host.stop_owned_runtime().await, CliOutcome::Completed);
    assert_eq!(launcher.stops(), [bundle_id()]);
    assert!(!host.has_owned_runtime());
    assert_eq!(
        host.stop_owned_runtime().await,
        CliOutcome::Refused {
            reason: "not_owned".to_owned()
        },
        "a stopped Runtime is not stopped again"
    );
    assert_eq!(launcher.stops().len(), 1);

    for served in [Served::Ready(None), Served::Nothing, Served::Starting] {
        let launcher = FakeLauncher::bundled();
        let host = runtime_host(
            FakeProbe::serving([served]),
            launcher.clone(),
            immediate_timeout_settings(),
        );
        boot(&host, Launch).await;
        assert!(!host.has_owned_runtime());
        assert_eq!(
            host.stop_owned_runtime().await,
            CliOutcome::Refused {
                reason: "not_owned".to_owned()
            }
        );
        assert!(launcher.stops().is_empty());
    }
}

#[tokio::test(start_paused = true)]
async fn a_refused_stop_revokes_authority_and_an_unfinished_one_keeps_it() {
    let refused = CliOutcome::Refused {
        reason: "runtime_id_mismatch".to_owned(),
    };
    let launcher = FakeLauncher::bundled();
    let host = runtime_host(
        FakeProbe::serving([Served::Ready(Some(bundle_id()))]),
        launcher.clone(),
        fast_settings(),
    );
    boot(&host, Launch).await;
    launcher.answer_stop(refused.clone());
    assert_eq!(host.stop_owned_runtime().await, refused);
    assert!(!host.has_owned_runtime());
    assert_eq!(
        host.stop_owned_runtime().await,
        CliOutcome::Refused {
            reason: "not_owned".to_owned()
        }
    );
    assert_eq!(launcher.stops().len(), 1);
    // A refusal does not prove this app's Runtime gone, so uninstall without
    // an origin to ask keeps the files.
    launcher.stop_resolving();
    assert_eq!(host.remove_private_runtime(None).await, RemovalOutcome::Kept);
    assert_eq!(launcher.stops().len(), 1);

    for unfinished in [
        CliOutcome::Failed {
            part: "service".to_owned(),
        },
        CliOutcome::Unrunnable,
    ] {
        let launcher = FakeLauncher::bundled();
        let host = runtime_host(
            FakeProbe::serving([Served::Ready(Some(bundle_id()))]),
            launcher.clone(),
            fast_settings(),
        );
        boot(&host, Launch).await;
        launcher.answer_stop(unfinished.clone());
        assert_eq!(host.stop_owned_runtime().await, unfinished);
        assert!(host.has_owned_runtime(), "{unfinished:?} keeps the Runtime stoppable");
        launcher.answer_stop(CliOutcome::Completed);
        assert_eq!(host.stop_owned_runtime().await, CliOutcome::Completed);
        assert_eq!(launcher.stops(), [bundle_id(), bundle_id()]);
    }
}

#[tokio::test(start_paused = true)]
async fn neither_stop_nor_uninstall_overlaps_a_launch_that_is_still_running() {
    let launcher = FakeLauncher::bundled();
    let host = runtime_host(
        FakeProbe::serving([Served::Nothing, Served::Ready(Some(bundle_id()))]),
        launcher.clone(),
        fast_settings(),
    );
    assert_eq!(boot(&host, Launch).await.notice.code, BootstrapNoticeCode::Ready);
    assert!(host.has_launched() && host.has_owned_runtime());

    assert_eq!(
        host.stop_owned_runtime().await,
        CliOutcome::Failed {
            part: "launch_pending".to_owned()
        }
    );
    assert_eq!(host.remove_private_runtime(Some(&origin())).await, RemovalOutcome::Kept);
    assert!(launcher.verbs().is_empty());
}

#[tokio::test(start_paused = true)]
async fn uninstall_stops_this_apps_runtime_then_removes_backends_then_its_files() {
    for served in [
        Served::Ready(Some(bundle_id())),
        Served::UiMismatch(bundle_id()),
        Served::Nothing,
    ] {
        let launcher = FakeLauncher::bundled();
        let host = runtime_host(FakeProbe::serving([served]), launcher.clone(), fast_settings());

        assert_eq!(
            host.remove_private_runtime(Some(&origin())).await,
            RemovalOutcome::Removed
        );
        assert_eq!(
            launcher.verbs(),
            [
                Verb::Stop {
                    runtime_id: bundle_id()
                },
                Verb::RemoveBackends,
                Verb::RemoveFiles,
            ]
        );
        assert_eq!(launcher.calls(), 0, "uninstall never starts a Runtime");
    }
}

#[tokio::test(start_paused = true)]
async fn uninstall_keeps_every_file_while_another_runtime_may_use_them() {
    for served in [
        Served::Ready(Some(predecessor())),
        Served::UiMismatch(predecessor()),
        Served::Ready(None),
        Served::Starting,
    ] {
        let launcher = FakeLauncher::bundled();
        let host = runtime_host(FakeProbe::serving([served]), launcher.clone(), fast_settings());

        assert_eq!(host.remove_private_runtime(Some(&origin())).await, RemovalOutcome::Kept);
        assert_eq!(
            host.remove_unverified_private_runtime(Some(&origin())).await,
            RemovalOutcome::Kept,
            "Delete anyway is never offered past a Runtime that answers"
        );
        assert!(launcher.verbs().is_empty(), "nothing is stopped or deleted");
    }
}

#[tokio::test(start_paused = true)]
async fn uninstall_keeps_every_file_when_this_apps_runtime_does_not_confirm_its_stop() {
    for (served, stop, removal) in [
        (
            Served::Ready(Some(bundle_id())),
            CliOutcome::Refused {
                reason: "runtime_id_mismatch".to_owned(),
            },
            RemovalOutcome::Kept,
        ),
        (
            Served::Ready(Some(bundle_id())),
            CliOutcome::Failed {
                part: "service".to_owned(),
            },
            RemovalOutcome::Kept,
        ),
        (
            Served::Ready(Some(bundle_id())),
            CliOutcome::Unrunnable,
            RemovalOutcome::Kept,
        ),
        (Served::Nothing, CliOutcome::Unrunnable, RemovalOutcome::Unverified),
        (
            Served::Nothing,
            CliOutcome::Failed {
                part: "remote_access".to_owned(),
            },
            RemovalOutcome::Kept,
        ),
    ] {
        let launcher = FakeLauncher::bundled();
        launcher.answer_stop(stop.clone());
        let host = runtime_host(FakeProbe::serving([served]), launcher.clone(), fast_settings());

        assert_eq!(host.remove_private_runtime(Some(&origin())).await, removal, "{stop:?}");
        assert_eq!(
            launcher.verbs(),
            [Verb::Stop {
                runtime_id: bundle_id()
            }],
            "{stop:?} deletes nothing"
        );
    }
}

#[tokio::test(start_paused = true)]
async fn uninstall_deletes_the_bundle_only_after_every_backend_install_was_claimed() {
    for (backends, removal) in [
        (
            CliOutcome::Refused {
                reason: "install_locked".to_owned(),
            },
            RemovalOutcome::Kept,
        ),
        (
            CliOutcome::Failed {
                part: "backend_removal_failed".to_owned(),
            },
            RemovalOutcome::Failed,
        ),
        (CliOutcome::Unrunnable, RemovalOutcome::Unverified),
    ] {
        let launcher = FakeLauncher::bundled();
        launcher.answer_remove_backends(backends.clone());
        let host = runtime_host(
            FakeProbe::serving([Served::Ready(Some(bundle_id())), Served::Nothing]),
            launcher.clone(),
            fast_settings(),
        );

        assert_eq!(
            host.remove_private_runtime(Some(&origin())).await,
            removal,
            "{backends:?}"
        );
        assert_eq!(
            launcher.verbs(),
            [
                Verb::Stop {
                    runtime_id: bundle_id()
                },
                Verb::RemoveBackends,
            ],
            "{backends:?} keeps the bundle"
        );
        assert!(!host.has_owned_runtime(), "the stopped Runtime is no longer stoppable");
    }
}

#[tokio::test(start_paused = true)]
async fn delete_anyway_rechecks_the_origin_and_deletes_only_while_nothing_answers() {
    let launcher = FakeLauncher::bundled();
    launcher.answer_stop(CliOutcome::Unrunnable);
    let host = runtime_host(
        FakeProbe::serving([Served::Nothing, Served::Nothing, Served::Ready(None)]),
        launcher.clone(),
        fast_settings(),
    );
    assert_eq!(
        host.remove_private_runtime(Some(&origin())).await,
        RemovalOutcome::Unverified
    );
    assert_eq!(
        host.remove_unverified_private_runtime(Some(&origin())).await,
        RemovalOutcome::Removed
    );
    assert_eq!(
        host.remove_unverified_private_runtime(Some(&origin())).await,
        RemovalOutcome::Kept,
        "a Runtime that answers again keeps the files"
    );
    assert_eq!(
        launcher.verbs(),
        [
            Verb::Stop {
                runtime_id: bundle_id()
            },
            Verb::RemoveUnverifiedFiles,
        ]
    );
}

#[tokio::test(start_paused = true)]
async fn uninstall_without_an_origin_keeps_the_files_while_a_launched_runtime_may_live() {
    // A never-launched shell whose bundle cannot run has nothing that could
    // still be installing, so only the user's choice is left.
    let launcher = FakeLauncher::bundled();
    launcher.stop_resolving();
    let host = runtime_host(
        FakeProbe::never_healthy(),
        launcher.clone(),
        immediate_timeout_settings(),
    );
    assert_eq!(host.remove_private_runtime(None).await, RemovalOutcome::Unverified);
    assert!(launcher.verbs().is_empty());

    // Once this shell has spawned a helper, a Runtime may be alive whatever the
    // helper reported, so without an origin to ask every file is kept.
    for exit in [LaunchExit::Started, LaunchExit::Failed] {
        let launcher = FakeLauncher::bundled_exiting(exit);
        let host = runtime_host(
            FakeProbe::never_healthy(),
            launcher.clone(),
            immediate_timeout_settings(),
        );
        boot(&host, Launch).await;
        boot(&host, Retry).await;
        assert!(!host.has_launched(), "{exit:?} released the retry slot");
        launcher.stop_resolving();
        assert_eq!(
            host.remove_private_runtime(None).await,
            RemovalOutcome::Kept,
            "{exit:?}"
        );
        assert_eq!(
            host.remove_unverified_private_runtime(None).await,
            RemovalOutcome::Kept,
            "{exit:?}"
        );
        assert!(launcher.verbs().is_empty(), "{exit:?}");
    }
}

#[tokio::test]
async fn uninstall_of_an_installed_runtime_changes_nothing() {
    let launcher = FakeLauncher::working();
    let host = runtime_host(FakeProbe::serving([Served::Nothing]), launcher.clone(), fast_settings());
    assert_eq!(
        host.remove_private_runtime(Some(&origin())).await,
        RemovalOutcome::NotPrivate
    );
    assert_eq!(
        host.remove_unverified_private_runtime(Some(&origin())).await,
        RemovalOutcome::NotPrivate
    );
    assert!(launcher.verbs().is_empty());
}

/// A bundle that cannot be prepared cannot run its CLI to prove its installs
/// stopped. The real bundled host keeps every file until the user chooses to
/// delete them anyway, and never touches anything outside its own roots.
#[tokio::test]
async fn a_broken_bundle_keeps_its_files_until_the_user_deletes_them_anyway() {
    let root = temp_root("broken-bundle");
    let installs = root.join("runtime");
    let backends = root.join("backends");
    std::fs::create_dir_all(installs.join("1.0.0")).unwrap();
    std::fs::create_dir_all(backends.join("opencode")).unwrap();
    std::fs::write(installs.join("1.0.0").join("python"), b"runtime bytes").unwrap();
    std::fs::write(backends.join("opencode").join("bin"), b"backend bytes").unwrap();
    std::fs::write(root.join("unrelated"), b"unrelated state").unwrap();
    let host = bundled_runtime_host(
        root.join("missing-bundle"),
        installs.clone(),
        backends.clone(),
        BootstrapLog::disabled(),
    )
    .expect("the bundled host builds");

    assert_eq!(host.remove_private_runtime(None).await, RemovalOutcome::Unverified);
    assert!(installs.join("1.0.0").join("python").exists());
    assert!(backends.join("opencode").join("bin").exists());

    assert_eq!(
        host.remove_unverified_private_runtime(None).await,
        RemovalOutcome::Removed
    );
    assert!(!installs.exists() && !backends.exists());
    assert_eq!(std::fs::read(root.join("unrelated")).unwrap(), b"unrelated state");
    std::fs::remove_dir_all(root).unwrap();
}

fn temp_root(label: &str) -> PathBuf {
    let unique = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .expect("clock after epoch")
        .as_nanos();
    let root = std::env::temp_dir().join(format!("avibe-host-{label}-{}-{unique}", std::process::id()));
    std::fs::create_dir_all(&root).unwrap();
    root
}

#[tokio::test(start_paused = true)]
async fn the_shell_learns_of_readiness_only_after_the_runtime_answers() {
    let probe = FakeProbe::healthy_from(4);
    let launcher = FakeLauncher::working();
    let host = runtime_host(probe.clone(), launcher.clone(), fast_settings());
    let recorder = Recorder::default();

    let status = host.bootstrap(&recorder, Launch).await;
    let statuses = recorder.statuses();

    // Ready is emitted once, last, and only on the probe call that succeeded.
    assert_eq!(statuses.iter().filter(|s| s.phase == BootstrapPhase::Ready).count(), 1);
    assert_eq!(statuses.last().map(|s| s.phase), Some(BootstrapPhase::Ready));
    assert_eq!(status.attempt as usize, probe.calls());
    assert!(status.phase.is_terminal());
    assert_eq!(
        Some(&status),
        statuses.last(),
        "the returned status is the published one"
    );
}

#[tokio::test(start_paused = true)]
async fn a_runtime_that_never_answers_fails_with_a_retryable_timeout() {
    let probe = FakeProbe::never_healthy();
    let launcher = FakeLauncher::bundled();
    let host = runtime_host(probe.clone(), launcher.clone(), fast_settings());
    let recorder = Recorder::default();

    let status = host.bootstrap(&recorder, Launch).await;

    assert_eq!(status.phase, BootstrapPhase::Failed);
    assert!(status.retryable, "a slow machine deserves another try");
    assert_eq!(status.notice.code, BootstrapNoticeCode::ReadyTimeout);
    assert_eq!(status.notice.seconds, Some(2));
    assert!(status.attempt > 1, "the wait is polled, not a single shot");
    assert_eq!(launcher.calls(), 1);
    assert!(launcher.verbs().is_empty(), "an unprovable Runtime is never stopped");
    assert!(
        !recorder.phases().contains(&BootstrapPhase::Ready),
        "a timed-out run must never report readiness"
    );
}

#[tokio::test(start_paused = true)]
async fn retrying_after_a_timeout_never_starts_a_second_runtime() {
    // Call 6 is the retry's first probe; the Runtime answers on call 7, after the
    // first run has already given up.
    let probe = FakeProbe::healthy_from(7);
    let launcher = FakeLauncher::working();
    let host = runtime_host(probe.clone(), launcher.clone(), fast_settings());

    let first = boot(&host, Launch).await;
    assert_eq!(first.phase, BootstrapPhase::Failed);
    assert_eq!(launcher.calls(), 1);

    let recorder = Recorder::default();
    let second = host.bootstrap(&recorder, Retry).await;

    assert_eq!(second.phase, BootstrapPhase::Ready);
    assert_eq!(
        launcher.calls(),
        1,
        "the retry must adopt the Runtime it already started"
    );
    assert!(
        recorder.phases().contains(&BootstrapPhase::Starting),
        "the retry still waits on the Runtime it started earlier"
    );
}

#[tokio::test(start_paused = true)]
async fn a_launcher_failure_observed_after_timeout_is_rechecked_on_retry() {
    let probe = FakeProbe::never_healthy();
    let launcher = FakeLauncher::dying();
    let host = runtime_host(probe, launcher.clone(), immediate_timeout_settings());

    // The zero-length budget expires before the polling loop can inspect the
    // watch, matching a real launcher whose non-zero exit lands just after a
    // normal timeout.
    let first = boot(&host, Launch).await;
    assert_eq!(first.phase, BootstrapPhase::Failed);
    assert_eq!(first.notice.code, BootstrapNoticeCode::ReadyTimeout);
    assert_eq!(first.notice.seconds, Some(0));
    assert!(host.has_launched(), "the timed-out launch watch must be retained");
    assert_eq!(launcher.calls(), 1);

    let second = boot(&host, Retry).await;
    assert_eq!(second.phase, BootstrapPhase::Failed);
    assert_eq!(second.notice.code, BootstrapNoticeCode::LauncherExited);
    assert!(
        !host.has_launched(),
        "a retained non-zero exit must release the launch slot"
    );
    assert_eq!(
        launcher.calls(),
        1,
        "observing the failed launch must not start a replacement in the same retry"
    );

    let third = boot(&host, Retry).await;
    assert_eq!(third.phase, BootstrapPhase::Failed);
    assert_eq!(launcher.calls(), 2, "the next retry may start the Runtime again");
}

#[tokio::test(start_paused = true)]
async fn a_long_running_launcher_is_retained_across_repeated_timeouts() {
    let probe = FakeProbe::never_healthy();
    let launcher = FakeLauncher::working();
    let host = runtime_host(probe, launcher.clone(), immediate_timeout_settings());

    let first = boot(&host, Launch).await;
    let second = boot(&host, Retry).await;

    assert_eq!(first.phase, BootstrapPhase::Failed);
    assert_eq!(second.phase, BootstrapPhase::Failed);
    assert!(first.retryable && second.retryable);
    assert!(host.has_launched());
    assert_eq!(
        launcher.calls(),
        1,
        "an unresolved launcher watch remains the same launch across retries"
    );
}

#[tokio::test(start_paused = true)]
async fn a_zero_exit_launch_without_readiness_can_be_retried() {
    let probe = FakeProbe::never_healthy();
    let launcher = FakeLauncher::zero_exit_without_runtime();
    let host = runtime_host(probe, launcher.clone(), immediate_timeout_settings());

    let first = boot(&host, Launch).await;
    assert_eq!(first.phase, BootstrapPhase::Failed);
    assert_eq!(first.notice.code, BootstrapNoticeCode::ReadyTimeout);
    assert!(
        !host.has_launched(),
        "a completed zero-exit helper must not block a retry after readiness timed out"
    );
    assert_eq!(launcher.calls(), 1);

    let second = boot(&host, Retry).await;
    assert_eq!(second.phase, BootstrapPhase::Failed);
    assert_eq!(
        launcher.calls(),
        2,
        "Retry may re-run the idempotent start command once the prior helper completed"
    );
}

#[tokio::test]
async fn readiness_loss_keeps_a_pending_helper_deduplicated() {
    let launcher = FakeLauncher::working();
    let host = runtime_host(
        FakeProbe::never_healthy(),
        launcher.clone(),
        immediate_timeout_settings(),
    );

    assert_eq!(boot(&host, Launch).await.notice.code, BootstrapNoticeCode::ReadyTimeout);
    assert!(host.has_launched(), "the timeout retains the pending helper");
    host.release_after_readiness_loss();

    assert!(
        host.has_launched(),
        "a pending helper remains the single recovery owner"
    );
    assert_eq!(launcher.calls(), 1);
}

#[tokio::test(start_paused = true)]
async fn concurrent_bootstraps_start_at_most_one_runtime() {
    let probe = FakeProbe::healthy_from(3);
    let launcher = FakeLauncher::working();
    let host = runtime_host(probe.clone(), launcher.clone(), fast_settings());

    let (left, right) = (Recorder::default(), Recorder::default());
    let (first, second) = tokio::join!(host.bootstrap(&left, Launch), host.bootstrap(&right, Launch));

    assert_eq!(first.phase, BootstrapPhase::Ready);
    assert_eq!(second.phase, BootstrapPhase::Ready);
    assert_eq!(launcher.calls(), 1, "two racing runs must not start two Runtimes");
}

#[tokio::test(start_paused = true)]
async fn a_missing_executable_fails_retryably_and_the_next_attempt_may_start_one() {
    let probe = FakeProbe::healthy_from(3);
    let launcher = FakeLauncher::failing_first(1);
    let host = runtime_host(probe.clone(), launcher.clone(), fast_settings());

    let first = boot(&host, Launch).await;
    assert_eq!(first.phase, BootstrapPhase::Failed);
    assert!(first.retryable);
    assert_eq!(launcher.calls(), 1);
    assert!(
        !host.has_launched(),
        "a launch that failed did not start anything, so it must not block the retry"
    );

    let second = boot(&host, Retry).await;
    assert_eq!(second.phase, BootstrapPhase::Ready);
    assert_eq!(launcher.calls(), 2, "the retry is allowed to start the Runtime");
    assert!(host.has_launched());
}

/// An installed `vibe` too old for the shell's arguments spawns fine, rejects an
/// argument, and exits without starting anything. The rest of the ready timeout
/// would then be spent polling an address that will never answer.
#[tokio::test(start_paused = true)]
async fn a_launcher_that_exits_without_starting_anything_gives_up_early() {
    let probe = FakeProbe::never_healthy();
    let launcher = FakeLauncher::dying();
    let host = runtime_host(probe.clone(), launcher.clone(), fast_settings());
    let recorder = Recorder::default();

    let started = tokio::time::Instant::now();
    let status = host.bootstrap(&recorder, Launch).await;
    let waited = started.elapsed();

    assert_eq!(status.phase, BootstrapPhase::Failed);
    assert!(status.retryable, "updating the Runtime makes the next attempt viable");
    assert_eq!(status.notice.code, BootstrapNoticeCode::LauncherExited);
    assert!(
        waited < fast_settings().ready_timeout,
        "waited {waited:?}, which is the full timeout this abort exists to avoid"
    );
    assert!(!recorder.phases().contains(&BootstrapPhase::Ready));

    // Nothing is running, so the retry has to be allowed to start one.
    assert!(!host.has_launched());
    assert_eq!(boot(&host, Retry).await.phase, BootstrapPhase::Failed);
    assert_eq!(launcher.calls(), 2, "the retry starts a Runtime again");
}

#[tokio::test(start_paused = true)]
async fn a_non_loopback_origin_fails_immediately_and_is_not_retryable() {
    let probe = FakeProbe::healthy_from(1);
    let launcher = FakeLauncher::working();
    let settings = RuntimeHostSettings {
        origin_override: Some("http://avibe.example.com:5123".to_owned()),
        ..fast_settings()
    };
    let host = runtime_host(probe.clone(), launcher.clone(), settings);
    let recorder = Recorder::default();

    let status = host.bootstrap(&recorder, Launch).await;

    assert_eq!(status.phase, BootstrapPhase::Failed);
    assert!(!status.retryable, "retrying cannot make a remote origin acceptable");
    assert_eq!(status.attempt, 0);
    assert_eq!(probe.calls(), 0, "a rejected origin is never contacted");
    assert_eq!(launcher.calls(), 0);
    assert_eq!(recorder.phases(), vec![BootstrapPhase::Failed]);
    // The rejected value is unvalidated input on its way to a WebView, so it is
    // dropped and represented only by a typed notice.
    assert!(status.origin.is_empty(), "got {:?}", status.origin);
    assert_eq!(status.notice.code, BootstrapNoticeCode::InvalidOrigin);
}

#[tokio::test(start_paused = true)]
async fn an_override_origin_is_adopted_only_and_never_starts_the_default_runtime() {
    let probe = FakeProbe::never_healthy();
    let launcher = FakeLauncher::working();
    let settings = RuntimeHostSettings {
        origin_override: Some(TEST_ORIGIN.to_owned()),
        ..fast_settings()
    };
    let host = runtime_host(probe.clone(), launcher.clone(), settings);
    let recorder = Recorder::default();

    let status = host.bootstrap(&recorder, Launch).await;

    assert_eq!(status.phase, BootstrapPhase::Failed);
    assert!(status.retryable);
    assert_eq!(status.origin, TEST_ORIGIN);
    assert_eq!(status.notice.code, BootstrapNoticeCode::RuntimeNotFound);
    assert_eq!(probe.calls(), 1, "the override origin is probed directly");
    assert_eq!(launcher.calls(), 0, "override mode must not launch the default Runtime");
    assert_eq!(recorder.phases(), vec![BootstrapPhase::Probing, BootstrapPhase::Failed]);
}

#[tokio::test(start_paused = true)]
async fn the_production_wait_is_bounded() {
    let probe = FakeProbe::never_healthy();
    let launcher = FakeLauncher::working();
    let host = runtime_host(probe.clone(), launcher.clone(), RuntimeHostSettings::default());

    let started = tokio::time::Instant::now();
    let status = boot(&host, Launch).await;
    let waited = started.elapsed();

    assert_eq!(status.phase, BootstrapPhase::Failed);
    assert!(status.retryable);
    let budget = RuntimeHostSettings::default().ready_timeout;
    assert!(waited >= budget, "waited {waited:?}, expected at least {budget:?}");
    assert!(
        waited < budget + Duration::from_secs(5),
        "waited {waited:?}, which overruns the bound"
    );
}
