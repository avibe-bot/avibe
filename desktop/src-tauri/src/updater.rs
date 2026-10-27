//! Desktop updater owned by the native shell and controlled by its Workbench popover.
use avibe_runtime_host::{
    download::{self, Length},
    update::{self, Channel, Manifest, Release, REPOSITORY},
    update_status::{UpdateStatusPhase, UpdateStatusRecord, UpdateStatusStore, UPDATE_STATUS_FILE},
};
use serde::{Deserialize, Serialize};
use std::{
    path::{Path, PathBuf},
    sync::{
        atomic::{AtomicBool, AtomicUsize, Ordering},
        Mutex,
    },
    time::{Duration, SystemTime},
};
use tauri::{
    menu::{CheckMenuItem, MenuItem},
    AppHandle, Manager, WebviewWindow,
};
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons, MessageDialogKind, MessageDialogResult};

pub const MENU_ID: &str = "desktop-update";
pub const CHANNEL_ID: &str = "desktop-update-test";
pub const OPEN_URL: &str = "avibe://updates";
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
struct State {
    phase: Phase,
    latest: Option<String>,
    channel: Channel,
    manifest: Option<Manifest>,
}

#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct DesktopUpdateState {
    phase: Phase,
    latest: Option<String>,
    channel: Channel,
    skipped_version: Option<String>,
}
pub struct Updater {
    state: Mutex<State>,
    busy: AtomicBool,
    /// Index into `update::sources` of the source that last completed a
    /// download; the next download tries it first.
    source: AtomicUsize,
    channel_path: PathBuf,
    skipped_path: PathBuf,
    /// The last completed check, published for the Runtime to relay to browsers.
    status: UpdateStatusStore,
    pub menu: MenuItem<tauri::Wry>,
    pub channel_menu: CheckMenuItem<tauri::Wry>,
}
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
    app.manage(Updater {
        state: Mutex::new(State {
            phase: Phase::Idle,
            latest: None,
            channel,
            manifest: None,
        }),
        busy: AtomicBool::new(false),
        source: AtomicUsize::new(0),
        channel_path,
        skipped_path: app.path().app_local_data_dir()?.join("update-skipped.json"),
        status: UpdateStatusStore::new(status_dir.join(UPDATE_STATUS_FILE)),
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
    Ok(())
}
fn set_state(app: &AppHandle, phase: Phase, latest: Option<String>) {
    let updater = app.state::<Updater>();
    let mut state = updater.state.lock().expect("updater state");
    state.phase = phase;
    if matches!(phase, Phase::Checking | Phase::Current | Phase::Disabled | Phase::Error) {
        state.manifest = None;
    }
    if latest.is_some() || phase == Phase::Checking {
        state.latest = latest;
    }
    let _ = updater
        .menu
        .set_text(format!("{} — {}", label(phase, &catalog()), app.package_info().version));
    let _ = updater.channel_menu.set_enabled(!updater.busy.load(Ordering::SeqCst));
}

fn state_snapshot(app: &AppHandle) -> DesktopUpdateState {
    let updater = app.state::<Updater>();
    let state = updater.state.lock().expect("updater state");
    DesktopUpdateState {
        phase: state.phase,
        latest: state.latest.clone(),
        channel: state.channel,
        skipped_version: skipped_version(&updater),
    }
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
    // Publishes the finished check in `update-status.json` for the Runtime to
    // relay. It is nested here so nothing but `check()` can publish. Install
    // progress and outcomes stay in memory: a failed install keeps the last
    // `Available` (the next check offers it again), and `Installed` only means
    // the installer took over — on Windows it runs after the shell exits and can
    // still restore this version — so the new app's startup check publishes
    // `Current`.
    fn publish(app: &AppHandle) {
        let updater = app.state::<Updater>();
        let state = updater.state.lock().expect("updater state");
        let Some(record) = check_record(&state, &app.package_info().version.to_string(), SystemTime::now()) else {
            return;
        };
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
        publish(&app);
        if interactive {
            show(&app, &catalog().disabled_detail, None);
        }
        return;
    }
    let channel = updater.state.lock().expect("updater state").channel;
    set_state(&app, Phase::Checking, None);
    tauri::async_runtime::spawn(async move {
        match find_update(&app, channel).await {
            Ok(Some(manifest)) => {
                set_available(&app, manifest.clone());
                publish(&app);
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
                publish(&app);
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
                publish(&app);
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
    check(app, false);
    Ok(())
}

#[tauri::command]
pub fn desktop_update_install(window: WebviewWindow, app: AppHandle) -> Result<(), String> {
    ensure_workbench(&window)?;
    let updater = app.state::<Updater>();
    let manifest = {
        let state = updater
            .state
            .lock()
            .map_err(|_| "Desktop updater state is unavailable.".to_owned())?;
        if state.phase != Phase::Available {
            return Err("No desktop update is ready to install.".to_owned());
        }
        state
            .manifest
            .clone()
            .ok_or_else(|| "No desktop update is ready to install.".to_owned())?
    };
    if updater.busy.swap(true, Ordering::SeqCst) {
        return Err("A desktop update is already in progress.".to_owned());
    }
    install(app, manifest);
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
fn finish(app: &AppHandle) {
    let updater = app.state::<Updater>();
    updater.busy.store(false, Ordering::SeqCst);
    let _ = updater.channel_menu.set_enabled(true);
}
fn install(app: AppHandle, manifest: Manifest) {
    set_state(&app, Phase::Downloading, None);
    tauri::async_runtime::spawn(async move {
        let result = async {
            let artifact = manifest
                .validate(manifest.channel, &manifest.tag, &manifest.source_sha, TARGET)?
                .clone();
            let download = match client() {
                Ok(client) => asset(&app, &client, &artifact.url, Length::Exact(artifact.size)).await,
                Err(error) => Err(error),
            };
            let handle = app.clone();
            tauri::async_runtime::spawn_blocking(move || {
                update::install_verified(download, &artifact, KEY, |data| {
                    set_state(&handle, Phase::Installing, None);
                    super::update_install::install(&handle, data, &manifest.version)
                })
            })
            .await
            .map_err(|_| "installer task failed")??;
            Ok::<(), String>(())
        }
        .await;
        finish(&app);
        match result {
            Ok(()) => {
                set_state(&app, Phase::Installed, None);
                #[cfg(target_os = "macos")]
                {
                    super::restart_after_computer_use_shutdown(app);
                }
            }
            Err(error) => {
                eprintln!("desktop update install: {error}");
                set_state(&app, Phase::Error, None);
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
