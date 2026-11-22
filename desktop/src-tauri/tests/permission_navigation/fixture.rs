#![allow(dead_code, non_snake_case)]
use avibe_runtime_host::computer_use::{
    CapabilityCache, ComputerUseLifecycle, ComputerUsePhase, Grants, HealthResult, LifecycleDirective, RuntimeSupport,
};
use avibe_runtime_host::LoopbackOrigin;
use std::cell::RefCell;
use std::io;
use std::path::{Path, PathBuf};
use std::sync::{atomic::AtomicBool, Arc, Mutex};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use tokio::sync::{mpsc, oneshot};
use tokio::time::timeout_at;

const TICK_INTERVAL: Duration = Duration::from_secs(86_400);
const PERMISSION_REQUEST_TIMEOUT: Duration = Duration::from_secs(1);
const SETUP_RESUME_FILE: &str = "permission-setup-resume";

#[derive(Default, Debug)]
struct Effects {
    grants: Option<Grants>,
    ax_prompts: usize,
    recording_prompts: usize,
    captures: usize,
    silent_reads: usize,
    urls: Vec<String>,
    directives: Vec<LifecycleDirective>,
}
thread_local! { static OS: RefCell<Effects> = RefCell::new(Effects::default()); }
fn effect<T>(f: impl FnOnce(&mut Effects) -> T) -> T {
    OS.with(|os| f(&mut os.borrow_mut()))
}
fn observed() -> Grants {
    effect(|os| os.grants.unwrap())
}
// Only these test adapters impersonate OS calls. The assembled source contains
// no framework links or native application. A call is observable, never a grant.
unsafe fn AXIsProcessTrusted() -> u8 {
    effect(|os| os.silent_reads += 1);
    observed().accessibility as u8
}
unsafe fn CGPreflightScreenCaptureAccess() -> bool {
    effect(|os| os.silent_reads += 1);
    observed().screen_recording
}
fn request_accessibility() -> bool {
    effect(|os| os.ax_prompts += 1);
    observed().accessibility
}
unsafe fn CGRequestScreenCaptureAccess() -> bool {
    effect(|os| os.recording_prompts += 1);
    observed().screen_recording
}
fn one_pixel_capture_probe(sender: oneshot::Sender<CaptureProbeOutcome>, _: Instant) {
    effect(|os| os.captures += 1);
    let _ = sender.send(CaptureProbeOutcome::Image);
}
mod tauri_plugin_opener {
    pub fn open_url(url: &str, _: Option<&str>) -> Result<(), String> {
        super::effect(|os| os.urls.push(url.to_owned()));
        Ok(())
    }
}
#[derive(Clone)]
struct AppHandle;
impl AppHandle {
    fn run_on_main_thread(&self, f: impl FnOnce()) -> Result<(), String> {
        f();
        Ok(())
    }
}
struct MainThreadMarker;
impl MainThreadMarker {
    fn new() -> Option<Self> {
        Some(Self)
    }
}
struct Diagnostics;
impl Diagnostics {
    fn record(&self, _: &str, _: &[(&str, String)]) {}
}
type BootstrapLog = Diagnostics;
#[derive(Debug)]
enum CaptureProbeOutcome {
    Image,
    MainThreadContextMismatch,
    MainThreadDispatchFailed(String),
    MainThreadRequestDropped,
    MainThreadRequestTimedOut,
    CallbackDropped,
    CallbackTimedOut,
}
struct PermissionRequestResult {
    checks: Option<PermissionRequestChecks>,
    capture: CaptureProbeOutcome,
}
impl PermissionRequestResult {
    fn grants(&self) -> Grants {
        self.checks
            .as_ref()
            .map(|checks| checks.grants)
            .unwrap_or_else(silent_grants)
    }
}
fn record_permission_request(_: &Diagnostics, _: &PermissionRequestResult) {}
struct PermissionChildRun;
struct ShellLock;
impl Drop for ShellLock {
    fn drop(&mut self) {}
}
struct Paths {
    state_dir: PathBuf,
}
// @SOURCE_DECLARATIONS@
// @SOURCE_CONTROLLER@
struct RuntimeState {
    app: AppHandle,
    view: Arc<Mutex<MenuView>>,
    diagnostics: Diagnostics,
    paths: Paths,
    lifecycle: ComputerUseLifecycle,
    capabilities: CapabilityCache,
    origin: Option<LoopbackOrigin>,
    assets_valid: bool,
    setup_resume_armed: bool,
    _shell_lock: ShellLock,
}
// @SOURCE_RUNTIME@
impl RuntimeState {
    fn update_view(&self, update: impl FnOnce(&mut MenuView)) {
        update(&mut self.view.lock().unwrap());
    }
    fn publish_view(&self) {
        self.update_view(|v| {
            v.enabled = self.lifecycle.enabled();
            v.phase = self.lifecycle.phase();
            v.reason = self.lifecycle.reason().map(str::to_owned);
        });
    }
    fn invalidate_start_retry(&mut self) {}
    // Record the production lifecycle's directive, including positive starts.
    // Process creation/health is outside this regression's boundary; a healthy
    // simulated driver acknowledges the issued start through the real lifecycle.
    async fn apply(&mut self, directive: LifecycleDirective) {
        effect(|os| os.directives.push(directive.clone()));
        if directive.spawn_daemon {
            self.lifecycle.startup_health(HealthResult::Pass, Duration::ZERO);
        }
        self.publish_view();
        self.settle_setup_resume();
    }
    async fn disable(&mut self) -> Result<(), String> {
        let directive = self.lifecycle.toggle_off();
        self.apply(directive).await;
        Ok(())
    }
    async fn recover_if_due(&mut self) {
        assert!(!self.lifecycle.recovering());
    }
    async fn shutdown(&mut self) {}
    async fn tick(&mut self) {
        panic!("fixture must finish before the tick")
    }
    async fn runtime_ready(&mut self, _: LoopbackOrigin, _: bool) {
        panic!("not exercised")
    }
    async fn runtime_lost(&mut self) {
        panic!("not exercised")
    }
    async fn runtime_stopped(&mut self) {
        panic!("not exercised")
    }
    async fn retry_start(&mut self, _: u64) {
        panic!("not exercised")
    }
    fn permission_probe_finished(&mut self, _: u64, _: PermissionChildRun) {
        panic!("not exercised")
    }
}
async fn enable_support(cache: &mut CapabilityCache, _: Option<&LoopbackOrigin>) -> RuntimeSupport {
    cache.current()
}
// @SOURCE_HELPERS@

