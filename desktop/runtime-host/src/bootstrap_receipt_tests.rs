//! Real cmd_start -> local HTTP -> stdout -> RuntimeHost admission.
//! Run with AVIBE_TEST_PYTHON pointing at the repository test environment and
//! `cargo test -p avibe-runtime-host cmd_start_receipt_admission -- --ignored --nocapture`.
use super::*;
use crate::bootstrap_log::BootstrapLog;
use crate::health::HttpHealthProbe;
use crate::launcher::LaunchWatch;
use crate::receipt::StartupReceiptStream;
use crate::status::BootstrapPhase;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicUsize, Ordering};

struct Fixture {
    child: Mutex<Child>,
    origin: LoopbackOrigin,
    scratch: PathBuf,
    receipt: bool,
    launches: AtomicUsize,
}

impl Fixture {
    fn start(root: &Path, case: &str, receipt: bool, delay: f64, housekeeping: f64, sample: usize) -> Arc<Self> {
        let scratch = std::env::temp_dir().join(format!(
            "avibe startup 非 ASCII {} {case} {receipt} {sample}",
            std::process::id()
        ));
        std::fs::create_dir_all(&scratch).unwrap();
        let python =
            std::env::var_os("AVIBE_TEST_PYTHON").expect("set absolute AVIBE_TEST_PYTHON to project test Python");
        let mut command = Command::new(python);
        command
            .env_clear()
            .env("PYTHONIOENCODING", "utf-8")
            .env("PYTHONDONTWRITEBYTECODE", "1");
        for key in ["SYSTEMROOT", "WINDIR"] {
            if let Some(value) = std::env::var_os(key) {
                command.env(key, value);
            }
        }
        for key in [
            "HOME",
            "USERPROFILE",
            "TEMP",
            "TMP",
            "TMPDIR",
            "XDG_CONFIG_HOME",
            "XDG_CACHE_HOME",
            "XDG_DATA_HOME",
            "XDG_STATE_HOME",
            "AVIBE_HOME",
            "CODEX_HOME",
            "CLAUDE_CONFIG_DIR",
            "APPDATA",
            "LOCALAPPDATA",
        ] {
            let home = scratch.join(key);
            std::fs::create_dir_all(&home).unwrap();
            command.env(key, home);
        }
        let mut child = command
            .current_dir(&scratch)
            .arg(root.join("tests/fixtures/desktop_startup_process.py"))
            .arg(&scratch)
            .arg(if receipt { "receipt" } else { "legacy" })
            .arg(delay.to_string())
            .arg(housekeeping.to_string())
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .expect("fixture Python starts");
        let deadline = std::time::Instant::now() + Duration::from_secs(15);
        let endpoint = scratch.join("endpoint");
        while !endpoint.exists() {
            if let Some(status) = child.try_wait().unwrap() {
                let output = child.wait_with_output().unwrap();
                panic!("fixture exited {status}: {}", String::from_utf8_lossy(&output.stderr));
            }
            if std::time::Instant::now() >= deadline {
                child.kill().ok();
                child.wait().ok();
                panic!("fixture initialization timed out");
            }
            std::thread::sleep(Duration::from_millis(5));
        }
        Arc::new(Self {
            origin: LoopbackOrigin::parse(&std::fs::read_to_string(endpoint).unwrap()).unwrap(),
            child: Mutex::new(child),
            scratch,
            receipt,
            launches: AtomicUsize::new(0),
        })
    }

