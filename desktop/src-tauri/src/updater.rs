//! Native-only desktop updater. Remote WebViews never receive updater commands.
use avibe_runtime_host::update::{self, Channel, Manifest, REPOSITORY};
use serde::Deserialize;
use std::{
    path::PathBuf,
    sync::{
        atomic::{AtomicBool, Ordering},
        Mutex,
    },
    time::Duration,
};
use tauri::{
    menu::{CheckMenuItem, MenuItem},
    AppHandle, Manager,
};
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons, MessageDialogKind, MessageDialogResult};
use tauri_plugin_updater::{Update, UpdaterExt};

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

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
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
}
pub struct Updater {
    state: Mutex<State>,
    busy: AtomicBool,
    channel_path: PathBuf,
    skipped_path: PathBuf,
    pub menu: MenuItem<tauri::Wry>,
    pub channel_menu: CheckMenuItem<tauri::Wry>,
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
    app.manage(Updater {
        state: Mutex::new(State {
            phase: Phase::Idle,
            latest: None,
            channel,
        }),
        busy: AtomicBool::new(false),
        channel_path,
        skipped_path: app.path().app_local_data_dir()?.join("update-skipped.json"),
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
    if latest.is_some() || phase == Phase::Checking {
        state.latest = latest;
    }
    let _ = updater
        .menu
        .set_text(format!("{} — {}", label(phase, &catalog()), app.package_info().version));
    let _ = updater.channel_menu.set_enabled(!updater.busy.load(Ordering::SeqCst));
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
    let result = path
        .parent()
        .ok_or_else(|| std::io::Error::other("missing config directory"))
        .and_then(std::fs::create_dir_all)
        .and_then(|()| std::fs::write(path, serde_json::to_vec(version).expect("version JSON")));
    if let Err(error) = result {
        eprintln!("desktop update skip: {error}");
    }
}

#[derive(Deserialize)]
struct Release {
    tag_name: String,
    draft: bool,
    prerelease: bool,
    assets: Vec<ReleaseAsset>,
}
#[derive(Deserialize)]
struct ReleaseAsset {
    name: String,
    browser_download_url: String,
}

async fn bytes(client: &reqwest::Client, url: &str, max: usize) -> Result<Vec<u8>, String> {
    let mut response = client
        .get(url)
        .send()
        .await
        .map_err(|_| "network request failed")?
        .error_for_status()
        .map_err(|_| "release metadata unavailable")?;
    let mut result = Vec::new();
    while let Some(chunk) = response.chunk().await.map_err(|_| "metadata download failed")? {
        if result.len() + chunk.len() > max {
            return Err("metadata too large".into());
        }
        result.extend_from_slice(&chunk);
    }
    Ok(result)
}
async fn find_update(app: &AppHandle, channel: Channel) -> Result<Option<(Update, Manifest)>, String> {
    let client = reqwest::Client::builder()
        .https_only(true)
        .timeout(Duration::from_secs(30))
        .user_agent("Avibe-desktop-updater")
        .build()
        .map_err(|_| "HTTP client failed")?;
    let mut candidates = Vec::new();
    // GitHub orders by creation, not SemVer. Walk the complete bounded history;
    // reaching the bound fails instead of claiming an incomplete list is current.
    for page in 1..=10 {
        let data = bytes(
            &client,
            &format!("https://api.github.com/repos/{REPOSITORY}/releases?per_page=100&page={page}"),
            8 * 1024 * 1024,
        )
        .await?;
        let releases: Vec<Release> = serde_json::from_slice(&data).map_err(|_| "invalid releases response")?;
        let done = releases.len() < 100;
        for release in releases {
            if !release.draft && release.prerelease == (channel == Channel::Test) {
                if let Ok(version) = channel.version(&release.tag_name) {
                    candidates.push((version, release));
                }
            }
        }
        if done {
            break;
        }
        if page == 10 {
            return Err("release history exceeds discovery bound".into());
        }
    }
    let Some((version, release)) = candidates.into_iter().max_by(|a, b| a.0.cmp(&b.0)) else {
        return Err("no release in this channel".into());
    };
    set_state(app, Phase::Checking, Some(version.to_string()));
    if version <= app.package_info().version {
        return Ok(None);
    }
    let name = update::manifest_name(TARGET);
    for name in [&name, &format!("{name}.sig")] {
        let expected = update::asset_url(&release.tag_name, name);
        if release
            .assets
            .iter()
            .filter(|asset| asset.name == *name && asset.browser_download_url == expected)
            .count()
            != 1
        {
            return Err("signed updater metadata unavailable; manual installation required".into());
        }
    }
    let manifest_url = update::asset_url(&release.tag_name, &name);
    let raw = bytes(&client, &manifest_url, 64 * 1024).await?;
    let sig = bytes(&client, &format!("{manifest_url}.sig"), 4096).await?;
    let manifest = Manifest::authenticated(&raw, std::str::from_utf8(&sig).map_err(|_| "invalid signature")?, KEY)?;
    let commit: serde_json::Value = serde_json::from_slice(
        &bytes(
            &client,
            &format!("https://api.github.com/repos/{REPOSITORY}/commits/{}", release.tag_name),
            2 * 1024 * 1024,
        )
        .await?,
    )
    .map_err(|_| "invalid source response")?;
    let artifact = manifest.validate(
        channel,
        &release.tag_name,
        commit["sha"].as_str().ok_or("missing source")?,
        TARGET,
    )?;
    if !release.assets.iter().any(|a| a.browser_download_url == artifact.url) {
        return Err("updater artifact missing".into());
    }
    let updater = app
        .updater_builder()
        .pubkey(KEY)
        .target(update::target_spec(TARGET)?.0)
        .endpoints(vec![manifest_url.parse().map_err(|_| "invalid endpoint")?])
        .map_err(|_| "invalid updater endpoint")?
        .timeout(Duration::from_secs(600))
        .build()
        .map_err(|_| "updater initialization failed")?;
    let update = updater
        .check()
        .await
        .map_err(|_| "updater check failed")?
        .ok_or("release changed during check")?;
    let authenticated: serde_json::Value = serde_json::from_slice(&raw).map_err(|_| "invalid manifest")?;
    if update.raw_json != authenticated
        || update.version != manifest.version
        || update.download_url.as_str() != artifact.url
        || update.signature != artifact.signature
    {
        return Err("updater response differs from signed manifest".into());
    }
    Ok(Some((update, manifest)))
}

pub fn check(app: AppHandle, interactive: bool) {
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
        if interactive {
            show(&app, &catalog().disabled_detail, None);
        }
        return;
    }
    let channel = updater.state.lock().expect("updater state").channel;
    set_state(&app, Phase::Checking, None);
    tauri::async_runtime::spawn(async move {
        match find_update(&app, channel).await {
            Ok(Some((update, manifest))) => {
                set_state(&app, Phase::Available, Some(manifest.version.clone()));
                let skipped = skipped_version(&app.state::<Updater>());
                if !should_prompt(interactive, skipped.as_deref(), &manifest.version) {
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
                        Choice::Install => install(confirmation, update, manifest),
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
                if interactive {
                    show(&app, &catalog().check_failed, None);
                }
            }
        }
    });
}
fn finish(app: &AppHandle) {
    let updater = app.state::<Updater>();
    updater.busy.store(false, Ordering::SeqCst);
    let _ = updater.channel_menu.set_enabled(true);
}
fn install(app: AppHandle, update: Update, manifest: Manifest) {
    set_state(&app, Phase::Downloading, None);
    tauri::async_runtime::spawn(async move {
        let result = async {
            let download = update
                .download(|_, _| {}, || {})
                .await
                .map_err(|_| "download or signature verification failed".to_string());
            let artifact = manifest
                .validate(manifest.channel, &manifest.tag, &manifest.source_sha, TARGET)?
                .clone();
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
                    app.state::<super::Shell>()
                        .exit_authorized
                        .store(true, Ordering::SeqCst);
                    app.restart();
                }
            }
            Err(error) => {
                eprintln!("desktop update install: {error}");
                set_state(&app, Phase::Error, None);
                app.dialog()
                    .message(catalog().install_failed)
                    .title(catalog().title)
                    .kind(MessageDialogKind::Error)
                    .show(|_| {});
            }
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn signed_manifests_are_consumed_by_the_pinned_official_updater() {
        for (target, key, _) in update::TARGETS {
            for channel in ["test", "stable"] {
                let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join(format!(
                    "../runtime-host/tests/fixtures/updater/{channel}-{target}.json"
                ));
                let raw = std::fs::read(path).unwrap();
                let release: tauri_plugin_updater::RemoteRelease = serde_json::from_slice(&raw).unwrap();
                let manifest: Manifest = serde_json::from_slice(&raw).unwrap();
                assert_eq!(release.version.to_string(), manifest.version);
                assert_eq!(
                    release.download_url(key).unwrap().as_str(),
                    manifest.platforms[*key].url
                );
                assert_eq!(release.signature(key).unwrap(), &manifest.platforms[*key].signature);
            }
        }
    }

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