#[cfg(test)]
mod tests {
    use super::*;
    use avibe_runtime_host::computer_use::{CapabilityProbe, CapabilityVerdict};

    fn fixture(
        grants: Grants,
        support: RuntimeSupport,
        enabled: bool,
    ) -> (
        Controller,
        RuntimeState,
        mpsc::UnboundedReceiver<Event>,
        tempfile::TempDir,
    ) {
        effect(|os| {
            *os = Effects {
                grants: Some(grants),
                ..Effects::default()
            }
        });
        let root = tempfile::tempdir().unwrap();
        let (sender, receiver) = mpsc::unbounded_channel();
        let view = Arc::new(Mutex::new(MenuView::default()));
        let mut lifecycle = ComputerUseLifecycle::off();
        if enabled {
            // Represents an existing opted-in installation, not a test call to
            // enable: the enable positive control below consumes the real event.
            lifecycle.toggle_on(RuntimeSupport::Supported, grants);
            if grants == Grants::all() {
                lifecycle.startup_health(HealthResult::Pass, Duration::ZERO);
            }
            lifecycle.capabilities(support, grants);
        }
        let mut capabilities = CapabilityCache::default();
        capabilities.observe(CapabilityProbe {
            controller_id: Some("fixture".to_owned()),
            verdict: match support {
                RuntimeSupport::Supported => CapabilityVerdict::Supported,
                RuntimeSupport::Unsupported => CapabilityVerdict::Unsupported,
                RuntimeSupport::Unknown => CapabilityVerdict::Transient,
            },
        });
        let runtime = RuntimeState {
            app: AppHandle,
            view: view.clone(),
            diagnostics: Diagnostics,
            paths: Paths {
                state_dir: root.path().to_owned(),
            },
            lifecycle,
            capabilities,
            origin: None,
            assets_valid: true,
            setup_resume_armed: false,
            _shell_lock: ShellLock,
        };
        runtime.publish_view();
        let controller = Controller {
            sender,
            view,
            shutdown_requested: Arc::new(AtomicBool::new(false)),
            shutdown_started: AtomicBool::new(false),
        };
        (controller, runtime, receiver, root)
    }

    async fn drain(controller: &Controller, runtime: RuntimeState, receiver: mpsc::UnboundedReceiver<Event>) {
        let (done, received) = oneshot::channel();
        controller.sender.send(Event::Shutdown(done)).unwrap();
        runtime.run(receiver).await;
        received.await.unwrap();
    }

