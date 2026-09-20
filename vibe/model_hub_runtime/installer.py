from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable

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
from storage.lock import MigrationFileLock
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
_INSTALL_STATE_SCHEMA_VERSION = 3
_INSTALL_STATE_RELEASED_SCHEMA_VERSIONS = frozenset({1, 2})
_INSTALL_STATE_SUPPORTED_SCHEMA_VERSIONS = frozenset(
    {*_INSTALL_STATE_RELEASED_SCHEMA_VERSIONS, _INSTALL_STATE_SCHEMA_VERSION}
)
_INSTALL_FAILURE_KEY = "settings.models.install.fail.detail"
INSTALL_ALREADY_RUNNING_REASON = "model_hub_engine_install_already_running"
INSTALL_PLATFORM_UNSUPPORTED_REASON = "model_hub_engine_platform_unsupported"
INSTALL_CLAIM_INVALID_REASON = "model_hub_engine_install_claim_invalid"
INSTALL_RECOVERY_TIMEOUT_REASON = "model_hub_engine_install_lock_timeout"
INSTALL_RECOVERY_ABANDONED_REASON = "model_hub_engine_install_abandoned"
INSTALL_RECOVERY_SCHEDULE_FAILED_REASON = "model_hub_engine_install_schedule_failed"
INSTALL_INSPECTION_FAILED_REASON = "model_hub_engine_install_inspection_failed"
_DEPENDENCY_LIFECYCLE_FAILURE_REASONS = frozenset(
    {
        INSTALL_CLAIM_INVALID_REASON,
        INSTALL_RECOVERY_TIMEOUT_REASON,
        INSTALL_RECOVERY_ABANDONED_REASON,
        INSTALL_RECOVERY_SCHEDULE_FAILED_REASON,
        INSTALL_INSPECTION_FAILED_REASON,
    }
)
_INSTALL_GENERATION_RE = re.compile(r"^[0-9a-f]{32}$")
_INSTALL_TARGET_FIELDS = frozenset(
    {
        "runtime_version",
        "platform",
        "archive_sha256",
        "binary_sha256",
    }
)
_RELEASED_INSTALL_TARGET_FIELDS = frozenset({*_INSTALL_TARGET_FIELDS, "manifest_sha256"})
_INSTALL_STATE_UNSET = object()
_ENGINE_SPEC = ManagedRuntimeSpec(
    runtime_id="model_hub_engine",
    manifest_resource="model_hub_runtime/cliproxyapi_manifest.json",
    version_field="version",
    default_bin_path="cli-proxy-api",
    archives_field="assets",
    archive_size_field="size_bytes",
    platform_aliases=tuple(_ENGINE_PLATFORM_MAP.items()),
)


logger = logging.getLogger(__name__)


class InstallClaimTransition(str, Enum):
    CREATE = "create"
    RESUME = "resume"
    ADMISSION_FAILURE = "admission_failure"
    SETTLE_SUCCESS = "settle_success"
    SETTLE_FAILURE = "settle_failure"
    ABANDON = "abandon"


