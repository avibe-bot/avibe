//! Behavioural coverage for the bootstrap state machine.
//!
//! Every test drives a real `RuntimeHost` through fake collaborators, so the
//! guarantees under test are the ones the shell actually relies on: adopt what
//! is already running, start what is not, never start twice, never navigate
//! before the Runtime answers.
//!
//! Time is virtual (`start_paused`), so the 120-second production wait is
//! exercised in microseconds.

use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use async_trait::async_trait;
use avibe_runtime_host::{
    BootstrapPhase, BootstrapStatus, HealthProbe, LaunchError, LaunchWatch, LaunchedRuntime, LoopbackOrigin,
    RuntimeHost, RuntimeHostSettings, RuntimeLauncher, StatusSink, DEFAULT_ORIGIN,
};

/// Answers "not yet" until the given probe call, then "ready" forever.
struct FakeProbe {
    healthy_from: usize,
    calls: AtomicUsize,
}

impl FakeProbe {
    fn healthy_from(call: usize) -> Arc<Self> {
        Arc::new(Self {
            healthy_from: call,
            calls: AtomicUsize::new(0),
        })
    }

    fn never_healthy() -> Arc<Self> {
        Self::healthy_from(usize::MAX)
    }

    fn calls(&self) -> usize {
        self.calls.load(Ordering::SeqCst)
    }
}

#[async_trait]
impl HealthProbe for FakeProbe {
    async fn is_healthy(&self, _origin: &LoopbackOrigin) -> bool {
        let call = self.calls.fetch_add(1, Ordering::SeqCst) + 1;
        call >= self.healthy_from
    }
}

/// Records launch attempts; the first `failures` of them report no executable.
struct FakeLauncher {
    calls: AtomicUsize,
    failures: usize,
    dies_immediately: bool,
}

impl FakeLauncher {
    fn working() -> Arc<Self> {
        Arc::new(Self {
            calls: AtomicUsize::new(0),
            failures: 0,
            dies_immediately: false,
        })
    }

    fn failing_first(failures: usize) -> Arc<Self> {
        Arc::new(Self {
            calls: AtomicUsize::new(0),
            failures,
            dies_immediately: false,
        })
    }

    /// Spawns successfully, then exits non-zero without starting a Runtime —
    /// what an installed `vibe` too old for the shell's arguments does.
    fn dying() -> Arc<Self> {
        Arc::new(Self {
            calls: AtomicUsize::new(0),
            failures: 0,
            dies_immediately: true,
        })
    }

    fn calls(&self) -> usize {
        self.calls.load(Ordering::SeqCst)
    }
}

