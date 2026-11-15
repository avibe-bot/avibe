use super::*;
use std::io::Read;
use std::os::windows::io::{AsRawHandle, FromRawHandle, OwnedHandle};
use windows_sys::Win32::Foundation::WAIT_OBJECT_0;
use windows_sys::Win32::System::Threading::{
    OpenProcess, WaitForSingleObject, PROCESS_QUERY_LIMITED_INFORMATION, PROCESS_SYNCHRONIZE,
};

#[test]
fn windows_requires_explicit_runtime_capability_not_legacy_schema_one() {
    for (payload, verdict) in [
        (
            json!({"computer_use_schema":1,"controller_id":"old"}),
            CapabilityVerdict::Unsupported,
        ),
        (
            json!({"computer_use_windows_schema":0,"controller_id":"new"}),
            CapabilityVerdict::Unsupported,
        ),
        (
            json!({"computer_use_windows_schema":true,"controller_id":"new"}),
            CapabilityVerdict::Unsupported,
        ),
        (
            json!({"computer_use_windows_schema":1,"controller_id":"new"}),
            CapabilityVerdict::Supported,
        ),
        (json!({"computer_use_windows_schema":1}), CapabilityVerdict::Transient),
    ] {
        assert_eq!(classify_capabilities(&payload).verdict, verdict);
    }
    let snapshot = include_bytes!("../../../cua-driver/tools-windows-v0.31.0.json");
    assert_eq!(
        lower_hex(&Sha256::digest(snapshot)),
        "20bfe2c6c205c22e35465706736704f5f8effbab88a1e79357646f556d9a5cad"
    );
}

#[test]
fn windows_health_requires_its_real_provider_not_macos_tcc_passes() {
    let report = json!({"schema_version":"1", "platform":"win32", "driver_version":"0.31.0", "overall":"ok",
    "checks":[
        {"name":"binary_version","status":"pass","message":"ok"},
        {"name":"platform_supported","status":"pass","message":"ok"},
        {"name":"session_active","status":"pass","message":"ok"},
        {"name":"bundle_identity","status":"skip","message":"not applicable on Windows"},
        {"name":"tcc_accessibility","status":"skip","message":"not applicable on Windows"},
        {"name":"tcc_screen_recording","status":"skip","message":"not applicable on Windows"},
        {"name":"ax_capability","status":"pass","message":"ok"},
        {"name":"screen_capture_capability","status":"pass","message":"ok"}
    ]});
    assert!(validate_health(&json!({"structuredContent":report})).is_ok());
    // Startup must reject incomplete or misleading provider evidence. The old
    // validator accepted omitted Windows skips, extra checks, and TCC passes.
    for index in 0..8 {
        let mut missing = report.clone();
        missing["checks"].as_array_mut().unwrap().remove(index);
        assert!(validate_health(&json!({"structuredContent":missing})).is_err());
        let mut wrong = report.clone();
        wrong["checks"][index]["status"] = json!(if (3..6).contains(&index) { "pass" } else { "fail" });
        assert!(validate_health(&json!({"structuredContent":wrong})).is_err());
        let mut duplicate = report.clone();
        duplicate["checks"][index] = report["checks"][(index + 1) % 8].clone();
        assert!(validate_health(&json!({"structuredContent":duplicate})).is_err());
    }
    for index in 3..6 {
        let mut misleading = report.clone();
        misleading["checks"][index]["message"] = json!("permission granted");
        assert!(validate_health(&json!({"structuredContent":misleading})).is_err());
    }
    let mut extra = report.clone();
    extra["checks"]
        .as_array_mut()
        .unwrap()
        .push(json!({"name":"unknown","status":"pass"}));
    assert!(validate_health(&json!({"structuredContent":extra})).is_err());
    let mut wrong_platform = report;
    wrong_platform["platform"] = json!("darwin");
    assert!(validate_health(&json!({"structuredContent":wrong_platform})).is_err());
    assert!(validate_capture(&json!({"content":[{"type":"text","text":"captured"}]})).is_err());
    assert!(validate_capture(&json!({"content":[{"type":"image","data":""}]})).is_err());
    assert!(validate_capture(&json!({"content":[{"type":"image","data":"fixture"}]})).is_ok());
}

#[test]
fn windows_security_refusals_survive_lifecycle_normalization() {
    // Low-level connected-object/token failures must remain a pipe refusal,
    // not get laundered into the generic health error in the published record.
    for reason in ["object_unverified", "token_unverified", "pipe_unverified"] {
        let mut lifecycle = ComputerUseLifecycle::off();
        lifecycle.set_error(stable_error(&io::Error::other(reason), "health_failed"));
        assert_eq!(lifecycle.reason(), Some("pipe_unverified"));
    }
}

