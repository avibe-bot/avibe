//! Verification and atomic installation of the app-private Avibe Runtime.
//!
//! Product packages carry one target-specific ZIP plus an embedded manifest.
//! Production trust comes from the desktop application's code signature; the
//! first launch then verifies the archive before extracting it into a versioned
//! user-data directory. The application bundle is never mutated, and an update
//! can install a successor without replacing files used by a running daemon.

use std::fs::{self, File, OpenOptions};
use std::io::{self, Read, Write};
use std::path::{Component, Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use zip::ZipArchive;

const MANIFEST_NAME: &str = "runtime-manifest.json";
const INSTALL_MARKER_NAME: &str = ".avibe-runtime.json";
const MAX_MANIFEST_BYTES: u64 = 32 * 1024;
const MAX_ARCHIVE_BYTES: u64 = 1024 * 1024 * 1024;
const MAX_UNPACKED_BYTES: u64 = 2 * 1024 * 1024 * 1024;
const MAX_ARCHIVE_ENTRIES: u64 = 200_000;
const TREE_HASH_DOMAIN: &[u8] = b"avibe-runtime-tree-v1\0";
static INSTALL_SEQUENCE: AtomicU64 = AtomicU64::new(0);

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RuntimeBundleManifest {
    pub schema_version: u32,
    pub runtime_version: String,
    pub os: String,
    pub arch: String,
    pub archive: String,
    pub archive_sha256: String,
    pub archive_size: u64,
    pub unpacked_size: u64,
    pub entry_count: u64,
    pub tree_sha256: String,
    pub python_entrypoint: String,
    pub node_entrypoint: String,
    pub codex_entrypoint: String,
    pub python_distribution: RuntimeSource,
    pub node_distribution: RuntimeSource,
    pub codex_version: String,
    pub avibe_wheel: RuntimeArtifact,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RuntimeSource {
    pub url: String,
    pub sha256: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RuntimeArtifact {
    pub name: String,
    pub sha256: String,
}

#[derive(Debug, thiserror::Error)]
pub enum PrivateRuntimeError {
    #[error("the bundled Runtime manifest is unavailable")]
    ManifestUnavailable(#[source] io::Error),
    #[error("the bundled Runtime manifest is invalid")]
    ManifestInvalid,
    #[error("the bundled Runtime targets another platform")]
    TargetMismatch,
    #[error("the bundled Runtime archive is unavailable")]
    ArchiveUnavailable(#[source] io::Error),
    #[error("the bundled Runtime archive failed verification")]
    ArchiveVerification,
    #[error("the bundled Runtime archive is invalid")]
    ArchiveInvalid,
    #[error("the private Runtime could not be installed")]
    Install(#[source] io::Error),
}

#[derive(Debug, Clone)]
pub struct InstalledPrivateRuntime {
    pub root: PathBuf,
    pub python: PathBuf,
    pub node: PathBuf,
    pub codex: PathBuf,
    pub runtime_id: String,
}

#[derive(Debug, Clone)]
pub struct PrivateRuntimeBundle {
    bundle_dir: PathBuf,
    install_root: PathBuf,
}

impl PrivateRuntimeBundle {
    pub fn new(bundle_dir: PathBuf, install_root: PathBuf) -> Self {
        Self {
            bundle_dir,
            install_root,
        }
    }

    pub fn prepare(&self) -> Result<InstalledPrivateRuntime, PrivateRuntimeError> {
        let manifest = self.read_manifest()?;
        validate_manifest(&manifest)?;

        let digest_prefix = manifest
            .archive_sha256
            .get(..16)
            .ok_or(PrivateRuntimeError::ManifestInvalid)?;
        let version_dir = self.install_root.join(&manifest.runtime_version);
        let primary_dir = version_dir.join(digest_prefix);
        let repair_dir = version_dir.join(format!("{digest_prefix}-repair"));
        for candidate in [&primary_dir, &repair_dir] {
            if path_present(candidate) {
                if let Ok(runtime) = installed_runtime(candidate, &manifest) {
                    return Ok(runtime);
                }
            }
        }

        let archive_path = self.bundle_dir.join(&manifest.archive);
        verify_archive(&archive_path, &manifest)?;
        fs::create_dir_all(&self.install_root).map_err(PrivateRuntimeError::Install)?;
        let install_dir = if !path_present(&primary_dir) {
            primary_dir
        } else if !path_present(&repair_dir) {
            repair_dir
        } else {
            // Both independently installed copies failed integrity validation.
            // Never execute either one, and do not mutate a directory that a
            // still-running daemon may have open.
            return Err(PrivateRuntimeError::ArchiveVerification);
        };

        let sequence = INSTALL_SEQUENCE.fetch_add(1, Ordering::SeqCst);
        let staging = self
            .install_root
            .join(format!(".install-{}-{sequence}", std::process::id()));
        if staging.exists() {
            fs::remove_dir_all(&staging).map_err(PrivateRuntimeError::Install)?;
        }
        fs::create_dir(&staging).map_err(PrivateRuntimeError::Install)?;

        let install_result = (|| {
            extract_archive(&archive_path, &staging, &manifest)?;
            validate_runtime_files(&staging, &manifest)?;
            verify_installed_tree(&staging, &manifest)?;
            write_marker(&staging, &manifest)?;
            if let Some(parent) = install_dir.parent() {
                fs::create_dir_all(parent).map_err(PrivateRuntimeError::Install)?;
            }
            match fs::rename(&staging, &install_dir) {
                Ok(()) => {}
                Err(error) => {
                    // Windows and Unix report different error kinds when
                    // another process wins this content-addressed install.
                    // Accept the race only after validating the winner.
                    if installed_runtime(&install_dir, &manifest).is_ok() {
                        fs::remove_dir_all(&staging).map_err(PrivateRuntimeError::Install)?;
                    } else {
                        return Err(PrivateRuntimeError::Install(error));
                    }
                }
            }
            installed_runtime(&install_dir, &manifest)
        })();

        if staging.exists() {
            let _ = fs::remove_dir_all(&staging);
        }
        install_result
    }

    /// Removes private Runtime trees that are no longer used by the active
    /// desktop-managed daemon.
    ///
    /// The caller invokes this only after `/ready` proves that `active_root` is
    /// the Runtime currently serving the desktop shell. Cleanup is deliberately
    /// outside `prepare`: an older daemon may still have its executable tree
    /// open while the successor is being installed.
    pub fn prune_superseded(&self, active_root: &Path) -> Result<(), PrivateRuntimeError> {
        let active_version = active_root
            .parent()
            .filter(|parent| parent.parent() == Some(self.install_root.as_path()))
            .ok_or(PrivateRuntimeError::Install(io::Error::new(
                io::ErrorKind::InvalidInput,
                "active Runtime is outside the private install root",
            )))?;

        let entries = match fs::read_dir(&self.install_root) {
            Ok(entries) => entries,
            Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(()),
            Err(error) => return Err(PrivateRuntimeError::Install(error)),
        };
        for entry in entries {
            let path = entry.map_err(PrivateRuntimeError::Install)?.path();
            if path == active_version {
                for candidate in fs::read_dir(&path).map_err(PrivateRuntimeError::Install)? {
                    let candidate = candidate.map_err(PrivateRuntimeError::Install)?.path();
                    if candidate != active_root {
                        remove_install_path(&candidate)?;
                    }
                }
            } else {
                remove_install_path(&path)?;
            }
        }
        Ok(())
    }

    fn read_manifest(&self) -> Result<RuntimeBundleManifest, PrivateRuntimeError> {
        let path = self.bundle_dir.join(MANIFEST_NAME);
        let file = File::open(path).map_err(PrivateRuntimeError::ManifestUnavailable)?;
        let mut bytes = Vec::new();
        file.take(MAX_MANIFEST_BYTES + 1)
            .read_to_end(&mut bytes)
            .map_err(PrivateRuntimeError::ManifestUnavailable)?;
        if bytes.len() > MAX_MANIFEST_BYTES as usize {
            return Err(PrivateRuntimeError::ManifestInvalid);
        }
        serde_json::from_slice(&bytes).map_err(|_| PrivateRuntimeError::ManifestInvalid)
    }
}

fn validate_manifest(manifest: &RuntimeBundleManifest) -> Result<(), PrivateRuntimeError> {
    if manifest.schema_version != 1
        || !safe_segment(&manifest.runtime_version)
        || !safe_file_name(&manifest.archive)
        || !valid_sha256(&manifest.archive_sha256)
        || manifest.archive_size == 0
        || manifest.archive_size > MAX_ARCHIVE_BYTES
        || manifest.unpacked_size == 0
        || manifest.unpacked_size > MAX_UNPACKED_BYTES
        || manifest.entry_count == 0
        || manifest.entry_count > MAX_ARCHIVE_ENTRIES
        || !valid_sha256(&manifest.tree_sha256)
        || !valid_relative_path(&manifest.python_entrypoint)
        || !valid_relative_path(&manifest.node_entrypoint)
        || !valid_relative_path(&manifest.codex_entrypoint)
        || !valid_sha256(&manifest.python_distribution.sha256)
        || !valid_sha256(&manifest.node_distribution.sha256)
        || !valid_sha256(&manifest.avibe_wheel.sha256)
        || manifest.python_distribution.url.is_empty()
        || manifest.node_distribution.url.is_empty()
        || manifest.codex_version.is_empty()
        || !safe_file_name(&manifest.avibe_wheel.name)
    {
        return Err(PrivateRuntimeError::ManifestInvalid);
    }
    if manifest.os != current_os() || manifest.arch != current_arch() {
        return Err(PrivateRuntimeError::TargetMismatch);
    }
    Ok(())
}

fn current_os() -> &'static str {
    match std::env::consts::OS {
        "macos" => "macos",
        "windows" => "windows",
        other => other,
    }
}

fn current_arch() -> &'static str {
    match std::env::consts::ARCH {
        "aarch64" => "aarch64",
        "x86_64" => "x86_64",
        other => other,
    }
}

fn safe_segment(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 128
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'+' | b'-'))
}

fn safe_file_name(value: &str) -> bool {
    safe_segment(value) && Path::new(value).file_name().is_some_and(|name| name == value)
}

fn valid_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
}

fn valid_relative_path(value: &str) -> bool {
    let path = Path::new(value);
    !value.is_empty()
        && !path.is_absolute()
        && path
            .components()
            .all(|component| matches!(component, Component::Normal(_)))
}

fn path_present(path: &Path) -> bool {
    fs::symlink_metadata(path).is_ok()
}

fn remove_install_path(path: &Path) -> Result<(), PrivateRuntimeError> {
    let metadata = fs::symlink_metadata(path).map_err(PrivateRuntimeError::Install)?;
    if metadata.file_type().is_dir() && !metadata.file_type().is_symlink() {
        fs::remove_dir_all(path).map_err(PrivateRuntimeError::Install)
    } else {
        fs::remove_file(path).map_err(PrivateRuntimeError::Install)
    }
}

fn verify_archive(path: &Path, manifest: &RuntimeBundleManifest) -> Result<(), PrivateRuntimeError> {
    let metadata = fs::metadata(path).map_err(PrivateRuntimeError::ArchiveUnavailable)?;
    if !metadata.is_file() || metadata.len() != manifest.archive_size {
        return Err(PrivateRuntimeError::ArchiveVerification);
    }

    let mut file = File::open(path).map_err(PrivateRuntimeError::ArchiveUnavailable)?;
    let mut hasher = Sha256::new();
    io::copy(&mut file, &mut hasher).map_err(PrivateRuntimeError::ArchiveUnavailable)?;
    let actual = format!("{:x}", hasher.finalize());
    if actual != manifest.archive_sha256 {
        return Err(PrivateRuntimeError::ArchiveVerification);
    }
    Ok(())
}

fn extract_archive(
    archive_path: &Path,
    destination: &Path,
    manifest: &RuntimeBundleManifest,
) -> Result<(), PrivateRuntimeError> {
    let file = File::open(archive_path).map_err(PrivateRuntimeError::ArchiveUnavailable)?;
    let mut archive = ZipArchive::new(file).map_err(|_| PrivateRuntimeError::ArchiveInvalid)?;
    if archive.len() as u64 != manifest.entry_count {
        return Err(PrivateRuntimeError::ArchiveInvalid);
    }

    let mut unpacked = 0_u64;
    for index in 0..archive.len() {
        let mut entry = archive
            .by_index(index)
            .map_err(|_| PrivateRuntimeError::ArchiveInvalid)?;
        unpacked = unpacked
            .checked_add(entry.size())
            .filter(|value| *value <= manifest.unpacked_size)
            .ok_or(PrivateRuntimeError::ArchiveInvalid)?;
        let relative = entry
            .enclosed_name()
            .filter(|path| path.components().all(|part| matches!(part, Component::Normal(_))))
            .ok_or(PrivateRuntimeError::ArchiveInvalid)?;
        if entry.unix_mode().is_some_and(|mode| mode & 0o170_000 == 0o120_000) {
            return Err(PrivateRuntimeError::ArchiveInvalid);
        }

        let output = destination.join(relative);
        if entry.is_dir() {
            fs::create_dir_all(&output).map_err(PrivateRuntimeError::Install)?;
            continue;
        }
        if let Some(parent) = output.parent() {
            fs::create_dir_all(parent).map_err(PrivateRuntimeError::Install)?;
        }
        let mut target = OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&output)
            .map_err(PrivateRuntimeError::Install)?;
        io::copy(&mut entry, &mut target).map_err(PrivateRuntimeError::Install)?;
        target.flush().map_err(PrivateRuntimeError::Install)?;
        set_entry_permissions(&output, entry.unix_mode())?;
    }
    if unpacked != manifest.unpacked_size {
        return Err(PrivateRuntimeError::ArchiveInvalid);
    }
    Ok(())
}

