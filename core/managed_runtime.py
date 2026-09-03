from __future__ import annotations

import contextlib
import hashlib
import importlib.resources as package_resources
import json
import logging
import os
import platform
import re
import shutil
import stat
import tarfile
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from sysconfig import get_platform
from typing import IO, Any, Iterator

from config.atomic_io import write_atomic
from core.dependency_network import (
    dependency_error_details,
    dependency_error_message,
    fetch_bytes,
    fetch_to_path,
    probe_url,
    redact_url,
)
from storage.lock import (
    MigrationFileLock,
    MigrationLockTimeout,
    fcntl_available,
    try_windows_exclusive_lock,
    unlock_windows_exclusive_lock,
)


logger = logging.getLogger(__name__)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MANIFEST_CACHE_RE = re.compile(r"^manifest-[0-9a-f]{16}\.json$")
_ARCHIVE_MTIME_GUARD_SECONDS = 15 * 60
_ARCHIVE_INSPECTION_FAILED = "archive_inspection_failed"
_ARCHIVE_REMOVAL_FAILED = "archive_removal_failed"
_ARCHIVE_PROVENANCE_FILENAME = "archive-provenance.json"
_ARCHIVE_PROVENANCE_SCHEMA_VERSION = 1
_PACKAGED_MANIFEST_LINEAGE = "packaged"
_CUSTOM_MANIFEST_LINEAGE = "custom"
_INSTALL_LOCKS: dict[str, threading.Lock] = {}
_INSTALL_LOCKS_GUARD = threading.Lock()
_ENSURE_FAILURE_SUFFIXES = frozenset(
    {
        "archive_checksum_mismatch",
        "archive_download_failed",
        "archive_size_mismatch",
        "archive_unavailable",
        "archive_unavailable_offline",
        "archive_url_unsupported",
        "binary_checksum_mismatch",
        "binary_not_runnable",
        "binary_prepare_failed",
        "candidate_validation_failed",
        "install_already_running",
        "install_claim_failed",
        "install_failed",
        "install_lock_failed",
        "install_missing_binary",
        "install_target_changed",
        "manifest_download_failed",
        "manifest_invalid",
        "manifest_missing",
        "manifest_unavailable",
        "manifest_unavailable_offline",
        "manifest_url_unsupported",
        "platform_unsupported",
        "pointer_write_failed",
    }
)


