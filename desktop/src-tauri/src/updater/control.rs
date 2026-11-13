//! Discovery/channel commands share updater ownership but never enter installation.
use super::*;
use avibe_runtime_host::update_control::{
    read_control_request, ControlAction, ControlPhase, ControlRequest, ControlRequestError,
    CONTROL_CHECK_TIMEOUT_SECONDS, CONTROL_REQUEST_FILE,
};

#[derive(Debug, PartialEq, Eq)]
enum Claim {
    Ignore,
    Busy,
    Start,
}

fn claim(state: &mut State, busy: &AtomicBool, request: &ControlRequest, now: SystemTime) -> Claim {
    if state
        .control_request
        .as_ref()
        .is_some_and(|receipt| receipt.request_id == request.request_id || receipt.phase == ControlPhase::Checking)
    {
        return Claim::Ignore;
    }
    if busy.swap(true, Ordering::SeqCst) {
        state.control_request = Some(ControlStatus::new(
            request,
            ControlPhase::Failed,
            Some("update_in_progress"),
            now,
        ));
        return Claim::Busy;
    }
    state.control_request = Some(ControlStatus::new(request, ControlPhase::Checking, None, now));
    Claim::Start
}

fn write_status(app: &AppHandle, state: &State) -> bool {
    let updater = app.state::<Updater>();
    let record = base_status(app, state)
        .with_remote(state.remote_install_allowed, state.remote_request.clone())
        .with_control(state.control_request.clone());
    if let Err(error) = updater.status.write(&record) {
        eprintln!("desktop update control status: {error}");
        false
    } else {
        true
    }
}

fn record(app: &AppHandle) -> bool {
    let updater = app.state::<Updater>();
    let saved = write_status(app, &updater.state.lock().expect("updater state"));
    emit_state(app);
    saved
}

fn settle(
    state: &mut State,
    busy: &AtomicBool,
    request: &ControlRequest,
    phase: ControlPhase,
    code: Option<&str>,
) -> bool {
    if !state
        .control_request
        .as_ref()
        .is_some_and(|receipt| receipt.request_id == request.request_id && receipt.phase == ControlPhase::Checking)
    {
        return false;
    }
    state.control_request = Some(ControlStatus::new(request, phase, code, SystemTime::now()));
    busy.store(false, Ordering::SeqCst);
    true
}

fn finish(app: &AppHandle, request: &ControlRequest, phase: ControlPhase, code: Option<&str>) {
    let updater = app.state::<Updater>();
    let changed = {
        let mut state = updater.state.lock().expect("updater state");
        let changed = settle(&mut state, &updater.busy, request, phase, code);
        if changed {
            write_status(app, &state);
        }
        changed
    };
    if changed {
        emit_state(app);
        let _ = updater.channel_menu.set_enabled(!updater.busy.load(Ordering::SeqCst));
    }
}

fn revalidate(path: &Path, request: &ControlRequest, now: SystemTime) -> Result<(), &'static str> {
    match read_control_request(path, now) {
        Ok(current) if current.same_intent(request) => Ok(()),
        Err(ControlRequestError::Expired(current)) if current.same_intent(request) => Err("expired"),
        _ => Err("request_replaced"),
    }
}

pub(super) fn poll(app: &AppHandle) {
    let updater = app.state::<Updater>();
    let path = updater.remote_request_path.with_file_name(CONTROL_REQUEST_FILE);
    let request = match read_control_request(&path, SystemTime::now()) {
        Ok(request) => request,
        Err(_) => return,
    };
    let result = claim(
        &mut updater.state.lock().expect("updater state"),
        &updater.busy,
        &request,
        SystemTime::now(),
    );
    if result == Claim::Ignore {
        return;
    }
    if result == Claim::Busy {
        record(app);
        return;
    }
    set_state(app, Phase::Checking, None);
    if !record(app) {
        // Without a durable acknowledgement the Runtime cannot know who owns
        // this request; do not perform its persistent channel mutation.
        set_state(app, Phase::Error, None);
        finish(app, &request, ControlPhase::Failed, Some("status_unavailable"));
        return;
    }
    let handle = app.clone();
    tauri::async_runtime::spawn(async move { run(handle, path, request).await });
}

