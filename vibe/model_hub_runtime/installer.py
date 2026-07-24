from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any

from config import paths
from core import managed_runtime
from core.managed_runtime import (
    ManagedRuntimeArchive,
    ManagedRuntimeManager,
    ManagedRuntimeManifest,
    ManagedRuntimeSpec,
    env_flag_enabled,
)
from core.process_isolation import isolated_subprocess_kwargs
from vibe.model_hub_runtime.environment import engine_subprocess_environment


_ENGINE_VERSION_RE = re.compile(r"CLIProxyAPI Version:\s*([\w.-]+)")
# A manifest override must not silently expand the engine's supported host set.
_ENGINE_PLATFORM_MAP = {
    "darwin-arm64": "darwin-arm64",
    "darwin-x64": "darwin-x64",
    "linux-x64": "linux-amd64",
    "linux-arm64": "linux-arm64",
}
_ENGINE_SPEC = ManagedRuntimeSpec(
    runtime_id="model_hub_engine",
    manifest_resource="model_hub_runtime/cliproxyapi_manifest.json",
    version_field="version",
    default_bin_path="cli-proxy-api",
    archives_field="assets",
    archive_size_field="size_bytes",
)


class EngineRuntimeManager(ManagedRuntimeManager):
    """Install and verify the pinned CLIProxyAPI engine dependency."""

    def __init__(
        self,
        *,
        runtime_dir: Path | None = None,
        manifest_path: Path | str | None = None,
        manifest_url: str | None = None,
        offline: bool | None = None,
    ) -> None:
        super().__init__(
            spec=_ENGINE_SPEC,
            runtime_dir=runtime_dir or paths.get_runtime_dir() / "model-hub" / "engine",
            manifest_path=manifest_path or os.environ.get("VIBE_MODEL_HUB_ENGINE_MANIFEST_PATH"),
            manifest_url=(
                manifest_url if manifest_url is not None else os.environ.get("VIBE_MODEL_HUB_ENGINE_MANIFEST_URL")
            ),
            offline=(env_flag_enabled("VIBE_MODEL_HUB_ENGINE_OFFLINE") if offline is None else offline),
        )

    def resolve_engine_path(self) -> Path | None:
        return self.resolve_binary()

    def contract_manifest(self) -> dict[str, Any]:
        manifest = self._load_manifest(allow_network=False)
        if manifest is None:
            return {"name": "cliproxyapi", "version": "", "source_sha": "", "assets": []}
        payload = manifest.payload
        return {
            "name": str(payload.get("name") or ""),
            "version": manifest.runtime_version,
            "source_sha": str(payload.get("source_sha") or ""),
            "assets": [
                {
                    "platform": asset["platform"],
                    "url": asset["url"],
                    "size_bytes": asset["size_bytes"],
                    "sha256": asset["sha256"],
                }
                for asset in payload.get("assets", [])
            ],
        }

    def _manifest_installable(self, manifest: ManagedRuntimeManifest) -> bool:
        payload = manifest.payload
        if not (
            payload.get("name") == "cliproxyapi"
            and payload.get("release_tag") == manifest.runtime_version
            and payload.get("license") == "MIT"
            and re.fullmatch(r"[0-9a-f]{40}", str(payload.get("source_sha") or ""))
        ):
            self._install_reason = "model_hub_engine_manifest_invalid"
            return False
        return True

    def _manifest_archive_for_platform(
        self,
        manifest: ManagedRuntimeManifest,
    ) -> ManagedRuntimeArchive | None:
        asset_platform = _ENGINE_PLATFORM_MAP.get(managed_runtime.runtime_platform_tag())
        archive = manifest.archives.get(asset_platform) if asset_platform is not None else None
        if archive is None:
            self._install_reason = "model_hub_engine_platform_unsupported"
        return archive

    def _binary_version(self, binary: Path | None) -> str | None:
        if binary is None:
            return None
        try:
            result = subprocess.run(
                [str(binary), "--help"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
                env=engine_subprocess_environment(),
                **isolated_subprocess_kwargs(),
            )
        except Exception:  # noqa: BLE001
            return None
        match = _ENGINE_VERSION_RE.search(f"{result.stdout}\n{result.stderr}")
        if match is None:
            return None
        return f"v{match.group(1).lstrip('v')}"