    fn requests() -> (usize, usize, usize) {
        effect(|os| (os.ax_prompts, os.recording_prompts, os.captures))
    }

    #[tokio::test]
    async fn accessibility_navigation_never_requests_grants_or_capture() {
        navigation_contract(PermissionPane::Accessibility).await;
    }

    #[tokio::test]
    async fn recording_navigation_never_requests_grants_or_capture() {
        navigation_contract(PermissionPane::ScreenRecording).await;
    }

    async fn navigation_contract(pane: PermissionPane) {
        for accessibility in [false, true] {
            for screen_recording in [false, true] {
                for support in [
                    RuntimeSupport::Supported,
                    RuntimeSupport::Unknown,
                    RuntimeSupport::Unsupported,
                ] {
                    for enabled in [false, true] {
                        let grants = Grants {
                            accessibility,
                            screen_recording,
                        };
                        let (controller, runtime, receiver, root) = fixture(grants, support, enabled);
                        let before = controller.view.lock().unwrap().clone();
                        controller.open_permission(pane).unwrap();
                        controller.open_permission(pane).unwrap();
                        drain(&controller, runtime, receiver).await;
                        assert_eq!(
                            requests(),
                            (0, 0, 0),
                            "{pane:?} {grants:?} {support:?} enabled={enabled}"
                        );
                        let expected = match pane {
                            PermissionPane::Accessibility => {
                                "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
                            }
                            PermissionPane::ScreenRecording => {
                                "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"
                            }
                        };
                        assert_eq!(effect(|os| os.urls.clone()), [expected, expected]);
                        assert_eq!(controller.view.lock().unwrap().clone(), before);
                        assert!(effect(|os| os.directives.iter().all(|d| !d.prompt_permissions
                            && !d.spawn_permission_probe
                            && !d.spawn_daemon)));
                        assert_eq!(
                            root.path().join(SETUP_RESUME_FILE).exists(),
                            pane == PermissionPane::ScreenRecording && enabled && !screen_recording
                        );
                    }
                }
            }
        }
    }

    #[tokio::test]
    async fn a_manual_grant_refreshes_the_real_lifecycle_and_settles_the_marker() {
        for pane in [PermissionPane::Accessibility, PermissionPane::ScreenRecording] {
            let denied = Grants {
                accessibility: false,
                screen_recording: false,
            };
            let (controller, mut runtime, mut receiver, root) = fixture(denied, RuntimeSupport::Supported, true);
            controller.open_permission(pane).unwrap();
            // Pause between two real controller events to represent the user
            // granting in System Settings. No fake grant from the opener.
            let Event::OpenPermission(selected) = receiver.recv().await.unwrap() else {
                panic!("wrong event")
            };
            runtime.open_permission(selected).await;
            assert_eq!(runtime.lifecycle.phase(), ComputerUsePhase::NeedsPermission);
            let reads_before = effect(|os| os.silent_reads);
            effect(|os| os.grants = Some(Grants::all()));
            controller.activated();
            controller.activated();
            drain(&controller, runtime, receiver).await;
            assert_eq!(requests(), (0, 0, 0));
            assert!(effect(|os| os.silent_reads) > reads_before);
            assert_eq!(controller.view.lock().unwrap().phase, ComputerUsePhase::Ready);
            assert_eq!(effect(|os| os.directives.iter().filter(|d| d.spawn_daemon).count()), 1);
            assert!(!root.path().join(SETUP_RESUME_FILE).exists());
        }
    }

    #[tokio::test]
    async fn enable_and_menu_toggle_keep_the_intentional_registration_request() {
        for menu in [false, true] {
            for grants in [
                Grants {
                    accessibility: false,
                    screen_recording: false,
                },
                Grants::all(),
            ] {
                let (controller, runtime, receiver, _root) = fixture(grants, RuntimeSupport::Supported, false);
                if menu {
                    controller.toggle();
                } else {
                    controller.turn_on().unwrap();
                }
                drain(&controller, runtime, receiver).await;
                assert_eq!(requests(), (1, 1, 1));
                assert_eq!(
                    effect(|os| os.directives.iter().filter(|d| d.spawn_permission_probe).count()),
                    usize::from(grants != Grants::all())
                );
                assert!(effect(|os| os.directives.iter().any(|d| d.prompt_permissions)));
                assert!(controller.view.lock().unwrap().enabled);
            }
        }
    }
}