async fn run(app: AppHandle, path: PathBuf, request: ControlRequest) {
    if let Err(code) = revalidate(&path, &request, SystemTime::now()) {
        set_state(&app, Phase::Error, None);
        finish(&app, &request, ControlPhase::Expired, Some(code));
        return;
    }
    if request.action == ControlAction::Prereleases {
        let channel = Channel::of(request.enabled.expect("validated channel intent"));
        let updater = app.state::<Updater>();
        // This control request owns busy. The ordinary menu setter deliberately
        // refuses busy work, so persist under the same state mutex here.
        let saved = {
            let mut state = updater.state.lock().expect("updater state");
            persist_channel(&updater.channel_path, channel).map(|()| {
                state.channel = channel;
                state.latest = None;
            })
        };
        if let Err(error) = saved {
            eprintln!("desktop update control channel: {error}");
            set_state(&app, Phase::Error, None);
            finish(&app, &request, ControlPhase::Failed, Some("channel_save_failed"));
            return;
        }
        let _ = updater.channel_menu.set_checked(channel == Channel::Test);
        emit_state(&app);
    }
    if KEY.trim().is_empty() || TARGET == "unsupported" {
        set_state(&app, Phase::Disabled, None);
        finish(&app, &request, ControlPhase::Failed, Some("updater_disabled"));
        return;
    }
    let channel = app.state::<Updater>().state.lock().expect("updater state").channel;
    let outcome = tokio::time::timeout(
        Duration::from_secs(CONTROL_CHECK_TIMEOUT_SECONDS),
        find_update(&app, channel),
    )
    .await;
    if let Err(code) = revalidate(&path, &request, SystemTime::now()) {
        set_state(&app, Phase::Error, None);
        finish(&app, &request, ControlPhase::Expired, Some(code));
        return;
    }
    match outcome {
        Ok(Ok(Some((manifest, _)))) => {
            set_available(&app, manifest);
            finish(&app, &request, ControlPhase::Completed, None);
        }
        Ok(Ok(None)) => {
            set_state(&app, Phase::Current, None);
            finish(&app, &request, ControlPhase::Completed, None);
        }
        error => {
            eprintln!("desktop update control check: {error:?}");
            set_state(&app, Phase::Error, None);
            finish(&app, &request, ControlPhase::Failed, Some("check_failed"));
        }
    }
    // A successful check exposes an offer only. Never prompt or install here.
}

#[cfg(test)]
mod tests {
    use super::*;

    fn request() -> ControlRequest {
        ControlRequest::parse(
            &serde_json::to_vec(&serde_json::json!({
                "schema_version": 1, "request_id": "00000000-0000-4000-8000-000000000001",
                "action": "check", "state": "pending",
                "created_at": "1970-01-01T00:16:40Z",
                "expires_at": "1970-01-01T00:18:40Z"
            }))
            .unwrap(),
            SystemTime::UNIX_EPOCH + Duration::from_secs(1000),
        )
        .unwrap()
    }

    fn state() -> State {
        State {
            phase: Phase::Current,
            latest: None,
            channel: Channel::Stable,
            manifest: None,
            availability_interactive: false,
            remote_install_allowed: false,
            remote_request: None,
            active_request: None,
            control_request: None,
        }
    }

    #[test]
    fn a_control_claim_is_idempotent_and_settles_its_busy_gate_once() {
        let request = request();
        let mut state = state();
        let busy = AtomicBool::new(false);
        assert_eq!(claim(&mut state, &busy, &request, SystemTime::now()), Claim::Start);
        assert!(busy.load(Ordering::SeqCst));
        assert_eq!(claim(&mut state, &busy, &request, SystemTime::now()), Claim::Ignore);
        assert!(settle(&mut state, &busy, &request, ControlPhase::Completed, None));
        assert!(!busy.load(Ordering::SeqCst));
        busy.store(true, Ordering::SeqCst); // another operation now owns it
        assert!(!settle(&mut state, &busy, &request, ControlPhase::Completed, None));
        assert!(busy.load(Ordering::SeqCst));
        assert_eq!(claim(&mut state, &busy, &request, SystemTime::now()), Claim::Ignore);
    }

