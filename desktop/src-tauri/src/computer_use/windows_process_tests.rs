use super::*;
use std::io::Read;
use std::process::{Child, Command, Stdio};
use std::time::Instant;
use windows_sys::Win32::System::Threading::{OpenProcess, PROCESS_SYNCHRONIZE};

const CHILD_TEST: &str = "computer_use_process::tests::fixture_child";
const HOST_TEST: &str = "computer_use_process::tests::fixture_host";
const FIXTURE: &str = "AVIBE_OWNED_DRIVER_TEST";

fn fixture_env(mode: &str, directory: &Path) -> Vec<(OsString, OsString)> {
    vec![
        (FIXTURE.into(), mode.into()),
        ("AVIBE_TEST_DIR".into(), directory.as_os_str().to_owned()),
        (
            "SystemRoot".into(),
            std::env::var_os("SystemRoot").expect("Windows SystemRoot"),
        ),
    ]
}

fn fixture_args() -> Vec<OsString> {
    ["--exact", CHILD_TEST, "--ignored", "--nocapture"]
        .map(OsString::from)
        .to_vec()
}

fn suspended(mode: &str, directory: &Path) -> OwnedDriver {
    OwnedDriver::spawn_suspended(
        &std::env::current_exe().unwrap(),
        &fixture_args(),
        &fixture_env(mode, directory),
        directory,
    )
    .expect("run process-lifecycle acceptance as a non-elevated Windows x64 user")
}

fn wait_for_file(path: &Path) -> String {
    let deadline = Instant::now() + Duration::from_secs(10);
    loop {
        if let Ok(value) = std::fs::read_to_string(path) {
            if !value.is_empty() {
                return value;
            }
        }
        assert!(Instant::now() < deadline, "fixture did not publish {}", path.display());
        std::thread::sleep(POLL_INTERVAL);
    }
}

fn process_handle(pid: u32) -> OwnedHandle {
    unsafe { owned(OpenProcess(PROCESS_SYNCHRONIZE, 0, pid)).unwrap() }
}

fn assert_exits(handle: &OwnedHandle) {
    assert_eq!(
        unsafe { WaitForSingleObject(handle.as_raw_handle(), 5000) },
        WAIT_OBJECT_0
    );
}

// These fixture entrypoints are not ordinary tests. Only a parent test
// supplies their private temporary-directory contract and runs them.
#[test]
#[ignore = "child entrypoint for process-lifecycle tests"]
fn fixture_child() {
    let mode = std::env::var(FIXTURE).unwrap();
    let directory = std::path::PathBuf::from(std::env::var_os("AVIBE_TEST_DIR").unwrap());
    match mode.as_str() {
        "eof" => {
            std::fs::write(directory.join("started"), "yes").unwrap();
            let mut input = Vec::new();
            std::io::stdin().read_to_end(&mut input).unwrap();
            std::fs::write(directory.join("eof"), "yes").unwrap();
        }
        "tree" => {
            // Intentionally outlives this fixture, but remains in the owned
            // job; the parent test waits its OS handle after stopping that job.
            #[allow(clippy::zombie_processes)]
            let descendant = Command::new(std::env::current_exe().unwrap())
                .args(fixture_args())
                .env_clear()
                .envs(fixture_env("hold", &directory))
                .stdin(Stdio::null())
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .spawn()
                .unwrap();
            std::fs::write(directory.join("descendant"), descendant.id().to_string()).unwrap();
            wait_for_file(&directory.join("started"));
            // Exit before the descendant: stop must inspect the job, not
            // mistake direct-child exit for complete tree teardown.
        }
        "hold" => {
            std::fs::write(directory.join("started"), "yes").unwrap();
            std::thread::sleep(Duration::from_secs(60));
        }
        _ => panic!("unknown fixture mode"),
    }
}

#[test]
#[ignore = "host entrypoint for shell-crash tests"]
fn fixture_host() {
    let mode = std::env::var(FIXTURE).unwrap();
    let directory = std::path::PathBuf::from(std::env::var_os("AVIBE_TEST_DIR").unwrap());
    let child = suspended("hold", &directory);
    let child = if mode == "running" {
        child.resume().unwrap()
    } else {
        child
    };
    std::fs::write(directory.join("child-pid"), child.id().to_string()).unwrap();
    // The external test kills this process without Rust destructors.
    std::thread::sleep(Duration::from_secs(60));
    drop(child);
}

