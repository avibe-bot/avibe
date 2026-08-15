from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
from collections.abc import Mapping
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
_ENGINE_ASSET_PLATFORMS = frozenset(_ENGINE_PLATFORM_MAP.values())
_INSTALL_STATE_SCHEMA_VERSION = 1
_INSTALL_FAILURE_KEY = "settings.models.install.fail.detail"
_INSTALL_CLAIM_INVALID_REASON = "model_hub_engine_install_claim_invalid"
_INSTALL_TARGET_FIELDS = frozenset(
    {
        "manifest_sha256",
        "runtime_version",
        "platform",
        "archive_sha256",
        "binary_sha256",
    }
)
_INSTALL_STATE_UNSET = object()
_ENGINE_SPEC = ManagedRuntimeSpec(
    runtime_id="model_hub_engine",
    manifest_resource="model_hub_runtime/cliproxyapi_manifest.json",
    version_field="version",
    default_bin_path="cli-proxy-api",
    archives_field="assets",
    archive_size_field="size_bytes",
)


logger = logging.getLogger(__name__)


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
        self._verified_binary_cache: tuple[tuple[object, ...], Path] | None = None
        self._install_state_lock = threading.RLock()
        self._install_state_override: object | dict[str, Any] | None = _INSTALL_STATE_UNSET

    @property
    def install_state_path(self) -> Path:
        return self.runtime_dir / "install-state.json"

    def host_platform(self) -> str:
        platform_tag = managed_runtime.runtime_platform_tag()
        return _ENGINE_PLATFORM_MAP.get(platform_tag, platform_tag)

    def install_failure_reasons(self) -> frozenset[str]:
        """Return every admission failure emitted by the shared installer."""

        return self._base_install_failure_reasons()

    def install_state(self) -> dict[str, Any] | None:
        with self._install_state_lock:
            if self._install_state_override is not _INSTALL_STATE_UNSET:
                override = self._install_state_override
                return dict(override) if isinstance(override, dict) else None
            try:
                payload = json.loads(self.install_state_path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                return None
            except (OSError, ValueError, TypeError):
                logger.warning("Ignoring invalid Model Hub runtime install state")
                return None
            if not isinstance(payload, dict) or payload.get("schema_version") != _INSTALL_STATE_SCHEMA_VERSION:
                logger.warning("Ignoring unsupported Model Hub runtime install state")
                return None
            state = payload.get("state")
            error_key = payload.get("error_key")
            if state not in {"installing", "not_installed"}:
                logger.warning("Ignoring invalid Model Hub runtime install state")
                return None
            if error_key not in {None, _INSTALL_FAILURE_KEY}:
                logger.warning("Ignoring invalid Model Hub runtime install state")
                return None
            if state == "installing" and error_key is not None:
                logger.warning("Ignoring contradictory Model Hub runtime install state")
                return None
            target = self._validated_install_target(payload.get("target"))
            if state == "installing" and target is None:
                logger.warning("Rejecting Model Hub runtime install state without a valid target")
                return self._failed_install_state(
                    target=None,
                    reason=_INSTALL_CLAIM_INVALID_REASON,
                )
            payload["target"] = target
            return payload

    def mark_installing(self, target: Mapping[str, Any]) -> None:
        resolved_target = self._validated_install_target(target)
        if resolved_target is None:
            raise ValueError("invalid Model Hub runtime install target")
        payload = {
            "schema_version": _INSTALL_STATE_SCHEMA_VERSION,
            "state": "installing",
            "error_key": None,
            "target": resolved_target,
        }
        with self._install_state_lock:
            managed_runtime.write_json_atomic(self.install_state_path, payload)
            self._install_state_override = payload

    def mark_install_failed(
        self,
        *,
        target: Mapping[str, Any] | None,
        reason: str,
    ) -> None:
        payload = self._failed_install_state(
            target=self._validated_install_target(target),
            reason=reason,
        )
        with self._install_state_lock:
            # Live projection settles before best-effort durable settlement so a
            # failed write cannot leave this process reporting a stale claim.
            self._install_state_override = payload
            try:
                managed_runtime.write_json_atomic(self.install_state_path, payload)
            except Exception:
                # If replacement is unavailable, removing the obsolete claim is
                # still preferable to replaying a terminal failure on restart.
                try:
                    self.install_state_path.unlink()
                except OSError:
                    pass
                raise

    def clear_install_state(self) -> None:
        with self._install_state_lock:
            self._install_state_override = None
            try:
                self.install_state_path.unlink()
            except FileNotFoundError:
                return

    @staticmethod
    def _validated_install_target(value: object) -> dict[str, str] | None:
        if not isinstance(value, Mapping) or set(value) != _INSTALL_TARGET_FIELDS:
            return None
        target: dict[str, str] = {}
        for field in _INSTALL_TARGET_FIELDS:
            item = value.get(field)
            if not isinstance(item, str) or not item:
                return None
            target[field] = item
        return target

    @staticmethod
    def _failed_install_state(
        *,
        target: dict[str, str] | None,
        reason: str,
    ) -> dict[str, Any]:
        return {
            "schema_version": _INSTALL_STATE_SCHEMA_VERSION,
            "state": "not_installed",
            "error_key": _INSTALL_FAILURE_KEY,
            "target": target,
            "reason": reason,
        }

    def resolve_engine_path(self) -> Path | None:
        return self.resolve_binary()

    def _verified_manifest_binary(
        self,
        install_dir: Path,
        manifest: ManagedRuntimeManifest,
        archive: ManagedRuntimeArchive,
    ) -> Path | None:
        binary = install_dir / archive.bin_path
        metadata = install_dir / self.spec.metadata_filename
        cache_key = self._verification_cache_key(binary, metadata, manifest, archive)
        cached = self._verified_binary_cache
        if cache_key is not None and cached is not None and cached[0] == cache_key:
            return cached[1]

        verified = super()._verified_manifest_binary(install_dir, manifest, archive)
        if verified is None:
            self._verified_binary_cache = None
            return None

        cache_key = self._verification_cache_key(verified, metadata, manifest, archive)
        self._verified_binary_cache = (cache_key, verified) if cache_key is not None else None
        return verified

    @staticmethod
    def _verification_cache_key(
        binary: Path,
        metadata: Path,
        manifest: ManagedRuntimeManifest,
        archive: ManagedRuntimeArchive,
    ) -> tuple[object, ...] | None:
        try:
            binary_stat = binary.stat()
            metadata_stat = metadata.stat()
        except OSError:
            return None

        return (
            manifest.digest,
            manifest.runtime_version,
            archive.platform,
            archive.sha256,
            archive.binary_sha256,
            archive.bin_path,
            binary_stat.st_dev,
            binary_stat.st_ino,
            binary_stat.st_mode,
            binary_stat.st_size,
            binary_stat.st_mtime_ns,
            binary_stat.st_ctime_ns,
            metadata_stat.st_dev,
            metadata_stat.st_ino,
            metadata_stat.st_size,
            metadata_stat.st_mtime_ns,
            metadata_stat.st_ctime_ns,
        )

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
                if asset.get("platform") in _ENGINE_ASSET_PLATFORMS
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
