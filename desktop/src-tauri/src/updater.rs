//! Desktop updater owned by the native shell and controlled by its Workbench popover.
use avibe_runtime_host::{
    download::{self, Length},
    update::{self, Channel, Manifest, Release, REPOSITORY},
    update_status::{
        read_desktop_update_request, DesktopUpdateRequest, DesktopUpdateRequestError, RemoteRequestPhase,
        RemoteRequestStatus, UpdateStatusPhase, UpdateStatusRecord, UpdateStatusStore, DESKTOP_UPDATE_REQUEST_FILE,
        UPDATE_STATUS_FILE,
    },
};
use serde::{Deserialize, Serialize};
use std::{
    fs::{self, OpenOptions},
    io::{self, Write},
    path::{Path, PathBuf},
    sync::{
        atomic::{AtomicBool, AtomicUsize, Ordering},
        Mutex,
    },
    time::{Duration, SystemTime},
};
use tauri::{
    menu::{CheckMenuItem, MenuItem},
    AppHandle, Emitter, Manager, WebviewWindow,
};
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons, MessageDialogKind, MessageDialogResult};
use tauri_plugin_notification::NotificationExt;

pub const MENU_ID: &str = "desktop-update";
pub const CHANNEL_ID: &str = "desktop-update-test";
pub const OPEN_URL: &str = "avibe://updates";
pub const STATE_EVENT: &str = "avibe:desktop-update-state";
const KEY: &str = match option_env!("AVIBE_DESKTOP_UPDATER_PUBLIC_KEY") {
    Some(key) => key,
    None => "",
};
const TARGET: &str = if cfg!(all(target_os = "macos", target_arch = "aarch64")) {
    "aarch64-apple-darwin"
} else if cfg!(all(target_os = "macos", target_arch = "x86_64")) {
    "x86_64-apple-darwin"
} else if cfg!(all(target_os = "windows", target_arch = "x86_64")) {
    "x86_64-pc-windows-msvc"
} else {
    "unsupported"
};

#[derive(Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Catalog {
    menu: String,
    test_channel: String,
    pub(super) title: String,
    current: String,
    latest: String,
    checking: String,
    up_to_date: String,
    available: String,
    confirm: String,
    downloading: String,
    installing: String,
    installed: String,
    disabled: String,
    disabled_detail: String,
    error: String,
    check_failed: String,
    pub(super) install_failed: String,
    install: String,
    skip: String,
    cancel: String,
}
fn catalog() -> Catalog {
    super::native_catalog_for_locales(sys_locale::get_locales()).updater
}

#[derive(Deserialize, Serialize)]
struct RemoteInstallPreference {
    enabled: bool,
}

fn read_remote_install_preference(path: &Path) -> io::Result<bool> {
    match fs::read(path) {
        Ok(bytes) => serde_json::from_slice::<RemoteInstallPreference>(&bytes)
            .map(|preference| preference.enabled)
            .map_err(io::Error::other),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(false),
        Err(error) => Err(error),
    }
}

fn write_remote_install_preference(path: &Path, enabled: bool) -> io::Result<()> {
    let parent = path
        .parent()
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "preference path has no parent"))?;
    fs::create_dir_all(parent)?;
    let suffix = REMOTE_PREFERENCE_SEQUENCE.fetch_add(1, Ordering::Relaxed);
    let temporary = parent.join(format!(
        ".{REMOTE_INSTALL_PREFERENCE_FILE}.{}.{}.tmp",
        std::process::id(),
        suffix
    ));
    let mut file = OpenOptions::new().create_new(true).write(true).open(&temporary)?;
    let bytes = serde_json::to_vec(&RemoteInstallPreference { enabled }).map_err(io::Error::other)?;
    let result = (|| {
        file.write_all(&bytes)?;
        file.write_all(b"\n")?;
        file.sync_all()?;
        drop(file);
        fs::rename(&temporary, path)
    })();
    if result.is_err() {
        let _ = fs::remove_file(&temporary);
    }
    result
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
enum Phase {
    Idle,
    Checking,
    Current,
    Available,
    Downloading,
    Installing,
    Installed,
    Disabled,
    Error,
}

#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct DesktopRemoteRequestState {
    request_id: String,
    target: String,
    phase: RemoteRequestPhase,
    progress: Option<u8>,
    error_code: Option<String>,
    updated_at: String,
}

impl From<&RemoteRequestStatus> for DesktopRemoteRequestState {
    fn from(status: &RemoteRequestStatus) -> Self {
        Self {
            request_id: status.request_id.clone(),
            target: status.target.clone(),
            phase: status.phase,
            progress: status.progress,
            error_code: status.error_code.clone(),
            updated_at: status.updated_at.clone(),
        }
    }
}

struct State {
    phase: Phase,
    latest: Option<String>,
    channel: Channel,
    manifest: Option<Manifest>,
    availability_interactive: bool,
    remote_install_allowed: bool,
    remote_request: Option<RemoteRequestStatus>,
}

#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct DesktopUpdateState {
    phase: Phase,
    latest: Option<String>,
    channel: Channel,
    skipped_version: Option<String>,
    interactive: bool,
    remote_install_allowed: bool,
    remote_request: Option<DesktopRemoteRequestState>,
}
pub struct Updater {
    state: Mutex<State>,
    busy: AtomicBool,
    /// Index into `update::sources` of the source that last completed a
    /// download; the next download tries it first.
    source: AtomicUsize,
    channel_path: PathBuf,
    skipped_path: PathBuf,
    remote_install_path: PathBuf,
    remote_request_path: PathBuf,
    /// The last completed check, published for the Runtime to relay to browsers.
    status: UpdateStatusStore,
    pub menu: MenuItem<tauri::Wry>,
    pub channel_menu: CheckMenuItem<tauri::Wry>,
}

const REMOTE_INSTALL_PREFERENCE_FILE: &str = "remote-install.json";
const REMOTE_REQUEST_EXPIRY_CODE: &str = "expired";
static REMOTE_PREFERENCE_SEQUENCE: AtomicUsize = AtomicUsize::new(0);
/// The record a finished check publishes, or None while no check result is in
/// memory. `Error` here means the check failed: only `check()` publishes.
fn check_record(state: &State, current: &str, checked_at: SystemTime) -> Option<UpdateStatusRecord> {
    let phase = match state.phase {
        Phase::Current => UpdateStatusPhase::Current,
        Phase::Available => UpdateStatusPhase::Available,
        Phase::Disabled => UpdateStatusPhase::Disabled,
        Phase::Error => UpdateStatusPhase::Error,
        Phase::Idle | Phase::Checking | Phase::Downloading | Phase::Installing | Phase::Installed => return None,
    };
    Some(UpdateStatusRecord::new(
        phase,
        current,
        state.latest.clone(),
        state.channel,
        checked_at,
    ))
}

