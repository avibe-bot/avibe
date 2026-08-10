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
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Callable, Iterable
from typing import Any, Protocol, runtime_checkable

from config import paths
from core.managed_runtime import (
    ManagedRuntimeArchive,
    ManagedRuntimeManager,
    ManagedRuntimeManifest,
    ManagedRuntimeSpec,
    archive_path_is_unsafe,
    env_flag_enabled,
    file_sha256,
    runtime_platform_tag,
)
from core.process_isolation import isolated_subprocess_kwargs
from core.memory.confined_filesystem import (
    ConfinedFilesystemError,
    create_confined_file,
    ensure_private_directory,
    fsync_directory,
    open_confined_regular_file,
    remove_confined_path,
    replace_confined,
)
from core.memory.provider_root import (
    PROVIDER_ROOT_CONTROL_FILES,
    ROOT_SENTINEL_FILENAME,
    ProviderRoot,
    ProviderRootError,
    ProviderRootMetadata,
    ProviderRootState,
)


EVEROS_VERSION = "1.2.3"
EMBEDDED_PYTHON_VERSION = "3.12.12"
PACKAGE_LOCK_SHA256 = "e6acc17e4c0969563d380326e90134965af0822259bb4a9adb4d54433e9737fe"
RUNTIME_BUILDER_UV_VERSION = "0.9.18"
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
_SMOKE_SCRIPT = (
    "from importlib.metadata import version\n"
    "import platform\n"
    "import everos\n"
    "import uvicorn\n"
    "from everos.entrypoints.api.app import create_app\n"
    f"assert version('everos') == '{EVEROS_VERSION}'\n"
    "assert platform.python_version() == '3.12.12'\n"
    "assert everos is not None and uvicorn is not None\n"
    "assert callable(create_app)\n"
    "print(version('everos'))\n"
    "print(platform.python_version())\n"
)


logger = logging.getLogger(__name__)


MemoryArtifactCandidate = ProviderRootMetadata
MemoryProviderRootState = ProviderRootState


class MemoryRuntimeActivationError(RuntimeError):
    """Closed failure raised before an unsafe Memory runtime pointer cutover."""


