//! Real cmd_start -> local HTTP -> stdout -> RuntimeHost admission.
//! Run with AVIBE_TEST_PYTHON pointing at the repository test environment and
//! `cargo test -p avibe-runtime-host cmd_start_receipt_admission -- --ignored --nocapture`.
use super::*;
use crate::bootstrap_log::BootstrapLog;
use crate::health::HttpHealthProbe;
use crate::launcher::LaunchWatch;
use crate::receipt::StartupReceiptStream;
use crate::status::BootstrapPhase;
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicUsize, Ordering};

struct Fixture {
    child: Mutex<Child>,
    origin: LoopbackOrigin,
    scratch: PathBuf,
    receipt: bool,
    launches: AtomicUsize,
    descendants: bool,
    expected_failure: bool,
}

impl Fixture {
    fn start(root: &Path, case: &str, receipt: bool, delay: f64, housekeeping: f64, sample: usize) -> Arc<Self> {
        let scratch = std::env::temp_dir().join(format!(
            "avibe startup 非 ASCII {} {case} {receipt} {sample}",
            std::process::id()
        ));
        std::fs::create_dir_all(&scratch).unwrap();
        let stderr_path = scratch.join("stderr.log");
        let stderr = std::fs::File::create(&stderr_path).unwrap();
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
            .arg(case)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::from(stderr))
            .spawn()
            .expect("fixture Python starts");
        let deadline = std::time::Instant::now() + Duration::from_secs(15);
        let endpoint = scratch.join("endpoint");
        while !endpoint.exists() {
            if let Some(status) = child.try_wait().unwrap() {
                panic!("fixture exited {status}: {}", diagnostic_tail(&stderr_path));
            }
            if std::time::Instant::now() >= deadline {
                child.kill().ok();
                child.wait().ok();
                panic!("fixture initialization timed out: {}", diagnostic_tail(&stderr_path));
            }
            std::thread::sleep(Duration::from_millis(5));
        }
        Arc::new(Self {
            origin: LoopbackOrigin::parse(&std::fs::read_to_string(endpoint).unwrap()).unwrap(),
            child: Mutex::new(child),
            scratch,
            receipt,
            launches: AtomicUsize::new(0),
            descendants: case == "eof",
            expected_failure: case == "budget",
        })
    }

    fn finish(&self) -> serde_json::Value {
        let mut child = self.child.lock().unwrap();
        writeln!(child.stdin.as_mut().unwrap(), "done").unwrap();
        let deadline = std::time::Instant::now() + Duration::from_secs(10);
        loop {
            if let Some(status) = child.try_wait().unwrap() {
                assert_eq!(
                    status.success(),
                    !self.expected_failure,
                    "{}",
                    diagnostic_tail(&self.scratch.join("stderr.log"))
                );
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
        if self.descendants {
            std::fs::write(self.scratch.join("children-stop"), "").ok();
            let deadline = std::time::Instant::now() + Duration::from_secs(5);
            while ["service-done", "ui-done"]
                .iter()
                .any(|name| !self.scratch.join(name).exists())
                && std::time::Instant::now() < deadline
            {
                std::thread::sleep(Duration::from_millis(20));
            }
        }
        if let Ok(child) = self.child.get_mut() {
            if child.try_wait().ok().flatten().is_none() {
                child.kill().ok();
            }
            child.wait().ok();
        }
        std::fs::remove_dir_all(&self.scratch).ok();
    }
}

fn diagnostic_tail(path: &Path) -> String {
    use std::io::{Seek, SeekFrom};
    let mut file = std::fs::File::open(path).unwrap();
    let size = file.metadata().unwrap().len();
    file.seek(SeekFrom::Start(size.saturating_sub(8192))).unwrap();
    let mut tail = Vec::new();
    file.take(8192).read_to_end(&mut tail).unwrap();
    String::from_utf8_lossy(&tail).into_owned()
}

#[test]
#[ignore = "requires repository Python dependencies; run on both desktop CI platforms"]
fn cmd_start_stdout_reaches_eof_while_runtime_children_remain_alive() {
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap()
        .parent()
        .unwrap()
        .to_owned();
    let fixture = Fixture::start(&root, "eof", true, 0.0, 0.0, 0);
    let mut stdout = {
        let mut child = fixture.child.lock().unwrap();
        let stdout = child.stdout.take().unwrap();
        writeln!(child.stdin.as_mut().unwrap(), "start").unwrap();
        stdout
    };
    let (sender, receiver) = std::sync::mpsc::sync_channel(1);
    let reader = std::thread::spawn(move || {
        let mut bytes = Vec::new();
        // This is the real launcher pipe, with no transport/spawn mocks.
        let result = stdout.read_to_end(&mut bytes).map(|_| bytes);
        let _ = sender.send(result);
    });
    fixture.finish(); // waits for actual cmd_start and the launcher process exit
    let bytes = receiver
        .recv_timeout(Duration::from_secs(1))
        .expect("reader sees EOF after helper exit")
        .unwrap();
    reader.join().unwrap();
    assert!(String::from_utf8(bytes).unwrap().contains("\"outcome\":\"ready\""));
    // These acknowledgements are written after the helper exited: the actual
    // service/UI spawn primitives and their log sinks did not inherit stdout.
    std::fs::write(fixture.scratch.join("prove-alive"), "").unwrap();
    let deadline = std::time::Instant::now() + Duration::from_secs(2);
    while ["service-alive", "ui-alive"]
        .iter()
        .any(|name| !fixture.scratch.join(name).exists())
    {
        assert!(
            std::time::Instant::now() < deadline,
            "both Runtime fixture children outlive the helper"
        );
        std::thread::sleep(Duration::from_millis(10));
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
        self.launch_with_ready_timeout(false, DEFAULT_READY_TIMEOUT)
    }
    fn launch_with_ready_timeout(
        &self,
        _hand_over: bool,
        ready_timeout: Duration,
    ) -> Result<LaunchedRuntime, LaunchError> {
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
        if self.receipt {
            // Use the exact argument builder used by the bundled launcher, then
            // let the actual Python parser and cmd_start consume its budget.
            let args = crate::launcher::desktop_start_arguments(ready_timeout);
            writeln!(
                child.stdin.as_mut().unwrap(),
                "{}",
                serde_json::to_string(&args).unwrap()
            )
            .unwrap();
        } else {
            writeln!(child.stdin.as_mut().unwrap(), "start").unwrap();
        }
        Ok(LaunchedRuntime { pid: child.id(), watch })
    }
    fn expected_runtime_id(&self) -> Option<&str> {
        Some("bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")
    }
    fn record_bootstrap_event(&self, event: &str) {
        BootstrapLog::at(self.scratch.join("bootstrap.log")).record(event, &[]);
    }
}

#[tokio::test]
#[ignore = "requires repository Python and AVIBE_DESKTOP_READY_TIMEOUT_SECONDS=1"]
async fn cmd_start_receipt_timeout_override() {
    let settings = RuntimeHostSettings::from_env();
    assert_eq!(
        settings.ready_timeout,
        Duration::from_secs(1),
        "run with the documented shell override"
    );
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap()
        .parent()
        .unwrap()
        .to_owned();
    let fixture = Fixture::start(&root, "budget", true, 2.5, 0.0, 0);
    let probe = Arc::new(HttpHealthProbe::new(settings.probe_timeout).unwrap());
    let host = RuntimeHost::new(probe, Arc::new(fixture.clone()), settings);
    let status = host.bootstrap(&DiscardStatus, BootstrapTrigger::Launch).await;
    assert_eq!(
        status.phase,
        BootstrapPhase::Failed,
        "the override expires before Controller readiness"
    );
    let trace = fixture.finish();
    assert_eq!(trace["ready_timeout"], 1.0);
    assert_eq!(
        trace["result"], 1,
        "the producer also expires, rather than keeping its old 120s budget"
    );
    assert!(trace["helper_work_ms"].as_f64().unwrap() < 1800.0);
    assert_eq!(fixture.launches.load(Ordering::SeqCst), 1);
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
