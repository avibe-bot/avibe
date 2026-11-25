//! The upgrade through a synthetic Runtime that answers the released loopback
//! endpoints, over the real coordinator and a Runtime host that adopted it.
use super::*;
use crate::{
    instance_client::{
        account::Passkeys,
        coordinator::ClientView,
        protocol::{ClientState, ClientTarget},
        storage::{ConnectionStore, Credentials},
        transport::{Request, Response, Transport},
    },
    launcher::ClaimHelperCommand,
    BootstrapPhase, BootstrapTrigger, DiscardStatus, HealthProbe, LaunchError, LaunchedRuntime, Presence,
    ResolvedRuntimeLauncher, RuntimeHostSettings, RuntimeLauncher,
};
use reqwest::header::{HeaderMap, SET_COOKIE};
use std::sync::Mutex as StdMutex;

type Result<T> = std::result::Result<T, ClientError>;

const ORIGIN: &str = "http://127.0.0.1:40125";

/// A Runtime that predates the claim protocol until it is upgraded.
#[derive(Default)]
struct Runtime {
    managed: bool,
    prereleases: bool,
    /// An update is offered on the stable channel.
    stable_update: bool,
    /// An update is offered once prereleases are on.
    prerelease_update: bool,
    upgrade_refused: bool,
    /// The upgrade is accepted, but the Runtime never comes back newer.
    never_restarts: bool,
    /// The newer Runtime still has no claim protocol.
    still_old: bool,
    /// The claim protocol is already there.
    current: bool,
    /// No local description (before 3.2.6rc16); what the owner's config
    /// projection says about the pairing.
    describes: bool,
    paired: Option<&'static str>,
    state: StdMutex<(bool, bool)>, // (prereleases switched on here, upgraded)
    calls: StdMutex<Vec<String>>,
}
impl Runtime {
    fn calls(&self) -> Vec<String> {
        self.calls.lock().unwrap().clone()
    }
}
#[async_trait::async_trait]
impl Transport for Runtime {
    async fn send(&self, r: Request) -> Result<Response> {
        assert_eq!(r.url.origin().ascii_serialization(), ORIGIN);
        let path = r.url.path().to_owned();
        self.calls.lock().unwrap().push(format!("{} {path}", r.method));
        let mut headers = HeaderMap::new();
        let (switched, upgraded) = *self.state.lock().unwrap();
        let prereleases = self.prereleases || switched;
        let (status, body) = match (r.method.as_str(), path.as_str()) {
            ("GET", "/api/version") => (
                200,
                json!({
                    "current": if upgraded { "3.2.9rc1" } else { "3.2.7" },
                    "latest": "3.2.9rc1",
                    "has_update": !upgraded && (self.stable_update || (prereleases && self.prerelease_update)),
                    "prereleases": prereleases,
                    "managed_by": if self.managed { json!("desktop") } else { Value::Null },
                }),
            ),
            ("GET", "/api/csrf-token") => {
                headers.insert(SET_COOKIE, "vibe_csrf_token=fixture; Path=/".parse().unwrap());
                (200, json!({"csrf_token": "fixture"}))
            }
            ("POST", "/api/update/prereleases") => {
                assert_eq!(r.body, Some(json!({"enabled": true})));
                assert_eq!(r.headers["origin"], ORIGIN);
                self.state.lock().unwrap().0 = true;
                (200, json!({"ok": true}))
            }
            ("POST", "/api/upgrade") => {
                // The released request: no confirmation field, CSRF and Origin as the Workbench sends.
                assert_eq!(r.body, Some(json!({})));
                assert_eq!(r.headers["x-vibe-csrf-token"], "fixture");
                if self.managed {
                    (200, json!({"ok": false, "code": "desktop_managed_runtime"}))
                } else if self.upgrade_refused {
                    (200, json!({"ok": false, "message": "Upgrade failed."}))
                } else {
                    if !self.never_restarts {
                        self.state.lock().unwrap().1 = true;
                    }
                    (200, json!({"ok": true, "restarting": true}))
                }
            }
            ("POST", "/api/desktop/claim/v1/admit") => {
                // The probe: an owner POST with an empty body. The handler refuses
                // it; a Runtime without the route answers its unknown-API 404.
                assert_eq!(r.body, Some(json!({})));
                assert_eq!(r.headers["x-vibe-csrf-token"], "fixture");
                let has = self.current || (upgraded && !self.still_old);
                (if has { 400 } else { 404 }, Value::Null)
            }
            ("GET", "/api/desktop/local-instance") if self.describes || upgraded => (
                200,
                json!({"schema_version":1,"installation_id":null,"device_name":"Studio","binding_revision":"r",
                    "pairing":null,"remote_access":{"configured_enabled":false,"requested":false,"running":false}}),
            ),
            ("GET", "/api/desktop/local-instance") => (404, Value::Null),
            // The released shape: `vibe_cloud` is the config dataclass with
            // credentials removed, plus `paired`; remote-access status has no ID.
            ("GET", "/api/config") => (
                200,
                json!({"remote_access": {"provider": "vibe_cloud", "vibe_cloud": {
                    "enabled": self.paired.is_some(),
                    "instance_id": self.paired.unwrap_or(""),
                    "public_url": "",
                    "paired": self.paired.is_some(),
                }}}),
            ),
            other => panic!("unexpected local request {other:?}"),
        };
        Ok(Response {
            status,
            body: if body.is_null() {
                vec![]
            } else {
                serde_json::to_vec(&body).unwrap()
            },
            headers,
        })
    }
}