fn status_for_publish(
    state: &State,
    current: &str,
    previous: Option<UpdateStatusRecord>,
    checked_at: SystemTime,
) -> UpdateStatusRecord {
    check_record(state, current, checked_at)
        .or(previous)
        .unwrap_or_else(|| {
            UpdateStatusRecord::new(
                UpdateStatusPhase::Current,
                current,
                state.latest.clone(),
                state.channel,
                checked_at,
            )
        })
}

fn base_status(app: &AppHandle, state: &State) -> UpdateStatusRecord {
    let updater = app.state::<Updater>();
    status_for_publish(
        state,
        &app.package_info().version.to_string(),
        updater.status.read().ok().flatten(),
        SystemTime::now(),
    )
}

fn publish_status(app: &AppHandle) {
    let updater = app.state::<Updater>();
    let state = updater.state.lock().expect("updater state");
    let record = base_status(app, &state).with_remote(state.remote_install_allowed, state.remote_request.clone());
    if let Err(error) = updater.status.write(&record) {
        eprintln!("desktop update status: {error}");
    }
}

fn set_remote_phase(
    app: &AppHandle,
    request_id: &str,
    target: &str,
    phase: RemoteRequestPhase,
    progress: Option<u8>,
    error_code: Option<&str>,
) {
    let status = RemoteRequestStatus::new(
        request_id,
        target,
        phase,
        progress,
        error_code.map(str::to_owned),
        SystemTime::now(),
    );
    {
        let updater = app.state::<Updater>();
        updater.state.lock().expect("updater state").remote_request = Some(status);
    }
    publish_status(app);
    emit_state(app);
}