def _is_exclusive_regular_file(info: os.stat_result) -> bool:
    """True only for a one-link regular file that is not a reparse point."""

    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        return False
    attrs = getattr(info, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return not (reparse and attrs & reparse)


def _is_reparse_point(info: os.stat_result) -> bool:
    attrs = getattr(info, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(reparse and attrs & reparse)


@dataclass(frozen=True)
class ManagedRuntimeArchive:
    platform: str
    name: str
    url: str
    sha256: str
    binary_sha256: str | None
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
    archives_field: str = "archives"
    archive_size_field: str = "size"
    platform_aliases: tuple[tuple[str, str], ...] = ()
    binary_artifact: bool = True
    allow_missing_binary_sha256: bool = False
    record_provider: str = "manifest"
    metadata_filename_override: str | None = None
    allow_legacy_missing_runtime_id: bool = False
    staging_prefixes: tuple[str, ...] = ("install-",)
    replace_target_on_force: bool = False
    replace_invalid_target_on_repair: bool = False
    include_manifest_digest_in_install_fingerprint: bool = False

    @property
    def metadata_filename(self) -> str:
        return self.metadata_filename_override or f".avibe-{self.runtime_id}-runtime.json"


@dataclass(frozen=True)
class _ManagedRuntimeInstall:
    path: Path
    lineage: str
    archive_name: str
    archive_sha256: str
    mtime: float


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
        self.runtime_dir = runtime_dir.expanduser().absolute()
        self.manifest_path = Path(manifest_path).expanduser() if manifest_path else None
        self.manifest_url = manifest_url
        self.offline = offline
        self._install_reason: str | None = None
        self._download_error: dict[str, Any] | None = None
        self._install_lock = install_lock_for(spec.runtime_id)
        self._install_file_lock_path = self.runtime_dir / ".install.lock"
        self._archive_provenance_path = self.runtime_dir / _ARCHIVE_PROVENANCE_FILENAME

    def ensure(
        self,
        *,
        force: bool = False,
        expected_target: Mapping[str, str] | None = None,
        on_resolved: Callable[[dict[str, str]], None] | None = None,
        validate_candidate: Callable[[Path], str | None] | None = None,
    ) -> dict[str, Any]:
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
        published_result: dict[str, Any] | None = None
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
            target = self._install_target_identity(manifest, archive)
            if (
                expected_target is not None
                and self._normalized_install_target(expected_target)
                != self._normalized_install_target(target)
            ):
                return self._failure(
                    self._reason("install_target_changed"),
                    manifest=manifest,
                    archive=archive,
                )
            if on_resolved is not None:
                try:
                    on_resolved(target)
                except Exception as exc:  # noqa: BLE001
                    logger.exception(
                        "Failed to persist managed %s runtime install claim",
                        self.spec.runtime_id,
                    )
                    return self._failure(
                        self._reason("install_claim_failed"),
                        manifest=manifest,
                        archive=archive,
                        message=str(exc),
                    )

            install_dir = self._manifest_install_dir(manifest, archive)
            try:
                current_install_dir = self._current_install_dir(self.runtime_dir / "versions")
            except OSError:
                # Ensure can repair a damaged pointer from the selected
                # manifest's admitted disk record. Cleanup cannot make the
                # same assumption because it has no replacement transaction.
                current_install_dir = None
            candidate_install_dirs: list[Path] = []
            if current_install_dir is not None:
                candidate_install_dirs.append(current_install_dir)
            candidate_install_dirs.extend(self._manifest_install_candidates(manifest, archive))
            unique_install_dirs = list(dict.fromkeys(candidate_install_dirs))
            existing_install_dir = unique_install_dirs[0]
            existing: Path | None = None
            canonical_target_was_rejected = False
            for candidate_install_dir in unique_install_dirs:
                candidate = self._verified_manifest_binary(candidate_install_dir, manifest, archive)
                if candidate_install_dir == install_dir and candidate is None:
                    canonical_target_was_rejected = True
                if candidate is not None:
                    existing_install_dir = candidate_install_dir
                    existing = candidate
                    break
            if existing is not None and not force:
                try:
                    validation_reason = validate_candidate(existing) if validate_candidate else None
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "Managed %s runtime candidate validation failed",
                        self.spec.runtime_id,
                        exc_info=True,
                    )
                    validation_reason = self._reason("candidate_validation_failed")
                if validation_reason:
                    return self._failure(
                        validation_reason,
                        manifest=manifest,
                        archive=archive,
                    )
                return self._reuse_existing_install(
                    existing,
                    existing_install_dir,
                    manifest,
                    archive,
                )

            archive_path = self._resolve_manifest_archive(archive)
            if archive_path is None:
                if existing is not None:
                    return self._reuse_existing_install(
                        existing,
                        existing_install_dir,
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
            candidate_install_dir: Path | None = None
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
                if self.spec.binary_artifact:
                    make_executable(staged_binary)
                preparation = self._prepare_binary_for_manifest(staged_binary, manifest)
                if not preparation.get("ok"):
                    return self._failure(
                        str(preparation.get("reason") or self._reason("binary_prepare_failed")),
                        manifest=manifest,
                        archive=archive,
                    )
                binary_sha256 = file_sha256(staged_binary) if self.spec.binary_artifact else None
                if (
                    self.spec.binary_artifact
                    and archive.binary_sha256 is not None
                    and binary_sha256 != archive.binary_sha256
                ):
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

                install_dir.parent.mkdir(parents=True, exist_ok=True)
                if install_dir.exists():
                    replace_target = (
                        force and self.spec.replace_target_on_force
                    ) or (
                        canonical_target_was_rejected
                        and self.spec.replace_invalid_target_on_repair
                    )
                    if replace_target:
                        if not self._remove_install_target_for_replacement(install_dir):
                            return self._failure(
                                self._reason("install_failed"),
                                manifest=manifest,
                                archive=archive,
                            )
                    else:
                        replacement = Path(
                            tempfile.mkdtemp(prefix=f"{install_dir.name}-", dir=install_dir.parent)
                        )
                        replacement.rmdir()
                        install_dir = replacement
                shutil.move(str(staging_dir), str(install_dir))
                candidate_install_dir = install_dir
                installed_binary = (install_dir / archive.bin_path).resolve(strict=True)
                self._write_manifest_install_metadata(
                    install_dir,
                    manifest,
                    archive,
                    binary_sha256=binary_sha256,
                )
                try:
                    validation_reason = (
                        validate_candidate(installed_binary)
                        if validate_candidate is not None
                        else None
                    )
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "Managed %s runtime candidate validation failed",
                        self.spec.runtime_id,
                        exc_info=True,
                    )
                    validation_reason = self._reason("candidate_validation_failed")
                if validation_reason:
                    return self._failure(
                        validation_reason,
                        manifest=manifest,
                        archive=archive,
                    )
                self._write_current_pointer(install_dir, manifest, archive)
                candidate_install_dir = None
                self._install_reason = None
                published_result = {
                    **self._success_payload(
                        installed_binary,
                        install_dir,
                        manifest,
                        archive,
                        changed=True,
                    ),
                    "preparation": preparation,
                }
                return published_result
            except Exception as exc:  # noqa: BLE001
                if candidate_install_dir is not None:
                    shutil.rmtree(candidate_install_dir, ignore_errors=True)
                logger.exception("Failed to install managed %s runtime", self.spec.runtime_id)
                return self._failure_for_install_exception(
                    exc,
                    manifest=manifest,
                    archive=archive,
                )
            finally:
                if candidate_install_dir is not None:
                    shutil.rmtree(candidate_install_dir, ignore_errors=True)
                if staging_dir.exists():
                    shutil.rmtree(staging_dir, ignore_errors=True)
        finally:
            self._release_mutation_lock(file_lock)
            if published_result is not None and published_result.get("ok"):
                try:
                    self._clean_after_successful_install()
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "Managed %s runtime post-install cleanup failed",
                        self.spec.runtime_id,
                        exc_info=True,
                    )

    def _clean_after_successful_install(self) -> None:
        result = self.clean(keep_previous=1)
        if not result.get("ok"):
            logger.warning(
                "Managed %s runtime post-install cleanup was incomplete: %s",
                self.spec.runtime_id,
                result.get("reason"),
            )

    def _failure_for_install_exception(
        self,
        error: Exception,
        *,
        manifest: ManagedRuntimeManifest,
        archive: ManagedRuntimeArchive,
    ) -> dict[str, Any]:
        """Map a failed activation while preserving subclass classifications."""

        return self._failure(
            self._reason("install_failed"),
            manifest=manifest,
            archive=archive,
            message=str(error),
        )

    def resolve_binary(self) -> Path | None:
        """Resolve an already installed runtime without performing network I/O."""

        inspection_reason = f"{self.spec.runtime_id}_install_inspection_failed"
        try:
            pointer = json.loads((self.runtime_dir / "current.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, RecursionError, UnicodeError, ValueError):
            self._install_reason = inspection_reason
            return None

        self._install_reason = inspection_reason
        try:
            if not isinstance(pointer, dict):
                return None
            runtime_version = pointer.get("runtime_version")
            platform_tag = pointer.get("platform")
            install_dir_value = pointer.get("install_dir")
            bin_path = pointer.get("bin_path", self.spec.default_bin_path)
            digest_fields = ("manifest_sha256", "archive_sha256")
            aliases = dict(self.spec.platform_aliases)
            host_platform = aliases.get(runtime_platform_tag(), runtime_platform_tag())
            installed_platform = aliases.get(platform_tag, platform_tag) if isinstance(platform_tag, str) else None
            if (
                not self._record_provider_matches(pointer.get("provider"))
                or not self._record_runtime_id_matches(pointer.get("runtime_id"))
                or not _safe_metadata_value(runtime_version)
                or not _safe_metadata_value(platform_tag)
                or installed_platform != host_platform
                or not isinstance(install_dir_value, str)
                or not isinstance(bin_path, str)
                or archive_path_is_unsafe(bin_path)
                or any(
                    not _SHA256_RE.fullmatch(str(pointer.get(field) or ""))
                    for field in digest_fields
                )
            ):
                return None

            configured_install_dir = Path(install_dir_value)
            if not configured_install_dir.is_absolute():
                return None
            install_dir = configured_install_dir.resolve(strict=True)
            versions_dir = (self.runtime_dir / "versions").resolve(strict=True)
            binary = (install_dir / bin_path).resolve(strict=True)
            if (
                install_dir == versions_dir
                or versions_dir not in install_dir.parents
                or install_dir not in binary.parents
            ):
                return None

            metadata = json.loads((install_dir / self.spec.metadata_filename).read_text(encoding="utf-8"))
            if not isinstance(metadata, dict):
                return None
            metadata_platform = metadata.get("platform")
            metadata_bin_path = metadata.get("bin_path", self.spec.default_bin_path)
            binary_sha256 = metadata.get("binary_sha256")
            binary_integrity_valid = (
                (
                    isinstance(binary_sha256, str)
                    and bool(_SHA256_RE.fullmatch(binary_sha256))
                )
                or (
                    binary_sha256 is None
                    and self.spec.allow_missing_binary_sha256
                    and self.spec.allow_legacy_missing_runtime_id
                    and metadata.get("runtime_id") is None
                )
                if self.spec.binary_artifact
                else (
                    binary_sha256 is None
                    or isinstance(binary_sha256, str)
                    and bool(_SHA256_RE.fullmatch(binary_sha256))
                )
            )
            if not (
                self._record_provider_matches(metadata.get("provider"))
                and self._record_runtime_id_matches(metadata.get("runtime_id"))
                and metadata.get("runtime_version") == runtime_version
                and isinstance(metadata_platform, str)
                and aliases.get(metadata_platform, metadata_platform) == installed_platform
                and all(metadata.get(field) == pointer.get(field) for field in digest_fields)
                and metadata_bin_path == bin_path
                and binary_integrity_valid
                and binary.is_file()
                and (not self.spec.binary_artifact or os.access(binary, os.X_OK))
                and self._record_matches_configured_source(metadata)
                and self._record_install_dir_matches(install_dir, metadata)
            ):
                return None

            manifest = self._load_manifest(allow_network=False)
            selected_is_installed = False
            if manifest is not None and self._manifest_installable(manifest):
                archive = self._manifest_archive_for_platform(manifest)
                selected_is_installed = archive is not None and (
                    runtime_version,
                    installed_platform,
                    pointer.get(digest_fields[1]),
                ) == (
                    manifest.runtime_version,
                    aliases.get(archive.platform, archive.platform),
                    archive.sha256,
                )
            if selected_is_installed:
                if self._verified_manifest_binary(install_dir, manifest, archive) != binary:
                    self._install_reason = inspection_reason
                    return None
            elif self.spec.binary_artifact and file_sha256(binary) != binary_sha256:
                self._install_reason = inspection_reason
                return None
            self._install_reason = None
            return binary
        except (OSError, RecursionError, RuntimeError, UnicodeError, ValueError):
            self._install_reason = inspection_reason
            logger.debug("Failed to resolve managed %s runtime", self.spec.runtime_id, exc_info=True)
            return None

    def status(self) -> dict[str, Any]:
        manifest = self._load_manifest(allow_network=False)
        archive = self._manifest_archive_for_platform(manifest) if manifest else None
        pointer_path = self.runtime_dir / "current.json"
        pointer: dict[str, Any] = {}
        binary: Path | None = None
        for _attempt in range(2):
            try:
                before = json.loads(pointer_path.read_text(encoding="utf-8"))
            except (OSError, RecursionError, UnicodeError, ValueError):
                before = {}
            if not isinstance(before, dict):
                before = {}
            binary = self.resolve_binary()
            try:
                after = json.loads(pointer_path.read_text(encoding="utf-8"))
            except (OSError, RecursionError, UnicodeError, ValueError):
                after = {}
            if not isinstance(after, dict):
                after = {}
            if before == after:
                pointer = before
                break
        else:
            binary = None
            self._install_reason = self._reason("install_inspection_failed")
        matches_manifest = False if binary is not None and manifest and archive else None
        if matches_manifest is not None and isinstance(pointer.get("install_dir"), str):
            with contextlib.suppress(Exception):  # noqa: BLE001
                matches_manifest = self._verified_manifest_binary(Path(pointer["install_dir"]), manifest, archive) == binary
        return {
            "id": self.spec.runtime_id,
            "provider": self.spec.record_provider,
            "platform": runtime_platform_tag(),
            "installed": binary is not None,
            "version": pointer.get("runtime_version") if binary is not None else None,
            "selected_version": manifest.runtime_version if manifest else None,
            "matches_manifest": matches_manifest,
            "status": "ready" if binary else "error" if str(self._install_reason or "").endswith("install_inspection_failed") else "missing",
            "path": str(binary) if binary else None,
            "install_dir": pointer.get("install_dir") if binary is not None else None,
            "manifest": self._manifest_status_payload(manifest),
            "archive": self._archive_status_payload(archive),
            "reason": self._install_reason if binary is None else None,
            "download_error": self._download_error,
        }

    def probe_archive_reachability(self, *, timeout: float = 10.0) -> dict[str, Any]:
        manifest = self.load_manifest_for_diagnostics()
        if manifest is None:
            return {
                "ok": False,
                "checked": bool(self._download_error),
                "reason": self._install_reason or self._reason("manifest_missing"),
                "download_error": self._download_error,
            }
        archive = self._manifest_archive_for_platform(manifest)
        if archive is None:
            return {"ok": False, "checked": False, "reason": self._install_reason}
        parsed = urllib.parse.urlparse(archive.url)
        if parsed.scheme not in {"https", "file"}:
            return {
                "ok": False,
                "checked": False,
                "reason": self._reason("archive_url_unsupported"),
                "url": redact_url(archive.url),
            }
        return probe_url(
            archive.url,
            timeout=timeout,
            opener=urllib.request.urlopen,
            user_agent=f"avibe-{self.spec.runtime_id}-doctor",
        )

    def clean(self, *, keep_previous: int = 1, dry_run: bool = False) -> dict[str, Any]:
        if dry_run:
            # Read-only preview: no lock acquisition (the file lock would create
            # ``.install.lock`` and mutate persistent state), so previews also
            # work on read-only runtime directories. Busy checks exclude both
            # same-process staging (in-process lock) and cross-process staging
            # (read-only existence probe of the lock file) — a preview must not
            # advertise removing live staging state. The probe stays held
            # through candidate planning so an install cannot start between
            # the check and the ``install-*`` enumeration.
            with self._preview_guard() as busy_reason:
                if busy_reason:
                    return {
                        "ok": False,
                        "removed": [],
                        "reason": busy_reason,
                        "message": (
                            "an install is currently running"
                            if busy_reason == self._reason("install_already_running")
                            else "the install guard could not be inspected"
                        ),
                    }
                try:
                    result = self._clean_locked(keep_previous=keep_previous, dry_run=True, removed=[])
                    raced_reason = self._preview_raced_busy()
                    if raced_reason:
                        return {
                            "ok": False,
                            "removed": [],
                            "reason": raced_reason,
                            "message": (
                                "an install is currently running"
                                if raced_reason == self._reason("install_already_running")
                                else "the install guard could not be inspected"
                            ),
                        }
                    return result
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "Managed %s runtime dry-run inspection failed",
                        self.spec.runtime_id,
                        exc_info=True,
                    )
                    return {
                        "ok": False,
                        "removed": [],
                        "reason": self._reason("clean_inspection_failed"),
                        "message": str(exc),
                        "archives": self._skipped_archive_report(_ARCHIVE_INSPECTION_FAILED),
                    }
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
        removed: list[str] = []
        try:
            return self._clean_locked(keep_previous=keep_previous, dry_run=dry_run, removed=removed)
        except Exception as cop:  # noqa: BLE001
            # Real cleanups hit the same traversal errors dry runs do; return
            # the structured inspection failure instead of raising through
            # _clean_git_runtime into a reasonless result. Staging removals
            # that already happened stay in the result.
            logger.exception("Managed %s runtime cleanup failed", self.spec.runtime_id)
            return {
                "ok": False,
                "removed": list(removed),
                "reason": self._reason("clean_inspection_failed"),
                "message": str(cop),
                "archives": self._skipped_archive_report(_ARCHIVE_INSPECTION_FAILED),
            }
        finally:
            self._release_mutation_lock(file_lock)

    @contextlib.contextmanager
    def _preview_guard(self):
        """Hold the read-only busy probe through preview candidate planning."""
        busy = self._preview_busy_reason()
        try:
            yield busy
        finally:
            self._release_preview_guard()

    def _release_preview_guard(self) -> None:
        fd = getattr(self, "_preview_guard_fd", None)
        if fd is not None:
            if getattr(self, "_preview_guard_msvcrt", False):
                unlock_windows_exclusive_lock(fd)
                self._preview_guard_msvcrt = False
            try:
                os.close(fd)
            except OSError:
                pass
            self._preview_guard_fd = None
        if getattr(self, "_preview_held_install_lock", False):
            self._preview_held_install_lock = False
            try:
                self._install_lock.release()
            except RuntimeError:
                pass

    def _guard_path_matches_fd(self, fd: int) -> bool:
        """True when the live path still names the locked descriptor."""
        try:
            open_stat = os.fstat(fd)
            path_stat = self._install_file_lock_path.lstat()
        except OSError:
            return False
        return (
            _is_exclusive_regular_file(open_stat)
            and _is_exclusive_regular_file(path_stat)
            and (open_stat.st_dev, open_stat.st_ino) == (path_stat.st_dev, path_stat.st_ino)
        )

    def _windows_preview_busy_reason(self) -> str | None:
        """Read-only Windows busy probe covering the pre-staging interval."""
        probe = self._preview_lock_probe()
        if probe is not None:
            return probe
        if getattr(self, "_preview_lock_was_absent", False):
            return None
        try:
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
            fd = os.open(self._install_file_lock_path, flags)
        except OSError:
            return self._reason("clean_inspection_failed")
        try:
            if not try_windows_exclusive_lock(fd):
                os.close(fd)
                return self._reason("install_already_running")
            if not self._guard_path_matches_fd(fd):
                os.close(fd)
                return self._reason("clean_inspection_failed")
            self._preview_guard_fd = fd
            self._preview_guard_msvcrt = True
            return None
        except OSError:
            os.close(fd)
            return self._reason("clean_inspection_failed")

    def _preview_busy_reason(self) -> str | None:
        """Read-only busy check for previews: never creates or rewrites files.

        On POSIX success the shared flock stays held (stored on the instance)
        until ``_release_preview_guard`` so an exclusive installer cannot
        start between the probe and staging enumeration. Native Windows uses
        a non-blocking ``msvcrt.locking`` on an existing lock file instead.
        After either acquisition, the live path is rechecked against the
        descriptor so a same-user swap cannot leave the preview on an
        orphaned inode.
        """
        if not self._install_lock.acquire(blocking=False):
            return self._reason("install_already_running")
        self._preview_held_install_lock = True
        try:
            if not fcntl_available():
                return self._windows_preview_busy_reason()
            import fcntl

            probe = self._preview_lock_probe()
            if probe is not None:
                return probe
            if getattr(self, "_preview_lock_was_absent", False):
                return None
            try:
                flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
                fd = os.open(self._install_file_lock_path, flags)
            except OSError:
                return self._reason("clean_inspection_failed")
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                if not self._guard_path_matches_fd(fd):
                    os.close(fd)
                    return self._reason("clean_inspection_failed")
                self._preview_guard_fd = fd
                return None
            except BlockingIOError:
                os.close(fd)
                return self._reason("install_already_running")
            except OSError:
                os.close(fd)
                return self._reason("clean_inspection_failed")
        except BaseException:
            self._release_preview_guard()
            raise

    def _preview_lock_probe(self) -> str | None:
        try:
            info = self._install_file_lock_path.lstat()
        except FileNotFoundError:
            self._preview_lock_was_absent = True
            return None
        except OSError:
            self._preview_lock_was_absent = False
            return self._reason("clean_inspection_failed")
        self._preview_lock_was_absent = False
        if not _is_exclusive_regular_file(info):
            return self._reason("clean_inspection_failed")
        return None

    def _preview_raced_busy(self) -> str | None:
        """Classify a guard that appeared after a lock-absent preview probe."""
        if getattr(self, "_preview_guard_fd", None) is not None:
            return None
        if not getattr(self, "_preview_lock_was_absent", False):
            return None
        reason = self._preview_lock_probe()
        if reason is not None:
            return reason
        if getattr(self, "_preview_lock_was_absent", False):
            return None
        return self._reason("install_already_running")

    def _rglob_install_metadata(self, versions_dir: Path) -> Iterator[Path]:
        """rglob metadata files with error-preserving traversal.

        ``Path.rglob`` suppresses subtree traversal errors and silently
        returns an incomplete candidate set; raise instead so the caller's
        inspection handling reports it (a misleading empty preview must not
        hide an unreadable versions tree).
        """
        stack: list[Path] = [versions_dir]
        while stack:
            parent = stack.pop()
            try:
                iterator = os.scandir(parent)
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise OSError(f"versions traversal failed: {parent}") from exc
            try:
                for entry in iterator:
                    if entry.name == self.spec.metadata_filename:
                        yield parent / entry.name
                    elif entry.is_dir(follow_symlinks=False):
                        stack.append(parent / entry.name)
            except OSError as exc:
                raise OSError(f"versions traversal failed: {parent}") from exc
            finally:
                iterator.close()

    def _clean_locked(
        self,
        *,
        keep_previous: int,
        dry_run: bool = False,
        removed: list[str] | None = None,
    ) -> dict[str, Any]:
        if removed is None:
            removed = []
        versions_dir = self.runtime_dir / "versions"
        current = self._current_install_dir(versions_dir)
        try:
            versions_info = versions_dir.lstat()
        except FileNotFoundError:
            versions_is_dir = False
        except OSError as exc:
            # An uninspectable versions tree must not silently preview as
            # empty (misleading "0 entries") — surface an inspection failure.
            raise OSError(f"versions directory cannot be inspected: {versions_dir}") from exc
        else:
            if _is_reparse_point(versions_info) or not stat.S_ISDIR(versions_info.st_mode):
                raise OSError(f"versions directory is not a confined directory: {versions_dir}")
            versions_is_dir = True

        install_dirs = (
            {
                metadata_path.parent
                for metadata_path in self._rglob_install_metadata(versions_dir)
                if metadata_path.parent.is_dir()
            }
            if versions_is_dir
            else set()
        )
        resolved_install_dirs = {path.resolve() for path in install_dirs}
        if current is not None and current not in resolved_install_dirs:
            raise OSError("current.json is unreadable")

        # Sibling metadata is not needed to reclaim abandoned staging.
        staging_candidates = self._staging_install_dirs()
        removal_failed = False
        for path in staging_candidates:
            if dry_run:
                removed.append(str(path))
                continue
            if self._remove_tree(path):
                removed.append(str(path))
            else:
                removal_failed = True

        downloads_present = self._downloads_namespace_present()
        installs = (
            self._read_managed_install_records(install_dirs)
            if current is not None or downloads_present
            else []
        )
        records_by_path = {record.path.resolve(): record for record in installs}
        if current is not None and current not in records_by_path:
            raise OSError("current install metadata is unreadable")

        protected = set(resolved_install_dirs) if current is None else {current}
        keep_count = max(0, keep_previous)
        if current is not None:
            by_lineage: dict[str, list[_ManagedRuntimeInstall]] = {}
            for record in installs:
                by_lineage.setdefault(record.lineage, []).append(record)
            current_lineage = records_by_path[current].lineage
            for lineage, lineage_installs in by_lineage.items():
                lineage_installs = self._retention_ranked_installs(
                    lineage_installs,
                    protected,
                )
                ranked = sorted(lineage_installs, key=lambda item: item.mtime, reverse=True)
                if lineage == current_lineage:
                    rollback = [record for record in ranked if record.path.resolve() != current]
                    protected.update(record.path.resolve() for record in rollback[:keep_count])
                elif ranked:
                    # Packaged and custom sources each keep a head. Switching
                    # source class must not erase the only locally recoverable
                    # copy of the alternate lineage.
                    protected.add(ranked[0].path.resolve())
                    protected.update(record.path.resolve() for record in ranked[1 : keep_count + 1])

        install_candidates = sorted(
            (
                path
                for path in install_dirs
                if not self._install_dir_is_protected(path.resolve(), protected)
            ),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        persisted_provenance = self._read_archive_provenance()
        candidate_paths = {path.resolve() for path in install_candidates}
        candidate_provenance = {
            (record.archive_name, record.archive_sha256)
            for record in installs
            if record.path.resolve() in candidate_paths
            and self._archive_name_is_owned(record.archive_name)
        }
        pending_provenance = persisted_provenance | candidate_provenance
        if not dry_run and pending_provenance != persisted_provenance:
            try:
                self._write_archive_provenance(pending_provenance)
            except OSError as exc:
                raise OSError("archive provenance cannot be persisted") from exc

        for path in install_candidates:
            if dry_run:
                removed.append(str(path))
                continue
            if self._remove_tree(path):
                removed.append(str(path))
            else:
                removal_failed = True

        if versions_is_dir and not dry_run:
            self._prune_empty_version_dirs(versions_dir)

        removed_install_paths = {
            path.resolve()
            for path in install_candidates
            if dry_run or not self._path_exists(path)
        }
        retained_installs = [
            record for record in installs if record.path.resolve() not in removed_install_paths
        ]
        protected_archive_sha256s = {record.archive_sha256 for record in retained_installs}
        if current is not None:
            protected_archive_sha256s.add(self._current_archive_sha256())
        install_provenance = {
            (record.archive_name, record.archive_sha256)
            for record in installs
            if self._archive_name_is_owned(record.archive_name)
        }
        archive_provenance = pending_provenance | install_provenance
        archives, terminal_archive_names = self._clean_downloaded_archives(
            archive_provenance=archive_provenance,
            protected_sha256s=protected_archive_sha256s,
            dry_run=dry_run,
        )
        if archives["failed_count"]:
            removal_failed = True
        if archives.get("skipped_reason") == _ARCHIVE_INSPECTION_FAILED:
            raise OSError("downloads directory cannot be inspected")

        provenance_failed = False
        retained_provenance = {
            pair for pair in pending_provenance if pair[0] not in terminal_archive_names
        }
        if not dry_run and retained_provenance != pending_provenance:
            try:
                self._write_archive_provenance(retained_provenance)
            except OSError:
                logger.warning(
                    "Failed to retire managed %s runtime archive provenance",
                    self.spec.runtime_id,
                    exc_info=True,
                )
                provenance_failed = True

        return {
            "ok": not removal_failed and not provenance_failed,
            "removed": removed,
            "reason": (
                self._reason("clean_inspection_failed")
                if provenance_failed
                else self._reason("clean_removal_failed")
                if removal_failed
                else None
            ),
            "archives": archives,
        }

    def _downloads_namespace_present(self) -> bool:
        downloads_dir = self.runtime_dir / "downloads"
        try:
            downloads_info = downloads_dir.lstat()
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise OSError(f"downloads directory cannot be inspected: {downloads_dir}") from exc
        if _is_reparse_point(downloads_info) or not stat.S_ISDIR(downloads_info.st_mode):
            raise OSError(f"downloads directory is not a confined directory: {downloads_dir}")
        return True

    def _read_managed_install_records(self, install_dirs: set[Path]) -> list[_ManagedRuntimeInstall]:
        records: list[_ManagedRuntimeInstall] = []
        for install_dir in install_dirs:
            metadata_path = install_dir / self.spec.metadata_filename
            try:
                install_info = install_dir.lstat()
                metadata_info = metadata_path.lstat()
                if _is_reparse_point(install_info) or not stat.S_ISDIR(install_info.st_mode):
                    raise OSError("install directory is not a directory")
                if not _is_exclusive_regular_file(metadata_info):
                    raise OSError("install metadata is not an exclusive regular file")
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                manifest_sha256 = metadata.get("manifest_sha256") if isinstance(metadata, dict) else None
                archive_name = metadata.get("archive_name") if isinstance(metadata, dict) else None
                archive_sha256 = metadata.get("archive_sha256") if isinstance(metadata, dict) else None
                manifest_source = metadata.get("manifest_source") if isinstance(metadata, dict) else None
                if (
                    not self._record_provider_matches(metadata.get("provider"))
                    or not self._record_runtime_id_matches(metadata.get("runtime_id"))
                    or not isinstance(manifest_sha256, str)
                    or not _SHA256_RE.fullmatch(manifest_sha256)
                    or not isinstance(archive_name, str)
                    or not self._archive_name_is_owned(archive_name)
                    or not isinstance(archive_sha256, str)
                    or not _SHA256_RE.fullmatch(archive_sha256)
                    or not isinstance(manifest_source, str)
                    or not manifest_source
                ):
                    raise ValueError("install metadata is incomplete")
                lineage = (
                    _PACKAGED_MANIFEST_LINEAGE
                    if manifest_source.startswith("package:")
                    else _CUSTOM_MANIFEST_LINEAGE
                )
                records.append(
                    _ManagedRuntimeInstall(
                        path=install_dir,
                        lineage=lineage,
                        archive_name=self._install_record_archive_name(
                            metadata,
                            archive_name,
                            archive_sha256,
                        ),
                        archive_sha256=archive_sha256,
                        mtime=install_info.st_mtime,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                raise OSError(f"install metadata is unreadable: {metadata_path}") from exc
        return records

    def _read_archive_provenance(self) -> set[tuple[str, str]]:
        try:
            provenance_info = self._archive_provenance_path.lstat()
        except FileNotFoundError:
            return set()
        except OSError as exc:
            raise OSError("archive provenance cannot be inspected") from exc
        if not _is_exclusive_regular_file(provenance_info):
            raise OSError("archive provenance is not an exclusive regular file")
        try:
            payload = json.loads(self._archive_provenance_path.read_text(encoding="utf-8"))
            entries = payload.get("archives") if isinstance(payload, dict) else None
            if (
                payload.get("schema_version") != _ARCHIVE_PROVENANCE_SCHEMA_VERSION
                or payload.get("runtime_id") != self.spec.runtime_id
                or not isinstance(entries, list)
            ):
                raise ValueError("archive provenance document is invalid")
            provenance: set[tuple[str, str]] = set()
            for entry in entries:
                name = entry.get("name") if isinstance(entry, dict) else None
                digest = entry.get("sha256") if isinstance(entry, dict) else None
                if (
                    not isinstance(name, str)
                    or not self._archive_name_is_owned(name)
                    or not isinstance(digest, str)
                    or not _SHA256_RE.fullmatch(digest)
                ):
                    raise ValueError("archive provenance entry is invalid")
                provenance.add((name, digest))
            return provenance
        except Exception as exc:  # noqa: BLE001
            raise OSError("archive provenance is unreadable") from exc

    def _write_archive_provenance(self, provenance: set[tuple[str, str]]) -> None:
        write_json_atomic(
            self._archive_provenance_path,
            {
                "schema_version": _ARCHIVE_PROVENANCE_SCHEMA_VERSION,
                "runtime_id": self.spec.runtime_id,
                "archives": [
                    {"name": name, "sha256": digest}
                    for name, digest in sorted(provenance)
                ],
            },
        )

    def _staging_install_dirs(self) -> list[Path]:
        try:
            runtime_info = self.runtime_dir.lstat()
        except FileNotFoundError:
            return []
        except OSError as exc:
            raise OSError(f"runtime directory cannot be inspected: {self.runtime_dir}") from exc
        if _is_reparse_point(runtime_info) or not stat.S_ISDIR(runtime_info.st_mode):
            raise OSError(f"runtime directory is not a confined directory: {self.runtime_dir}")
        staging: list[Path] = []
        try:
            with os.scandir(self.runtime_dir) as entries:
                for entry in entries:
                    if entry.name.startswith(self.spec.staging_prefixes) and entry.is_dir(follow_symlinks=False):
                        staging.append(self.runtime_dir / entry.name)
        except OSError as exc:
            raise OSError(f"runtime directory cannot be inspected: {self.runtime_dir}") from exc
        return sorted(staging)

    @staticmethod
    def _path_exists(path: Path) -> bool:
        try:
            path.lstat()
        except FileNotFoundError:
            return False
        return True

    def _remove_tree(self, path: Path) -> bool:
        try:
            shutil.rmtree(path)
            if self._path_exists(path):
                raise OSError("path still exists after removal")
        except OSError:
            logger.warning("Failed to remove managed runtime directory %s", path, exc_info=True)
            return False
        return True

    def _remove_install_target_for_replacement(self, path: Path) -> bool:
        try:
            leaf_info = path.lstat()
            versions_dir = (self.runtime_dir / "versions").resolve(strict=True)
            parent = path.parent.resolve(strict=True)
        except (OSError, RuntimeError):
            return False
        if (
            stat.S_ISLNK(leaf_info.st_mode)
            or _is_reparse_point(leaf_info)
            or not stat.S_ISDIR(leaf_info.st_mode)
            or (
                parent != versions_dir
                and versions_dir not in parent.parents
            )
        ):
            return False
        return self._remove_tree(path)

    def _current_archive_sha256(self) -> str:
        pointer_path = self.runtime_dir / "current.json"
        try:
            pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
            digest = pointer.get("archive_sha256") if isinstance(pointer, dict) else None
            if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
                raise ValueError("current pointer archive digest is invalid")
            return digest
        except Exception as exc:  # noqa: BLE001
            raise OSError("current.json is unreadable") from exc

    @staticmethod
    def _archive_name_is_owned(name: str) -> bool:
        return bool(name) and Path(name).name == name

    @classmethod
    def _archive_name_is_candidate(cls, name: str) -> bool:
        return (
            cls._archive_name_is_owned(name)
            and name.isprintable()
            and not name.endswith(".tmp")
            and not _MANIFEST_CACHE_RE.fullmatch(name)
        )

    @staticmethod
    def _skipped_archive_report(reason: str) -> dict[str, Any]:
        return {
            "outcome": "skipped",
            "removed_count": 0,
            "removed_bytes": 0,
            "candidate_count": 0,
            "candidate_bytes": 0,
            "failed_count": 0,
            "skipped_reason": reason,
        }

    @staticmethod
    def _archive_report(
        *,
        dry_run: bool,
        candidate_count: int,
        candidate_bytes: int,
        removed_count: int,
        removed_bytes: int,
        failed_count: int,
    ) -> dict[str, Any]:
        report: dict[str, Any] = {
            "outcome": "partial" if dry_run and candidate_count else "cleaned",
            "removed_count": removed_count,
            "removed_bytes": removed_bytes,
            "candidate_count": candidate_count,
            "candidate_bytes": candidate_bytes,
            "failed_count": failed_count,
            "skipped_reason": None,
        }
        if failed_count:
            report["outcome"] = "partial" if removed_count else "skipped"
            report["skipped_reason"] = _ARCHIVE_REMOVAL_FAILED
        return report

    @staticmethod
    def _sha256_from_fd(fd: int) -> str:
        digest = hashlib.sha256()
        with os.fdopen(os.dup(fd), "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _archive_candidate_sha256(self, name: str, fd: int) -> str:
        del name
        return self._sha256_from_fd(fd)

    def _clean_downloaded_archives(
        self,
        *,
        archive_provenance: set[tuple[str, str]],
        protected_sha256s: set[str],
        dry_run: bool,
    ) -> tuple[dict[str, Any], set[str]]:
        provenance_by_name: dict[str, set[str]] = {}
        for name, digest in archive_provenance:
            provenance_by_name.setdefault(name, set()).add(digest)
        known_archive_names = set(provenance_by_name)
        downloads_dir = self.runtime_dir / "downloads"
        try:
            downloads_info = downloads_dir.lstat()
        except FileNotFoundError:
            return (
                self._archive_report(
                    dry_run=dry_run,
                    candidate_count=0,
                    candidate_bytes=0,
                    removed_count=0,
                    removed_bytes=0,
                    failed_count=0,
                ),
                known_archive_names,
            )
        except OSError as exc:
            raise OSError("downloads directory cannot be inspected") from exc
        if _is_reparse_point(downloads_info) or not stat.S_ISDIR(downloads_info.st_mode):
            raise OSError("downloads directory is a symlink or not a directory")

        mtime_floor = time.time() - _ARCHIVE_MTIME_GUARD_SECONDS
        candidates: list[tuple[Path, int, str, int]] = []
        terminal_names: set[str] = set()
        if os.name == "nt":
            identity = (downloads_info.st_dev, downloads_info.st_ino)
            try:
                with os.scandir(downloads_dir) as entries:
                    names = sorted(entry.name for entry in entries)
            except OSError as exc:
                raise OSError("downloads directory cannot be inspected") from exc
            terminal_names.update(known_archive_names - set(names))
            for name in names:
                if name not in known_archive_names or not self._archive_name_is_candidate(name):
                    continue
                path = downloads_dir / name
                try:
                    entry_info = path.lstat()
                except FileNotFoundError:
                    terminal_names.add(name)
                    continue
                except OSError as exc:
                    raise OSError(f"archive cannot be inspected: {name}") from exc
                if not _is_exclusive_regular_file(entry_info) or entry_info.st_mtime > mtime_floor:
                    continue
                flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
                try:
                    fd = os.open(path, flags)
                    try:
                        opened = os.fstat(fd)
                        if (opened.st_dev, opened.st_ino) != (entry_info.st_dev, entry_info.st_ino):
                            raise OSError("archive was replaced during inspection")
                        archive_sha256 = self._archive_candidate_sha256(name, fd)
                    finally:
                        os.close(fd)
                except OSError as exc:
                    raise OSError(f"archive cannot be inspected: {name}") from exc
                if (
                    archive_sha256 in provenance_by_name[name]
                    and archive_sha256 not in protected_sha256s
                ):
                    candidates.append((path, entry_info.st_size, name, entry_info.st_ino))

            removed_count = 0
            removed_bytes = 0
            failed_count = 0
            if not dry_run:
                for path, size, name, inode in candidates:
                    try:
                        current_dir = downloads_dir.lstat()
                        if (
                            _is_reparse_point(current_dir)
                            or not stat.S_ISDIR(current_dir.st_mode)
                            or (current_dir.st_dev, current_dir.st_ino) != identity
                        ):
                            raise OSError("downloads directory was replaced")
                        current = path.lstat()
                        if current.st_ino != inode or not _is_exclusive_regular_file(current):
                            raise OSError("archive was replaced after inspection")
                        path.unlink()
                    except FileNotFoundError:
                        terminal_names.add(name)
                        continue
                    except OSError:
                        logger.warning("Failed to remove managed runtime archive %s", path, exc_info=True)
                        failed_count += 1
                        continue
                    removed_count += 1
                    removed_bytes += size
                    terminal_names.add(name)
            return (
                self._archive_report(
                    dry_run=dry_run,
                    candidate_count=len(candidates),
                    candidate_bytes=sum(item[1] for item in candidates),
                    removed_count=removed_count,
                    removed_bytes=removed_bytes,
                    failed_count=failed_count,
                ),
                terminal_names,
            )

        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_DIRECTORY", 0)
        dir_fd = os.open(downloads_dir, flags)
        try:
            opened_dir = os.fstat(dir_fd)
            if (opened_dir.st_dev, opened_dir.st_ino) != (downloads_info.st_dev, downloads_info.st_ino):
                raise OSError("downloads directory was replaced before scan")
            with os.scandir(dir_fd) as entries:
                names = sorted(entry.name for entry in entries)
            terminal_names.update(known_archive_names - set(names))
            for name in names:
                if name not in known_archive_names or not self._archive_name_is_candidate(name):
                    continue
                try:
                    entry_info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
                except FileNotFoundError:
                    terminal_names.add(name)
                    continue
                except OSError as exc:
                    raise OSError(f"archive cannot be inspected: {name}") from exc
                if not _is_exclusive_regular_file(entry_info) or entry_info.st_mtime > mtime_floor:
                    continue
                file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                try:
                    file_fd = os.open(name, file_flags, dir_fd=dir_fd)
                except FileNotFoundError:
                    terminal_names.add(name)
                    continue
                try:
                    opened_file = os.fstat(file_fd)
                    if (opened_file.st_dev, opened_file.st_ino) != (
                        entry_info.st_dev,
                        entry_info.st_ino,
                    ):
                        raise OSError("archive was replaced during inspection")
                    archive_sha256 = self._archive_candidate_sha256(name, file_fd)
                finally:
                    os.close(file_fd)
                if (
                    archive_sha256 in provenance_by_name[name]
                    and archive_sha256 not in protected_sha256s
                ):
                    candidates.append((downloads_dir / name, entry_info.st_size, name, entry_info.st_ino))

            removed_count = 0
            removed_bytes = 0
            failed_count = 0
            if not dry_run:
                for path, size, name, inode in candidates:
                    try:
                        current = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
                        if current.st_ino != inode or not _is_exclusive_regular_file(current):
                            raise OSError("archive was replaced after inspection")
                        os.unlink(name, dir_fd=dir_fd)
                    except FileNotFoundError:
                        terminal_names.add(name)
                        continue
                    except OSError:
                        logger.warning("Failed to remove managed runtime archive %s", path, exc_info=True)
                        failed_count += 1
                        continue
                    removed_count += 1
                    removed_bytes += size
                    terminal_names.add(name)
            return (
                self._archive_report(
                    dry_run=dry_run,
                    candidate_count=len(candidates),
                    candidate_bytes=sum(item[1] for item in candidates),
                    removed_count=removed_count,
                    removed_bytes=removed_bytes,
                    failed_count=failed_count,
                ),
                terminal_names,
            )
        finally:
            os.close(dir_fd)

    def _manifest_installable(self, manifest: ManagedRuntimeManifest) -> bool:
        return True

    def _manifest_install_candidates(
        self,
        manifest: ManagedRuntimeManifest,
        archive: ManagedRuntimeArchive,
    ) -> Iterator[Path]:
        yield self._manifest_install_dir(manifest, archive)

    def _manifest_identity_fields(self, manifest: ManagedRuntimeManifest) -> dict[str, str]:
        del manifest
        return {}

    def _metadata_matches_install_target(
        self,
        metadata: Mapping[str, Any],
        target: Mapping[str, str],
    ) -> bool:
        return all(metadata.get(key) == value for key, value in target.items())

    def _record_provider_matches(self, value: object) -> bool:
        return value == self.spec.record_provider

    def _record_runtime_id_matches(self, value: object) -> bool:
        return value == self.spec.runtime_id or (
            value is None and self.spec.allow_legacy_missing_runtime_id
        )

    def _archive_cache_name(self, archive: ManagedRuntimeArchive) -> str:
        return archive.name

    def _install_record_archive_name(
        self,
        metadata: Mapping[str, Any],
        archive_name: str,
        archive_sha256: str,
    ) -> str:
        del metadata, archive_sha256
        return archive_name

    def _record_matches_configured_source(self, metadata: Mapping[str, Any]) -> bool:
        del metadata
        return True

    def _manifest_path_read_error_reason(self) -> str:
        return self._reason("manifest_missing")

    def _record_install_dir_matches(
        self,
        install_dir: Path,
        metadata: Mapping[str, Any],
    ) -> bool:
        del install_dir, metadata
        return True

    def _install_dir_is_protected(self, install_dir: Path, protected: set[Path]) -> bool:
        return any(
            install_dir == item or install_dir in item.parents or item in install_dir.parents
            for item in protected
        )

    def _retention_ranked_installs(
        self,
        installs: list[_ManagedRuntimeInstall],
        protected: set[Path],
    ) -> list[_ManagedRuntimeInstall]:
        del protected
        return installs

    def _prepare_binary(self, binary: Path) -> dict[str, Any]:
        return {"ok": True, "skipped": True}

    def _prepare_binary_for_manifest(
        self,
        binary: Path,
        manifest: ManagedRuntimeManifest,
    ) -> dict[str, Any]:
        """Prepare an extracted binary with any runtime-specific manifest contract."""

        del manifest
        return self._prepare_binary(binary)

    def _binary_version(self, binary: Path | None) -> str | None:
        raise NotImplementedError

    def _binary_matches_manifest(self, binary: Path, manifest: ManagedRuntimeManifest) -> bool:
        return self._binary_version(binary) == manifest.runtime_version

    def load_manifest_for_diagnostics(self) -> ManagedRuntimeManifest | None:
        """Load the selected manifest without writing runtime state."""

        return self._load_manifest(
            allow_network=not self.offline,
            persist_remote_cache=False,
        )

    def _load_manifest(
        self,
        *,
        allow_network: bool,
        persist_remote_cache: bool = True,
    ) -> ManagedRuntimeManifest | None:
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
                self._install_reason = self._manifest_path_read_error_reason()
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
                    payload = fetch_bytes(
                        self.manifest_url,
                        timeout=30,
                        opener=urllib.request.urlopen,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.exception("Failed to download %s manifest", self.spec.runtime_id)
                    self._install_reason = self._reason("manifest_download_failed")
                    self._download_error = dependency_error_details(exc, self.manifest_url)
                    return None
                loaded_from = self.manifest_url
                cache_remote = persist_remote_cache
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
                write_atomic(cached_manifest, payload)
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
            raw_archives = data.get(self.spec.archives_field) or {}
            if isinstance(raw_archives, dict):
                archive_entries = raw_archives.items()
            elif isinstance(raw_archives, list):
                archive_entries = ((item.get("platform"), item) for item in raw_archives if isinstance(item, dict))
            else:
                raise ValueError("invalid archive collection")
            for platform_tag, item in archive_entries:
                if not _safe_metadata_value(platform_tag) or not isinstance(item, dict):
                    raise ValueError("invalid archive entry")
                url = str(item["url"])
                name = str(item.get("name") or Path(urllib.parse.urlparse(url).path).name)
                sha256 = str(item["sha256"]).lower()
                raw_binary_sha256 = item.get("binary_sha256")
                binary_sha256 = (
                    str(raw_binary_sha256).lower()
                    if raw_binary_sha256 is not None
                    else None
                )
                bin_path = str(item.get("bin_path") or self.spec.default_bin_path)
                raw_size = item.get(self.spec.archive_size_field)
                size = int(raw_size) if raw_size is not None else None
                if not self._archive_name_is_owned(name):
                    raise ValueError("unsafe archive name")
                if not _SHA256_RE.fullmatch(sha256):
                    raise ValueError("invalid archive sha256")
                if self.spec.binary_artifact and (
                    (
                        binary_sha256 is None
                        and not self.spec.allow_missing_binary_sha256
                    )
                    or (
                        binary_sha256 is not None
                        and not _SHA256_RE.fullmatch(binary_sha256)
                    )
                ):
                    raise ValueError("invalid binary sha256")
                if binary_sha256 is not None and not _SHA256_RE.fullmatch(binary_sha256):
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
            runtime_version = str(data.get(self.spec.version_field) or "")
            if not _safe_metadata_value(runtime_version):
                raise ValueError("invalid runtime version")
            manifest = ManagedRuntimeManifest(
                schema_version=int(data.get("schema_version")),
                runtime_version=runtime_version,
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
        platform_tag = runtime_platform_tag()
        platform_aliases = dict(self.spec.platform_aliases)
        archive = manifest.archives.get(platform_tag)
        if archive is None:
            alias = platform_aliases.get(platform_tag)
            archive = manifest.archives.get(alias) if alias else None
        if archive is None:
            self._install_reason = self._reason("platform_unsupported")
        return archive

    def _resolve_manifest_archive(self, archive: ManagedRuntimeArchive) -> Path | None:
        cached = self.runtime_dir / "downloads" / self._archive_cache_name(archive)
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
            fetch_to_path(
                archive.url,
                temporary,
                timeout=60,
                opener=urllib.request.urlopen,
            )
            self._download_error = None
            if not self._downloaded_archive_matches(temporary, archive):
                temporary.unlink(missing_ok=True)
                return None
            temporary.replace(cached)
            self._install_reason = None
            return cached
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to download %s archive", self.spec.runtime_id)
            temporary.unlink(missing_ok=True)
            self._install_reason = self._reason("archive_download_failed")
            self._download_error = dependency_error_details(exc, archive.url)
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
        if self.spec.include_manifest_digest_in_install_fingerprint:
            fingerprint_input = f"{manifest.digest}:{archive.sha256}"
        else:
            fingerprint_input = f"{manifest.runtime_version}:{archive.platform}:{archive.sha256}"
        fingerprint = hashlib.sha256(fingerprint_input.encode("utf-8")).hexdigest()[:16]
        return (
            self.runtime_dir
            / "versions"
            / safe_path_part(manifest.runtime_version)
            / safe_path_part(archive.platform)
            / fingerprint
        )

    def _install_target_identity(
        self,
        manifest: ManagedRuntimeManifest,
        archive: ManagedRuntimeArchive,
    ) -> dict[str, str]:
        target = {
            "runtime_version": manifest.runtime_version,
            "platform": archive.platform,
            "archive_sha256": archive.sha256,
        }
        if archive.binary_sha256 is not None:
            target["binary_sha256"] = archive.binary_sha256
        target.update(self._manifest_identity_fields(manifest))
        return target

    @staticmethod
    def _normalized_install_target(target: Mapping[str, str]) -> dict[str, str]:
        return {key: value for key, value in target.items() if key != "manifest_sha256"}

    def _verified_manifest_binary(
        self,
        install_dir: Path,
        manifest: ManagedRuntimeManifest,
        archive: ManagedRuntimeArchive,
    ) -> Path | None:
        try:
            metadata = json.loads((install_dir / self.spec.metadata_filename).read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return None
        bin_path = metadata.get("bin_path", self.spec.default_bin_path)
        if not isinstance(bin_path, str) or archive_path_is_unsafe(bin_path):
            return None
        try:
            install_dir_resolved = install_dir.resolve(strict=True)
            binary = (install_dir_resolved / bin_path).resolve(strict=True)
        except (OSError, RuntimeError):
            return None
        if (
            install_dir_resolved not in binary.parents
            or not binary.is_file()
            or self.spec.binary_artifact
            and not os.access(binary, os.X_OK)
        ):
            return None
        target = self._install_target_identity(manifest, archive)
        target_platform = target.pop("platform")
        metadata_platform = metadata.get("platform")
        aliases = dict(self.spec.platform_aliases)
        expected_binary_sha256 = archive.binary_sha256
        legacy_without_binary_digest = (
            self.spec.binary_artifact
            and expected_binary_sha256 is None
            and metadata.get("binary_sha256") is None
            and self.spec.allow_missing_binary_sha256
            and self.spec.allow_legacy_missing_runtime_id
            and metadata.get("runtime_id") is None
        )
        if self.spec.binary_artifact and expected_binary_sha256 is None:
            persisted_binary_sha256 = metadata.get("binary_sha256")
            if isinstance(persisted_binary_sha256, str) and _SHA256_RE.fullmatch(
                persisted_binary_sha256
            ):
                expected_binary_sha256 = persisted_binary_sha256
            elif not legacy_without_binary_digest:
                return None
        if not (
            self._record_provider_matches(metadata.get("provider"))
            and self._record_runtime_id_matches(metadata.get("runtime_id"))
            and self._metadata_matches_install_target(metadata, target)
            and isinstance(metadata_platform, str)
            and aliases.get(metadata_platform, metadata_platform)
            == aliases.get(target_platform, target_platform)
            and bin_path == archive.bin_path
            and (
                not self.spec.binary_artifact
                or expected_binary_sha256 is None
                or file_sha256(binary) == expected_binary_sha256
            )
        ):
            return None
        if not self._binary_matches_manifest(binary, manifest):
            self._install_reason = self._reason("binary_not_runnable")
            return None
        return binary

    def _write_manifest_install_metadata(
        self,
        install_dir: Path,
        manifest: ManagedRuntimeManifest,
        archive: ManagedRuntimeArchive,
        *,
        binary_sha256: str | None,
    ) -> None:
        payload: dict[str, Any] = {
            "provider": self.spec.record_provider,
            "runtime_id": self.spec.runtime_id,
            "manifest_sha256": manifest.digest,
            "runtime_version": manifest.runtime_version,
            "platform": archive.platform,
            "archive_name": archive.name,
            "archive_sha256": archive.sha256,
            "bin_path": archive.bin_path,
            "manifest_source": manifest.loaded_from,
            "source": manifest.source,
            **self._manifest_identity_fields(manifest),
        }
        if binary_sha256 is not None:
            payload["binary_sha256"] = binary_sha256
        write_json_atomic(
            install_dir / self.spec.metadata_filename,
            payload,
        )

    def _write_current_pointer(
        self,
        install_dir: Path,
        manifest: ManagedRuntimeManifest,
        archive: ManagedRuntimeArchive,
    ) -> None:
        manifest_sha256 = manifest.digest
        try:
            metadata = json.loads((install_dir / self.spec.metadata_filename).read_text(encoding="utf-8"))
            persisted_digest = metadata.get("manifest_sha256") if isinstance(metadata, dict) else None
            if isinstance(persisted_digest, str) and _SHA256_RE.fullmatch(persisted_digest):
                manifest_sha256 = persisted_digest
        except (OSError, RecursionError, UnicodeError, ValueError):
            pass
        write_json_atomic(
            self.runtime_dir / "current.json",
            {
                "provider": self.spec.record_provider,
                "runtime_id": self.spec.runtime_id,
                "runtime_version": manifest.runtime_version,
                "platform": archive.platform,
                "install_dir": str(install_dir),
                "manifest_sha256": manifest_sha256,
                "archive_sha256": archive.sha256,
                "bin_path": archive.bin_path,
                **self._manifest_identity_fields(manifest),
            },
        )

    def _current_install_dir(self, versions_dir: Path) -> Path | None:
        pointer_path = self.runtime_dir / "current.json"
        try:
            payload = pointer_path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            try:
                pointer_path.lstat()
            except FileNotFoundError:
                return None
            except OSError as inspect_exc:
                raise OSError("current.json is unreadable") from inspect_exc
            raise OSError("current.json is unreadable") from exc
        except (OSError, RecursionError, UnicodeError) as exc:
            raise OSError("current.json is unreadable") from exc

        try:
            pointer = json.loads(payload)
            install_dir = pointer.get("install_dir") if isinstance(pointer, dict) else None
            if not isinstance(install_dir, str) or not install_dir or not Path(install_dir).is_absolute():
                raise ValueError("current.json has no absolute install_dir")
            candidate = Path(install_dir).resolve()
            versions_root = versions_dir.resolve()
            if candidate == versions_root or versions_root not in candidate.parents:
                raise ValueError("current.json install_dir is outside versions")
            return candidate
        except (OSError, RecursionError, RuntimeError, UnicodeError, ValueError) as exc:
            raise OSError("current.json is unreadable") from exc

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

    def _archive_status_payload(self, archive: ManagedRuntimeArchive | None) -> dict[str, Any] | None:
        if archive is None:
            return None
        payload = {
            "platform": archive.platform,
            "name": archive.name,
            "url": redact_url(archive.url),
            "sha256": archive.sha256,
            "size": archive.size,
            "bin_path": archive.bin_path,
        }
        if archive.binary_sha256 is not None:
            payload["binary_sha256"] = archive.binary_sha256
        return payload

    def _reuse_existing_install(
        self,
        binary: Path,
        install_dir: Path,
        manifest: ManagedRuntimeManifest,
        archive: ManagedRuntimeArchive,
        *,
        reason: str | None = None,
    ) -> dict[str, Any]:
        if ManagedRuntimeManager.resolve_binary(self) != binary:
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
        self._download_error = None
        return {
            "ok": True,
            "installed": True,
            "changed": changed,
            "path": str(binary),
            "version": manifest.runtime_version,
            "platform": archive.platform,
            "install_dir": str(install_dir),
            "target": self._install_target_identity(manifest, archive),
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
            "message": message
            or (
                dependency_error_message(self._download_error, label=f"{self.spec.runtime_id} dependency download")
                if self._download_error
                else reason
            ),
            "version": manifest.runtime_version if manifest else None,
            "platform": archive.platform if archive else runtime_platform_tag(),
            "path": None,
            "download_error": self._download_error,
        }

    def _reason(self, suffix: str) -> str:
        return f"{self.spec.runtime_id}_{suffix}"

    def _base_install_failure_reasons(self) -> frozenset[str]:
        """Return failures produced by the shared ``ensure`` implementation."""

        return frozenset(self._reason(suffix) for suffix in _ENSURE_FAILURE_SUFFIXES)

    def _acquire_mutation_lock(self) -> MigrationFileLock | None:
        if not self._install_lock.acquire(blocking=False):
            return None
        try:
            file_lock = MigrationFileLock(
                self._install_file_lock_path,
                timeout_seconds=0,
                _handle_opener=self._open_mutation_lock_handle,
                _handle_validator=lambda handle: self._guard_path_matches_fd(handle.fileno()),
            )
            file_lock.acquire()
        except MigrationLockTimeout:
            self._install_lock.release()
            return None
        except Exception:
            self._install_lock.release()
            raise
        return file_lock

    def _open_mutation_lock_handle(self, lock_path: Path) -> IO[str]:
        try:
            info = lock_path.lstat()
        except FileNotFoundError:
            pass
        else:
            if not _is_exclusive_regular_file(info):
                raise OSError(f"Install guard is not an exclusive regular file: {lock_path}")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(lock_path, flags, 0o644)
        try:
            if not self._guard_path_matches_fd(fd):
                raise OSError(f"Install guard path does not match its descriptor: {lock_path}")
            return os.fdopen(fd, "r+", encoding="utf-8")
        except BaseException:
            os.close(fd)
            raise

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
    supports_data_filter = hasattr(tarfile, "data_filter")
    destination_resolved = destination.resolve()
    members = archive.getmembers()
    for member in members:
        if not (member.isfile() or member.isdir() or member.issym() or member.islnk()):
            raise ValueError(f"Unsupported managed runtime archive member: {member.name}")
        if archive_path_is_unsafe(member.name):
            raise ValueError(f"Unsafe managed runtime archive path: {member.name}")
        target = (destination / member.name).resolve()
        if target != destination_resolved and destination_resolved not in target.parents:
            raise ValueError(f"Unsafe managed runtime archive path: {member.name}")
        if member.issym():
            link_target = (destination / member.name).parent / member.linkname
            link_target_resolved = link_target.resolve()
            if (
                link_target_resolved != destination_resolved
                and destination_resolved not in link_target_resolved.parents
            ):
                raise ValueError(f"Unsafe managed runtime archive link target: {member.name}")
        elif member.islnk():
            link_target = destination / member.linkname
            link_target_resolved = link_target.resolve()
            if (
                link_target_resolved != destination_resolved
                and destination_resolved not in link_target_resolved.parents
            ):
                raise ValueError(f"Unsafe managed runtime archive link target: {member.name}")
    if supports_data_filter:
        archive.extractall(destination, filter="data")
    else:
        _extract_tar_without_filter(archive, destination, members)


def _extract_tar_without_filter(
    archive: tarfile.TarFile,
    destination: Path,
    members: list[tarfile.TarInfo],
) -> None:
    member_paths: dict[tuple[str, ...], tarfile.TarInfo] = {}
    link_targets: dict[tuple[str, ...], tuple[str, ...]] = {}
    for member in members:
        path = _normalized_tar_path(member.name)
        if not path and not member.isdir():
            raise ValueError(f"Unsafe managed runtime archive path: {member.name}")
        if path in member_paths:
            raise ValueError(f"Managed runtime archive path collision: {member.name}")
        member_paths[path] = member

    for path, member in member_paths.items():
        for index in range(1, len(path)):
            ancestor = member_paths.get(path[:index])
            if ancestor is not None and not ancestor.isdir():
                raise ValueError(f"Managed runtime archive path/type collision: {member.name}")

    for path, member in member_paths.items():
        if member.issym():
            target = _normalized_tar_link_target(path[:-1], member.linkname, member.name)
            resolved_target = _resolved_tar_symlink_target(target, member_paths)
            if resolved_target and resolved_target not in member_paths and not any(
                candidate[: len(resolved_target)] == resolved_target
                for candidate in member_paths
            ):
                raise ValueError(f"Missing managed runtime archive link target: {member.name}")
            link_targets[path] = target
        elif member.islnk():
            target = _normalized_tar_link_target((), member.linkname, member.name)
            target_member = member_paths.get(target)
            if target_member is None or not target_member.isfile():
                raise ValueError(f"Unsafe managed runtime archive hardlink target: {member.name}")
            link_targets[path] = target

    try:
        destination_info = destination.lstat()
    except FileNotFoundError:
        destination.mkdir(parents=True)
    else:
        if _is_reparse_point(destination_info) or not stat.S_ISDIR(destination_info.st_mode):
            raise ValueError("Managed runtime archive destination is not a directory")
        if any(destination.iterdir()):
            raise ValueError("Managed runtime archive destination is not empty")

    directories = sorted(
        ((path, member) for path, member in member_paths.items() if member.isdir()),
        key=lambda item: len(item[0]),
    )
    for path, _member in directories:
        destination.joinpath(*path).mkdir(parents=True, exist_ok=True)

    for path, member in member_paths.items():
        if not member.isfile():
            continue
        target = destination.joinpath(*path)
        target.parent.mkdir(parents=True, exist_ok=True)
        source = archive.extractfile(member)
        if source is None:
            raise ValueError(f"Unreadable managed runtime archive member: {member.name}")
        with source, target.open("xb") as handle:
            shutil.copyfileobj(source, handle)
        target.chmod(member.mode & 0o777)

    for path, member in member_paths.items():
        if not member.islnk():
            continue
        target = destination.joinpath(*path)
        target.parent.mkdir(parents=True, exist_ok=True)
        os.link(destination.joinpath(*link_targets[path]), target)

    for path, member in member_paths.items():
        if not member.issym():
            continue
        target = destination.joinpath(*path)
        target.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(member.linkname, target)

    for path, member in reversed(directories):
        destination.joinpath(*path).chmod(member.mode & 0o777)


def _normalized_tar_path(value: str) -> tuple[str, ...]:
    if archive_path_is_unsafe(value) or "\\" in value:
        raise ValueError(f"Unsafe managed runtime archive path: {value}")
    parts = tuple(part for part in PurePosixPath(value).parts if part not in {"", "."})
    return parts


def _normalized_tar_link_target(
    base: tuple[str, ...],
    value: str,
    member_name: str,
) -> tuple[str, ...]:
    posix_path = PurePosixPath(value)
    windows_path = PureWindowsPath(value)
    if (
        not value
        or "\\" in value
        or posix_path.is_absolute()
        or windows_path.is_absolute()
        or windows_path.drive
        or windows_path.root
    ):
        raise ValueError(f"Unsafe managed runtime archive link target: {member_name}")
    parts = list(base)
    for part in posix_path.parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if not parts:
                raise ValueError(f"Unsafe managed runtime archive link target: {member_name}")
            parts.pop()
        else:
            parts.append(part)
    return tuple(parts)


def _resolved_tar_symlink_target(
    target: tuple[str, ...],
    member_paths: Mapping[tuple[str, ...], tarfile.TarInfo],
) -> tuple[str, ...]:
    pending = list(target)
    resolved: list[str] = []
    visited: set[tuple[str, ...]] = set()
    while pending:
        resolved.append(pending.pop(0))
        candidate = tuple(resolved)
        member = member_paths.get(candidate)
        if member is None or not member.issym():
            continue
        if candidate in visited:
            raise ValueError(f"Managed runtime archive symlink cycle: {member.name}")
        visited.add(candidate)
        linked = _normalized_tar_link_target(
            candidate[:-1],
            member.linkname,
            member.name,
        )
        resolved = []
        pending = [*linked, *pending]
    return tuple(resolved)


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


def _safe_metadata_value(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and len(value.encode("utf-8")) <= 128
        and all(
            character.isascii() and (character.isalnum() or character in {".", "-", "_", "+"})
            for character in value
        )
    )


def env_flag_enabled(name: str, *, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    # ``sort_keys`` keeps the manifest and install-state files diffable across
    # runs; the swap itself belongs to ``write_atomic``.
    write_atomic(path, json.dumps(payload, sort_keys=True) + "\n")
