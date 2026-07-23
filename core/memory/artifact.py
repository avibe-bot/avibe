"""Managed EverOS runtime specialization for Memory.

The shared manager owns manifest parsing, downloads, extraction, checksums, and
the active ``current.json`` pointer. This module adds only the pinned Python
identity and EverOS smoke checks needed by the Memory sidecar.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable, Iterable
from typing import Any

from config import paths
from core.managed_runtime import (
    ManagedRuntimeArchive,
    ManagedRuntimeManager,
    ManagedRuntimeManifest,
    ManagedRuntimeSpec,
    env_flag_enabled,
    write_json_atomic,
)
from core.process_isolation import isolated_subprocess_kwargs


EVEROS_VERSION = "1.1.3"
_MANIFEST_RESOURCE = "memory_runtime_manifest.json"
_SPEC = ManagedRuntimeSpec(
    runtime_id="memory-runtime",
    manifest_resource=_MANIFEST_RESOURCE,
    version_field="everos_version",
    default_bin_path="bin/python",
)
_SMOKE_SCRIPT = (
    "from importlib.metadata import version\n"
    "import everos\n"
    "import uvicorn\n"
    "assert version('everos') == '1.1.3'\n"
    "assert everos is not None and uvicorn is not None\n"
    "print(version('everos'))\n"
)


@dataclass(frozen=True)
class MemoryArtifactCandidate:
    """Verified metadata needed to atomically activate one runtime artifact."""

    provider_root_format: str
    compatible_provider_root_formats: frozenset[str]
    artifact_fingerprint: str


@dataclass(frozen=True)
class MemoryProviderRootState:
    """A bounded, non-secret snapshot used before an active-pointer cutover."""

    exists: bool
    provider_root_format: str | None = None
    empty: bool = False


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
        self._provider_root = Path(provider_root) if provider_root is not None else paths.get_vibe_remote_dir() / "memory" / "everos-root"
        self._activation_coordinator: MemoryArtifactActivationCoordinator | None = None

    def set_activation_coordinator(self, coordinator: MemoryArtifactActivationCoordinator | None) -> None:
        """Register the controller-owned lifecycle bridge for active cutovers."""

        self._activation_coordinator = coordinator

    def set_provider_root(self, provider_root: Path | str) -> None:
        """Bind activation compatibility checks to the controller's effective home."""

        self._provider_root = Path(provider_root)

    def resolve_python(self) -> Path | None:
        """Return a verified embedded Python without starting or downloading it."""

        return self.resolve_binary()

    def provider_root_format(self) -> str | None:
        pointer = self._active_pointer()
        value = pointer.get("provider_root_format") if pointer is not None else None
        return value if _safe_metadata_value(value) else None

    def compatible_provider_root_formats(self) -> frozenset[str]:
        """Return the active artifact's declared root formats, including itself."""

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
        pointer = self._active_pointer()
        value = pointer.get("artifact_fingerprint") if pointer is not None else None
        return value if _safe_metadata_value(value) else None

    def _manifest_installable(self, manifest: ManagedRuntimeManifest) -> bool:
        if str(manifest.payload.get("release_state") or "published") != "published":
            self._install_reason = "memory_runtime_unpublished"
            return False
        if manifest.runtime_version != EVEROS_VERSION:
            self._install_reason = "memory_runtime_version_unsupported"
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
        root_state = self._inspect_provider_root(candidate)
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

    def _inspect_provider_root(self, candidate: MemoryArtifactCandidate) -> MemoryProviderRootState:
        """Fail closed unless an existing root has an owned compatible sentinel."""

        try:
            root_info = self._provider_root.lstat()
        except FileNotFoundError:
            return MemoryProviderRootState(exists=False)
        if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
            raise MemoryRuntimeActivationError("memory provider root is unsafe")
        if hasattr(os, "getuid") and root_info.st_uid != os.getuid():
            raise MemoryRuntimeActivationError("memory provider root owner mismatch")
        if stat.S_IMODE(root_info.st_mode) != 0o700:
            raise MemoryRuntimeActivationError("memory provider root mode mismatch")

        sentinel_path = self._provider_root / ".avibe-memory-root.json"
        try:
            sentinel_info = sentinel_path.lstat()
        except FileNotFoundError as exc:
            raise MemoryRuntimeActivationError("memory provider root sentinel missing") from exc
        if stat.S_ISLNK(sentinel_info.st_mode) or not stat.S_ISREG(sentinel_info.st_mode):
            raise MemoryRuntimeActivationError("memory provider root sentinel is unsafe")
        if hasattr(os, "getuid") and sentinel_info.st_uid != os.getuid():
            raise MemoryRuntimeActivationError("memory provider root sentinel owner mismatch")
        if stat.S_IMODE(sentinel_info.st_mode) != 0o600 or sentinel_info.st_size > 4096:
            raise MemoryRuntimeActivationError("memory provider root sentinel is invalid")
        sentinel = _read_owned_root_sentinel(sentinel_path, sentinel_info)
        provider_root_format = sentinel["provider_root_format"]
        if not _safe_metadata_value(provider_root_format):
            raise MemoryRuntimeActivationError("memory provider root sentinel is invalid")
        if provider_root_format not in candidate.compatible_provider_root_formats:
            raise MemoryRuntimeActivationError("memory provider root format is incompatible")
        try:
            with os.scandir(self._provider_root) as entries:
                empty = all(entry.name == sentinel_path.name for entry in entries)
        except OSError as exc:
            raise MemoryRuntimeActivationError("memory provider root cannot be inspected") from exc
        if provider_root_format != candidate.provider_root_format and not empty:
            raise MemoryRuntimeActivationError("memory provider root format is incompatible")
        return MemoryProviderRootState(
            exists=True,
            provider_root_format=provider_root_format,
            empty=empty,
        )

    def _active_pointer(self) -> dict[str, Any] | None:
        try:
            pointer = json.loads((self.runtime_dir / "current.json").read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            return None
        return pointer if isinstance(pointer, dict) else None

    def _write_memory_current_pointer(
        self,
        install_dir: Path,
        manifest: ManagedRuntimeManifest,
        archive: ManagedRuntimeArchive,
        candidate: MemoryArtifactCandidate,
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
                "provider_root_format": candidate.provider_root_format,
                "compatible_provider_root_formats": sorted(candidate.compatible_provider_root_formats - {candidate.provider_root_format}),
                "artifact_fingerprint": candidate.artifact_fingerprint,
            },
        )

    def _restore_current_pointer(self, pointer: dict[str, Any] | None) -> None:
        current = self.runtime_dir / "current.json"
        if pointer is None:
            current.unlink(missing_ok=True)
            return
        write_json_atomic(current, pointer)

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
        if result.returncode != 0 or result.stdout.strip() != EVEROS_VERSION:
            return {"ok": False, "reason": "memory_runtime_smoke_failed"}
        return {"ok": True, "everos_version": EVEROS_VERSION}


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