#[cfg(unix)]
fn set_entry_permissions(path: &Path, mode: Option<u32>) -> Result<(), PrivateRuntimeError> {
    use std::os::unix::fs::PermissionsExt;
    let mode = mode.ok_or(PrivateRuntimeError::ArchiveInvalid)? & 0o777;
    fs::set_permissions(path, fs::Permissions::from_mode(mode)).map_err(PrivateRuntimeError::Install)
}

#[cfg(not(unix))]
fn set_entry_permissions(_path: &Path, _mode: Option<u32>) -> Result<(), PrivateRuntimeError> {
    Ok(())
}

fn validate_runtime_files(root: &Path, manifest: &RuntimeBundleManifest) -> Result<(), PrivateRuntimeError> {
    for relative in [
        &manifest.python_entrypoint,
        &manifest.node_entrypoint,
        &manifest.codex_entrypoint,
    ] {
        let path = root.join(relative);
        let metadata = fs::symlink_metadata(path).map_err(PrivateRuntimeError::Install)?;
        if !metadata.file_type().is_file() {
            return Err(PrivateRuntimeError::ArchiveInvalid);
        }
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            if metadata.permissions().mode() & 0o111 == 0 {
                return Err(PrivateRuntimeError::ArchiveInvalid);
            }
        }
    }
    Ok(())
}

