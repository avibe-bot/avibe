use super::*;
use std::io::Read;
use std::os::windows::io::{FromRawHandle, OwnedHandle};
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
        "0f48a585592d90136b417417602af36e919ca645538e9588d6deb1eb9d8e1293"
    );
}

#[test]
fn windows_health_requires_its_real_provider_not_macos_tcc_passes() {
    let mut report = json!({"schema_version":"1", "platform":"win32", "driver_version":"0.31.0", "overall":"ok",
    "checks":[
        {"name":"binary_version","status":"pass"}, {"name":"platform_supported","status":"pass"},
        {"name":"session_active","status":"pass"}, {"name":"ax_capability","status":"pass"},
        {"name":"screen_capture_capability","status":"pass"}, {"name":"bundle_identity","status":"skip"},
        {"name":"tcc_accessibility","status":"skip"}, {"name":"tcc_screen_recording","status":"skip"}
    ]});
    assert!(validate_health(&json!({"structuredContent":report})).is_ok());
    for index in 0..5 {
        report["checks"][index]["status"] = json!("fail");
        assert!(validate_health(&json!({"structuredContent":report})).is_err());
        report["checks"][index]["status"] = json!("pass");
    }
    report["platform"] = json!("darwin");
    assert!(validate_health(&json!({"structuredContent":report})).is_err());
    assert!(validate_capture(&json!({"content":[{"type":"text","text":"captured"}]})).is_err());
    assert!(validate_capture(&json!({"content":[{"type":"image","data":""}]})).is_err());
    assert!(validate_capture(&json!({"content":[{"type":"image","data":"fixture"}]})).is_ok());
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
    std::fs::write(root.join("started"), "started").unwrap();
    std::io::stdin().read_to_end(&mut Vec::new()).unwrap();
    if std::env::var_os("AVIBE_EXPECT_NON_READY").is_some() {
        let state: Value = serde_json::from_slice(&std::fs::read(root.join("computer-use.json")).unwrap()).unwrap();
        assert_ne!(state["state"], "ready", "non-ready must be published before driver EOF");
    }
    std::fs::write(root.join("retired"), "retired").unwrap();
}

/// The ordinary-user hosted harness runs this against the production transition
/// function and actual Job, not a mock kill callback or alternate test flags.
#[tokio::test(flavor = "current_thread")]
#[ignore = "requires disposable standard-user account; run by hosted process harness"]
async fn rollback_off_quit_and_failed_publication_retire_owned_driver() {
    require_ordinary_user().unwrap();
    for cause in ["rollback", "off", "quit", "write_failure"] {
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
        if cause != "write_failure" {
            env.push(("AVIBE_EXPECT_NON_READY".into(), "1".into()));
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
            "off" => lifecycle.toggle_off(),
            "quit" => lifecycle.quit(),
            _ => LifecycleDirective {
                write_state: true,
                ..LifecycleDirective::default()
            },
        };
        record.state = lifecycle.phase();
        record.enabled = lifecycle.enabled();
        record.reason = lifecycle.reason().map(str::to_owned);
        if cause == "write_failure" {
            let unwritable = ComputerUseStateStore::new(root.path().to_owned()); // file cannot replace a directory
            assert!(apply_transition(&unwritable, &record, directive, &mut driver)
                .await
                .is_err());
        } else {
            apply_transition(&store, &record, directive, &mut driver).await.unwrap();
        }
        assert!(driver.is_none());
        assert_eq!(unsafe { WaitForSingleObject(handle.as_raw_handle(), 0) }, WAIT_OBJECT_0);
        assert!(
            root.path().join("retired").is_file(),
            "child did not observe graceful EOF for {cause}"
        );
    }
}