impl RuntimeLauncher for FakeLauncher {
    fn launch(&self) -> Result<LaunchedRuntime, LaunchError> {
        let call = self.calls.fetch_add(1, Ordering::SeqCst) + 1;
        if call <= self.failures {
            return Err(LaunchError::ExecutableNotFound);
        }
        Ok(LaunchedRuntime {
            pid: 4242,
            watch: if self.dies_immediately {
                LaunchWatch::exited(false)
            } else {
                LaunchWatch::default()
            },
        })
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

fn host(probe: Arc<FakeProbe>, launcher: Arc<FakeLauncher>, settings: RuntimeHostSettings) -> RuntimeHost {
    RuntimeHost::new(probe, launcher, settings)
}

#[tokio::test(start_paused = true)]
async fn an_already_running_runtime_is_adopted_without_starting_anything() {
    let probe = FakeProbe::healthy_from(1);
    let launcher = FakeLauncher::working();
    let host = host(probe.clone(), launcher.clone(), fast_settings());
    let recorder = Recorder::default();

    let status = host.bootstrap(&recorder).await;

    assert_eq!(status.phase, BootstrapPhase::Ready);
    assert_eq!(status.origin, DEFAULT_ORIGIN);
    assert_eq!(status.attempt, 1);
    assert_eq!(launcher.calls(), 0, "an adopted Runtime must never be re-started");
    assert_eq!(probe.calls(), 1);
    assert_eq!(recorder.phases(), vec![BootstrapPhase::Probing, BootstrapPhase::Ready]);
    assert!(!host.has_launched());
}

#[tokio::test(start_paused = true)]
async fn an_absent_runtime_is_started_and_adopted_once_it_answers() {
    let probe = FakeProbe::healthy_from(3);
    let launcher = FakeLauncher::working();
    let host = host(probe.clone(), launcher.clone(), fast_settings());
    let recorder = Recorder::default();

    let status = host.bootstrap(&recorder).await;

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
async fn the_shell_learns_of_readiness_only_after_the_runtime_answers() {
    let probe = FakeProbe::healthy_from(4);
    let launcher = FakeLauncher::working();
    let host = host(probe.clone(), launcher.clone(), fast_settings());
    let recorder = Recorder::default();

    let status = host.bootstrap(&recorder).await;
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
    let launcher = FakeLauncher::working();
    let host = host(probe.clone(), launcher.clone(), fast_settings());
    let recorder = Recorder::default();

    let status = host.bootstrap(&recorder).await;

    assert_eq!(status.phase, BootstrapPhase::Failed);
    assert!(status.retryable, "a slow machine deserves another try");
    assert!(status.message.contains("2 seconds"), "got {:?}", status.message);
    assert!(status.attempt > 1, "the wait is polled, not a single shot");
    assert_eq!(launcher.calls(), 1);
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
    let host = host(probe.clone(), launcher.clone(), fast_settings());

    let first = host.bootstrap(&Recorder::default()).await;
    assert_eq!(first.phase, BootstrapPhase::Failed);
    assert_eq!(launcher.calls(), 1);

    let recorder = Recorder::default();
    let second = host.bootstrap(&recorder).await;

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
async fn concurrent_bootstraps_start_at_most_one_runtime() {
    let probe = FakeProbe::healthy_from(3);
    let launcher = FakeLauncher::working();
    let host = host(probe.clone(), launcher.clone(), fast_settings());

    let (left, right) = (Recorder::default(), Recorder::default());
    let (first, second) = tokio::join!(host.bootstrap(&left), host.bootstrap(&right));

    assert_eq!(first.phase, BootstrapPhase::Ready);
    assert_eq!(second.phase, BootstrapPhase::Ready);
    assert_eq!(launcher.calls(), 1, "two racing runs must not start two Runtimes");
}

#[tokio::test(start_paused = true)]
async fn a_missing_executable_fails_retryably_and_the_next_attempt_may_start_one() {
    let probe = FakeProbe::healthy_from(3);
    let launcher = FakeLauncher::failing_first(1);
    let host = host(probe.clone(), launcher.clone(), fast_settings());

    let first = host.bootstrap(&Recorder::default()).await;
    assert_eq!(first.phase, BootstrapPhase::Failed);
    assert!(first.retryable);
    assert_eq!(launcher.calls(), 1);
    assert!(
        !host.has_launched(),
        "a launch that failed did not start anything, so it must not block the retry"
    );

    let second = host.bootstrap(&Recorder::default()).await;
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
    let host = host(probe.clone(), launcher.clone(), fast_settings());
    let recorder = Recorder::default();

    let started = tokio::time::Instant::now();
    let status = host.bootstrap(&recorder).await;
    let waited = started.elapsed();

    assert_eq!(status.phase, BootstrapPhase::Failed);
    assert!(status.retryable, "updating the Runtime makes the next attempt viable");
    assert!(status.message.contains("vibe upgrade"), "got {:?}", status.message);
    assert!(
        waited < fast_settings().ready_timeout,
        "waited {waited:?}, which is the full timeout this abort exists to avoid"
    );
    assert!(!recorder.phases().contains(&BootstrapPhase::Ready));

    // Nothing is running, so the retry has to be allowed to start one.
    assert!(!host.has_launched());
    assert_eq!(host.bootstrap(&Recorder::default()).await.phase, BootstrapPhase::Failed);
    assert_eq!(launcher.calls(), 2, "the retry starts a Runtime again");
}

#[tokio::test(start_paused = true)]
async fn a_non_loopback_origin_fails_immediately_and_is_not_retryable() {
    let probe = FakeProbe::healthy_from(1);
    let launcher = FakeLauncher::working();
    let settings = RuntimeHostSettings {
        origin: "http://avibe.example.com:5123".to_owned(),
        ..fast_settings()
    };
    let host = host(probe.clone(), launcher.clone(), settings);
    let recorder = Recorder::default();

    let status = host.bootstrap(&recorder).await;

    assert_eq!(status.phase, BootstrapPhase::Failed);
    assert!(!status.retryable, "retrying cannot make a remote origin acceptable");
    assert_eq!(status.attempt, 0);
    assert_eq!(probe.calls(), 0, "a rejected origin is never contacted");
    assert_eq!(launcher.calls(), 0);
    assert_eq!(recorder.phases(), vec![BootstrapPhase::Failed]);
    // The rejected value is echoed back so the user can see what is misconfigured.
    assert_eq!(status.origin, "http://avibe.example.com:5123");
}

#[tokio::test(start_paused = true)]
async fn the_production_wait_is_bounded() {
    let probe = FakeProbe::never_healthy();
    let launcher = FakeLauncher::working();
    let host = host(probe.clone(), launcher.clone(), RuntimeHostSettings::default());

    let started = tokio::time::Instant::now();
    let status = host.bootstrap(&Recorder::default()).await;
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