#[derive(Clone)]
struct Adopted;
#[async_trait::async_trait]
impl HealthProbe for Adopted {
    async fn presence(&self, _: &LoopbackOrigin, _: Option<&str>) -> Presence {
        Presence::Unmanaged
    }
}
impl RuntimeLauncher for Adopted {
    fn resolve(&self) -> std::result::Result<Arc<dyn ResolvedRuntimeLauncher>, LaunchError> {
        Ok(Arc::new(self.clone()))
    }
}
impl ResolvedRuntimeLauncher for Adopted {
    fn endpoint(&self) -> std::result::Result<LoopbackOrigin, LaunchError> {
        Ok(LoopbackOrigin::parse(ORIGIN).unwrap())
    }
    fn launch(&self, _: bool) -> std::result::Result<LaunchedRuntime, LaunchError> {
        panic!("an upgrade never starts a Runtime itself")
    }
    fn claim_helper(&self) -> Option<ClaimHelperCommand> {
        // Never run here: only its presence says the Runtime is this Desktop's.
        Some(ClaimHelperCommand(std::process::Command::new("unused-claim-helper")))
    }
}

struct Quiet;
impl Credentials for Quiet {
    fn read(&self, _: &str) -> Result<Option<Vec<u8>>> {
        Ok(None)
    }
    fn write(&self, _: &str, _: &[u8]) -> Result<()> {
        Ok(())
    }
    fn erase(&self, _: &str) -> Result<()> {
        Ok(())
    }
}
#[async_trait::async_trait]
impl Passkeys for Quiet {
    async fn assert(&self, _: Value, _: Epoch) -> Result<Value> {
        panic!("no passkey in an upgrade")
    }
}
#[async_trait::async_trait]
impl ClientView for Quiet {
    fn changed(&self, _: &ClientState) {}
    fn invalidate(&self, _: u64) {}
    async fn commit(&self, _: Epoch, _: ClientTarget, _: Vec<cookie::Cookie<'static>>) -> Result<()> {
        panic!("no instance view in an upgrade")
    }
    async fn restore(&self, _: Epoch, _: ClientTarget, _: bool) -> Result<()> {
        Ok(())
    }
    async fn signed_out(&self, _: Epoch, _: ClientTarget, _: bool) -> Result<()> {
        Ok(())
    }
}

async fn setup(runtime: Runtime, desktop: &str) -> (Arc<Coordinator>, Arc<Runtime>, RuntimeHost, tempfile::TempDir) {
    let root = tempfile::tempdir().unwrap();
    let store = ConnectionStore::new(root.path().join("connection.json"));
    let record = store.load("Studio").unwrap();
    let runtime = Arc::new(runtime);
    let c = Arc::new(Coordinator::new(
        ClientState::new("Studio".into(), desktop.into(), json!({})),
        record,
        store,
        runtime.clone(),
        Arc::new(Quiet),
        Arc::new(Quiet),
        Arc::new(Quiet),
    ));
    let host = RuntimeHost::new(Arc::new(Adopted), Arc::new(Adopted), RuntimeHostSettings::default());
    assert_eq!(
        host.bootstrap(&DiscardStatus, BootstrapTrigger::Recovery).await.phase,
        BootstrapPhase::Ready
    );
    c.local_origin(Some(LoopbackOrigin::parse(ORIGIN).unwrap()));
    c.note_runtime_too_old();
    (c, runtime, host, root)
}

fn state(result: std::result::Result<DirectResult, ClientError>) -> String {
    match result {
        Ok(DirectResult::LocalRuntimeUpgrade { state }) => state,
        Ok(other) => panic!("unexpected result {other:?}"),
        Err(error) => error.code,
    }
}
fn upgrades(runtime: &Runtime) -> usize {
    runtime.calls().iter().filter(|c| *c == "POST /api/upgrade").count()
}