#[test]
fn windows_unavailable_activation_controls_preserve_the_off_route() {
    // Capability alone used to enable a no-op tray/Settings action when assets
    // or saving failed. Test the shared production command/tray decision.
    let mut view = MenuView {
        available: true,
        ..MenuView::default()
    };
    assert!(crate::computer_use_control_enabled(&view));
    for (assets, save, enabled, expected) in [
        (true, false, false, false),
        (false, true, false, false),
        (true, false, true, true),
        (false, true, true, true),
    ] {
        view.asset_error = assets;
        view.save_failed = save;
        view.enabled = enabled;
        assert_eq!(crate::computer_use_control_enabled(&view), expected);
    }
    view = MenuView {
        available: true,
        invalid_state: true,
        ..MenuView::default()
    };
    assert!(
        crate::computer_use_control_enabled(&view),
        "recovered invalid state can be enabled"
    );
    view.initialization_error = true;
    assert!(!crate::computer_use_control_enabled(&view));
    view.initialization_error = false;
    view.available = false;
    view.enabled = true;
    assert!(
        !crate::computer_use_control_enabled(&view),
        "rollback disables the tray"
    );
}

#[test]
fn windows_driver_environment_does_not_inherit_arbitrary_parent_values() {
    let values = driver_environment(std::path::Path::new(r"C:\fixture\policy.yaml"));
    let allowed = [
        "SystemRoot",
        "WINDIR",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "TEMP",
        "TMP",
        "CUA_DRIVER_EMBEDDED",
        "CUA_DRIVER_RS_TELEMETRY_ENABLED",
        "CUA_DRIVER_RS_UPDATE_CHECK",
        "CUA_DRIVER_EMBEDDED_HOST_PID",
        "CUA_DRIVER_MANAGED_POLICY_FILE",
        "CUA_DRIVER_WINDOW_CHANGE_TIMEOUT_MS",
    ];
    assert!(values
        .iter()
        .all(|(key, _)| allowed.contains(&key.to_string_lossy().as_ref())));
    assert!(values.contains(&(
        "CUA_DRIVER_MANAGED_POLICY_FILE".into(),
        r"C:\fixture\policy.yaml".into()
    )));
    assert!(values.contains(&("CUA_DRIVER_EMBEDDED".into(), "1".into())));
}

#[test]
#[ignore = "child entrypoint for isolated ordinary-user activation test"]
fn owned_child() {
    let root = PathBuf::from(std::env::var_os("AVIBE_ACTIVATION_FIXTURE").expect("test-owned root"));
    if std::env::var_os("AVIBE_OFF_COMPLETION_BARRIER").is_some() {
        // A helper that ignores EOF forces the production Job stop fallback.
        // Never an external/user process: this is another entrypoint of this
        // isolated test executable, under the same owned Job.
        let mut helper = std::process::Command::new(std::env::current_exe().unwrap())
            .args(["--exact", "computer_use::tests::off_helper", "--ignored", "--nocapture"])
            .spawn()
            .unwrap();
        std::fs::write(root.join("helper-pid"), helper.id().to_string()).unwrap();
        // Job owns cleanup; retain the child handle without waiting here.
        let _ = helper.try_wait();
    }
    std::fs::write(root.join("started"), "started").unwrap();
    std::io::stdin().read_to_end(&mut Vec::new()).unwrap();
    if std::env::var_os("AVIBE_OFF_COMPLETION_BARRIER").is_some() {
        while !root.join("release-stop").exists() {
            std::thread::sleep(Duration::from_millis(10));
        }
    }
    if std::env::var_os("AVIBE_EXPECT_NON_READY").is_some() {
        let state: Value = serde_json::from_slice(&std::fs::read(root.join("computer-use.json")).unwrap()).unwrap();
        assert_ne!(state["state"], "ready", "non-ready must be published before driver EOF");
    }
    std::fs::write(root.join("retired"), "retired").unwrap();
}

#[test]
#[ignore = "helper entrypoint owned by the native Off fixture's Job"]
fn off_helper() {
    assert!(std::env::var_os("AVIBE_OFF_COMPLETION_BARRIER").is_some());
    std::thread::sleep(Duration::from_secs(30));
}