MemoryArtifactActivationCoordinator = Callable[
    [MemoryArtifactCandidate, MemoryProviderRootState, Callable[[], None], Callable[[], None]],
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
            return self._verified_active_pointer_binary(pointer)
        except Exception:  # noqa: BLE001
            return None

    def status(self) -> dict[str, Any]:
        """Keep the manifest's release-state reason visible to Dependencies."""

        if self._dev_runtime_configured():
            return self._dev_runtime_status(self._dev_runtime_python())
        pointer, pointer_invalid = self._read_active_pointer()
        if pointer is not None and self._verified_active_pointer_binary(pointer) is None:
            pointer_invalid = True
        status_payload = super().status()
        if pointer_invalid:
            status_payload.update(
                {
                    "installed": False,
                    "status": "error",
                    "path": None,
                    "reason": "memory_runtime_install_failed",
                }
            )
            return status_payload
        if status_payload.get("reason") is not None:
            return status_payload
        try:
            manifest = self._load_manifest(allow_network=False)
            if manifest is not None and not self._manifest_installable(manifest):
                status_payload["reason"] = self._install_reason
        except Exception:  # noqa: BLE001
            status_payload["reason"] = "memory_runtime_install_failed"
        return status_payload

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
        return True

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
            raise MemoryRuntimeActivationError(str(error)) from error
        previous_pointer = self._active_pointer()

        def commit() -> None:
            self._write_memory_current_pointer(install_dir, manifest, archive, candidate)

        def rollback() -> None:
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
            descriptor = open_confined_regular_file(self.runtime_dir, path)
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
        except (UnicodeError, ValueError):
            return None, True
        return (pointer, False) if isinstance(pointer, dict) else (None, True)

    def _verified_active_pointer_binary(self, pointer: dict[str, Any]) -> Path | None:
        """Verify the binary referenced by ``current.json`` without a manifest lookup."""

        install_dir_value = pointer.get("install_dir")
        bin_path = pointer.get("bin_path")
        if (
            pointer.get("provider") != "manifest"
            or pointer.get("runtime_id") != self.spec.runtime_id
            or not _safe_metadata_value(pointer.get("runtime_version"))
            or not _safe_metadata_value(pointer.get("platform"))
            # Well-formed is not the same as usable here. Installation rejects a
            # manifest whose runtime_version is not EVEROS_VERSION, but the
            # pointer outlives that check: a ``~/.avibe`` copied between
            # architectures, or an Avibe upgrade that moves EVEROS_VERSION,
            # leaves an active pointer at an executable this build cannot run.
            # Accepting it makes ``resolve_python`` hand back that binary and
            # Dependencies report ready until the sidecar or processing probe
            # fails much later, with a far less obvious error.
            or pointer.get("platform") != runtime_platform_tag()
            or pointer.get("runtime_version") != EVEROS_VERSION
            or not _valid_sha256(pointer.get("manifest_sha256"))
            or not _valid_sha256(pointer.get("archive_sha256"))
            or not isinstance(install_dir_value, str)
            or not isinstance(bin_path, str)
            or archive_path_is_unsafe(bin_path)
        ):
            return None

        configured_install_dir = Path(install_dir_value)
        if not configured_install_dir.is_absolute():
            return None
        try:
            install_dir = configured_install_dir.resolve(strict=True)
            versions_dir = (self.runtime_dir / "versions").resolve(strict=True)
            binary = (install_dir / bin_path).resolve(strict=True)
        except OSError:
            return None
        if install_dir == versions_dir or versions_dir not in install_dir.parents or install_dir not in binary.parents:
            return None
        if not binary.is_file() or not os.access(binary, os.X_OK):
            return None

        try:
            metadata = json.loads((install_dir / self.spec.metadata_filename).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            return None
        binary_sha256 = metadata.get("binary_sha256") if isinstance(metadata, dict) else None
        if not (
            isinstance(metadata, dict)
            and metadata.get("provider") == "manifest"
            and metadata.get("runtime_id") == self.spec.runtime_id
            and metadata.get("runtime_version") == pointer["runtime_version"]
            and metadata.get("platform") == pointer["platform"]
            and metadata.get("manifest_sha256") == pointer["manifest_sha256"]
            and metadata.get("archive_sha256") == pointer["archive_sha256"]
            and metadata.get("bin_path") == bin_path
            and _valid_sha256(binary_sha256)
            and file_sha256(binary) == binary_sha256
        ):
            return None
        return binary

    def _write_memory_current_pointer(
        self,
        install_dir: Path,
        manifest: ManagedRuntimeManifest,
        archive: ManagedRuntimeArchive,
        candidate: MemoryArtifactCandidate,
    ) -> None:
        _write_memory_pointer_atomic(
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
                "provider_root_format": candidate.provider_root_format,
                "compatible_provider_root_formats": sorted(candidate.compatible_provider_root_formats - {candidate.provider_root_format}),
                "artifact_fingerprint": candidate.artifact_fingerprint,
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
        _write_memory_pointer_atomic(self.runtime_dir, current, pointer)

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

    def _prepare_binary(self, binary: Path) -> dict[str, Any]:
        try:
            result = subprocess.run(
                [str(binary), "-I", "-c", _SMOKE_SCRIPT],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
                **isolated_subprocess_kwargs(),
            )
        except (OSError, subprocess.SubprocessError):
            return {"ok": False, "reason": "memory_runtime_smoke_failed"}
        if result.returncode != 0 or result.stdout.splitlines() != [EVEROS_VERSION, EMBEDDED_PYTHON_VERSION]:
            return {"ok": False, "reason": "memory_runtime_smoke_failed"}
        return {
            "ok": True,
            "everos_version": EVEROS_VERSION,
            "python_version": EMBEDDED_PYTHON_VERSION,
            "lock_sha256": PACKAGE_LOCK_SHA256,
        }


def _write_memory_pointer_atomic(
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
                raise OSError("memory runtime pointer write made no progress")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        replace_confined(runtime_root, temporary, path)
        fsync_directory(runtime_root)
    except (ConfinedFilesystemError, OSError) as error:
        raise MemoryRuntimeActivationError(
            "memory runtime pointer could not be written safely"
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


def _safe_metadata_value(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and len(value.encode("utf-8")) <= 128
        and all(character.isascii() and (character.isalnum() or character in {".", "-", "_"}) for character in value)
    )


def _valid_sha256(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(character in "0123456789abcdef" for character in value)