/// Upgraded and adopted again, with the claim protocol: the step can be
/// repeated, and this computer's row no longer says it is too old. A
/// prerelease Desktop first turns on the Runtime's prereleases, and leaves
/// them on; a stable one never touches the switch.
#[tokio::test(start_paused = true)]
async fn an_upgrade_runs_the_workbench_calls_and_waits_for_the_runtime_again() {
    for (desktop, offered_on, switches) in [("3.2.9-rc.1", "prerelease", true), ("3.2.9", "stable", false)] {
        let runtime = Runtime {
            stable_update: offered_on == "stable",
            prerelease_update: true,
            describes: true,
            ..Default::default()
        };
        let (c, runtime, host, _root) = setup(runtime, desktop).await;
        assert!(c.state().local.runtime_too_old);
        assert_eq!(state(c.upgrade_local_runtime(&host).await), "upgraded", "{desktop}");
        let calls = runtime.calls();
        let switched = calls.iter().position(|c| c == "POST /api/update/prereleases");
        let upgraded = calls.iter().position(|c| c == "POST /api/upgrade").unwrap();
        assert_eq!(switched.is_some(), switches, "{desktop}: {calls:?}");
        assert!(switched.is_none_or(|at| at < upgraded));
        assert_eq!(upgrades(&runtime), 1);
        assert!(!c.state().local.runtime_too_old, "{desktop}");
    }
}

/// Desktop's own Runtime is upgraded by Desktop's update; a Runtime with
/// nothing newer, or one that refuses, is not waited for.
#[tokio::test(start_paused = true)]
async fn what_cannot_be_upgraded_here_is_said_without_waiting() {
    for (case, runtime, expected, upgrade_sent) in [
        (
            "desktop's own",
            Runtime {
                managed: true,
                describes: true,
                stable_update: true,
                ..Default::default()
            },
            "desktop_update",
            false,
        ),
        (
            "nothing newer",
            Runtime {
                describes: true,
                ..Default::default()
            },
            "local_runtime_still_too_old",
            false,
        ),
        (
            "refused",
            Runtime {
                describes: true,
                stable_update: true,
                upgrade_refused: true,
                ..Default::default()
            },
            "local_runtime_upgrade_failed",
            true,
        ),
        (
            "already current",
            Runtime {
                describes: true,
                current: true,
                ..Default::default()
            },
            "upgraded",
            false,
        ),
    ] {
        let (c, runtime, host, _root) = setup(runtime, "3.2.9").await;
        let started = tokio::time::Instant::now();
        assert_eq!(state(c.upgrade_local_runtime(&host).await), expected, "{case}");
        assert_eq!(upgrades(&runtime), usize::from(upgrade_sent), "{case}");
        assert!(started.elapsed() < POLL, "{case}");
    }
}

/// After an accepted upgrade: a Runtime that never comes back newer times out
/// after the whole wait; one that comes back still without the claim protocol
/// says so. Either way the button can be pressed again.
#[tokio::test(start_paused = true)]
async fn an_upgrade_that_does_not_help_ends_with_its_own_answer() {
    for (case, runtime, expected) in [
        (
            "never newer",
            Runtime {
                describes: true,
                stable_update: true,
                never_restarts: true,
                ..Default::default()
            },
            "local_runtime_upgrade_timed_out",
        ),
        (
            "still old",
            Runtime {
                describes: true,
                stable_update: true,
                still_old: true,
                ..Default::default()
            },
            "local_runtime_still_too_old",
        ),
    ] {
        let (c, runtime, host, _root) = setup(runtime, "3.2.9").await;
        let started = tokio::time::Instant::now();
        assert_eq!(state(c.upgrade_local_runtime(&host).await), expected, "{case}");
        if case == "never newer" {
            assert!(started.elapsed() >= UPGRADE_WAIT);
        }
        assert_eq!(upgrades(&runtime), 1, "{case}");
        assert!(c.state().local.runtime_too_old, "{case}");
        // Not busy afterwards.
        assert_ne!(state(c.upgrade_local_runtime(&host).await), "busy", "{case}");
    }
}

/// A Runtime too old to describe itself keeps this computer's row when its
/// remote-access status says which instance it is bound to, and only then.
#[tokio::test]
async fn a_runtime_too_old_to_describe_itself_still_names_its_binding() {
    for (said, expected) in [(Some("inst_local"), Some("inst_local")), (None, None)] {
        let runtime = Runtime {
            paired: said,
            ..Default::default()
        };
        let (c, _runtime, _host, _root) = setup(runtime, "3.2.9").await;
        c.refresh_local().await;
        let local = c.state().local;
        assert!(local.runtime_too_old);
        assert_eq!(local.instance_id.as_deref(), expected);
    }
}