/// The ordinary-user hosted harness runs this against the production transition
/// function and actual Job, not a mock kill callback or alternate test flags.
#[tokio::test(flavor = "current_thread")]
#[ignore = "requires disposable standard-user account; run by hosted process harness"]
async fn rollback_off_quit_and_failed_publication_retire_owned_driver() {
    require_ordinary_user().unwrap();
    for cause in [
        "rollback",
        "off",
        "quit",
        "write_failure",
        "off_write_failure",
        "off_stop_failure",
    ] {
        let root = tempfile::tempdir().unwrap();
        let executable = std::env::current_exe().unwrap();
        let store = ComputerUseStateStore::new(root.path().join("computer-use.json"));
        let mut lifecycle = ComputerUseLifecycle::off();
        lifecycle.toggle_on(RuntimeSupport::Supported, Grants::all());
        lifecycle.startup_health(HealthResult::Pass, Duration::ZERO);
        let mut record = ComputerUseRecord {
            schema_version: 1,
            enabled: true,
            state: ComputerUsePhase::Ready,
            reason: None,
            shell_pid: std::process::id(),
            instance_id: "fixture".to_owned(),
            generation: 1,
            driver_version: "0.31.0".to_owned(),
            tool_snapshot: ToolSnapshot {
                path: root.path().join("tools.json"),
                sha256: COMPUTER_USE_WINDOWS_TOOL_SNAPSHOT_SHA256.to_owned(),
            },
            socket_path: Some(PathBuf::from(r"\\.\pipe\unused-activation-fixture")),
            proxy_executable: Some(executable.clone()),
            host_bundle_id: Some("fixture".to_owned()),
        };
        store.write(&record).unwrap();
        let mut env = vec![("AVIBE_ACTIVATION_FIXTURE".into(), root.path().as_os_str().to_owned())];
        if !cause.ends_with("write_failure") {
            env.push(("AVIBE_EXPECT_NON_READY".into(), "1".into()));
        }
        if cause == "off" || cause == "off_stop_failure" {
            env.push(("AVIBE_OFF_COMPLETION_BARRIER".into(), "1".into()));
        }
        if let Some(system_root) = std::env::var_os("SystemRoot") {
            env.push(("SystemRoot".into(), system_root));
        }
        let args: Vec<OsString> = [
            "--exact",
            "computer_use::tests::owned_child",
            "--ignored",
            "--nocapture",
        ]
        .into_iter()
        .map(Into::into)
        .collect();
        let driver = OwnedDriver::spawn_suspended(&executable, &args, &env, root.path())
            .unwrap()
            .resume()
            .unwrap();
        let handle = unsafe { OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SYNCHRONIZE, 0, driver.id()) };
        assert!(!handle.is_null());
        let handle = unsafe { OwnedHandle::from_raw_handle(handle) };
        let mut driver = Some(driver);
        let mut pending_off = false;
        timeout(Duration::from_secs(5), async {
            while !root.path().join("started").exists() {
                sleep(Duration::from_millis(20)).await;
            }
        })
        .await
        .unwrap();
        let directive = match cause {
            "rollback" => {
                let mut cache = CapabilityCache::default();
                assert_eq!(
                    cache.observe(classify_capabilities(
                        &json!({"controller_id":"same", "computer_use_windows_schema":1})
                    )),
                    RuntimeSupport::Supported
                );
                let support = cache.observe(classify_capabilities(
                    &json!({"controller_id":"same", "computer_use_windows_schema":0}),
                ));
                lifecycle.capabilities(support, Grants::all())
            }
            "off" | "off_write_failure" | "off_stop_failure" => lifecycle.toggle_off(),
            "quit" => lifecycle.quit(),
            _ => LifecycleDirective {
                write_state: true,
                ..LifecycleDirective::default()
            },
        };
        record.state = lifecycle.phase();
        record.enabled = lifecycle.enabled();
        record.reason = lifecycle.reason().map(str::to_owned);
        if cause.ends_with("write_failure") {
            let unwritable = ComputerUseStateStore::new(root.path().to_owned()); // file cannot replace a directory
            assert!(apply_transition(
                &unwritable,
                &record,
                directive,
                &mut driver,
                &mut lifecycle,
                &mut pending_off
            )
            .await
            .is_err());
        } else if cause == "off" || cause == "off_stop_failure" {
            let helper_pid: u32 = std::fs::read_to_string(root.path().join("helper-pid"))
                .unwrap()
                .parse()
                .unwrap();
            let helper = unsafe { OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SYNCHRONIZE, 0, helper_pid) };
            assert!(!helper.is_null());
            let helper = unsafe { OwnedHandle::from_raw_handle(helper) };
            driver.as_ref().unwrap().assert_member(helper.as_handle());
            if cause == "off_stop_failure" {
                let original_pid = driver.as_ref().unwrap().id();
                driver.as_mut().unwrap().fail_stops(2);
                for _ in 0..2 {
                    assert!(apply_transition(
                        &store,
                        &record,
                        directive.clone(),
                        &mut driver,
                        &mut lifecycle,
                        &mut pending_off
                    )
                    .await
                    .is_err());
                    assert_eq!(
                        driver.as_ref().unwrap().id(),
                        original_pid,
                        "failed stop must retain its owner"
                    );
                    assert!(pending_off);
                    assert!(!lifecycle.enabled());
                    assert_ne!(unsafe { WaitForSingleObject(helper.as_raw_handle(), 0) }, WAIT_OBJECT_0);
                }
            }
            let mut transition = Box::pin(apply_transition(
                &store,
                &record,
                directive,
                &mut driver,
                &mut lifecycle,
                &mut pending_off,
            ));
            assert!(timeout(Duration::from_millis(50), &mut transition).await.is_err());
            let StoredComputerUseState::Current(saved) = store.read() else {
                panic!("off was not written first")
            };
            assert!(!saved.enabled);
            assert_ne!(unsafe { WaitForSingleObject(handle.as_raw_handle(), 0) }, WAIT_OBJECT_0);
            assert_ne!(unsafe { WaitForSingleObject(helper.as_raw_handle(), 0) }, WAIT_OBJECT_0);
            std::fs::write(root.path().join("release-stop"), "release").unwrap();
            transition.await.unwrap();
            assert_eq!(
                unsafe { WaitForSingleObject(helper.as_raw_handle(), 0) },
                WAIT_OBJECT_0,
                "Off cannot complete with an owned helper still alive"
            );
        } else {
            apply_transition(
                &store,
                &record,
                directive,
                &mut driver,
                &mut lifecycle,
                &mut pending_off,
            )
            .await
            .unwrap();
        }
        assert!(driver.is_none());
        assert_eq!(unsafe { WaitForSingleObject(handle.as_raw_handle(), 0) }, WAIT_OBJECT_0);
        assert!(
            root.path().join("retired").is_file(),
            "child did not observe graceful EOF for {cause}"
        );
        if cause.starts_with("off") {
            assert!(!lifecycle.enabled());
            // The production post-failure lifecycle cannot silently respawn on
            // later capability/health ticks. Only a subsequent explicit On can.
            for tick in 1..=5 {
                if cause == "off_write_failure" {
                    assert!(pending_off);
                    let unwritable = ComputerUseStateStore::new(root.path().to_owned());
                    let off = lifecycle.toggle_off();
                    assert!(
                        apply_transition(&unwritable, &record, off, &mut driver, &mut lifecycle, &mut pending_off)
                            .await
                            .is_err()
                    );
                    assert!(pending_off);
                    assert!(driver.is_none());
                }
                let capability_tick = lifecycle.capabilities(RuntimeSupport::Supported, Grants::all());
                assert!(!capability_tick.spawn_daemon);
                apply_transition(
                    &store,
                    &record,
                    capability_tick,
                    &mut driver,
                    &mut lifecycle,
                    &mut pending_off,
                )
                .await
                .unwrap();
                assert_eq!(pending_off, cause == "off_write_failure");
                assert!(
                    !lifecycle
                        .retry_recovery(
                            RuntimeSupport::Supported,
                            Grants::all(),
                            Duration::from_secs(tick * 5),
                            true
                        )
                        .spawn_daemon
                );
                assert!(!lifecycle.enabled());
            }
            if cause == "off_write_failure" {
                let StoredComputerUseState::Current(saved) = store.read() else {
                    panic!("saved record disappeared")
                };
                assert!(saved.enabled, "failed write must not pretend disk is off");
                assert!(
                    ComputerUseLifecycle::from_record(&saved).enabled(),
                    "restart still honors old opt-in"
                );
            }
            // A repeated Disable retries persistence, remains Off, and has no
            // owned process to kill; no toggle-back-to-On semantics.
            for _ in 0..2 {
                let off = lifecycle.toggle_off();
                apply_transition(&store, &record, off, &mut driver, &mut lifecycle, &mut pending_off)
                    .await
                    .unwrap();
                let StoredComputerUseState::Current(saved) = store.read() else {
                    panic!("off not persisted")
                };
                assert!(!saved.enabled);
                assert!(!pending_off, "successful off persistence/teardown settles tick retry");
                assert!(driver.is_none());
                assert!(!ComputerUseLifecycle::from_record(&saved).enabled());
            }
        }
    }
}