class ManifestResolution(str, Enum):
    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class _InstallStateOverride:
    generation: str
    payload: dict[str, Any] | None


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
        self._install_state_override: object | _InstallStateOverride = _INSTALL_STATE_UNSET

    def offline_copy(self) -> EngineRuntimeManager:
        """Return the same runtime target with network access disabled."""

        if self.offline:
            return self
        return EngineRuntimeManager(
            runtime_dir=self.runtime_dir,
            manifest_path=self.manifest_path,
            manifest_url=self.manifest_url,
            offline=True,
        )

    def ensure(
        self,
        *,
        force: bool = False,
        expected_target: Mapping[str, str] | None = None,
        on_resolved: Callable[[dict[str, str]], None] | None = None,
        validate_candidate: Callable[[Path], str | None] | None = None,
    ) -> dict[str, Any]:
        if on_resolved is not None or expected_target is not None:
            return super().ensure(
                force=force,
                expected_target=expected_target,
                on_resolved=on_resolved,
                validate_candidate=validate_candidate,
            )

        generation = uuid.uuid4().hex
        resolved_target: dict[str, str] | None = None
        previous_state = self.install_state()

        def capture_target(target: dict[str, str]) -> None:
            nonlocal resolved_target
            resolved_target = dict(target)

        result = super().ensure(
            force=force,
            expected_target=expected_target,
            on_resolved=capture_target,
            validate_candidate=validate_candidate,
        )
        reason = str(result.get("reason") or "model_hub_engine_install_failed")
        try:
            if result.get("ok") and previous_state is not None:
                self._clear_superseded_install_state(previous_state)
            elif not result.get("ok") and reason not in {
                INSTALL_ALREADY_RUNNING_REASON,
                INSTALL_PLATFORM_UNSUPPORTED_REASON,
            }:
                self.transition_install_claim(
                    InstallClaimTransition.ADMISSION_FAILURE,
                    generation=generation,
                    target=resolved_target,
                    reason=reason,
                )
        except Exception:  # noqa: BLE001
            logger.exception("Failed to persist direct Model Hub runtime ensure outcome")
        return result

    def _clear_superseded_install_state(self, expected: Mapping[str, Any]) -> bool:
        """Forget the exact pre-ensure state without disturbing a newer owner."""

        with self._install_state_lock:
            with MigrationFileLock(self.install_state_lock_path):
                current = self._read_install_state_file()
                if current != expected:
                    return False
                try:
                    self.install_state_path.unlink()
                except FileNotFoundError:
                    pass
                self._install_state_override = _INSTALL_STATE_UNSET
                return True

    def status(self) -> dict[str, Any]:
        managed = super().status()
        install_state = self.install_state()
        if install_state and install_state.get("state") == "not_installed":
            reason = install_state.get("reason")
            if isinstance(reason, str) and reason:
                managed["status"] = "error"
                managed["reason"] = reason
        return managed

    @property
    def install_state_path(self) -> Path:
        return self.runtime_dir / "install-state.json"

    @property
    def install_state_lock_path(self) -> Path:
        return self.runtime_dir / ".install-state.lock"

    def host_platform(self) -> str:
        return self._normalize_engine_platform(managed_runtime.runtime_platform_tag())

    def supports_host_platform(self) -> bool:
        """Return whether Avibe's fixed CPA target has an asset for this host."""

        if self.host_platform() not in _ENGINE_ASSET_PLATFORMS:
            return False
        manifest = self._load_manifest(allow_network=False)
        if manifest is None:
            return True
        resolution, _archive = self._resolve_manifest_state(manifest)
        return resolution is not ManifestResolution.UNSUPPORTED

    @staticmethod
    def _normalize_engine_platform(platform_tag: str) -> str:
        return _ENGINE_PLATFORM_MAP.get(platform_tag, platform_tag)

    def install_failure_reasons(self) -> frozenset[str]:
        """Return every admission failure emitted by the shared installer."""

        return self._base_install_failure_reasons()

    def dependency_failure_reasons(self) -> frozenset[str]:
        """Return every CPA failure token exposed by Dependencies."""

        return self.install_failure_reasons() | _DEPENDENCY_LIFECYCLE_FAILURE_REASONS

    def install_state(self) -> dict[str, Any] | None:
        with self._install_state_lock:
            durable = self._read_install_state_file()
            override = self._install_state_override
            if not isinstance(override, _InstallStateOverride):
                return durable
            durable_generation = self._installing_generation(durable)
            if durable is None or durable_generation == override.generation:
                return dict(override.payload) if override.payload is not None else None
            self._install_state_override = _INSTALL_STATE_UNSET
            return durable

    def transition_install_claim(
        self,
        transition: InstallClaimTransition,
        *,
        generation: str,
        target: Mapping[str, Any] | None = None,
        previous_generation: str | None = None,
        reason: str | None = None,
    ) -> bool:
        if not _INSTALL_GENERATION_RE.fullmatch(generation):
            raise ValueError("invalid Model Hub runtime install generation")
        resolved_target = self._validated_install_target(target)
        if transition in {InstallClaimTransition.CREATE, InstallClaimTransition.RESUME}:
            if resolved_target is None:
                raise ValueError("invalid Model Hub runtime install target")
        elif target is not None and resolved_target is None:
            raise ValueError("invalid Model Hub runtime install target")
        if transition in {
            InstallClaimTransition.ADMISSION_FAILURE,
            InstallClaimTransition.SETTLE_FAILURE,
            InstallClaimTransition.ABANDON,
        }:
            if not reason:
                raise ValueError("missing Model Hub runtime install failure reason")

        with self._install_state_lock:
            with MigrationFileLock(self.install_state_lock_path):
                current = self._read_install_state_file()
                current_generation = self._installing_generation(current)
                current_target = (
                    self._validated_install_target(current.get("target"))
                    if current is not None
                    else None
                )

                if transition is InstallClaimTransition.CREATE:
                    if current_generation is not None or (
                        current is not None and current.get("state") == "installing"
                    ):
                        return False
                    self._write_installing_claim(generation, resolved_target)
                    return True

                if transition is InstallClaimTransition.RESUME:
                    if (
                        current is None
                        or current.get("state") != "installing"
                        or current_generation != previous_generation
                        or current_target != resolved_target
                    ):
                        return False
                    self._write_installing_claim(generation, resolved_target)
                    return True

                if transition is InstallClaimTransition.ADMISSION_FAILURE:
                    if current is not None and current.get("state") == "installing":
                        return False
                    assert reason is not None
                    payload = self._failed_install_state(
                        generation=generation,
                        target=resolved_target,
                        reason=reason,
                    )
                    self._write_owned_failure(generation, payload)
                    return True

                if (
                    current is None
                    or current.get("state") != "installing"
                    or current_generation != generation
                    or (resolved_target is not None and current_target != resolved_target)
                ):
                    return False

                if transition is InstallClaimTransition.SETTLE_SUCCESS:
                    self._clear_owned_install_state(generation)
                    return True

                assert reason is not None
                payload = self._failed_install_state(
                    generation=generation,
                    target=current_target,
                    reason=reason,
                )
                self._write_owned_failure(generation, payload)
                return True

    def _read_install_state_file(self) -> dict[str, Any] | None:
        try:
            payload = json.loads(self.install_state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError, TypeError):
            logger.warning("Ignoring invalid Model Hub runtime install state")
            return None
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") not in _INSTALL_STATE_SUPPORTED_SCHEMA_VERSIONS
        ):
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
        target = self._validated_install_target(
            payload.get("target"),
            schema_version=int(payload["schema_version"]),
        )
        generation = payload.get("generation")
        if generation is not None and (
            not isinstance(generation, str) or not _INSTALL_GENERATION_RE.fullmatch(generation)
        ):
            logger.warning("Ignoring invalid Model Hub runtime install generation")
            return None
        if state == "installing" and target is None:
            logger.warning("Rejecting Model Hub runtime install state without a valid target")
            return self._failed_install_state(
                generation=generation,
                target=None,
                reason=INSTALL_CLAIM_INVALID_REASON,
            )
        payload["target"] = target
        payload["generation"] = generation
        return payload

    @staticmethod
    def _installing_generation(payload: Mapping[str, Any] | None) -> str | None:
        if payload is None or payload.get("state") != "installing":
            return None
        generation = payload.get("generation")
        return generation if isinstance(generation, str) else None

    def _write_installing_claim(self, generation: str, target: dict[str, str] | None) -> None:
        assert target is not None
        payload = {
            "schema_version": _INSTALL_STATE_SCHEMA_VERSION,
            "state": "installing",
            "generation": generation,
            "error_key": None,
            "target": target,
        }
        managed_runtime.write_json_atomic(self.install_state_path, payload)
        self._install_state_override = _INSTALL_STATE_UNSET

    def _write_owned_failure(self, generation: str, payload: dict[str, Any]) -> None:
        self._install_state_override = _InstallStateOverride(generation, payload)
        try:
            managed_runtime.write_json_atomic(self.install_state_path, payload)
        except Exception:
            try:
                self.install_state_path.unlink()
            except OSError:
                pass
            raise
        self._install_state_override = _INSTALL_STATE_UNSET

    def _clear_owned_install_state(self, generation: str) -> None:
        self._install_state_override = _InstallStateOverride(generation, None)
        try:
            self.install_state_path.unlink()
        except FileNotFoundError:
            self._install_state_override = _INSTALL_STATE_UNSET
        except Exception:
            raise
        else:
            self._install_state_override = _INSTALL_STATE_UNSET

    def _validated_install_target(
        self,
        value: object,
        *,
        schema_version: int = _INSTALL_STATE_SCHEMA_VERSION,
    ) -> dict[str, str] | None:
        expected_fields = (
            _RELEASED_INSTALL_TARGET_FIELDS
            if schema_version in _INSTALL_STATE_RELEASED_SCHEMA_VERSIONS
            else _INSTALL_TARGET_FIELDS
        )
        if not isinstance(value, Mapping) or set(value) != expected_fields:
            return None
        target: dict[str, str] = {}
        for field in _INSTALL_TARGET_FIELDS:
            item = value.get(field)
            if not isinstance(item, str) or not item:
                return None
            target[field] = item
        return self._normalized_install_target(target)

    def _normalized_install_target(self, target: Mapping[str, str]) -> dict[str, str]:
        normalized = super()._normalized_install_target(target)
        normalized["platform"] = self._normalize_engine_platform(normalized["platform"])
        return normalized

    @staticmethod
    def _failed_install_state(
        *,
        generation: str | None,
        target: dict[str, str] | None,
        reason: str,
    ) -> dict[str, Any]:
        return {
            "schema_version": _INSTALL_STATE_SCHEMA_VERSION,
            "state": "not_installed",
            "generation": generation,
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
            return {
                "name": "cliproxyapi",
                "resolution": ManifestResolution.UNRESOLVED.value,
                "assets": [],
            }
        resolution, _archive = self._resolve_manifest_state(manifest)
        if resolution is ManifestResolution.UNRESOLVED:
            return {
                "name": "cliproxyapi",
                "resolution": resolution.value,
                "assets": [],
            }
        payload = manifest.payload
        return {
            "name": str(payload.get("name") or ""),
            "resolution": resolution.value,
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
        resolution, _archive = self._resolve_manifest_state(manifest)
        if resolution is ManifestResolution.UNRESOLVED:
            self._install_reason = "model_hub_engine_manifest_invalid"
            return False
        return True

    def _manifest_archive_for_platform(
        self,
        manifest: ManagedRuntimeManifest,
    ) -> ManagedRuntimeArchive | None:
        resolution, archive = self._resolve_manifest_state(manifest)
        if resolution is ManifestResolution.UNRESOLVED:
            self._install_reason = "model_hub_engine_manifest_invalid"
            return None
        if resolution is ManifestResolution.UNSUPPORTED:
            self._install_reason = INSTALL_PLATFORM_UNSUPPORTED_REASON
            return None
        return archive

    def _resolve_manifest_state(
        self,
        manifest: ManagedRuntimeManifest,
    ) -> tuple[ManifestResolution, ManagedRuntimeArchive | None]:
        payload = manifest.payload
        if not (
            payload.get("name") == "cliproxyapi"
            and payload.get("release_tag") == manifest.runtime_version
            and payload.get("license") == "MIT"
            and re.fullmatch(r"[0-9a-f]{40}", str(payload.get("source_sha") or ""))
        ):
            return ManifestResolution.UNRESOLVED, None
        asset_platform = _ENGINE_PLATFORM_MAP.get(managed_runtime.runtime_platform_tag())
        archive = manifest.archives.get(asset_platform) if asset_platform is not None else None
        if archive is None:
            return ManifestResolution.UNSUPPORTED, None
        return ManifestResolution.RESOLVED, archive

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