    fn finish(&self) -> serde_json::Value {
        let mut child = self.child.lock().unwrap();
        writeln!(child.stdin.as_mut().unwrap(), "done").unwrap();
        let deadline = std::time::Instant::now() + Duration::from_secs(10);
        loop {
            if let Some(status) = child.try_wait().unwrap() {
                assert!(status.success());
                break;
            }
            assert!(
                std::time::Instant::now() < deadline,
                "fixture exits after bounded housekeeping"
            );
            std::thread::sleep(Duration::from_millis(5));
        }
        serde_json::from_slice(&std::fs::read(self.scratch.join("trace.json")).unwrap()).unwrap()
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        if let Ok(child) = self.child.get_mut() {
            if child.try_wait().ok().flatten().is_none() {
                child.kill().ok();
            }
            child.wait().ok();
        }
        std::fs::remove_dir_all(&self.scratch).ok();
    }
}
impl RuntimeLauncher for Arc<Fixture> {
    fn resolve(&self) -> Result<Arc<dyn ResolvedRuntimeLauncher>, LaunchError> {
        Ok(Arc::new(self.clone()))
    }
}
impl ResolvedRuntimeLauncher for Arc<Fixture> {
    fn endpoint(&self) -> Result<LoopbackOrigin, LaunchError> {
        Ok(self.origin.clone())
    }
    fn launch(&self, _hand_over: bool) -> Result<LaunchedRuntime, LaunchError> {
        self.launches.fetch_add(1, Ordering::SeqCst);
        let mut child = self.child.lock().unwrap();
        let watch = if self.receipt {
            LaunchWatch::with_receipt(StartupReceiptStream::spawn(
                child.stdout.take().unwrap(),
                BootstrapLog::at(self.scratch.join("bootstrap.log")),
            ))
        } else {
            LaunchWatch::default()
        };
        writeln!(child.stdin.as_mut().unwrap(), "start").unwrap();
        Ok(LaunchedRuntime { pid: child.id(), watch })
    }
    fn expected_runtime_id(&self) -> Option<&str> {
        Some("bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")
    }
    fn record_bootstrap_event(&self, event: &str) {
        BootstrapLog::at(self.scratch.join("bootstrap.log")).record(event, &[]);
    }
}
struct CountProbe {
    inner: HttpHealthProbe,
    calls: AtomicUsize,
}
#[async_trait::async_trait]
impl HealthProbe for CountProbe {
    async fn presence(&self, origin: &LoopbackOrigin, expected: Option<&str>) -> Presence {
        self.calls.fetch_add(1, Ordering::SeqCst);
        self.inner.presence(origin, expected).await
    }
}

#[tokio::test]
#[ignore = "requires repository Python dependencies; explicit cross-language acceptance gate"]
async fn cmd_start_receipt_admission() {
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap()
        .parent()
        .unwrap()
        .to_owned();
    for (case, delay, housekeeping) in [
        ("early", 0.0, 0.0),
        ("controller", 1.1, 0.0),
        ("housekeeping", 0.0, 1.2),
    ] {
        for sample in 0..3 {
            let mut elapsed = Vec::new();
            for receipt in [false, true] {
                let fixture = Fixture::start(&root, case, receipt, delay, housekeeping, sample);
                let probe = Arc::new(CountProbe {
                    inner: HttpHealthProbe::new(Duration::from_secs(1)).unwrap(),
                    calls: AtomicUsize::new(0),
                });
                let host = RuntimeHost::new(probe.clone(), Arc::new(fixture.clone()), RuntimeHostSettings::default());
                let start = std::time::Instant::now();
                let status = host.bootstrap(&DiscardStatus, BootstrapTrigger::Launch).await;
                let ready_ms = start.elapsed().as_secs_f64() * 1000.0;
                assert_eq!(status.phase, BootstrapPhase::Ready, "{case}, receipt={receipt}");
                let probes = probe.calls.load(Ordering::SeqCst);
                if receipt {
                    assert_eq!(probes, 2, "initial adoption and one final real HTTP probe");
                    if housekeeping > 0.0 {
                        assert!(
                            !fixture.scratch.join("housekeeping-finished").exists(),
                            "admission must precede housekeeping completion"
                        );
                    }
                }
                // A subsequent live adoption never asks the fixture to start twice.
                assert_eq!(
                    host.bootstrap(&DiscardStatus, BootstrapTrigger::Retry).await.phase,
                    BootstrapPhase::Ready
                );
                assert_eq!(fixture.launches.load(Ordering::SeqCst), 1);
                let trace = fixture.finish();
                assert_eq!(trace["service_starts"], 1);
                assert_eq!(trace["ui_starts"], 1);
                eprintln!("startup fixture case={case} sample={sample} receipt={receipt} shell_probes={probes} admission_ms={ready_ms:.1} trace={trace}");
                if receipt {
                    eprintln!(
                        "{}",
                        std::fs::read_to_string(fixture.scratch.join("bootstrap.log")).unwrap()
                    );
                }
                elapsed.push(ready_ms);
            }
            // Same process fixture and production poll interval. This allowance
            // covers scheduling noise, not a claimed native Windows speedup.
            assert!(
                elapsed[1] <= elapsed[0] + 100.0,
                "receipt admission regressed: {elapsed:?}"
            );
        }
    }
}