def _read_owned_root_sentinel(path: Path, expected: os.stat_result) -> dict[str, Any]:
    """Read a bounded sentinel without following a replacement symlink."""

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise MemoryRuntimeActivationError("memory provider root sentinel is invalid") from exc
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_dev != expected.st_dev
            or info.st_ino != expected.st_ino
            or (hasattr(os, "getuid") and info.st_uid != os.getuid())
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size > 4096
        ):
            raise MemoryRuntimeActivationError("memory provider root sentinel is invalid")
        contents = os.read(descriptor, 4097)
    except OSError as exc:
        raise MemoryRuntimeActivationError("memory provider root sentinel is invalid") from exc
    finally:
        os.close(descriptor)
    if len(contents) > 4096:
        raise MemoryRuntimeActivationError("memory provider root sentinel is invalid")
    try:
        sentinel = json.loads(contents.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise MemoryRuntimeActivationError("memory provider root sentinel is invalid") from exc
    expected_keys = {
        "schema_version",
        "provider_root_id",
        "provider_id",
        "provider_root_format",
        "created_by_artifact_fingerprint",
    }
    if (
        not isinstance(sentinel, dict)
        or set(sentinel) != expected_keys
        or type(sentinel.get("schema_version")) is not int
        or sentinel.get("schema_version") != 1
        or sentinel.get("provider_id") != "everos"
        or not _safe_metadata_value(sentinel.get("provider_root_id"))
        or not _safe_metadata_value(sentinel.get("provider_root_format"))
        or not _safe_metadata_value(sentinel.get("created_by_artifact_fingerprint"))
    ):
        raise MemoryRuntimeActivationError("memory provider root sentinel is invalid")
    return sentinel