#[test]
fn suspended_child_never_executes_and_drop_terminates_it() {
    let directory = tempfile::tempdir().unwrap();
    let child = suspended("hold", directory.path());
    let handle = process_handle(child.id());
    assert!(!child.has_exited().unwrap());
    assert!(!directory.path().join("started").exists());
    drop(child);
    assert_exits(&handle);
    assert!(!directory.path().join("started").exists());
}

#[tokio::test]
async fn failed_start_and_suspended_stop_leave_no_running_child() {
    let directory = tempfile::tempdir().unwrap();
    assert!(OwnedDriver::spawn_suspended(
        &directory.path().join("missing-driver.exe"),
        &[],
        &fixture_env("hold", directory.path()),
        directory.path(),
    )
    .is_err());
    let child = suspended("hold", directory.path());
    let handle = process_handle(child.id());
    child.stop(Duration::from_secs(60)).await.unwrap();
    assert_exits(&handle);
    assert!(!directory.path().join("started").exists());
}

#[tokio::test]
async fn stop_is_graceful_for_eof_and_forceful_for_remaining_descendants() {
    for mode in ["eof", "tree"] {
        let directory = tempfile::tempdir().unwrap();
        let child = suspended(mode, directory.path()).resume().unwrap();
        let root = process_handle(child.id());
        wait_for_file(&directory.path().join("started"));
        let descendant = (mode == "tree").then(|| {
            let pid = wait_for_file(&directory.path().join("descendant")).parse().unwrap();
            process_handle(pid)
        });
        let start = Instant::now();
        child.stop(Duration::from_millis(100)).await.unwrap();
        assert!(start.elapsed() < Duration::from_secs(5));
        assert_exits(&root);
        if let Some(descendant) = descendant {
            assert_exits(&descendant);
        }
        if mode == "eof" {
            assert!(directory.path().join("eof").exists());
        }
    }
}

#[tokio::test]
async fn cancelled_stop_terminates_owned_child_and_allows_fresh_start() {
    let directory = tempfile::tempdir().unwrap();
    let child = suspended("hold", directory.path()).resume().unwrap();
    let handle = process_handle(child.id());
    let task = tokio::spawn(child.stop(Duration::from_secs(60)));
    tokio::task::yield_now().await;
    task.abort();
    assert!(task.await.unwrap_err().is_cancelled());
    assert_exits(&handle);
    let next = suspended("eof", directory.path()).resume().unwrap();
    next.stop(Duration::from_secs(3)).await.unwrap();
}

struct Host(Child);
impl Drop for Host {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

#[test]
fn killed_shell_cleans_suspended_and_running_children_without_destructors() {
    for mode in ["suspended", "running"] {
        let directory = tempfile::tempdir().unwrap();
        let mut host = Host(
            Command::new(std::env::current_exe().unwrap())
                .args(["--exact", HOST_TEST, "--ignored", "--nocapture"])
                .env_clear()
                .envs(fixture_env(mode, directory.path()))
                .stdin(Stdio::null())
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .spawn()
                .unwrap(),
        );
        let pid = wait_for_file(&directory.path().join("child-pid")).parse().unwrap();
        let handle = process_handle(pid);
        if mode == "running" {
            wait_for_file(&directory.path().join("started"));
        }
        host.0.kill().unwrap();
        host.0.wait().unwrap();
        assert_exits(&handle);
    }
}

#[test]
fn launch_encoding_preserves_arguments_and_never_inherits_environment() {
    let arguments = ["", "a b", "trailing\\", "a\\\"b", "中文"].map(OsString::from);
    let line = command_line(OsStr::new(r"C:\Program Files\driver.exe"), &arguments).unwrap();
    assert_eq!(
        String::from_utf16(&line[..line.len() - 1]).unwrap(),
        "\"C:\\Program Files\\driver.exe\" \"\" \"a b\" \"trailing\\\\\" \"a\\\\\\\"b\" \"中文\""
    );
    assert_eq!(environment_block(&[]).unwrap(), vec![0, 0]);
    let env = environment_block(&[("z".into(), "value".into()), ("a".into(), "".into())]).unwrap();
    assert_eq!(String::from_utf16(&env).unwrap(), "A=\0Z=value\0\0");
    assert!(environment_block(&[("a".into(), "1".into()), ("A".into(), "2".into())]).is_err());
    assert!(environment_block(&[("bad=key".into(), "x".into())]).is_err());
    assert!(environment_block(&[("key".into(), "x\0y".into())]).is_err());
    assert!(command_line(OsStr::new("x\0y"), &[]).is_err());
}
