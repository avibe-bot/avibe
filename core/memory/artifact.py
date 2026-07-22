"""Managed EverOS runtime specialization for Memory.

The shared manager owns manifest parsing, downloads, extraction, checksums, and
the active ``current.json`` pointer. This module adds only the pinned Python
identity and EverOS smoke checks needed by the Memory sidecar.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

from config import paths
from core.managed_runtime import (
    ManagedRuntimeManager,
    ManagedRuntimeManifest,
    ManagedRuntimeSpec,
    env_flag_enabled,
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


class MemoryArtifactManager(ManagedRuntimeManager):
    """Install and resolve the Avibe-pinned EverOS Python runtime."""

    def __init__(
        self,
        *,
        runtime_dir: Path | None = None,
        manifest_path: Path | str | None = None,
        manifest_url: str | None = None,
        offline: bool | None = None,
    ) -> None:
        manifest_path_value = manifest_path or os.environ.get("VIBE_MEMORY_MANIFEST_PATH")
        super().__init__(
            spec=_SPEC,
            runtime_dir=runtime_dir or paths.get_runtime_dir() / "memory",
            manifest_path=manifest_path_value,
            manifest_url=manifest_url if manifest_url is not None else os.environ.get("VIBE_MEMORY_MANIFEST_URL"),
            offline=env_flag_enabled("VIBE_MEMORY_OFFLINE") if offline is None else offline,
        )

    def resolve_python(self) -> Path | None:
        """Return a verified embedded Python without starting or downloading it."""

        return self.resolve_binary()

    def provider_root_format(self) -> str | None:
        manifest = self._load_manifest(allow_network=False)
        if manifest is None:
            return None
        value = manifest.payload.get("provider_root_format")
        return value if _safe_metadata_value(value) else None

    def artifact_fingerprint(self) -> str | None:
        manifest = self._load_manifest(allow_network=False)
        if manifest is None:
            return None
        return manifest.digest[:16]

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
        return True

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