fn remote_request_matches(state: &State, request_id: &str, phase: RemoteRequestPhase) -> bool {
    state
        .remote_request
        .as_ref()
        .is_some_and(|request| request.request_id == request_id && request.phase == phase)
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum RemoteRequestStart {
    Started,
    AlreadyHandled,
    Busy,
}

fn claim_remote_request(
    state: &mut State,
    busy: &AtomicBool,
    request: &DesktopUpdateRequest,
    now: SystemTime,
) -> RemoteRequestStart {
    if state.remote_request.as_ref().is_some_and(|previous| {
        previous.request_id == request.request_id && (previous.phase.is_terminal() || busy.load(Ordering::SeqCst))
    }) {
        return RemoteRequestStart::AlreadyHandled;
    }
    if busy
        .compare_exchange(false, true, Ordering::SeqCst, Ordering::SeqCst)
        .is_err()
    {
        state.remote_request = Some(RemoteRequestStatus::new(
            &request.request_id,
            &request.target,
            RemoteRequestPhase::Failed,
            None,
            Some("update_in_progress".to_owned()),
            now,
        ));
        return RemoteRequestStart::Busy;
    }
    state.remote_request = Some(RemoteRequestStatus::new(
        &request.request_id,
        &request.target,
        RemoteRequestPhase::Requested,
        None,
        None,
        now,
    ));
    RemoteRequestStart::Started
}

fn remote_preference_can_change(state: &State) -> bool {
    state
        .remote_request
        .as_ref()
        .is_none_or(|request| request.phase.is_terminal())
}

fn restart_outcome(target: &str, current: &str) -> (RemoteRequestPhase, Option<&'static str>) {
    if target == current {
        (RemoteRequestPhase::Completed, None)
    } else {
        (RemoteRequestPhase::Failed, Some("version_mismatch"))
    }
}

fn reconcile_restarting(app: &AppHandle) {
    let request = {
        let updater = app.state::<Updater>();
        let request = updater.state.lock().expect("updater state").remote_request.clone();
        request
    };
    let Some(request) = request.filter(|request| request.phase == RemoteRequestPhase::Restarting) else {
        return;
    };
    let (phase, error_code) = restart_outcome(&request.target, &app.package_info().version.to_string());
    set_remote_phase(app, &request.request_id, &request.target, phase, None, error_code);
}

fn recover_remote_request(app: &AppHandle) {
    let request = {
        let updater = app.state::<Updater>();
        let request = updater.state.lock().expect("updater state").remote_request.clone();
        request
    };
    let Some(request) = request else {
        return;
    };
    if request.phase.is_terminal() || request.phase == RemoteRequestPhase::Restarting {
        return;
    }
    let path = app.state::<Updater>().remote_request_path.clone();
    match read_desktop_update_request(&path, SystemTime::now()) {
        Ok(_) => handle_remote_update_request(app),
        Err(DesktopUpdateRequestError::Expired(request)) => set_remote_phase(
            app,
            &request.request_id,
            &request.target,
            RemoteRequestPhase::Expired,
            None,
            Some(REMOTE_REQUEST_EXPIRY_CODE),
        ),
        Err(_) => set_remote_phase(
            app,
            &request.request_id,
            &request.target,
            RemoteRequestPhase::Failed,
            None,
            Some("request_unavailable"),
        ),
    }
}
fn label(phase: Phase, c: &Catalog) -> &str {
    match phase {
        Phase::Idle => &c.menu,
        Phase::Checking => &c.checking,
        Phase::Current => &c.up_to_date,
        Phase::Available => &c.available,
        Phase::Downloading => &c.downloading,
        Phase::Installing => &c.installing,
        Phase::Installed => &c.installed,
        Phase::Disabled => &c.disabled,
        Phase::Error => &c.error,
    }
}
pub fn init(app: &AppHandle) -> tauri::Result<()> {
    let c = catalog();
    let channel_path = app.path().app_local_data_dir()?.join("update-channel.json");
    let channel = std::fs::read(&channel_path)
        .ok()
        .and_then(|bytes| serde_json::from_slice(&bytes).ok())
        .unwrap_or_else(|| Channel::for_version(&app.package_info().version));
    // Beside `computer-use.json`, where the Runtime already finds desktop state.
    #[cfg(target_os = "macos")]
    let status_dir = super::computer_use::state_dir(app)?;
    #[cfg(not(target_os = "macos"))]
    let status_dir = app.path().app_data_dir()?;
    let status = UpdateStatusStore::new(status_dir.join(UPDATE_STATUS_FILE));
    let remote_request = status.read().ok().flatten().and_then(|record| record.remote_request);
    let remote_install_path = app.path().app_local_data_dir()?.join(REMOTE_INSTALL_PREFERENCE_FILE);
    let remote_install_allowed = read_remote_install_preference(&remote_install_path).unwrap_or_else(|error| {
        eprintln!("desktop remote-install preference: {error}");
        false
    });
    app.manage(Updater {
        state: Mutex::new(State {
            phase: Phase::Idle,
            latest: None,
            channel,
            manifest: None,
            availability_interactive: false,
            remote_install_allowed,
            remote_request,
        }),
        busy: AtomicBool::new(false),
        source: AtomicUsize::new(0),
        channel_path,
        skipped_path: app.path().app_local_data_dir()?.join("update-skipped.json"),
        remote_install_path,
        remote_request_path: status_dir.join(DESKTOP_UPDATE_REQUEST_FILE),
        status,
        menu: MenuItem::with_id(app, MENU_ID, &c.menu, true, None::<&str>)?,
        channel_menu: CheckMenuItem::with_id(
            app,
            CHANNEL_ID,
            &c.test_channel,
            true,
            channel == Channel::Test,
            None::<&str>,
        )?,
    });
    reconcile_restarting(app);
    recover_remote_request(app);
    Ok(())
}
fn set_state(app: &AppHandle, phase: Phase, latest: Option<String>) {
    let updater = app.state::<Updater>();
    {
        let mut state = updater.state.lock().expect("updater state");
        state.phase = phase;
        if matches!(phase, Phase::Checking | Phase::Current | Phase::Disabled | Phase::Error) {
            state.manifest = None;
        }
        if !matches!(
            phase,
            Phase::Available | Phase::Checking | Phase::Downloading | Phase::Installing
        ) {
            state.availability_interactive = false;
        }
        if latest.is_some() || phase == Phase::Checking {
            state.latest = latest;
        }
    }
    let _ = updater
        .menu
        .set_text(format!("{} — {}", label(phase, &catalog()), app.package_info().version));
    let _ = updater.channel_menu.set_enabled(!updater.busy.load(Ordering::SeqCst));
    emit_state(app);
}

fn state_snapshot(app: &AppHandle) -> DesktopUpdateState {
    let updater = app.state::<Updater>();
    let state = updater.state.lock().expect("updater state");
    DesktopUpdateState {
        phase: state.phase,
        latest: state.latest.clone(),
        channel: state.channel,
        skipped_version: skipped_version(&updater),
        interactive: state.availability_interactive,
        remote_install_allowed: state.remote_install_allowed,
        remote_request: state.remote_request.as_ref().map(DesktopRemoteRequestState::from),
    }
}

fn emit_state(app: &AppHandle) {
    let _ = app.emit_to(super::MAIN_WINDOW, STATE_EVENT, state_snapshot(app));
}

fn ensure_workbench(window: &WebviewWindow) -> Result<(), String> {
    if super::pet::is_workbench(window) {
        Ok(())
    } else {
        Err("This command is only available to the Avibe Workbench.".to_owned())
    }
}

fn workbench_ready(app: &AppHandle) -> bool {
    app.get_webview_window("main")
        .is_some_and(|window| super::pet::is_workbench(&window))
}

fn set_available(app: &AppHandle, manifest: Manifest) {
    let latest = manifest.version.clone();
    set_state(app, Phase::Available, Some(latest));
    app.state::<Updater>().state.lock().expect("updater state").manifest = Some(manifest);
    emit_state(app);
}
pub fn toggle_channel(app: &AppHandle) {
    let updater = app.state::<Updater>();
    if updater.busy.load(Ordering::SeqCst) {
        return;
    }
    let result = (|| -> Result<(), Box<dyn std::error::Error>> {
        let mut state = updater.state.lock().expect("updater state");
        let channel = if state.channel == Channel::Test {
            Channel::Stable
        } else {
            Channel::Test
        };
        std::fs::create_dir_all(updater.channel_path.parent().ok_or("missing config directory")?)?;
        std::fs::write(&updater.channel_path, serde_json::to_vec(&channel)?)?;
        state.channel = channel;
        state.latest = None;
        updater.channel_menu.set_checked(channel == Channel::Test)?;
        Ok(())
    })();
    if result.is_err() {
        show(app, &catalog().check_failed, None);
        return;
    }
    emit_state(app);
    check(app.clone(), true);
}
fn show(app: &AppHandle, message: &str, latest: Option<&str>) {
    let c = catalog();
    let mut text = format!("{}: {}\n{}", c.current, app.package_info().version, message);
    if let Some(version) = latest {
        text.push_str(&format!("\n{}: {version}", c.latest));
    }
    app.dialog().message(text).title(c.title).show(|_| {});
}

#[derive(Debug, PartialEq, Eq)]
enum Choice {
    Install,
    Skip,
    Later,
}
/// Closing the dialog (Esc, window close) is never an implicit install or skip.
fn choice(result: &MessageDialogResult, c: &Catalog) -> Choice {
    match result {
        MessageDialogResult::Custom(label) if *label == c.install => Choice::Install,
        MessageDialogResult::Custom(label) if *label == c.skip => Choice::Skip,
        _ => Choice::Later,
    }
}
/// Only the automatic startup check honours a skipped version; an explicit menu
/// check always offers it, and any newer release prompts again.
fn should_prompt(interactive: bool, skipped: Option<&str>, version: &str) -> bool {
    interactive || skipped != Some(version)
}
fn skipped_version(updater: &Updater) -> Option<String> {
    std::fs::read(&updater.skipped_path)
        .ok()
        .and_then(|bytes| serde_json::from_slice(&bytes).ok())
}
fn skip_version(app: &AppHandle, version: &str) {
    let path = &app.state::<Updater>().skipped_path;
    let result = persist_skipped_version(path, version);
    if let Err(error) = result {
        eprintln!("desktop update skip: {error}");
    }
}

fn persist_skipped_version(path: &Path, version: &str) -> std::io::Result<()> {
    path.parent()
        .ok_or_else(|| std::io::Error::other("missing config directory"))
        .and_then(std::fs::create_dir_all)
        .and_then(|()| std::fs::write(path, serde_json::to_vec(version).expect("version JSON")))
}

#[derive(Deserialize)]
struct GithubRelease {
    tag_name: String,
    draft: bool,
    prerelease: bool,
    assets: Vec<GithubAsset>,
}
#[derive(Deserialize)]
struct GithubAsset {
    name: String,
    browser_download_url: String,
}

/// Connects fail fast; a download that stops moving hands over to the next
/// source, while a slow one that keeps moving is left to finish.
fn client() -> Result<reqwest::Client, String> {
    reqwest::Client::builder()
        .https_only(true)
        .connect_timeout(Duration::from_secs(10))
        .read_timeout(Duration::from_secs(30))
        .user_agent("Avibe-desktop-updater")
        .build()
        .map_err(|_| "HTTP client failed".into())
}
async fn fetch(client: &reqwest::Client, url: String, max: u64) -> Result<Vec<u8>, String> {
    Ok(download::fetch(client, &[url], 0, Length::AtMost(max)).await?.0)
}
/// A release asset from the mirror or GitHub, starting with the last source
/// that worked. Callers authenticate the bytes.
async fn asset(app: &AppHandle, client: &reqwest::Client, url: &str, length: Length) -> Result<Vec<u8>, String> {
    let source = &app.state::<Updater>().source;
    let (data, used) = download::fetch(client, &update::sources(url), source.load(Ordering::SeqCst), length).await?;
    source.store(used, Ordering::SeqCst);
    Ok(data)
}

async fn asset_with_progress(
    app: &AppHandle,
    client: &reqwest::Client,
    url: &str,
    length: Length,
    progress: impl FnMut(u64, u64) + Send,
) -> Result<Vec<u8>, String> {
    let source = &app.state::<Updater>().source;
    let (data, used) = download::fetch_with_progress(
        client,
        &update::sources(url),
        source.load(Ordering::SeqCst),
        length,
        progress,
    )
    .await?;
    source.store(used, Ordering::SeqCst);
    Ok(data)
}

#[derive(Clone, Debug, PartialEq, Eq)]
struct RemoteInstallContext {
    request_id: String,
    target: String,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum RemoteDelivery {
    Prompt,
    Install,
}

fn remote_delivery(allowed: bool) -> RemoteDelivery {
    if allowed {
        RemoteDelivery::Install
    } else {
        RemoteDelivery::Prompt
    }
}

fn should_publish_download_progress(current: Option<&RemoteRequestStatus>, request_id: &str, progress: u8) -> bool {
    current.is_some_and(|current| {
        current.request_id == request_id
            && current.phase == RemoteRequestPhase::Downloading
            && current.progress.is_none_or(|published| progress > published)
    })
}

fn validate_remote_target(latest: &str, requested: &str) -> Result<(), &'static str> {
    if latest == requested {
        Ok(())
    } else {
        Err("target_mismatch")
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum RemoteRequestGateError {
    Unavailable,
    Expired,
    Mismatch,
}

fn validate_remote_request_gate(
    request: &DesktopUpdateRequest,
    context: &RemoteInstallContext,
    now: SystemTime,
) -> Result<(), RemoteRequestGateError> {
    if request.request_id != context.request_id || request.target != context.target {
        return Err(RemoteRequestGateError::Mismatch);
    }
    if request.expires_in(now).is_zero() {
        return Err(RemoteRequestGateError::Expired);
    }
    if !request.is_pending() {
        return Err(RemoteRequestGateError::Mismatch);
    }
    Ok(())
}
/// The mirror's index lists releases and their commits in one request; the
/// paginated GitHub API is the fallback when the mirror is unreachable.
async fn releases(client: &reqwest::Client) -> Result<Vec<Release>, String> {
    match fetch(client, update::INDEX_URL.into(), 8 * 1024 * 1024)
        .await
        .and_then(|data| update::index_releases(&data))
    {
        Ok(releases) => return Ok(releases),
        Err(error) => eprintln!("desktop update index: {error}; falling back to GitHub"),
    }
    let mut releases = Vec::new();
    // GitHub orders by creation, not SemVer. Walk the complete bounded history;
    // reaching the bound fails instead of claiming an incomplete list is current.
    for page in 1..=10 {
        let data = fetch(
            client,
            format!("https://api.github.com/repos/{REPOSITORY}/releases?per_page=100&page={page}"),
            8 * 1024 * 1024,
        )
        .await?;
        let listed: Vec<GithubRelease> = serde_json::from_slice(&data).map_err(|_| "invalid releases response")?;
        let done = listed.len() < 100;
        for release in listed.into_iter().filter(|release| !release.draft) {
            let assets = release
                .assets
                .into_iter()
                .filter(|asset| asset.browser_download_url == update::asset_url(&release.tag_name, &asset.name))
                .map(|asset| asset.name)
                .collect();
            releases.push(Release {
                tag: release.tag_name,
                prerelease: release.prerelease,
                commit: None,
                assets,
            });
        }
        if done {
            return Ok(releases);
        }
    }
    Err("release history exceeds discovery bound".into())
}
async fn find_update(app: &AppHandle, channel: Channel) -> Result<Option<Manifest>, String> {
    let client = client()?;
    let Some((version, release)) = channel.latest(releases(&client).await?) else {
        return Err("no release in this channel".into());
    };
    set_state(app, Phase::Checking, Some(version.to_string()));
    if version <= app.package_info().version {
        return Ok(None);
    }
    let name = update::manifest_name(TARGET);
    let signature = format!("{name}.sig");
    if !release.assets.contains(&name) || !release.assets.contains(&signature) {
        return Err("signed updater metadata unavailable; manual installation required".into());
    }
    let raw = asset(
        app,
        &client,
        &update::asset_url(&release.tag, &name),
        Length::AtMost(64 * 1024),
    )
    .await?;
    let sig = asset(
        app,
        &client,
        &update::asset_url(&release.tag, &signature),
        Length::AtMost(4096),
    )
    .await?;
    let manifest = Manifest::authenticated(&raw, std::str::from_utf8(&sig).map_err(|_| "invalid signature")?, KEY)?;
    let commit = match release.commit {
        Some(commit) => commit,
        None => serde_json::from_slice::<serde_json::Value>(
            &fetch(
                &client,
                format!("https://api.github.com/repos/{REPOSITORY}/commits/{}", release.tag),
                2 * 1024 * 1024,
            )
            .await?,
        )
        .map_err(|_| "invalid source response")?["sha"]
            .as_str()
            .ok_or("missing source")?
            .to_owned(),
    };
    let artifact = manifest.validate(channel, &release.tag, &commit, TARGET)?;
    if !release
        .assets
        .iter()
        .any(|name| update::asset_url(&release.tag, name) == artifact.url)
    {
        return Err("updater artifact missing".into());
    }
    Ok(Some(manifest))
}

pub fn check(app: AppHandle, interactive: bool) {
    // Install progress and outcomes stay in memory: a failed install keeps
    // the last `Available`, and the new app's startup check publishes
    // `Current` after a successful hand-over.
    fn publish_check(app: &AppHandle) {
        let updater = app.state::<Updater>();
        let state = updater.state.lock().expect("updater state");
        let Some(record) = check_record(&state, &app.package_info().version.to_string(), SystemTime::now()) else {
            return;
        };
        let record = record.with_remote(state.remote_install_allowed, state.remote_request.clone());
        if let Err(error) = updater.status.write(&record) {
            eprintln!("desktop update status: {error}");
        }
    }
    let updater = app.state::<Updater>();
    if updater.busy.swap(true, Ordering::SeqCst) {
        if interactive {
            let state = updater.state.lock().expect("updater state");
            show(&app, label(state.phase, &catalog()), state.latest.as_deref());
        }
        return;
    }
    if KEY.trim().is_empty() || TARGET == "unsupported" {
        updater.busy.store(false, Ordering::SeqCst);
        set_state(&app, Phase::Disabled, None);
        publish_check(&app);
        if interactive {
            show(&app, &catalog().disabled_detail, None);
        }
        return;
    }
    let channel = updater.state.lock().expect("updater state").channel;
    updater.state.lock().expect("updater state").availability_interactive = interactive;
    set_state(&app, Phase::Checking, None);
    tauri::async_runtime::spawn(async move {
        match find_update(&app, channel).await {
            Ok(Some(manifest)) => {
                set_available(&app, manifest.clone());
                publish_check(&app);
                let skipped = skipped_version(&app.state::<Updater>());
                if !should_prompt(interactive, skipped.as_deref(), &manifest.version) {
                    finish(&app);
                    return;
                }
                if workbench_ready(&app) {
                    // The Workbench owns the confirmation UI. Keep the signed
                    // manifest in native state, but release the check lock so
                    // closing the popover cannot strand the updater as busy.
                    finish(&app);
                    return;
                }
                let c = catalog();
                let confirmation = app.clone();
                app.dialog()
                    .message(format!(
                        "{}: {}\n{}: {}\n\n{}",
                        c.current,
                        app.package_info().version,
                        c.latest,
                        manifest.version,
                        c.confirm
                    ))
                    .title(c.title.clone())
                    // The dialog plugin reports Esc/close as the third label, so
                    // that slot must be the harmless "Later", never "Skip".
                    .buttons(MessageDialogButtons::YesNoCancelCustom(
                        c.install.clone(),
                        c.skip.clone(),
                        c.cancel.clone(),
                    ))
                    .show_with_result(move |result| match choice(&result, &c) {
                        Choice::Install => install(confirmation, manifest),
                        Choice::Skip => {
                            skip_version(&confirmation, &manifest.version);
                            finish(&confirmation);
                        }
                        Choice::Later => finish(&confirmation),
                    });
            }
            Ok(None) => {
                finish(&app);
                set_state(&app, Phase::Current, None);
                publish_check(&app);
                if interactive {
                    let latest = app
                        .state::<Updater>()
                        .state
                        .lock()
                        .expect("updater state")
                        .latest
                        .clone();
                    show(&app, &catalog().up_to_date, latest.as_deref());
                }
            }
            Err(error) => {
                eprintln!("desktop update check: {error}");
                finish(&app);
                set_state(&app, Phase::Error, None);
                publish_check(&app);
                if interactive {
                    show(&app, &catalog().check_failed, None);
                }
            }
        }
    });
}

#[tauri::command]
pub fn desktop_update_state(window: WebviewWindow, app: AppHandle) -> Result<DesktopUpdateState, String> {
    ensure_workbench(&window)?;
    Ok(state_snapshot(&app))
}

#[tauri::command]
pub fn desktop_update_check(window: WebviewWindow, app: AppHandle) -> Result<(), String> {
    ensure_workbench(&window)?;
    check(app, true);
    Ok(())
}

#[tauri::command]
pub fn desktop_remote_install_allowed(window: WebviewWindow, app: AppHandle) -> Result<bool, String> {
    ensure_workbench(&window)?;
    Ok(app
        .state::<Updater>()
        .state
        .lock()
        .expect("updater state")
        .remote_install_allowed)
}

#[tauri::command]
pub fn desktop_remote_install_set(window: WebviewWindow, app: AppHandle, enabled: bool) -> Result<bool, String> {
    ensure_workbench(&window)?;
    let updater = app.state::<Updater>();
    {
        let mut state = updater.state.lock().expect("updater state");
        if !remote_preference_can_change(&state) {
            return Err("Remote install preference cannot change while an update request is active.".to_owned());
        }
        write_remote_install_preference(&updater.remote_install_path, enabled)
            .map_err(|error| format!("Remote install preference could not be saved: {error}"))?;
        state.remote_install_allowed = enabled;
    }
    publish_status(&app);
    emit_state(&app);
    Ok(enabled)
}

#[tauri::command]
pub fn desktop_update_install(window: WebviewWindow, app: AppHandle) -> Result<(), String> {
    ensure_workbench(&window)?;
    let updater = app.state::<Updater>();
    let (manifest, remote) = {
        let state = updater
            .state
            .lock()
            .map_err(|_| "Desktop updater state is unavailable.".to_owned())?;
        let remote = state.remote_request.as_ref().and_then(|request| {
            (request.phase == RemoteRequestPhase::AwaitingHost).then(|| RemoteInstallContext {
                request_id: request.request_id.clone(),
                target: request.target.clone(),
            })
        });
        if remote.is_none() && state.phase != Phase::Available {
            return Err("No desktop update is ready to install.".to_owned());
        }
        let manifest = state
            .manifest
            .clone()
            .ok_or_else(|| "No desktop update is ready to install.".to_owned())?;
        (manifest, remote)
    };
    if remote.is_none() && updater.busy.swap(true, Ordering::SeqCst) {
        return Err("A desktop update is already in progress.".to_owned());
    }
    install_with_context(app, manifest, remote);
    Ok(())
}

#[tauri::command]
pub fn desktop_update_skip(window: WebviewWindow, app: AppHandle) -> Result<(), String> {
    ensure_workbench(&window)?;
    let version = {
        let updater = app.state::<Updater>();
        let state = updater
            .state
            .lock()
            .map_err(|_| "Desktop updater state is unavailable.".to_owned())?;
        if state
            .remote_request
            .as_ref()
            .is_some_and(|request| request.phase == RemoteRequestPhase::AwaitingHost)
        {
            return Err("A remote desktop update is waiting for host action; use Ignore.".to_owned());
        }
        if state.phase != Phase::Available {
            return Err("No desktop update is available to skip.".to_owned());
        }
        state
            .latest
            .clone()
            .ok_or_else(|| "The available desktop update has no version.".to_owned())?
    };
    skip_version(&app, &version);
    finish(&app);
    Ok(())
}

#[tauri::command]
pub fn desktop_update_remote_decline(window: WebviewWindow, app: AppHandle) -> Result<(), String> {
    ensure_workbench(&window)?;
    let request = {
        let updater = app.state::<Updater>();
        let request = updater
            .state
            .lock()
            .map_err(|_| "Desktop updater state is unavailable.".to_owned())?
            .remote_request
            .clone();
        request
    };
    let Some(request) = request.filter(|request| request.phase == RemoteRequestPhase::AwaitingHost) else {
        return Err("No remote desktop update is waiting for host action.".to_owned());
    };
    set_remote_phase(
        &app,
        &request.request_id,
        &request.target,
        RemoteRequestPhase::Declined,
        None,
        None,
    );
    finish(&app);
    Ok(())
}

fn reread_remote_request(app: &AppHandle, context: &RemoteInstallContext) -> Result<(), RemoteRequestGateError> {
    let path = app.state::<Updater>().remote_request_path.clone();
    let request = match read_desktop_update_request(&path, SystemTime::now()) {
        Ok(request) => request,
        Err(DesktopUpdateRequestError::Expired(_)) => return Err(RemoteRequestGateError::Expired),
        Err(_) => return Err(RemoteRequestGateError::Unavailable),
    };
    validate_remote_request_gate(&request, context, SystemTime::now())
}

fn record_remote_gate_failure(
    app: &AppHandle,
    context: &RemoteInstallContext,
    error: RemoteRequestGateError,
) -> String {
    let (phase, code) = match error {
        RemoteRequestGateError::Expired => (RemoteRequestPhase::Expired, "expired"),
        RemoteRequestGateError::Unavailable => (RemoteRequestPhase::Failed, "request_unavailable"),
        RemoteRequestGateError::Mismatch => (RemoteRequestPhase::Failed, "request_mismatch"),
    };
    set_remote_phase(app, &context.request_id, &context.target, phase, None, Some(code));
    code.to_owned()
}

fn finish_remote_gate_failure(
    app: &AppHandle,
    context: &RemoteInstallContext,
    error: RemoteRequestGateError,
) -> String {
    let code = record_remote_gate_failure(app, context, error);
    finish(app);
    code
}

fn notify_remote_request(app: &AppHandle) {
    let c = catalog();
    if app
        .notification()
        .builder()
        .title(c.title)
        .body(c.available)
        .show()
        .is_err()
    {
        eprintln!("The desktop update notification could not be submitted to the operating system.");
    }
}

fn schedule_remote_expiry(app: &AppHandle, request: DesktopUpdateRequest) {
    let wait = request.expires_in(SystemTime::now());
    let handle = app.clone();
    tauri::async_runtime::spawn(async move {
        tokio::time::sleep(wait).await;
        let current = {
            let updater = handle.state::<Updater>();
            updater.state.lock().ok().and_then(|state| {
                remote_request_matches(&state, &request.request_id, RemoteRequestPhase::AwaitingHost)
                    .then(|| state.remote_request.clone())
                    .flatten()
            })
        };
        if let Some(current) = current {
            set_remote_phase(
                &handle,
                &current.request_id,
                &current.target,
                RemoteRequestPhase::Expired,
                None,
                Some(REMOTE_REQUEST_EXPIRY_CODE),
            );
            finish(&handle);
        }
    });
}

fn fail_remote_request(app: &AppHandle, request: &DesktopUpdateRequest, error_code: &'static str) {
    set_remote_phase(
        app,
        &request.request_id,
        &request.target,
        RemoteRequestPhase::Failed,
        None,
        Some(error_code),
    );
    finish(app);
}

pub fn handle_remote_update_request(app: &AppHandle) {
    let request_path = app.state::<Updater>().remote_request_path.clone();
    let request = match read_desktop_update_request(&request_path, SystemTime::now()) {
        Ok(request) => request,
        Err(DesktopUpdateRequestError::Expired(request)) => {
            set_remote_phase(
                app,
                &request.request_id,
                &request.target,
                RemoteRequestPhase::Expired,
                None,
                Some(REMOTE_REQUEST_EXPIRY_CODE),
            );
            return;
        }
        Err(_) => return,
    };
    let updater = app.state::<Updater>();
    let start = {
        let mut state = updater.state.lock().expect("updater state");
        claim_remote_request(&mut state, &updater.busy, &request, SystemTime::now())
    };
    if start == RemoteRequestStart::AlreadyHandled {
        return;
    }
    publish_status(app);
    emit_state(app);
    if start == RemoteRequestStart::Busy {
        return;
    }
    let handle = app.clone();
    tauri::async_runtime::spawn(async move {
        process_remote_update_request(handle, request).await;
    });
}

async fn process_remote_update_request(app: AppHandle, request: DesktopUpdateRequest) {
    let remote_install_allowed = app
        .state::<Updater>()
        .state
        .lock()
        .expect("updater state")
        .remote_install_allowed;
    set_remote_phase(
        &app,
        &request.request_id,
        &request.target,
        RemoteRequestPhase::Acknowledged,
        None,
        None,
    );
    set_remote_phase(
        &app,
        &request.request_id,
        &request.target,
        RemoteRequestPhase::Checking,
        None,
        None,
    );
    let channel = app.state::<Updater>().state.lock().expect("updater state").channel;
    match find_update(&app, channel).await {
        Ok(Some(manifest)) => {
            app.state::<Updater>()
                .state
                .lock()
                .expect("updater state")
                .availability_interactive = true;
            set_available(&app, manifest.clone());
            if let Err(error_code) = validate_remote_target(&manifest.version, &request.target) {
                fail_remote_request(&app, &request, error_code);
                return;
            }
            publish_status(&app);
            let remote = RemoteInstallContext {
                request_id: request.request_id.clone(),
                target: request.target.clone(),
            };
            if let Err(error) = reread_remote_request(&app, &remote) {
                let code = finish_remote_gate_failure(&app, &remote, error);
                eprintln!("remote desktop update request gate: {code}");
                return;
            }
            match remote_delivery(remote_install_allowed) {
                RemoteDelivery::Install => install_with_context(app, manifest, Some(remote)),
                RemoteDelivery::Prompt => {
                    set_remote_phase(
                        &app,
                        &request.request_id,
                        &request.target,
                        RemoteRequestPhase::AwaitingHost,
                        None,
                        None,
                    );
                    super::request_and_open_desktop_updates(&app);
                    notify_remote_request(&app);
                    schedule_remote_expiry(&app, request);
                }
            }
        }
        Ok(None) => {
            set_state(&app, Phase::Current, None);
            publish_status(&app);
            fail_remote_request(&app, &request, "target_mismatch");
        }
        Err(error) => {
            eprintln!("remote desktop update check: {error}");
            set_state(&app, Phase::Error, None);
            publish_status(&app);
            fail_remote_request(&app, &request, "check_failed");
        }
    }
}
fn finish(app: &AppHandle) {
    let updater = app.state::<Updater>();
    updater.busy.store(false, Ordering::SeqCst);
    let _ = updater.channel_menu.set_enabled(true);
    emit_state(app);
}

fn install(app: AppHandle, manifest: Manifest) {
    install_with_context(app, manifest, None);
}

fn install_with_context(app: AppHandle, manifest: Manifest, remote: Option<RemoteInstallContext>) {
    if let Some(remote) = &remote {
        if let Err(error) = reread_remote_request(&app, remote) {
            let code = finish_remote_gate_failure(&app, remote, error);
            eprintln!("remote desktop update request gate: {code}");
            return;
        }
    }
    set_state(&app, Phase::Downloading, None);
    if let Some(remote) = &remote {
        set_remote_phase(
            &app,
            &remote.request_id,
            &remote.target,
            RemoteRequestPhase::Downloading,
            Some(0),
            None,
        );
    }
    tauri::async_runtime::spawn(async move {
        let result = async {
            let artifact = manifest
                .validate(manifest.channel, &manifest.tag, &manifest.source_sha, TARGET)?
                .clone();
            let download = match client() {
                Ok(client) => {
                    let progress_app = app.clone();
                    let progress_remote = remote.clone();
                    asset_with_progress(
                        &app,
                        &client,
                        &artifact.url,
                        Length::Exact(artifact.size),
                        move |received, total| {
                            let Some(remote) = &progress_remote else {
                                return;
                            };
                            let progress = if total == 0 {
                                0
                            } else {
                                received.saturating_mul(100).checked_div(total).unwrap_or(0).min(100) as u8
                            };
                            let current = progress_app
                                .state::<Updater>()
                                .state
                                .lock()
                                .ok()
                                .and_then(|state| state.remote_request.clone());
                            if should_publish_download_progress(current.as_ref(), &remote.request_id, progress) {
                                set_remote_phase(
                                    &progress_app,
                                    &remote.request_id,
                                    &remote.target,
                                    RemoteRequestPhase::Downloading,
                                    Some(progress),
                                    None,
                                );
                            }
                        },
                    )
                    .await
                }
                Err(error) => Err(error),
            };
            let handle = app.clone();
            let install_remote = remote.clone();
            let install_version = manifest.version.clone();
            tauri::async_runtime::spawn_blocking(move || {
                update::install_verified(download, &artifact, KEY, |data| {
                    if let Some(remote) = &install_remote {
                        if let Err(error) = reread_remote_request(&handle, remote) {
                            let code = record_remote_gate_failure(&handle, remote, error);
                            return Err(format!("remote_request_gate:{code}"));
                        }
                    }
                    set_state(&handle, Phase::Installing, None);
                    if let Some(remote) = &install_remote {
                        set_remote_phase(
                            &handle,
                            &remote.request_id,
                            &remote.target,
                            RemoteRequestPhase::Installing,
                            None,
                            None,
                        );
                    }
                    let before_exit_remote = install_remote.clone();
                    let before_exit_app = handle.clone();
                    super::update_install::install_with_before_exit(&handle, data, &install_version, move || {
                        if let Some(remote) = before_exit_remote {
                            set_remote_phase(
                                &before_exit_app,
                                &remote.request_id,
                                &remote.target,
                                RemoteRequestPhase::Restarting,
                                None,
                                None,
                            );
                        }
                    })
                })
            })
            .await
            .map_err(|_| "installer task failed")??;
            Ok::<(), String>(())
        }
        .await;
        match result {
            Ok(()) => {
                if let Some(remote) = &remote {
                    set_remote_phase(
                        &app,
                        &remote.request_id,
                        &remote.target,
                        RemoteRequestPhase::Restarting,
                        None,
                        None,
                    );
                } else {
                    set_state(&app, Phase::Installed, None);
                }
                finish(&app);
                #[cfg(target_os = "macos")]
                {
                    super::restart_after_computer_use_shutdown(app);
                }
            }
            Err(error) => {
                eprintln!("desktop update install: {error}");
                if error.strip_prefix("remote_request_gate:").is_some() {
                    finish(&app);
                    return;
                }
                if let Some(remote) = &remote {
                    let error_code = if error == "requires_host" {
                        "requires_host"
                    } else {
                        "install_failed"
                    };
                    set_remote_phase(
                        &app,
                        &remote.request_id,
                        &remote.target,
                        RemoteRequestPhase::Failed,
                        None,
                        Some(error_code),
                    );
                } else {
                    set_state(&app, Phase::Error, None);
                }
                finish(&app);
                if !workbench_ready(&app) {
                    app.dialog()
                        .message(catalog().install_failed)
                        .title(catalog().title)
                        .kind(MessageDialogKind::Error)
                        .show(|_| {});
                }
            }
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn dialog_buttons_map_to_one_choice_and_dismissal_is_later() {
        for locale in ["en", "zh"] {
            let c = super::super::native_catalog_for_locales([locale.to_owned()]).updater;
            let labels = [&c.install, &c.skip, &c.cancel];
            assert_eq!(
                labels.iter().collect::<std::collections::HashSet<_>>().len(),
                3,
                "button labels identify the choice"
            );
            assert_eq!(
                choice(&MessageDialogResult::Custom(c.install.clone()), &c),
                Choice::Install
            );
            assert_eq!(choice(&MessageDialogResult::Custom(c.skip.clone()), &c), Choice::Skip);
            assert_eq!(
                choice(&MessageDialogResult::Custom(c.cancel.clone()), &c),
                Choice::Later
            );
            for dismissed in [
                MessageDialogResult::Cancel,
                MessageDialogResult::Ok,
                MessageDialogResult::Yes,
                MessageDialogResult::No,
            ] {
                assert_eq!(choice(&dismissed, &c), Choice::Later);
            }
        }
    }

    #[test]
    fn only_the_startup_check_honours_a_skipped_version() {
        assert!(!should_prompt(false, Some("1.2.3"), "1.2.3"));
        assert!(should_prompt(false, Some("1.2.3"), "1.2.4"));
        assert!(should_prompt(false, None, "1.2.3"));
        assert!(should_prompt(true, Some("1.2.3"), "1.2.3"));
    }

    #[test]
    fn skipped_version_is_persisted_as_the_release_string() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("update-skipped.json");
        persist_skipped_version(&path, "3.2.3").unwrap();
        assert_eq!(
            serde_json::from_slice::<String>(&std::fs::read(path).unwrap()).unwrap(),
            "3.2.3"
        );
    }

    #[test]
    fn desktop_update_state_serializes_the_interactive_check_contract() {
        let state = DesktopUpdateState {
            phase: Phase::Available,
            latest: Some("3.2.3".to_owned()),
            channel: Channel::Test,
            skipped_version: Some("3.2.3".to_owned()),
            interactive: true,
            remote_install_allowed: false,
            remote_request: None,
        };
        assert_eq!(
            serde_json::to_value(state).unwrap(),
            serde_json::json!({
                "phase": "available",
                "latest": "3.2.3",
                "channel": "test",
                "skippedVersion": "3.2.3",
                "interactive": true,
                "remoteInstallAllowed": false,
                "remoteRequest": null,
            })
        );
    }

    #[test]
    fn check_results_produce_their_record_and_other_phases_publish_nothing() {
        for phase in [
            Phase::Idle,
            Phase::Checking,
            Phase::Current,
            Phase::Available,
            Phase::Downloading,
            Phase::Installing,
            Phase::Installed,
            Phase::Disabled,
            Phase::Error,
        ] {
            let state = State {
                phase,
                latest: Some("3.2.3".to_owned()),
                channel: Channel::Test,
                manifest: None,
                availability_interactive: false,
                remote_install_allowed: false,
                remote_request: None,
            };
            let expected = match phase {
                Phase::Current => Some(UpdateStatusPhase::Current),
                Phase::Available => Some(UpdateStatusPhase::Available),
                Phase::Disabled => Some(UpdateStatusPhase::Disabled),
                Phase::Error => Some(UpdateStatusPhase::Error),
                Phase::Idle | Phase::Checking | Phase::Downloading | Phase::Installing | Phase::Installed => None,
            };
            assert_eq!(
                check_record(&state, "3.2.2", SystemTime::now()).map(|record| record.phase),
                expected,
                "{phase:?}"
            );
        }
    }

    #[test]
    fn completed_remote_check_replaces_the_ordinary_result_and_preserves_remote_state() {
        let previous = UpdateStatusRecord::new(
            UpdateStatusPhase::Available,
            "3.2.2",
            Some("3.2.3".to_owned()),
            Channel::Test,
            SystemTime::UNIX_EPOCH,
        );
        let remote = RemoteRequestStatus::new(
            "00000000-0000-4000-8000-000000000000",
            "3.2.4",
            RemoteRequestPhase::Failed,
            None,
            Some("target_mismatch".to_owned()),
            SystemTime::UNIX_EPOCH,
        );
        let mut state = State {
            phase: Phase::Error,
            latest: Some("3.2.4".to_owned()),
            channel: Channel::Stable,
            manifest: None,
            availability_interactive: true,
            remote_install_allowed: true,
            remote_request: Some(remote.clone()),
        };

        let published = status_for_publish(&state, "3.2.2", Some(previous.clone()), SystemTime::UNIX_EPOCH)
            .with_remote(state.remote_install_allowed, state.remote_request.clone());
        assert_eq!(published.phase, UpdateStatusPhase::Error);
        assert_eq!(published.channel, Channel::Stable);
        assert_eq!(published.latest, None);
        assert_eq!(published.remote_request, Some(remote));

        state.phase = Phase::Checking;
        assert_eq!(
            status_for_publish(&state, "3.2.2", Some(previous.clone()), SystemTime::UNIX_EPOCH,),
            previous,
            "in-flight phases keep the last completed ordinary result"
        );
    }

    #[test]
    fn remote_explicit_check_bypasses_a_saved_skip_without_rewriting_it() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("update-skipped.json");
        persist_skipped_version(&path, "3.2.3").unwrap();

        assert!(should_prompt(true, Some("3.2.3"), "3.2.3"));
        assert_eq!(
            serde_json::from_slice::<String>(&std::fs::read(path).unwrap()).unwrap(),
            "3.2.3",
            "a remote explicit check must not mutate the global skip preference"
        );
    }

    #[test]
    fn remote_delivery_is_selected_only_by_the_local_shell_preference() {
        assert_eq!(remote_delivery(false), RemoteDelivery::Prompt);
        assert_eq!(remote_delivery(true), RemoteDelivery::Install);
    }

    #[test]
    fn remote_download_progress_never_regresses_across_source_retries() {
        let current = RemoteRequestStatus::new(
            "00000000-0000-4000-8000-000000000000",
            "3.2.4",
            RemoteRequestPhase::Downloading,
            Some(42),
            None,
            SystemTime::UNIX_EPOCH,
        );

        assert!(!should_publish_download_progress(
            Some(&current),
            &current.request_id,
            0
        ));
        assert!(!should_publish_download_progress(
            Some(&current),
            &current.request_id,
            42
        ));
        assert!(should_publish_download_progress(
            Some(&current),
            &current.request_id,
            43
        ));
    }

    #[test]
    fn duplicate_nudges_cannot_turn_the_active_request_into_a_busy_failure() {
        let request = DesktopUpdateRequest::parse(
            serde_json::to_string(&serde_json::json!({
                "schema_version": 1,
                "request_id": "00000000-0000-4000-8000-000000000000",
                "target": "3.2.4",
                "state": "pending",
                "created_at": "1970-01-01T00:01:40Z",
                "expires_at": "1970-01-01T00:02:40Z",
            }))
            .unwrap()
            .as_bytes(),
            SystemTime::UNIX_EPOCH + Duration::from_secs(100),
        )
        .unwrap();
        let mut state = State {
            phase: Phase::Idle,
            latest: None,
            channel: Channel::Stable,
            manifest: None,
            availability_interactive: false,
            remote_install_allowed: false,
            remote_request: None,
        };
        let busy = AtomicBool::new(false);

        assert_eq!(
            claim_remote_request(
                &mut state,
                &busy,
                &request,
                SystemTime::UNIX_EPOCH + Duration::from_secs(101),
            ),
            RemoteRequestStart::Started
        );
        assert!(busy.load(Ordering::SeqCst));
        assert_eq!(
            claim_remote_request(
                &mut state,
                &busy,
                &request,
                SystemTime::UNIX_EPOCH + Duration::from_secs(102),
            ),
            RemoteRequestStart::AlreadyHandled
        );
        assert_eq!(
            state.remote_request.as_ref().map(|status| status.phase),
            Some(RemoteRequestPhase::Requested)
        );
    }

    #[test]
    fn remote_install_preference_is_stable_for_the_lifetime_of_a_request() {
        let remote = |phase| {
            RemoteRequestStatus::new(
                "00000000-0000-4000-8000-000000000000",
                "3.2.4",
                phase,
                None,
                None,
                SystemTime::UNIX_EPOCH,
            )
        };
        let mut state = State {
            phase: Phase::Idle,
            latest: None,
            channel: Channel::Stable,
            manifest: None,
            availability_interactive: false,
            remote_install_allowed: false,
            remote_request: None,
        };

        assert!(remote_preference_can_change(&state));
        state.remote_request = Some(remote(RemoteRequestPhase::Checking));
        assert!(!remote_preference_can_change(&state));
        state.remote_request = Some(remote(RemoteRequestPhase::Completed));
        assert!(remote_preference_can_change(&state));
    }

    #[test]
    fn remote_target_must_equal_the_fresh_current_channel_result() {
        assert_eq!(validate_remote_target("3.2.4", "3.2.4"), Ok(()));
        assert_eq!(validate_remote_target("3.2.5", "3.2.4"), Err("target_mismatch"));
    }

    #[test]
    fn remote_install_preference_defaults_off_and_round_trips_locally() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("remote-install.json");
        assert!(!read_remote_install_preference(&path).unwrap());

        write_remote_install_preference(&path, true).unwrap();
        assert!(read_remote_install_preference(&path).unwrap());
        write_remote_install_preference(&path, false).unwrap();
        assert!(!read_remote_install_preference(&path).unwrap());
    }

    #[test]
    fn remote_request_gate_rejects_replacement_and_expiry() {
        let created = SystemTime::UNIX_EPOCH + Duration::from_secs(100);
        let expires = created + Duration::from_secs(60);
        let request = DesktopUpdateRequest::parse(
            serde_json::to_string(&serde_json::json!({
                "schema_version": 1,
                "request_id": "00000000-0000-4000-8000-000000000000",
                "target": "3.2.4",
                "state": "pending",
                "created_at": "1970-01-01T00:01:40Z",
                "expires_at": "1970-01-01T00:02:40Z",
            }))
            .unwrap()
            .as_bytes(),
            created,
        )
        .unwrap();
        let context = RemoteInstallContext {
            request_id: request.request_id.clone(),
            target: request.target.clone(),
        };

        assert_eq!(validate_remote_request_gate(&request, &context, created), Ok(()));
        assert_eq!(
            validate_remote_request_gate(
                &request,
                &RemoteInstallContext {
                    request_id: "11111111-1111-4111-8111-111111111111".to_owned(),
                    target: context.target.clone(),
                },
                created,
            ),
            Err(RemoteRequestGateError::Mismatch)
        );
        assert_eq!(
            validate_remote_request_gate(&request, &context, expires),
            Err(RemoteRequestGateError::Expired)
        );
    }

    #[test]
    fn restart_receipt_is_completed_only_by_the_matching_new_shell() {
        assert_eq!(restart_outcome("3.2.4", "3.2.4"), (RemoteRequestPhase::Completed, None));
        assert_eq!(
            restart_outcome("3.2.4", "3.2.3"),
            (RemoteRequestPhase::Failed, Some("version_mismatch"))
        );
    }

    #[test]
    fn every_native_update_state_has_localized_copy() {
        for locale in ["en", "zh"] {
            let c = super::super::native_catalog_for_locales([locale.to_owned()]).updater;
            for phase in [
                Phase::Idle,
                Phase::Checking,
                Phase::Current,
                Phase::Available,
                Phase::Downloading,
                Phase::Installing,
                Phase::Installed,
                Phase::Disabled,
                Phase::Error,
            ] {
                assert!(!label(phase, &c).is_empty());
            }
        }
    }
}