    #[test]
    fn restart_keeps_a_tombstone_until_a_new_control_identity_arrives() {
        let request = request();
        let now = SystemTime::UNIX_EPOCH + Duration::from_secs(1000);
        let mut state = state();
        state.control_request = Some(ControlStatus::new(&request, ControlPhase::Checking, None, now).interrupted(now));
        let busy = AtomicBool::new(false);
        assert_eq!(
            state.control_request.as_ref().unwrap().error_code.as_deref(),
            Some("shell_interrupted")
        );
        assert_eq!(claim(&mut state, &busy, &request, now), Claim::Ignore);
        assert!(!busy.load(Ordering::SeqCst));
        let mut new_request = request.clone();
        new_request.request_id = "00000000-0000-4000-8000-000000000002".into();
        assert_eq!(claim(&mut state, &busy, &new_request, now), Claim::Start);
        assert!(busy.load(Ordering::SeqCst));
    }

    #[test]
    fn stale_local_settlement_cannot_release_a_control_owner() {
        let request = request();
        let mut state = state();
        let busy = AtomicBool::new(false);
        assert_eq!(claim(&mut state, &busy, &request, SystemTime::now()), Claim::Start);
        assert!(!super::super::settle(
            &mut state,
            &busy,
            None,
            Settlement::Completed,
            SystemTime::now()
        ));
        assert!(busy.load(Ordering::SeqCst));
    }

    #[test]
    fn mailbox_revalidation_binds_the_whole_intent_and_expiry() {
        let fixture: serde_json::Value =
            serde_json::from_str(include_str!("../../../../tests/fixtures/desktop_update_control.json")).unwrap();
        let raw = fixture["request"].clone();
        let now = SystemTime::UNIX_EPOCH + Duration::from_secs(1000);
        let request = ControlRequest::parse(&serde_json::to_vec(&raw).unwrap(), now).unwrap();
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join(CONTROL_REQUEST_FILE);
        fs::write(&path, serde_json::to_vec(&raw).unwrap()).unwrap();
        assert_eq!(revalidate(&path, &request, now), Ok(()));
        assert_eq!(
            revalidate(&path, &request, now + Duration::from_secs(120)),
            Err("expired")
        );
        for (key, value, expected) in [
            ("enabled", serde_json::json!(true), "request_replaced"),
            (
                "expires_at",
                serde_json::json!("1970-01-01T00:18:39Z"),
                "request_replaced",
            ),
            ("state", serde_json::json!("expired"), "expired"),
        ] {
            let mut changed = raw.clone();
            changed[key] = value;
            fs::write(&path, serde_json::to_vec(&changed).unwrap()).unwrap();
            assert_eq!(revalidate(&path, &request, now), Err(expected));
        }
        fs::remove_file(&path).unwrap();
        assert_eq!(revalidate(&path, &request, now), Err("request_replaced"));
    }

    #[test]
    fn native_channel_persistence_keeps_the_released_shape_and_fails_closed() {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("channel.json");
        persist_channel(&path, Channel::Test).unwrap();
        assert_eq!(fs::read_to_string(&path).unwrap(), "\"test\"");
        persist_channel(&path, Channel::Stable).unwrap();
        assert_eq!(fs::read_to_string(&path).unwrap(), "\"stable\"");
        assert!(persist_channel(&path.join("blocked.json"), Channel::Test).is_err());
        assert_eq!(fs::read_to_string(&path).unwrap(), "\"stable\"");
    }

    #[test]
    fn a_busy_install_or_local_check_is_never_released_by_a_control() {
        let request = request();
        let mut state = state();
        let busy = AtomicBool::new(true);
        assert_eq!(claim(&mut state, &busy, &request, SystemTime::now()), Claim::Busy);
        assert_eq!(
            state.control_request.as_ref().unwrap().error_code.as_deref(),
            Some("update_in_progress")
        );
        assert!(!settle(&mut state, &busy, &request, ControlPhase::Failed, None));
        assert!(busy.load(Ordering::SeqCst));
    }
}
