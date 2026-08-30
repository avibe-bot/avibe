"""Managed EverOS runtime specialization for Memory.

The shared manager owns manifest parsing, downloads, extraction, checksums, and
the active ``current.json`` pointer. This module adds only the pinned Python
identity and EverOS smoke checks needed by the Memory sidecar.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import stat
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Callable, Iterable, Mapping
from typing import Any, Protocol, runtime_checkable

from config import paths
from core.managed_runtime import (
    _safe_metadata_value,
    ManagedRuntimeArchive,
    ManagedRuntimeManager,
    ManagedRuntimeManifest,
    ManagedRuntimeSpec,
    env_flag_enabled,
    file_sha256,
    runtime_platform_tag,
    write_json_atomic,
)
from core.process_isolation import isolated_subprocess_kwargs
from core.memory.confined_filesystem import (
    ConfinedFilesystemError,
    create_confined_file,
    ensure_private_directory,
    fsync_directory,
    open_and_harden_confined_regular_file,
    open_confined_regular_file,
    remove_confined_path,
    replace_confined,
)
from core.memory.artifact_contract import (
    EMBEDDED_PYTHON_VERSION,
    EVEROS_VERSION,
    run_cold_artifact_admission,
)
from core.memory.provider_root import (
    PROVIDER_ROOT_CONTROL_FILES,
    ROOT_SENTINEL_FILENAME,
    ProviderRoot,
    ProviderRootError,
    ProviderRootMetadata,
    ProviderRootState,
)


PACKAGE_LOCK_SHA256 = "e6acc17e4c0969563d380326e90134965af0822259bb4a9adb4d54433e9737fe"
RUNTIME_BUILDER_UV_VERSION = "0.9.18"
ARTIFACT_ADMISSION_REVISION = 1
_DEV_RUNTIME_ENV = "AVIBE_MEMORY_DEV_RUNTIME"
_DEV_RUNTIME_FAILURE_REASON = "memory_runtime_install_failed"
_DEV_PROVIDER_ROOT_FORMAT = f"everos-{EVEROS_VERSION}"
_DEV_ARTIFACT_FINGERPRINT = f"dev-everos-{EVEROS_VERSION}"
_MANIFEST_RESOURCE = "memory_runtime_manifest.json"
_MAX_CURRENT_POINTER_BYTES = 16 * 1024
_SPEC = ManagedRuntimeSpec(
    runtime_id="memory-runtime",
    manifest_resource=_MANIFEST_RESOURCE,
    version_field="everos_version",
    default_bin_path="bin/python",
)
_SCRUBBER_ADMISSION_SCRIPT = (
    "from core.memory.secret_scrubber import install_error_scrubbers\n"
    "install_error_scrubbers()\n"
)
_SCRUBBER_ADMISSION_TIMEOUT_SECONDS = 30
_SCRUBBER_ADMISSION_TIMEOUT_REASON = "memory_runtime_preparation_scrubber_timeout"
_SCRUBBER_ADMISSION_FAILURE_REASON = "memory_runtime_preparation_scrubber_failed"
_SYNC_ADMISSION_FAILURE_REASON = "memory_runtime_preparation_sync_contract_failed"
_PREPARATION_FAILURE_REASON = "memory_runtime_preparation_failed"
_LATEST_INSTALL_FAILURE_FILENAME = "last-install-failure.json"
_MAX_LATEST_INSTALL_FAILURE_BYTES = 4 * 1024
_PROVIDER_ROOT_REPAIR_MARKERS = (
    "incompatible",
    "does not match",
    "not empty",
    "sentinel is unsafe",
    "sentinel is invalid",
    "metadata is invalid",
)


logger = logging.getLogger(__name__)


def _provider_root_failure_reason(error: ProviderRootError) -> str | None:
    """Classify only a proven incompatible local root as repairable."""

    detail = str(error).lower()
    if any(marker in detail for marker in _PROVIDER_ROOT_REPAIR_MARKERS):
        return "memory_local_data_unusable"
    return None


MemoryArtifactCandidate = ProviderRootMetadata
MemoryProviderRootState = ProviderRootState


class MemoryRuntimeActivationError(RuntimeError):
    """Closed failure raised before an unsafe Memory runtime pointer cutover."""

    def __init__(self, message: str, *, reason: str | None = None) -> None:
        super().__init__(message)
        self.reason = reason


MemoryArtifactActivationCoordinator = Callable[
    [MemoryArtifactCandidate, MemoryProviderRootState | None, Callable[[], None], Callable[[], None]],
    None,
]


class MemoryArtifactManager(ManagedRuntimeManager):
    """Install and resolve the Avibe-pinned EverOS Python runtime."""

    def __init__(
        self,
        *,
        runtime_dir: Path | None = None,
        manifest_path: Path | str | None = None,
        manifest_url: str | None = None,
        offline: bool | None = None,
        provider_root: Path | str | None = None,
    ) -> None:
        manifest_path_value = manifest_path or os.environ.get("VIBE_MEMORY_MANIFEST_PATH")
        super().__init__(
            spec=_SPEC,
            runtime_dir=runtime_dir or paths.get_runtime_dir() / "memory",
            manifest_path=manifest_path_value,
            manifest_url=manifest_url if manifest_url is not None else os.environ.get("VIBE_MEMORY_MANIFEST_URL"),
            offline=env_flag_enabled("VIBE_MEMORY_OFFLINE") if offline is None else offline,
        )
        provider_root_path = (
            Path(provider_root)
            if provider_root is not None
            else paths.get_vibe_remote_dir() / "memory" / "everos-root"
        )
        self._provider_root = ProviderRoot(
            provider_root_path,
            effective_home=provider_root_path.parent.parent,
        )
        self._activation_coordinator: MemoryArtifactActivationCoordinator | None = None
        self._dev_runtime_checked = False
        self._dev_runtime_checked_value: str | None = None
        self._dev_runtime_cached_python: Path | None = None
        self._dev_runtime_warning_logged = False
        self._dev_runtime_failure_logged: str | None = None

    def set_activation_coordinator(self, coordinator: MemoryArtifactActivationCoordinator | None) -> None:
        """Register the controller-owned lifecycle bridge for active cutovers."""

        self._activation_coordinator = coordinator

    def set_provider_root(self, provider_root: Path | str) -> None:
        """Bind activation compatibility checks to the controller's effective home."""

        provider_root_path = Path(provider_root)
        self._provider_root = ProviderRoot(
            provider_root_path,
            effective_home=provider_root_path.parent.parent,
        )

    def resolve_python(self) -> Path | None:
        """Return a verified embedded Python without starting or downloading it."""

        return self.resolve_binary()

    def resolve_binary(self) -> Path | None:
        """Resolve only the executable selected by the active pointer."""

        if self._dev_runtime_configured():
            return self._dev_runtime_python()
        pointer = self._active_pointer()
        if pointer is None:
            return None
        try:
            return self._admitted_active_pointer_binary(pointer)
        except Exception:  # noqa: BLE001
            return None

    def status(self) -> dict[str, Any]:
        """Report the active pointer without running compatibility probes."""

        if self._dev_runtime_configured():
            return self._dev_runtime_status(self._dev_runtime_python())
        self._install_reason = None
        latest_failure = self._read_latest_install_failure()
        manifest = self._load_manifest(allow_network=False)
        if manifest is not None:
            self._manifest_installable(manifest)
        archive = self._manifest_archive_for_platform(manifest) if manifest else None
        pointer, invalid = self._read_active_pointer()
        try:
            binary = self._verified_active_pointer_binary(pointer) if pointer is not None else None
        except OSError:
            binary = None
            invalid = True
        admission_rejected = (
            not invalid
            and pointer is not None
            and binary is not None
            and pointer.get("admission_revision") == ARTIFACT_ADMISSION_REVISION
            and pointer.get("admission_ok") is not True
        )
        if admission_rejected:
            binary = None
            invalid = True
        invalid = invalid or (pointer is not None and binary is None)
        selected_version = manifest.runtime_version if manifest is not None else None
        matches_manifest = None
        if binary is not None and manifest is not None and archive is not None:
            try:
                matches_manifest = (
                    self._verified_manifest_binary(
                        Path(pointer["install_dir"]), manifest, archive
                    )
                    == binary
                )
            except OSError:
                binary = None
                invalid = True
            if matches_manifest:
                try:
                    candidate = self._candidate_from_manifest(manifest)
                    compatible_formats = pointer.get("compatible_provider_root_formats")
                    matches_manifest = (
                        pointer.get("provider_root_format") == candidate.provider_root_format
                        and isinstance(compatible_formats, list)
                        and all(_safe_metadata_value(value) for value in compatible_formats)
                        and frozenset(
                            {
                                pointer.get("provider_root_format"),
                                *compatible_formats,
                            }
                        )
                        == candidate.compatible_provider_root_formats
                        and _sync_contract_from_payload(pointer)
                        == _sync_contract_from_payload(manifest.payload)
                    )
                except (MemoryRuntimeActivationError, ValueError):
                    matches_manifest = False
        installed_version = pointer.get("runtime_version") if binary is not None else None
        persisted_reason = latest_failure.get("reason") if latest_failure is not None else None
        if admission_rejected and persisted_reason is not None:
            failure_reason = persisted_reason
        elif invalid:
            failure_reason = "memory_runtime_install_failed"
        else:
            failure_reason = persisted_reason or (
                self._install_reason if binary is None else None
            )
        return {
            "id": self.spec.runtime_id,
            "provider": "manifest",
            "platform": runtime_platform_tag(),
            "installed": binary is not None,
            "version": installed_version,
            "selected_version": selected_version,
            "matches_manifest": matches_manifest,
            "status": (
                "ready"
                if binary is not None
                else ("error" if invalid or persisted_reason is not None else "missing")
            ),
            "path": str(binary) if binary is not None else None,
            "install_dir": pointer.get("install_dir") if binary is not None else None,
            "manifest": self._manifest_status_payload(manifest),
            "archive": self._archive_status_payload(archive),
            "reason": failure_reason,
            "download_error": self._download_error,
        }

    def provider_root_format(self) -> str | None:
        if self._dev_runtime_configured():
            return _DEV_PROVIDER_ROOT_FORMAT if self._dev_runtime_python() is not None else None
        pointer = self._active_pointer()
        value = pointer.get("provider_root_format") if pointer is not None else None
        return value if _safe_metadata_value(value) else None

    def compatible_provider_root_formats(self) -> frozenset[str]:
        """Return the active artifact's declared root formats, including itself."""

        if self._dev_runtime_configured():
            return frozenset({_DEV_PROVIDER_ROOT_FORMAT}) if self._dev_runtime_python() is not None else frozenset()
        pointer = self._active_pointer()
        if pointer is None:
            return frozenset()
        provider_root_format = pointer.get("provider_root_format")
        values = pointer.get("compatible_provider_root_formats")
        if not _safe_metadata_value(provider_root_format) or not isinstance(values, list):
            return frozenset()
        compatible = {provider_root_format}
        compatible.update(value for value in values if _safe_metadata_value(value))
        return frozenset(compatible)

    def artifact_fingerprint(self) -> str | None:
        if self._dev_runtime_configured():
            return _DEV_ARTIFACT_FINGERPRINT if self._dev_runtime_python() is not None else None
        pointer = self._active_pointer()
        value = pointer.get("artifact_fingerprint") if pointer is not None else None
        return value if _safe_metadata_value(value) else None

    def sync_capability(self) -> bool:
        """Return true only when the active artifact has the complete sync contract."""

        if self._dev_runtime_configured():
            return False
        pointer = self._active_pointer()
        if pointer is None:
            return False
        try:
            contract = _sync_contract_from_payload(pointer)
        except ValueError:
            return False
        binary = self._admitted_active_pointer_binary(pointer)
        return contract is not None and binary is not None and self._admit_sync_contract(binary, contract)

    def ensure(self, *, force: bool = False) -> dict[str, Any]:
        """Use an explicitly configured development runtime without installing archives."""

        if not self._dev_runtime_configured():
            return super().ensure(force=force)
        python = self._dev_runtime_python()
        if python is None:
            return {
                "ok": False,
                "reason": _DEV_RUNTIME_FAILURE_REASON,
                "download_error": None,
            }
        return {
            "ok": True,
            "changed": False,
            "path": str(python),
            "version": EVEROS_VERSION,
            "reason": None,
            "download_error": None,
        }

    def _dev_runtime_configured(self) -> bool:
        return _DEV_RUNTIME_ENV in os.environ

    def _dev_runtime_python(self) -> Path | None:
        """Validate the opt-in development interpreter without touching managed state."""

        configured = os.environ.get(_DEV_RUNTIME_ENV)
        if configured is None:
            self._dev_runtime_checked = False
            self._dev_runtime_checked_value = None
            self._dev_runtime_cached_python = None
            return None
        if self._dev_runtime_checked and configured == self._dev_runtime_checked_value:
            # Cache only SUCCESSFUL probes. A failed probe (cached_python is None
            # despite a prior check at this value) must retry on the next call so
            # that a developer who fixes/installs everos at the same path and hits
            # Repair sees it resolve without a restart or env-string change.
            if self._dev_runtime_cached_python is not None:
                return self._dev_runtime_cached_python
        self._dev_runtime_checked_value = configured
        self._dev_runtime_cached_python = None
        self._dev_runtime_warning_logged = False
        self._dev_runtime_failure_logged = None
        if not configured.strip():
            self._log_dev_runtime_failure("it must name a Python executable")
            return None
        try:
            # Do not resolve symlinks: a venv's ``bin/python`` often needs its
            # own path to discover ``pyvenv.cfg`` and its site-packages.
            python = Path(os.path.abspath(Path(configured).expanduser()))
            if not python.is_file() or not os.access(python, os.X_OK):
                self._log_dev_runtime_failure("the configured Python is not an executable file")
                return None
        except (OSError, RuntimeError, ValueError):
            self._log_dev_runtime_failure("the configured Python path is invalid")
            return None
        if not self._prepare_binary(python).get("ok"):
            self._log_dev_runtime_failure(
                f"the configured Python cannot import compatible everos {EVEROS_VERSION} and uvicorn"
            )
            return None
        self._dev_runtime_checked = True
        self._dev_runtime_cached_python = python
        if not self._dev_runtime_warning_logged:
            logger.warning(
                "DEV RUNTIME bypass active - not for production; using %s from %s",
                python,
                _DEV_RUNTIME_ENV,
            )
            self._dev_runtime_warning_logged = True
        return python

    def _dev_runtime_status(self, python: Path | None) -> dict[str, Any]:
        return {
            "id": self.spec.runtime_id,
            "provider": "development",
            "platform": runtime_platform_tag(),
            "installed": python is not None,
            "version": EVEROS_VERSION,
            "status": "ready" if python is not None else "error",
            "path": str(python) if python is not None else None,
            "install_dir": None,
            "manifest": {
                "everos_version": EVEROS_VERSION,
                "source": "development",
            },
            "archive": None,
            "reason": None if python is not None else _DEV_RUNTIME_FAILURE_REASON,
            "download_error": None,
        }

    def _log_dev_runtime_failure(self, detail: str) -> None:
        if self._dev_runtime_failure_logged == detail:
            return
        logger.error(
            "%s is configured but unusable; refusing DEV RUNTIME bypass: %s",
            _DEV_RUNTIME_ENV,
            detail,
        )
        self._dev_runtime_failure_logged = detail

    def _manifest_installable(self, manifest: ManagedRuntimeManifest) -> bool:
        if str(manifest.payload.get("release_state") or "published") != "published":
            self._install_reason = "memory_runtime_unpublished"
            return False
        if manifest.runtime_version != EVEROS_VERSION:
            self._install_reason = "memory_runtime_version_unsupported"
            return False
        if (
            manifest.payload.get("python_version") != EMBEDDED_PYTHON_VERSION
            or manifest.payload.get("lock_sha256") != PACKAGE_LOCK_SHA256
            or manifest.payload.get("lock_id") != f"uv-lock-sha256:{PACKAGE_LOCK_SHA256}"
            or manifest.payload.get("uv_version") != RUNTIME_BUILDER_UV_VERSION
        ):
            self._install_reason = "memory_runtime_manifest_invalid"
            return False
        if not _safe_metadata_value(manifest.payload.get("provider_root_format")):
            self._install_reason = "memory_runtime_manifest_invalid"
            return False
        compatible_formats = manifest.payload.get("compatible_provider_root_formats", [])
        if not isinstance(compatible_formats, list) or any(
            not _safe_metadata_value(value) for value in compatible_formats
        ):
            self._install_reason = "memory_runtime_manifest_invalid"
            return False
        try:
            _sync_contract_from_payload(manifest.payload)
        except ValueError:
            self._install_reason = "memory_runtime_manifest_invalid"
            return False
        return True

    def _prepare_binary_for_manifest(
        self,
        binary: Path,
        manifest: ManagedRuntimeManifest,
    ) -> dict[str, Any]:
        return self._prepare_binary(
            binary,
            sync_contract=_sync_contract_from_payload(manifest.payload),
        )

    def _reuse_existing_install(
        self,
        binary: Path,
        install_dir: Path,
        manifest: ManagedRuntimeManifest,
        archive: ManagedRuntimeArchive,
        *,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Re-admit and atomically activate an existing Memory runtime contract."""

        try:
            sync_contract = _sync_contract_from_payload(manifest.payload)
            preparation = self._prepare_binary(binary, sync_contract=sync_contract)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to admit existing Memory runtime binary")
            preparation = {"ok": False, "reason": _PREPARATION_FAILURE_REASON}
        if preparation.get("ok") is not True:
            return self._failure(
                str(preparation.get("reason") or _PREPARATION_FAILURE_REASON),
                manifest=manifest,
                archive=archive,
            )

        candidate = self._candidate_from_manifest(manifest)
        current, invalid = self._read_active_pointer()
        current_binary = (
            self._verified_active_pointer_binary(current)
            if not invalid and current is not None
            else None
        )
        current_contract = None
        if current is not None:
            try:
                current_contract = _sync_contract_from_payload(current)
            except ValueError:
                pass
        pointer_is_current = (
            current is not None
            and current_binary == binary
            and current.get("admission_revision") == ARTIFACT_ADMISSION_REVISION
            and current.get("admission_ok") is True
            and current.get("provider_root_format") == candidate.provider_root_format
            and isinstance(current.get("compatible_provider_root_formats"), list)
            and all(
                _safe_metadata_value(value)
                for value in current["compatible_provider_root_formats"]
            )
            and frozenset(
                {
                    current.get("provider_root_format"),
                    *current["compatible_provider_root_formats"],
                }
            )
            == candidate.compatible_provider_root_formats
            and current_contract == sync_contract
        )
        if not pointer_is_current:
            try:
                self._write_current_pointer(install_dir, manifest, archive)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Failed to refresh Memory runtime pointer")
                return self._failure(
                    getattr(exc, "reason", None) or self._reason("pointer_write_failed"),
                    manifest=manifest,
                    archive=archive,
                    message=str(exc),
                )
        payload = self._success_payload(binary, install_dir, manifest, archive, changed=False)
        if reason:
            payload["reason"] = reason
        return payload

    def _write_current_pointer(
        self,
        install_dir: Path,
        manifest: ManagedRuntimeManifest,
        archive: ManagedRuntimeArchive,
    ) -> None:
        """Activate a verified artifact only through the Memory lifecycle bridge."""

        candidate = self._candidate_from_manifest(manifest)
        try:
            root_state = self._provider_root.inspect(candidate)
        except ProviderRootError as error:
            repair_reason = _provider_root_failure_reason(error)
            if repair_reason is not None:
                raise MemoryRuntimeActivationError(
                    str(error),
                    reason=repair_reason,
                ) from error
            # A durable factory-reset fence may intentionally leave an old,
            # incompatible root in place until the retry deletes it. Let the
            # lifecycle coordinator decide whether pointer-only repair is safe;
            # ordinary activation still fails closed on ``None`` below.
            if self._activation_coordinator is None:
                raise MemoryRuntimeActivationError(str(error)) from error
            root_state = None
        previous_pointer = self._active_pointer()
        metadata_path = install_dir / self.spec.metadata_filename
        try:
            previous_metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            previous_metadata = None

        def commit() -> None:
            self._write_manifest_install_metadata(
                install_dir,
                manifest,
                archive,
                binary_sha256=archive.binary_sha256,
            )
            self._write_memory_current_pointer(install_dir, manifest, archive, candidate)

        def rollback() -> None:
            if previous_metadata is not None:
                write_json_atomic(metadata_path, previous_metadata)
            self._restore_current_pointer(previous_pointer)

        coordinator = self._activation_coordinator
        if coordinator is None:
            # With no live controller there cannot be a safe proof that an
            # existing root has no worker/sidecar using it. A fresh install has
            # no root and can safely establish its first pointer directly.
            if root_state.exists:
                raise MemoryRuntimeActivationError("memory runtime activation is unavailable")
            commit()
            return
        coordinator(candidate, root_state, commit, rollback)

    def _failure_for_install_exception(
        self,
        error: Exception,
        *,
        manifest: ManagedRuntimeManifest,
        archive: ManagedRuntimeArchive,
    ) -> dict[str, Any]:
        """Keep local-root incompatibility visible to Memory Wake."""

        reason = getattr(error, "reason", None)
        if isinstance(reason, str) and reason:
            return self._failure(
                reason,
                manifest=manifest,
                archive=archive,
                message=str(error),
            )
        return super()._failure_for_install_exception(
            error,
            manifest=manifest,
            archive=archive,
        )

    def _success_payload(
        self,
        binary: Path,
        install_dir: Path,
        manifest: ManagedRuntimeManifest,
        archive: ManagedRuntimeArchive,
        *,
        changed: bool,
    ) -> dict[str, Any]:
        payload = super()._success_payload(
            binary,
            install_dir,
            manifest,
            archive,
            changed=changed,
        )
        self._clear_latest_install_failure()
        return payload

    def _failure(
        self,
        reason: str,
        *,
        manifest: ManagedRuntimeManifest | None = None,
        archive: ManagedRuntimeArchive | None = None,
        message: str | None = None,
        skipped: bool = False,
    ) -> dict[str, Any]:
        payload = super()._failure(
            reason,
            manifest=manifest,
            archive=archive,
            message=message,
            skipped=skipped,
        )
        if not skipped:
            self._write_latest_install_failure(reason)
        return payload

    def _candidate_from_manifest(self, manifest: ManagedRuntimeManifest) -> MemoryArtifactCandidate:
        provider_root_format = manifest.payload.get("provider_root_format")
        compatible_values = manifest.payload.get("compatible_provider_root_formats", [])
        if not _safe_metadata_value(provider_root_format) or not isinstance(compatible_values, list):
            raise MemoryRuntimeActivationError("memory runtime manifest is invalid")
        compatible = {provider_root_format}
        compatible.update(value for value in compatible_values if _safe_metadata_value(value))
        return MemoryArtifactCandidate(
            provider_root_format=provider_root_format,
            compatible_provider_root_formats=frozenset(compatible),
            artifact_fingerprint=manifest.digest[:16],
        )

    def _active_pointer(self) -> dict[str, Any] | None:
        pointer, _invalid = self._read_active_pointer()
        return pointer

    def _read_active_pointer(self) -> tuple[dict[str, Any] | None, bool]:
        """Read the active pointer without treating an existing corrupt file as absent."""

        path = self.runtime_dir / "current.json"
        try:
            expected = path.lstat()
        except FileNotFoundError:
            return None, False
        except OSError:
            return None, True
        if not stat.S_ISREG(expected.st_mode) or expected.st_size > _MAX_CURRENT_POINTER_BYTES:
            return None, True

        try:
            ensure_private_directory(self.runtime_dir, self.runtime_dir)
            descriptor = open_and_harden_confined_regular_file(self.runtime_dir, path)
        except (ConfinedFilesystemError, OSError):
            return None, True
        try:
            actual = os.fstat(descriptor)
            if (
                not stat.S_ISREG(actual.st_mode)
                or actual.st_dev != expected.st_dev
                or actual.st_ino != expected.st_ino
                or actual.st_size > _MAX_CURRENT_POINTER_BYTES
            ):
                return None, True
            payload = os.read(descriptor, _MAX_CURRENT_POINTER_BYTES + 1)
        except OSError:
            return None, True
        finally:
            os.close(descriptor)
        if len(payload) > _MAX_CURRENT_POINTER_BYTES:
            return None, True
        try:
            pointer = json.loads(payload.decode("utf-8"))
        except (RecursionError, UnicodeError, ValueError):
            return None, True
        return (pointer, False) if isinstance(pointer, dict) else (None, True)

    def _verified_active_pointer_binary(self, pointer: dict[str, Any]) -> Path | None:
        """Apply Memory's build pin around shared installed-binary verification."""

        if (
            not _safe_metadata_value(pointer.get("runtime_version"))
            # Well-formed is not the same as usable here. Installation rejects a
            # manifest whose runtime_version is not EVEROS_VERSION, but the
            # pointer outlives that check: a ``~/.avibe`` copied between
            # architectures, or an Avibe upgrade that moves EVEROS_VERSION,
            # leaves an active pointer at an executable this build cannot run.
            # Accepting it makes ``resolve_python`` hand back that binary and
            # Dependencies report ready until the sidecar or processing probe
            # fails much later, with a far less obvious error.
            or pointer.get("runtime_version") != EVEROS_VERSION
        ):
            return None
        binary = super().resolve_binary()
        current, invalid = self._read_active_pointer()
        return binary if binary is not None and not invalid and current == pointer else None

    def _admitted_active_pointer_binary(
        self,
        pointer: dict[str, Any],
    ) -> Path | None:
        """Re-admit artifacts accepted under an older compatibility contract."""

        binary = self._verified_active_pointer_binary(pointer)
        if binary is None:
            return None
        revision = pointer.get("admission_revision")
        if type(revision) is int and revision == ARTIFACT_ADMISSION_REVISION:
            if pointer.get("admission_ok") is True:
                return binary
            self._install_reason = "memory_runtime_install_failed"
            return None
        try:
            file_lock = self._acquire_mutation_lock()
        except Exception:  # noqa: BLE001
            self._install_reason = "memory_runtime_install_failed"
            return None
        if file_lock is None:
            return None
        try:
            current, invalid = self._read_active_pointer()
            if invalid or current is None:
                return None
            binary = self._verified_active_pointer_binary(current)
            if binary is None:
                return None
            revision = current.get("admission_revision")
            if type(revision) is int and revision == ARTIFACT_ADMISSION_REVISION:
                if current.get("admission_ok") is True:
                    return binary
                self._install_reason = "memory_runtime_install_failed"
                return None
            sync_contract = _sync_contract_from_payload(current)
            preparation = self._prepare_binary(binary, sync_contract=sync_contract)
            admission_ok = preparation.get("ok") is True
            admitted_pointer = dict(current)
            admitted_pointer["admission_revision"] = ARTIFACT_ADMISSION_REVISION
            admitted_pointer["admission_ok"] = admission_ok
            self._restore_current_pointer(admitted_pointer)
            if not admission_ok:
                admission_reason = str(
                    preparation.get("reason") or _PREPARATION_FAILURE_REASON
                )
                self._install_reason = admission_reason
                self._write_latest_install_failure(admission_reason)
                return None
        except Exception:  # noqa: BLE001
            self._install_reason = "memory_runtime_install_failed"
            return None
        finally:
            self._release_mutation_lock(file_lock)
        self._install_reason = None
        return binary

    def _write_memory_current_pointer(
        self,
        install_dir: Path,
        manifest: ManagedRuntimeManifest,
        archive: ManagedRuntimeArchive,
        candidate: MemoryArtifactCandidate,
    ) -> None:
        _write_memory_state_atomic(
            self.runtime_dir,
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
                "admission_revision": ARTIFACT_ADMISSION_REVISION,
                "admission_ok": True,
                "provider_root_format": candidate.provider_root_format,
                "compatible_provider_root_formats": sorted(candidate.compatible_provider_root_formats - {candidate.provider_root_format}),
                "artifact_fingerprint": candidate.artifact_fingerprint,
                **_sync_contract_pointer_fields(manifest.payload),
            },
        )

    def _restore_current_pointer(self, pointer: dict[str, Any] | None) -> None:
        current = self.runtime_dir / "current.json"
        if pointer is None:
            try:
                remove_confined_path(self.runtime_dir, current)
            except FileNotFoundError:
                pass
            except ConfinedFilesystemError as error:
                raise MemoryRuntimeActivationError(
                    "memory runtime pointer could not be removed safely"
                ) from error
            return
        _write_memory_state_atomic(self.runtime_dir, current, pointer)

    def _binary_version(self, binary: Path | None) -> str | None:
        if binary is None or not binary.is_file():
            return None
        try:
            result = subprocess.run(
                [str(binary), "-I", "-c", "from importlib.metadata import version; print(version('everos'))"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
                **isolated_subprocess_kwargs(),
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0:
            return None
        version = result.stdout.strip()
        return version if version else None

    def _prepare_binary(
        self,
        binary: Path,
        *,
        sync_contract: tuple[int, tuple[str, ...], str, str] | None = None,
    ) -> dict[str, Any]:
        cold_admission = run_cold_artifact_admission(binary)
        logger.info(
            "Memory runtime cold import admission completed in %d ms (ok=%s, reason=%s)",
            cold_admission.duration_ms,
            cold_admission.ok,
            cold_admission.reason,
        )
        if not cold_admission.ok:
            return {
                "ok": False,
                "reason": cold_admission.reason or _PREPARATION_FAILURE_REASON,
            }
        scrubber_failure = self._admit_error_scrubbers(binary)
        if scrubber_failure is not None:
            return {"ok": False, "reason": scrubber_failure}
        if not self._admit_sync_contract(binary, sync_contract):
            return {"ok": False, "reason": _SYNC_ADMISSION_FAILURE_REASON}
        return {
            "ok": True,
            "everos_version": EVEROS_VERSION,
            "python_version": EMBEDDED_PYTHON_VERSION,
            "lock_sha256": PACKAGE_LOCK_SHA256,
        }

    @staticmethod
    def _admit_sync_contract(
        binary: Path,
        expected: tuple[int, tuple[str, ...], str, str] | None,
    ) -> bool:
        """Hash both packaged sync modules when the artifact carries the contract."""

        if expected is None:
            return True

        try:
            runtime_root = binary.resolve(strict=True).parent.parent
            candidates = tuple(runtime_root.glob("lib/python*/site-packages"))
            if len(candidates) != 1:
                return False
            site_packages = candidates[0]
            bootstrap = site_packages / "avibe_memory_sync_bootstrap.py"
            scrubbers = site_packages / "avibe_memory_sync_scrubbers.py"
            marker = site_packages / "avibe_memory_sync_bootstrap.pth"
            if not all(path.is_file() and not path.is_symlink() for path in (bootstrap, scrubbers, marker)):
                return False
            bootstrap_digest = file_sha256(bootstrap)
            scrubbers_digest = file_sha256(scrubbers)
            return (
                bootstrap_digest == expected[2]
                and scrubbers_digest == expected[3]
                and marker.read_text(encoding="ascii") == "import avibe_memory_sync_bootstrap\n"
            )
        except (OSError, UnicodeError):
            return False

    def _admit_error_scrubbers(self, binary: Path) -> str | None:
        """Prove the child can install mandatory diagnostic scrubbers before launch."""

        source_root = Path(__file__).resolve().parents[2]
        try:
            with tempfile.TemporaryDirectory(prefix="avibe-memory-admission-") as home:
                child_home = Path(home)
                result = subprocess.run(
                    [str(binary), "-c", _SCRUBBER_ADMISSION_SCRIPT],
                    capture_output=True,
                    text=True,
                    timeout=_SCRUBBER_ADMISSION_TIMEOUT_SECONDS,
                    check=False,
                    cwd=str(source_root),
                    env={
                        "HOME": str(child_home),
                        "PATH": os.defpath,
                        "PYTHONNOUSERSITE": "1",
                        "PYTHONPATH": str(source_root),
                        "XDG_CACHE_HOME": str(child_home / ".cache"),
                        "XDG_CONFIG_HOME": str(child_home / ".config"),
                        "XDG_DATA_HOME": str(child_home / ".local" / "share"),
                        "XDG_STATE_HOME": str(child_home / ".local" / "state"),
                    },
                    **isolated_subprocess_kwargs(),
                )
        except subprocess.TimeoutExpired:
            return _SCRUBBER_ADMISSION_TIMEOUT_REASON
        except (OSError, subprocess.SubprocessError):
            return _SCRUBBER_ADMISSION_FAILURE_REASON
        return None if result.returncode == 0 else _SCRUBBER_ADMISSION_FAILURE_REASON

    def _read_latest_install_failure(self) -> dict[str, str] | None:
        path = self.runtime_dir / _LATEST_INSTALL_FAILURE_FILENAME
        try:
            expected = path.lstat()
        except OSError:
            return None
        if (
            not stat.S_ISREG(expected.st_mode)
            or expected.st_size > _MAX_LATEST_INSTALL_FAILURE_BYTES
        ):
            return None
        try:
            ensure_private_directory(self.runtime_dir, self.runtime_dir)
            descriptor = open_and_harden_confined_regular_file(self.runtime_dir, path)
        except (ConfinedFilesystemError, OSError):
            return None
        try:
            actual = os.fstat(descriptor)
            if (
                not stat.S_ISREG(actual.st_mode)
                or actual.st_dev != expected.st_dev
                or actual.st_ino != expected.st_ino
                or actual.st_size > _MAX_LATEST_INSTALL_FAILURE_BYTES
            ):
                return None
            encoded = os.read(descriptor, _MAX_LATEST_INSTALL_FAILURE_BYTES + 1)
        except OSError:
            return None
        finally:
            os.close(descriptor)
        if len(encoded) > _MAX_LATEST_INSTALL_FAILURE_BYTES:
            return None
        try:
            payload = json.loads(encoded.decode("utf-8"))
        except (RecursionError, UnicodeError, ValueError):
            return None
        if (
            not isinstance(payload, dict)
            or payload.get("status") != "error"
            or not _safe_metadata_value(payload.get("reason"))
        ):
            return None
        return {"status": "error", "reason": payload["reason"]}

    def _write_latest_install_failure(self, reason: str) -> None:
        bounded_reason = reason if _safe_metadata_value(reason) else _PREPARATION_FAILURE_REASON
        try:
            _write_memory_state_atomic(
                self.runtime_dir,
                self.runtime_dir / _LATEST_INSTALL_FAILURE_FILENAME,
                {"status": "error", "reason": bounded_reason},
            )
        except MemoryRuntimeActivationError:
            logger.warning("Failed to persist Memory runtime install failure", exc_info=True)

    def _clear_latest_install_failure(self) -> None:
        try:
            remove_confined_path(
                self.runtime_dir,
                self.runtime_dir / _LATEST_INSTALL_FAILURE_FILENAME,
            )
        except FileNotFoundError:
            pass
        except (ConfinedFilesystemError, OSError):
            logger.warning("Failed to clear Memory runtime install failure", exc_info=True)


def _write_memory_state_atomic(
    runtime_root: Path,
    path: Path,
    payload: dict[str, Any],
) -> None:
    temporary = runtime_root / f".{path.name}.{secrets.token_hex(8)}.tmp"
    descriptor: int | None = None
    try:
        ensure_private_directory(runtime_root, runtime_root)
        descriptor = create_confined_file(runtime_root, temporary)
        encoded = (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")
        view = memoryview(encoded)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("memory runtime state write made no progress")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        replace_confined(runtime_root, temporary, path)
        fsync_directory(runtime_root)
    except (ConfinedFilesystemError, OSError) as error:
        raise MemoryRuntimeActivationError(
            "memory runtime state could not be written safely"
        ) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            remove_confined_path(runtime_root, temporary)
        except (ConfinedFilesystemError, OSError):
            pass


@runtime_checkable
class MemoryArtifactPort(Protocol):
    """What the runtime needs from the managed EverOS artifact, and nothing more.

    Eight members over ``MemoryArtifactManager``'s ~670 lines: downloads,
    checksums, safe extraction, and the ``current.json`` pointer stay behind this
    interface. Declaring it also removes the reason ``runtime.py`` had to reach
    for ``getattr(manager, "set_provider_root", None)`` — a partial fake can no
    longer satisfy the port.
    """

    def resolve_python(self) -> Path | None: ...

    def status(self) -> dict[str, Any]: ...

    def ensure(self, *, force: bool = False) -> dict[str, Any]: ...

    def provider_root_format(self) -> str | None: ...

    def artifact_fingerprint(self) -> str | None: ...

    def sync_capability(self) -> bool: ...

    def compatible_provider_root_formats(self) -> frozenset[str]: ...

    def set_provider_root(self, provider_root: Path | str) -> None: ...

    def set_activation_coordinator(self, coordinator: MemoryArtifactActivationCoordinator | None) -> None: ...


@dataclass
class FakeMemoryArtifactManager:
    """In-memory artifact fake for runtime contract tests.

    Satisfies ``MemoryArtifactPort`` without touching a manifest, an archive, or
    the filesystem. ``python`` is what ``resolve_python`` returns — set it to
    ``None`` to exercise the not-installed paths.
    """

    python: Path | None = None
    status_payload: dict[str, Any] = field(
        default_factory=lambda: {"installed": True, "status": "ready", "reason": None}
    )
    ensure_payload: dict[str, Any] = field(
        default_factory=lambda: {"ok": True, "changed": False, "reason": None, "download_error": None}
    )
    ensure_failure: BaseException | None = None
    root_format: str | None = f"everos-{EVEROS_VERSION}"
    fingerprint: str | None = f"fake-everos-{EVEROS_VERSION}"
    sync_available: bool = False
    compatible_formats: frozenset[str] = field(default_factory=lambda: frozenset({f"everos-{EVEROS_VERSION}"}))
    provider_root: Path | None = None
    activation_coordinator: MemoryArtifactActivationCoordinator | None = None
    ensure_calls: list[bool] = field(default_factory=list)

    def resolve_python(self) -> Path | None:
        return self.python

    def status(self) -> dict[str, Any]:
        return dict(self.status_payload)

    def ensure(self, *, force: bool = False) -> dict[str, Any]:
        self.ensure_calls.append(force)
        if self.ensure_failure is not None:
            raise self.ensure_failure
        return dict(self.ensure_payload)

    def provider_root_format(self) -> str | None:
        return self.root_format

    def artifact_fingerprint(self) -> str | None:
        return self.fingerprint

    def sync_capability(self) -> bool:
        return self.sync_available

    def compatible_provider_root_formats(self) -> frozenset[str]:
        return self.compatible_formats

    def set_provider_root(self, provider_root: Path | str) -> None:
        self.provider_root = Path(provider_root)

    def set_activation_coordinator(self, coordinator: MemoryArtifactActivationCoordinator | None) -> None:
        self.activation_coordinator = coordinator


_manager: MemoryArtifactManager | None = None


def get_memory_artifact_manager() -> MemoryArtifactManager:
    global _manager
    if _manager is None:
        _manager = MemoryArtifactManager()
    return _manager


def set_memory_artifact_manager_for_tests(manager: MemoryArtifactManager | None) -> None:
    global _manager
    _manager = manager


def _sync_contract_from_payload(
    payload: Mapping[str, Any],
) -> tuple[int, tuple[str, ...], str, str] | None:
    keys = (
        "sync_bootstrap_revision",
        "sync_argv",
        "sync_bootstrap_sha256",
        "sync_scrubbers_sha256",
    )
    values = tuple(payload.get(key) for key in keys)
    if all(value is None for value in values):
        return None
    revision, argv, bootstrap_digest, scrubbers_digest = values
    expected_argv = ("-I", "-m", "everos.entrypoints.cli.main", "cascade", "sync")
    if (
        revision != 1
        or not isinstance(argv, list)
        or tuple(argv) != expected_argv
        or not _valid_sha256(bootstrap_digest)
        or not _valid_sha256(scrubbers_digest)
    ):
        raise ValueError("Memory Runtime sync contract is incomplete or invalid")
    return revision, expected_argv, bootstrap_digest, scrubbers_digest


def _sync_contract_pointer_fields(payload: Mapping[str, Any]) -> dict[str, Any]:
    contract = _sync_contract_from_payload(payload)
    if contract is None:
        return {}
    revision, argv, bootstrap_digest, scrubbers_digest = contract
    return {
        "sync_bootstrap_revision": revision,
        "sync_argv": list(argv),
        "sync_bootstrap_sha256": bootstrap_digest,
        "sync_scrubbers_sha256": scrubbers_digest,
    }


def _valid_sha256(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(character in "0123456789abcdef" for character in value)