fn verify_installed_tree(root: &Path, manifest: &RuntimeBundleManifest) -> Result<(), PrivateRuntimeError> {
    let root_metadata = fs::symlink_metadata(root).map_err(PrivateRuntimeError::Install)?;
    if !root_metadata.file_type().is_dir() {
        return Err(PrivateRuntimeError::ArchiveInvalid);
    }

    let mut files = Vec::new();
    collect_tree_files(root, root, &mut files)?;
    files.sort_by(|left, right| left.0.cmp(&right.0));

    let mut hasher = Sha256::new();
    hasher.update(TREE_HASH_DOMAIN);
    let mut unpacked_size = 0_u64;
    for (relative, path, size) in &files {
        unpacked_size = unpacked_size
            .checked_add(*size)
            .filter(|value| *value <= manifest.unpacked_size)
            .ok_or(PrivateRuntimeError::ArchiveVerification)?;
        update_tree_header(&mut hasher, relative, *size);
        let mut file = File::open(path).map_err(PrivateRuntimeError::Install)?;
        io::copy(&mut file, &mut hasher).map_err(PrivateRuntimeError::Install)?;
    }
    if files.len() as u64 != manifest.entry_count || unpacked_size != manifest.unpacked_size {
        return Err(PrivateRuntimeError::ArchiveVerification);
    }
    let actual = format!("{:x}", hasher.finalize());
    if actual != manifest.tree_sha256 {
        return Err(PrivateRuntimeError::ArchiveVerification);
    }
    Ok(())
}

