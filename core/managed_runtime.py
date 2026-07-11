from __future__ import annotations

import hashlib
import importlib.resources as package_resources
import json
import logging
import os
import platform
import re
import shutil
import stat
import sys
import tarfile
import tempfile
import threading
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from sysconfig import get_platform
from typing import Any

from storage.lock import MigrationFileLock, MigrationLockTimeout


logger = logging.getLogger(__name__)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_INSTALL_LOCKS: dict[str, threading.Lock] = {}
_INSTALL_LOCKS_GUARD = threading.Lock()


@dataclass(frozen=True)
class ManagedRuntimeArchive:
    platform: str
    name: str
    url: str
    sha256: str
    binary_sha256: str
    size: int | None
    bin_path: str


@dataclass(frozen=True)
class ManagedRuntimeManifest:
    schema_version: int
    runtime_version: str
    source: str
    source_url: str | None
    archives: dict[str, ManagedRuntimeArchive]
    digest: str
    loaded_from: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class ManagedRuntimeSpec:
    runtime_id: str
    manifest_resource: str
    version_field: str
    default_bin_path: str
    package: str = "vibe"

    @property
    def metadata_filename(self) -> str:
        return f".avibe-{self.runtime_id}-runtime.json"


class ManagedRuntimeManager:
    """Shared manifest/download/verify/install core for managed runtimes."""

    def __init__(
        self,
        *,
        spec: ManagedRuntimeSpec,
        runtime_dir: Path,
        manifest_path: Path | str | None = None,
        manifest_url: str | None = None,
        offline: bool = False,
    ) -> None:
        self.spec = spec
        self.runtime_dir = runtime_dir
        self.manifest_path = Path(manifest_path).expanduser() if manifest_path else None
        self.manifest_url = manifest_url
        self.offline = offline
        self._install_reason: str | None = None
        self._install_lock = install_lock_for(spec.runtime_id)
        self._install_file_lock_path = self.runtime_dir / ".install.lock"

    def ensure(self, *, force: bool = False) -> dict[str, Any]:
        try:
            file_lock = self._acquire_mutation_lock()
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to acquire managed %s runtime lock", self.spec.runtime_id)
            return self._failure(self._reason("install_lock_failed"), message=str(exc))
        if file_lock is None:
            return self._failure(
                self._reason("install_already_running"),
                message=f"{self.spec.runtime_id} install or repair is already running; try again shortly.",
                skipped=True,
            )
        try:
            manifest = self._load_manifest(allow_network=not self.offline)
            if manifest is None:
                return self._failure(self._install_reason or self._reason("manifest_missing"))
            if not self._manifest_installable(manifest):
                return self._failure(self._install_reason or self._reason("manifest_unavailable"), manifest=manifest)
            archive = self._manifest_archive_for_platform(manifest)
            if archive is None:
                return self._failure(
                    self._install_reason or self._reason("platform_unsupported"),
                    manifest=manifest,
                )

            install_dir = self._manifest_install_dir(manifest, archive)
            existing = self._verified_manifest_binary(install_dir, manifest, archive)
            if existing is not None and not force:
                return self._reuse_existing_install(existing, install_dir, manifest, archive)

            archive_path = self._resolve_manifest_archive(archive)
            if archive_path is None:
                if existing is not None:
                    return self._reuse_existing_install(
                        existing,
                        install_dir,
                        manifest,
                        archive,
                        reason=self._install_reason,
                    )
                return self._failure(
                    self._install_reason or self._reason("archive_unavailable"),
                    manifest=manifest,
                    archive=archive,
                )

            self.runtime_dir.mkdir(parents=True, exist_ok=True)
            staging_dir = Path(tempfile.mkdtemp(prefix="install-", dir=self.runtime_dir))
            try:
                with tarfile.open(archive_path, "r:gz") as archive_file:
                    safe_extract_tar(archive_file, staging_dir)
                staged_binary = staging_dir / archive.bin_path
                if not staged_binary.is_file():
                    return self._failure(
                        self._reason("install_missing_binary"),
                        manifest=manifest,
                        archive=archive,
                    )
                make_executable(staged_binary)
                preparation = self._prepare_binary(staged_binary)
                if not preparation.get("ok"):
                    return self._failure(
                        str(preparation.get("reason") or self._reason("binary_prepare_failed")),
                        manifest=manifest,
                        archive=archive,
                    )
                binary_sha256 = file_sha256(staged_binary)
                if binary_sha256 != archive.binary_sha256:
                    return self._failure(
                        self._reason("binary_checksum_mismatch"),
                        manifest=manifest,
                        archive=archive,
                    )
                if not self._binary_matches_manifest(staged_binary, manifest):
                    return self._failure(
                        self._reason("binary_not_runnable"),
                        manifest=manifest,
                        archive=archive,
                    )

                if install_dir.exists():
                    shutil.rmtree(install_dir)
                install_dir.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(staging_dir), str(install_dir))
                installed_binary = install_dir / archive.bin_path
                self._write_manifest_install_metadata(
                    install_dir,
                    manifest,
                    archive,
                    binary_sha256=binary_sha256,
                )
                self._write_current_pointer(install_dir, manifest, archive)
                self._install_reason = None
                return {
                    **self._success_payload(
                        installed_binary,
                        install_dir,
                        manifest,
                        archive,
                        changed=True,
                    ),
                    "preparation": preparation,
                }
            except Exception as exc:  # noqa: BLE001
                logger.exception("Failed to install managed %s runtime", self.spec.runtime_id)
                return self._failure(
                    self._reason("install_failed"),
                    manifest=manifest,
                    archive=archive,
                    message=str(exc),
                )
            finally:
                if staging_dir.exists():
                    shutil.rmtree(staging_dir, ignore_errors=True)
        finally:
            self._release_mutation_lock(file_lock)

    def resolve_binary(self) -> Path | None:
        """Resolve an already installed runtime without performing network I/O."""

        try:
            manifest = self._load_manifest(allow_network=False)
            if manifest is None or not self._manifest_installable(manifest):
                return None
            archive = self._manifest_archive_for_platform(manifest)
            if archive is None:
                return None
            return self._verified_manifest_binary(
                self._manifest_install_dir(manifest, archive),
                manifest,
                archive,
            )
        except Exception:  # noqa: BLE001
            logger.debug("Failed to resolve managed %s runtime", self.spec.runtime_id, exc_info=True)
            return None

    def status(self) -> dict[str, Any]:
        manifest = self._load_manifest(allow_network=False)
        platform_tag = runtime_platform_tag()
        archive = manifest.archives.get(platform_tag) if manifest else None
        install_dir = self._manifest_install_dir(manifest, archive) if manifest and archive else None
        binary = self.resolve_binary() if manifest and archive else None
        return {
            "id": self.spec.runtime_id,
            "provider": "manifest",
            "platform": platform_tag,
            "installed": binary is not None,
            "version": manifest.runtime_version if manifest else None,
            "status": "ready" if binary else "missing",
            "path": str(binary) if binary else None,
            "install_dir": str(install_dir) if install_dir else None,
            "manifest": self._manifest_status_payload(manifest),
            "archive": self._archive_status_payload(archive),
            "reason": self._install_reason,
        }

    def clean(self, *, keep_previous: int = 1) -> dict[str, Any]:
        try:
            file_lock = self._acquire_mutation_lock()
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to acquire managed %s runtime lock", self.spec.runtime_id)
            return {
                "ok": False,
                "removed": [],
                "reason": self._reason("clean_lock_failed"),
                "message": str(exc),
            }
        if file_lock is None:
            return {
                "ok": False,
                "removed": [],
                "reason": self._reason("install_already_running"),
            }
        try:
            return self._clean_locked(keep_previous=keep_previous)
        finally:
            self._release_mutation_lock(file_lock)

    def _clean_locked(self, *, keep_previous: int) -> dict[str, Any]:
        removed: list[str] = []
        for staging_dir in self.runtime_dir.glob("install-*"):
            if staging_dir.is_dir():
                shutil.rmtree(staging_dir, ignore_errors=True)
                removed.append(str(staging_dir))

        versions_dir = self.runtime_dir / "versions"
        if not versions_dir.is_dir():
            return {"ok": True, "removed": removed}

        install_dirs = {
            metadata_path.parent
            for metadata_path in versions_dir.rglob(self.spec.metadata_filename)
            if metadata_path.parent.is_dir()
        }
        current = self._current_install_dir(versions_dir)
        protected = {current} if current is not None else set()
        candidates = sorted(
            (path for path in install_dirs if path.resolve() not in protected),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for path in candidates[max(0, keep_previous) :]:
            shutil.rmtree(path, ignore_errors=True)
            removed.append(str(path))
        self._prune_empty_version_dirs(versions_dir)
        return {"ok": True, "removed": removed}

    def _manifest_installable(self, manifest: ManagedRuntimeManifest) -> bool:
        return True

    def _prepare_binary(self, binary: Path) -> dict[str, Any]:
        return {"ok": True, "skipped": True}

    def _binary_version(self, binary: Path | None) -> str | None:
        raise NotImplementedError

    def _binary_matches_manifest(self, binary: Path, manifest: ManagedRuntimeManifest) -> bool:
        return self._binary_version(binary) == manifest.runtime_version

    def _load_manifest(self, *, allow_network: bool) -> ManagedRuntimeManifest | None:
        payload: bytes
        loaded_from: str
        cache_remote = False
        if self.manifest_path is not None:
            if not self.manifest_path.is_file():
                self._install_reason = self._reason("manifest_missing")
                return None
            try:
                payload = self.manifest_path.read_bytes()
            except OSError:
                self._install_reason = self._reason("manifest_missing")
                return None
            loaded_from = str(self.manifest_path)
        elif self.manifest_url:
            cached_manifest = self._remote_manifest_cache_path()
            if self.offline or not allow_network:
                if not cached_manifest.is_file():
                    self._install_reason = self._reason("manifest_unavailable_offline")
                    return None
                try:
                    payload = cached_manifest.read_bytes()
                except OSError:
                    self._install_reason = self._reason("manifest_unavailable_offline")
                    return None
                loaded_from = f"cache:{self.manifest_url}"
            else:
                parsed_url = urllib.parse.urlparse(self.manifest_url)
                if parsed_url.scheme not in {"https", "file"}:
                    self._install_reason = self._reason("manifest_url_unsupported")
                    return None
                try:
                    with urllib.request.urlopen(self.manifest_url, timeout=30) as response:
                        payload = response.read()
                except Exception:  # noqa: BLE001
                    logger.exception("Failed to download %s manifest", self.spec.runtime_id)
                    self._install_reason = self._reason("manifest_download_failed")
                    return None
                loaded_from = self.manifest_url
                cache_remote = True
        else:
            try:
                resource = package_resources.files(self.spec.package).joinpath(self.spec.manifest_resource)
            except Exception:  # noqa: BLE001
                resource = None
            if resource is None or not resource.is_file():
                self._install_reason = self._reason("manifest_missing")
                return None
            try:
                payload = resource.read_bytes()
            except OSError:
                self._install_reason = self._reason("manifest_missing")
                return None
            loaded_from = f"package:{self.spec.manifest_resource}"

        manifest = self._parse_manifest(payload, loaded_from=loaded_from)
        if manifest is None:
            return None
        if cache_remote:
            cached_manifest = self._remote_manifest_cache_path()
            try:
                write_bytes_atomic(cached_manifest, payload)
            except OSError:
                logger.warning("Failed to cache %s manifest", self.spec.runtime_id, exc_info=True)
        return manifest

    def _remote_manifest_cache_path(self) -> Path:
        url_digest = hashlib.sha256(str(self.manifest_url).encode("utf-8")).hexdigest()[:16]
        return self.runtime_dir / "downloads" / f"manifest-{url_digest}.json"

    def _parse_manifest(self, payload: bytes, *, loaded_from: str) -> ManagedRuntimeManifest | None:
        try:
            data = json.loads(payload.decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("manifest root must be an object")
            archives: dict[str, ManagedRuntimeArchive] = {}
            for platform_tag, item in (data.get("archives") or {}).items():
                if not isinstance(platform_tag, str) or not isinstance(item, dict):
                    raise ValueError("invalid archive entry")
                name = str(item["name"])
                url = str(item["url"])
                sha256 = str(item["sha256"]).lower()
                binary_sha256 = str(item["binary_sha256"]).lower()
                bin_path = str(item.get("bin_path") or self.spec.default_bin_path)
                size = int(item["size"]) if item.get("size") is not None else None
                if Path(name).name != name or not name:
                    raise ValueError("unsafe archive name")
                if not _SHA256_RE.fullmatch(sha256):
                    raise ValueError("invalid archive sha256")
                if not _SHA256_RE.fullmatch(binary_sha256):
                    raise ValueError("invalid binary sha256")
                if size is not None and size < 0:
                    raise ValueError("invalid archive size")
                if archive_path_is_unsafe(bin_path):
                    raise ValueError("unsafe binary path")
                archives[platform_tag] = ManagedRuntimeArchive(
                    platform=platform_tag,
                    name=name,
                    url=url,
                    sha256=sha256,
                    binary_sha256=binary_sha256,
                    size=size,
                    bin_path=bin_path,
                )
            manifest = ManagedRuntimeManifest(
                schema_version=int(data.get("schema_version")),
                runtime_version=str(data.get(self.spec.version_field) or ""),
                source=str(data.get("source") or ""),
                source_url=str(data.get("source_url") or "") or None,
                archives=archives,
                digest=hashlib.sha256(payload).hexdigest(),
                loaded_from=loaded_from,
                payload=data,
            )
        except Exception:  # noqa: BLE001
            self._install_reason = self._reason("manifest_invalid")
            return None
        if manifest.schema_version != 1 or not manifest.runtime_version or not manifest.archives:
            self._install_reason = self._reason("manifest_invalid")
            return None
        self._install_reason = None
        return manifest

    def _manifest_archive_for_platform(
        self,
        manifest: ManagedRuntimeManifest,
    ) -> ManagedRuntimeArchive | None:
        archive = manifest.archives.get(runtime_platform_tag())
        if archive is None:
            self._install_reason = self._reason("platform_unsupported")
        return archive

    def _resolve_manifest_archive(self, archive: ManagedRuntimeArchive) -> Path | None:
        cached = self.runtime_dir / "downloads" / archive.name
        if cached.is_file() and self._downloaded_archive_matches(cached, archive):
            return cached
        if self.offline:
            self._install_reason = self._reason("archive_unavailable_offline")
            return None

        parsed = urllib.parse.urlparse(archive.url)
        if parsed.scheme not in {"https", "file"}:
            self._install_reason = self._reason("archive_url_unsupported")
            return None
        cached.parent.mkdir(parents=True, exist_ok=True)
        temporary = cached.with_suffix(cached.suffix + ".tmp")
        try:
            with urllib.request.urlopen(archive.url, timeout=60) as response, temporary.open("wb") as destination:
                shutil.copyfileobj(response, destination)
            if not self._downloaded_archive_matches(temporary, archive):
                temporary.unlink(missing_ok=True)
                return None
            temporary.replace(cached)
            self._install_reason = None
            return cached
        except Exception:  # noqa: BLE001
            logger.exception("Failed to download %s archive", self.spec.runtime_id)
            temporary.unlink(missing_ok=True)
            self._install_reason = self._reason("archive_download_failed")
            return None

    def _downloaded_archive_matches(self, path: Path, archive: ManagedRuntimeArchive) -> bool:
        if archive.size is not None and path.stat().st_size != archive.size:
            self._install_reason = self._reason("archive_size_mismatch")
            return False
        if file_sha256(path) != archive.sha256:
            self._install_reason = self._reason("archive_checksum_mismatch")
            return False
        return True

    def _manifest_install_dir(
        self,
        manifest: ManagedRuntimeManifest,
        archive: ManagedRuntimeArchive,
    ) -> Path:
        fingerprint = hashlib.sha256(f"{manifest.digest}:{archive.sha256}".encode()).hexdigest()[:16]
        return (
            self.runtime_dir
            / "versions"
            / safe_path_part(manifest.runtime_version)
            / safe_path_part(archive.platform)
            / fingerprint
        )

    def _verified_manifest_binary(
        self,
        install_dir: Path,
        manifest: ManagedRuntimeManifest,
        archive: ManagedRuntimeArchive,
    ) -> Path | None:
        binary = install_dir / archive.bin_path
        if not binary.is_file() or not os.access(binary, os.X_OK):
            return None
        try:
            metadata = json.loads((install_dir / self.spec.metadata_filename).read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return None
        if not (
            metadata.get("provider") == "manifest"
            and metadata.get("runtime_id") == self.spec.runtime_id
            and metadata.get("manifest_sha256") == manifest.digest
            and metadata.get("runtime_version") == manifest.runtime_version
            and metadata.get("platform") == archive.platform
            and metadata.get("archive_sha256") == archive.sha256
            and metadata.get("bin_path") == archive.bin_path
            and metadata.get("binary_sha256") == archive.binary_sha256
            and file_sha256(binary) == archive.binary_sha256
        ):
            return None
        return binary

    def _write_manifest_install_metadata(
        self,
        install_dir: Path,
        manifest: ManagedRuntimeManifest,
        archive: ManagedRuntimeArchive,
        *,
        binary_sha256: str,
    ) -> None:
        write_json_atomic(
            install_dir / self.spec.metadata_filename,
            {
                "provider": "manifest",
                "runtime_id": self.spec.runtime_id,
                "manifest_sha256": manifest.digest,
                "runtime_version": manifest.runtime_version,
                "platform": archive.platform,
                "archive_name": archive.name,
                "archive_sha256": archive.sha256,
                "binary_sha256": binary_sha256,
                "bin_path": archive.bin_path,
                "manifest_source": manifest.loaded_from,
                "source": manifest.source,
            },
        )

    def _write_current_pointer(
        self,
        install_dir: Path,
        manifest: ManagedRuntimeManifest,
        archive: ManagedRuntimeArchive,
    ) -> None:
        write_json_atomic(
            self.runtime_dir / "current.json",
            {
                "provider": "manifest",
                "runtime_id": self.spec.runtime_id,
                "runtime_version": manifest.runtime_version,
                "platform": archive.platform,
                "install_dir": str(install_dir),
                "manifest_sha256": manifest.digest,
                "archive_sha256": archive.sha256,
                "bin_path": archive.bin_path,
            },
        )

    def _current_install_dir(self, versions_dir: Path) -> Path | None:
        try:
            pointer = json.loads((self.runtime_dir / "current.json").read_text(encoding="utf-8"))
            candidate = Path(str(pointer.get("install_dir") or "")).resolve()
            if versions_dir.resolve() in candidate.parents:
                return candidate
        except Exception:  # noqa: BLE001
            return None
        return None

    def _prune_empty_version_dirs(self, versions_dir: Path) -> None:
        for depth in (3, 2, 1):
            for path in sorted(versions_dir.glob("/".join("*" for _ in range(depth))), reverse=True):
                if path.is_dir() and not any(path.iterdir()):
                    path.rmdir()

    def _manifest_status_payload(self, manifest: ManagedRuntimeManifest | None) -> dict[str, Any] | None:
        if manifest is None:
            return None
        return {
            "schema_version": manifest.schema_version,
            self.spec.version_field: manifest.runtime_version,
            "source": manifest.source,
            "source_url": manifest.source_url,
            "sha256": manifest.digest,
            "loaded_from": manifest.loaded_from,
            "release_state": manifest.payload.get("release_state"),
        }

    @staticmethod
    def _archive_status_payload(archive: ManagedRuntimeArchive | None) -> dict[str, Any] | None:
        if archive is None:
            return None
        return {
            "platform": archive.platform,
            "name": archive.name,
            "url": archive.url,
            "sha256": archive.sha256,
            "binary_sha256": archive.binary_sha256,
            "size": archive.size,
            "bin_path": archive.bin_path,
        }

    def _reuse_existing_install(
        self,
        binary: Path,
        install_dir: Path,
        manifest: ManagedRuntimeManifest,
        archive: ManagedRuntimeArchive,
        *,
        reason: str | None = None,
    ) -> dict[str, Any]:
        try:
            self._write_current_pointer(install_dir, manifest, archive)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to refresh managed %s runtime pointer", self.spec.runtime_id)
            return self._failure(
                self._reason("pointer_write_failed"),
                manifest=manifest,
                archive=archive,
                message=str(exc),
            )
        payload = self._success_payload(binary, install_dir, manifest, archive, changed=False)
        if reason:
            payload["reason"] = reason
        return payload

    def _success_payload(
        self,
        binary: Path,
        install_dir: Path,
        manifest: ManagedRuntimeManifest,
        archive: ManagedRuntimeArchive,
        *,
        changed: bool,
    ) -> dict[str, Any]:
        return {
            "ok": True,
            "installed": True,
            "changed": changed,
            "path": str(binary),
            "version": manifest.runtime_version,
            "platform": archive.platform,
            "install_dir": str(install_dir),
        }

    def _failure(
        self,
        reason: str,
        *,
        manifest: ManagedRuntimeManifest | None = None,
        archive: ManagedRuntimeArchive | None = None,
        message: str | None = None,
        skipped: bool = False,
    ) -> dict[str, Any]:
        self._install_reason = reason
        return {
            "ok": False,
            "installed": False,
            "changed": False,
            "skipped": skipped,
            "reason": reason,
            "message": message or reason,
            "version": manifest.runtime_version if manifest else None,
            "platform": archive.platform if archive else runtime_platform_tag(),
            "path": None,
        }

    def _reason(self, suffix: str) -> str:
        return f"{self.spec.runtime_id}_{suffix}"

    def _acquire_mutation_lock(self) -> MigrationFileLock | None:
        if not self._install_lock.acquire(blocking=False):
            return None
        try:
            file_lock = MigrationFileLock(self._install_file_lock_path, timeout_seconds=0)
            file_lock.acquire()
        except MigrationLockTimeout:
            self._install_lock.release()
            return None
        except Exception:
            self._install_lock.release()
            raise
        return file_lock

    def _release_mutation_lock(self, file_lock: MigrationFileLock) -> None:
        try:
            file_lock.release()
        finally:
            self._install_lock.release()


def runtime_platform_tag() -> str:
    raw = get_platform().lower()
    machine = raw.rsplit("-", 1)[-1]
    if machine == "universal2":
        machine = platform.machine().lower()
    if machine in {"amd64", "x86_64"}:
        arch = "x64"
    elif machine in {"arm64", "aarch64"}:
        arch = "arm64"
    else:
        arch = machine
    if raw.startswith("macosx"):
        os_name = "darwin"
    elif raw.startswith("linux"):
        os_name = "linux"
    elif raw.startswith("win"):
        os_name = "win32"
    else:
        os_name = os.name
    return f"{os_name}-{arch}"


def install_lock_for(runtime_id: str) -> threading.Lock:
    with _INSTALL_LOCKS_GUARD:
        return _INSTALL_LOCKS.setdefault(runtime_id, threading.Lock())


def safe_extract_tar(archive: tarfile.TarFile, destination: Path) -> None:
    destination_resolved = destination.resolve()
    for member in archive.getmembers():
        if not (member.isfile() or member.isdir()):
            raise ValueError(f"Unsupported managed runtime archive member: {member.name}")
        if archive_path_is_unsafe(member.name):
            raise ValueError(f"Unsafe managed runtime archive path: {member.name}")
        target = (destination / member.name).resolve()
        if target != destination_resolved and destination_resolved not in target.parents:
            raise ValueError(f"Unsafe managed runtime archive path: {member.name}")
    if sys.version_info >= (3, 12):
        archive.extractall(destination, filter="data")
    else:
        archive.extractall(destination)


def archive_path_is_unsafe(value: str) -> bool:
    if not value:
        return True
    posix_path = PurePosixPath(value)
    windows_path = PureWindowsPath(value)
    if posix_path.is_absolute() or windows_path.is_absolute():
        return True
    if windows_path.drive or windows_path.root:
        return True
    return ".." in posix_path.parts or ".." in windows_path.parts


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def make_executable(path: Path) -> None:
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def safe_path_part(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "-" for ch in value.strip())
    return cleaned.strip(".-") or "unknown"


def env_flag_enabled(name: str, *, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def write_bytes_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(payload)
    temporary.replace(path)


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)