fn collect_tree_files(
    root: &Path,
    directory: &Path,
    files: &mut Vec<(String, PathBuf, u64)>,
) -> Result<(), PrivateRuntimeError> {
    for entry in fs::read_dir(directory).map_err(PrivateRuntimeError::Install)? {
        let entry = entry.map_err(PrivateRuntimeError::Install)?;
        let path = entry.path();
        let metadata = fs::symlink_metadata(&path).map_err(PrivateRuntimeError::Install)?;
        let file_type = metadata.file_type();
        if file_type.is_symlink() {
            return Err(PrivateRuntimeError::ArchiveVerification);
        }
        if file_type.is_dir() {
            collect_tree_files(root, &path, files)?;
            continue;
        }
        if !file_type.is_file() {
            return Err(PrivateRuntimeError::ArchiveVerification);
        }
        let relative_path = path
            .strip_prefix(root)
            .map_err(|_| PrivateRuntimeError::ArchiveVerification)?;
        if relative_path == Path::new(INSTALL_MARKER_NAME) {
            continue;
        }
        let relative = portable_relative_path(relative_path)?;
        files.push((relative, path, metadata.len()));
    }
    Ok(())
}

fn portable_relative_path(path: &Path) -> Result<String, PrivateRuntimeError> {
    path.components()
        .map(|component| match component {
            Component::Normal(value) => value.to_str().ok_or(PrivateRuntimeError::ArchiveVerification),
            _ => Err(PrivateRuntimeError::ArchiveVerification),
        })
        .collect::<Result<Vec<_>, _>>()
        .map(|parts| parts.join("/"))
}

fn update_tree_header(hasher: &mut Sha256, relative: &str, size: u64) {
    let relative = relative.as_bytes();
    hasher.update((relative.len() as u64).to_be_bytes());
    hasher.update(relative);
    hasher.update(size.to_be_bytes());
}

fn write_marker(root: &Path, manifest: &RuntimeBundleManifest) -> Result<(), PrivateRuntimeError> {
    let bytes = serde_json::to_vec(manifest).map_err(|_| PrivateRuntimeError::ManifestInvalid)?;
    let marker = root.join(INSTALL_MARKER_NAME);
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(marker)
        .map_err(PrivateRuntimeError::Install)?;
    file.write_all(&bytes).map_err(PrivateRuntimeError::Install)?;
    file.sync_all().map_err(PrivateRuntimeError::Install)
}

fn installed_runtime(
    root: &Path,
    manifest: &RuntimeBundleManifest,
) -> Result<InstalledPrivateRuntime, PrivateRuntimeError> {
    let marker = fs::read(root.join(INSTALL_MARKER_NAME)).map_err(PrivateRuntimeError::Install)?;
    let installed: RuntimeBundleManifest =
        serde_json::from_slice(&marker).map_err(|_| PrivateRuntimeError::ArchiveInvalid)?;
    if &installed != manifest {
        return Err(PrivateRuntimeError::ArchiveInvalid);
    }
    validate_runtime_files(root, manifest)?;
    verify_installed_tree(root, manifest)?;
    Ok(InstalledPrivateRuntime {
        root: root.to_owned(),
        python: root.join(&manifest.python_entrypoint),
        node: root.join(&manifest.node_entrypoint),
        codex: root.join(&manifest.codex_entrypoint),
        runtime_id: manifest.archive_sha256.clone(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;
    use zip::write::SimpleFileOptions;
    use zip::{CompressionMethod, ZipWriter};

    fn scratch(label: &str) -> PathBuf {
        let sequence = INSTALL_SEQUENCE.fetch_add(1, Ordering::SeqCst);
        let path = std::env::temp_dir().join(format!(
            "avibe-private-runtime-{label}-{}-{sequence}",
            std::process::id()
        ));
        fs::create_dir_all(&path).expect("scratch directory");
        path
    }

    fn entrypoints() -> (&'static str, &'static str, &'static str) {
        if cfg!(windows) {
            ("python/python.exe", "tools/bin/node.exe", "tools/bin/codex.exe")
        } else {
            ("python/bin/python3", "tools/bin/node", "tools/bin/codex")
        }
    }

    fn write_bundle(root: &Path, extra_entry: Option<&str>) -> RuntimeBundleManifest {
        let bundle = root.join("bundle");
        fs::create_dir_all(&bundle).expect("bundle directory");
        let archive_path = bundle.join("runtime.zip");
        let archive_file = File::create(&archive_path).expect("archive");
        let mut zip = ZipWriter::new(archive_file);
        let options = SimpleFileOptions::default()
            .compression_method(CompressionMethod::Deflated)
            .unix_permissions(0o755);
        let (python, node, codex) = entrypoints();
        let mut unpacked_size = 0_u64;
        let mut entry_count = 0_u64;
        let entries: Vec<_> = [python, node, codex].into_iter().chain(extra_entry).collect();
        for name in &entries {
            zip.start_file(name, options).expect("zip entry");
            zip.write_all(b"runtime").expect("zip bytes");
            unpacked_size += 7;
            entry_count += 1;
        }
        zip.finish().expect("zip finished");

        let mut tree_entries = entries;
        tree_entries.sort_unstable();
        let mut tree_hasher = Sha256::new();
        tree_hasher.update(TREE_HASH_DOMAIN);
        for name in tree_entries {
            update_tree_header(&mut tree_hasher, name, 7);
            tree_hasher.update(b"runtime");
        }
        let bytes = fs::read(&archive_path).expect("archive bytes");
        let manifest = RuntimeBundleManifest {
            schema_version: 1,
            runtime_version: "3.0.0-test".to_owned(),
            os: current_os().to_owned(),
            arch: current_arch().to_owned(),
            archive: "runtime.zip".to_owned(),
            archive_sha256: format!("{:x}", Sha256::digest(&bytes)),
            archive_size: bytes.len() as u64,
            unpacked_size,
            entry_count,
            tree_sha256: format!("{:x}", tree_hasher.finalize()),
            python_entrypoint: python.to_owned(),
            node_entrypoint: node.to_owned(),
            codex_entrypoint: codex.to_owned(),
            python_distribution: RuntimeSource {
                url: "https://example.invalid/python".to_owned(),
                sha256: "a".repeat(64),
            },
            node_distribution: RuntimeSource {
                url: "https://example.invalid/node".to_owned(),
                sha256: "b".repeat(64),
            },
            codex_version: "0.1.0".to_owned(),
            avibe_wheel: RuntimeArtifact {
                name: "avibe_os-3.0.0-py3-none-any.whl".to_owned(),
                sha256: "c".repeat(64),
            },
        };
        fs::write(
            bundle.join(MANIFEST_NAME),
            serde_json::to_vec(&manifest).expect("manifest JSON"),
        )
        .expect("manifest");
        manifest
    }

    #[test]
    fn installs_once_into_a_versioned_content_addressed_directory() {
        let root = scratch("install");
        let manifest = write_bundle(&root, None);
        let bundle = PrivateRuntimeBundle::new(root.join("bundle"), root.join("installs"));

        let first = bundle.prepare().expect("first install");
        let second = bundle.prepare().expect("existing install");

        assert_eq!(first.root, second.root);
        assert!(first.root.ends_with(&manifest.archive_sha256[..16]));
        assert!(first.python.is_file());
        assert!(first.node.is_file());
        assert!(first.codex.is_file());
        assert_eq!(
            fs::read_dir(root.join("installs").join(&manifest.runtime_version))
                .expect("version directory")
                .count(),
            1
        );
        fs::remove_dir_all(root).ok();
    }

    #[test]
    fn rejects_an_archive_that_does_not_match_the_signed_manifest() {
        let root = scratch("digest");
        write_bundle(&root, None);
        let archive = root.join("bundle/runtime.zip");
        fs::OpenOptions::new()
            .append(true)
            .open(&archive)
            .expect("archive")
            .write_all(b"tampered")
            .expect("tamper");
        let bundle = PrivateRuntimeBundle::new(root.join("bundle"), root.join("installs"));

        assert!(matches!(
            bundle.prepare(),
            Err(PrivateRuntimeError::ArchiveVerification)
        ));
        fs::remove_dir_all(root).ok();
    }

    #[test]
    fn a_tampered_install_is_never_reused_and_repairs_once() {
        let root = scratch("installed-tamper");
        let manifest = write_bundle(&root, None);
        let bundle = PrivateRuntimeBundle::new(root.join("bundle"), root.join("installs"));
        let first = bundle.prepare().expect("first install");
        fs::write(&first.python, b"tampered").expect("tamper installed interpreter");

        let repaired = bundle.prepare().expect("repair install");

        assert_ne!(repaired.root, first.root);
        assert!(repaired
            .root
            .ends_with(format!("{}-repair", &manifest.archive_sha256[..16])));
        assert_eq!(fs::read(&repaired.python).expect("repaired interpreter"), b"runtime");
        assert_eq!(bundle.prepare().expect("reuse repair").root, repaired.root);
        fs::remove_dir_all(root).ok();
    }

    #[cfg(unix)]
    #[test]
    fn a_runtime_with_a_non_executable_entrypoint_is_repaired() {
        use std::os::unix::fs::PermissionsExt;

        let root = scratch("installed-mode-tamper");
        let manifest = write_bundle(&root, None);
        let bundle = PrivateRuntimeBundle::new(root.join("bundle"), root.join("installs"));
        let first = bundle.prepare().expect("first install");
        fs::set_permissions(&first.python, fs::Permissions::from_mode(0o644)).expect("remove execute bits");

        let repaired = bundle.prepare().expect("repair install");

        assert_ne!(repaired.root, first.root);
        assert!(repaired
            .root
            .ends_with(format!("{}-repair", &manifest.archive_sha256[..16])));
        assert_ne!(
            fs::metadata(&repaired.python)
                .expect("repaired metadata")
                .permissions()
                .mode()
                & 0o111,
            0
        );
        fs::remove_dir_all(root).ok();
    }

    #[test]
    fn superseded_installs_are_pruned_only_after_an_active_runtime_is_selected() {
        let root = scratch("prune");
        let manifest = write_bundle(&root, None);
        let install_root = root.join("installs");
        let bundle = PrivateRuntimeBundle::new(root.join("bundle"), install_root.clone());
        let active = bundle.prepare().expect("active install");
        let sibling = active
            .root
            .with_file_name(format!("{}-repair", &manifest.archive_sha256[..16]));
        fs::create_dir_all(&sibling).expect("sibling install");
        fs::write(sibling.join("unused"), b"unused").expect("sibling file");
        let old = install_root.join("2.9.0").join("old-digest");
        fs::create_dir_all(&old).expect("old install");
        fs::write(old.join("unused"), b"unused").expect("old file");
        let staging = install_root.join(".install-abandoned");
        fs::create_dir_all(&staging).expect("staging install");

        bundle.prune_superseded(&active.root).expect("prune succeeds");

        assert!(active.root.is_dir());
        assert!(!sibling.exists());
        assert!(!old.exists());
        assert!(!staging.exists());
        fs::remove_dir_all(root).ok();
    }

    #[test]
    fn two_tampered_install_slots_fail_closed() {
        let root = scratch("installed-double-tamper");
        write_bundle(&root, None);
        let bundle = PrivateRuntimeBundle::new(root.join("bundle"), root.join("installs"));
        let primary = bundle.prepare().expect("primary install");
        fs::write(&primary.python, b"tampered").expect("tamper primary");
        let repair = bundle.prepare().expect("repair install");
        fs::write(&repair.node, b"tampered").expect("tamper repair");

        assert!(matches!(
            bundle.prepare(),
            Err(PrivateRuntimeError::ArchiveVerification)
        ));
        fs::remove_dir_all(root).ok();
    }

    #[test]
    fn rejects_path_traversal_before_any_file_can_escape() {
        let root = scratch("traversal");
        write_bundle(&root, Some("../escape"));
        let bundle = PrivateRuntimeBundle::new(root.join("bundle"), root.join("installs"));

        assert!(matches!(bundle.prepare(), Err(PrivateRuntimeError::ArchiveInvalid)));
        assert!(!root.join("escape").exists());
        fs::remove_dir_all(root).ok();
    }

    #[test]
    fn rejects_a_manifest_for_another_architecture() {
        let root = scratch("target");
        let mut manifest = write_bundle(&root, None);
        manifest.arch = if current_arch() == "aarch64" {
            "x86_64".to_owned()
        } else {
            "aarch64".to_owned()
        };
        fs::write(
            root.join("bundle").join(MANIFEST_NAME),
            serde_json::to_vec(&manifest).expect("manifest JSON"),
        )
        .expect("manifest");
        let bundle = PrivateRuntimeBundle::new(root.join("bundle"), root.join("installs"));

        assert!(matches!(bundle.prepare(), Err(PrivateRuntimeError::TargetMismatch)));
        fs::remove_dir_all(root).ok();
    }
}
