from __future__ import annotations

import hashlib
import importlib.resources as package_resources
import json
import logging
import os
import re
import subprocess
import threading
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, replace
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
from core.windows_arch import windows_native_arm64
from storage.lock import MigrationFileLock
from vibe.model_hub_runtime.environment import engine_subprocess_environment


_ENGINE_VERSION_RE = re.compile(r"CLIProxyAPI Version:\s*([\w.-]+)")


def _engine_platform_map(windows_on_arm: bool) -> dict[str, str]:
    """Python's platform tag -> the engine asset this host runs.

    x64 Python runs emulated on Windows on Arm (core/windows_arch.py), but the
    engine is a native program, so there it is still the arm64 build. The base
    manager reads this as its platform aliases, so every install, status and
    pointer check agrees on that choice.
    """
    return {
        "darwin-arm64": "darwin-arm64",
        "darwin-x64": "darwin-x64",
        "linux-x64": "linux-amd64",
        "linux-arm64": "linux-arm64",
        "win32-x64": "windows-arm64" if windows_on_arm else "windows-amd64",
        "win32-arm64": "windows-arm64",
    }


# A manifest override must not silently expand the engine's supported host set.
# Every asset any host runs, whichever host this is.
_ENGINE_ASSET_PLATFORMS = frozenset(
    {*_engine_platform_map(False).values(), *_engine_platform_map(True).values()}
)
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
    # Set per manager from the host it runs on; see _engine_platform_map.
    platform_aliases=(),
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
        self._platform_map = _engine_platform_map(windows_native_arm64())
        super().__init__(
            spec=replace(_ENGINE_SPEC, platform_aliases=tuple(self._platform_map.items())),
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
        if self.lifecycle_enabled:
            return self.stage_required(
                force=force, expected_target=expected_target, on_resolved=on_resolved,
                validate_candidate=validate_candidate,
            )
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

    @property
    def lifecycle_enabled(self) -> bool:
        return self.manifest_path is None and not self.manifest_url

    def bundled_manifest_bytes(self) -> bytes:
        return package_resources.files(self.spec.package).joinpath(self.spec.manifest_resource).read_bytes()

    def bundled_manifest(self) -> ManagedRuntimeManifest | None:
        if not self.lifecycle_enabled:
            return super()._load_manifest(allow_network=False)
        return self._parse_manifest(self.bundled_manifest_bytes(), loaded_from="package:bundled")

    def package_identity(self) -> str:
        from vibe import __version__

        return hashlib.sha256(__version__.encode() + b"\n" + self.bundled_manifest_bytes()).hexdigest()

    def required_generation(self):
        from vibe.model_hub_runtime.lifecycle import EngineGeneration, EngineJournal

        value = EngineJournal(self.runtime_dir).load()
        if value is None:
            return None
        # Identity/inspection is not execution authorization. A blocked or
        # package-stale C remains the required C, never an implicit bundled B.
        if value["active"] is not None:
            return EngineGeneration.read(value["active"], self.runtime_dir)
        return None

    def _load_manifest(self, *, allow_network: bool, persist_remote_cache: bool = True):
        if not self.lifecycle_enabled:
            return super()._load_manifest(allow_network=allow_network, persist_remote_cache=persist_remote_cache)
        from vibe.model_hub_runtime.journal import JournalError
        from vibe.model_hub_runtime.lifecycle import EngineJournal

        try:
            generation = self.required_generation()
            if generation is None:
                return self.bundled_manifest()
            raw = EngineJournal(self.runtime_dir).provenance(generation.manifest_sha256, kind="manifest")
            return self._parse_manifest(raw, loaded_from="lifecycle:committed")
        except (JournalError, OSError):
            self._install_reason = "engine_recovery_pending"
            return None

    def _manifest_install_candidates(self, manifest, archive):
        if self.lifecycle_enabled:
            generation = self.required_generation()
            if generation is not None:
                yield Path(generation.install_dir)
                return
        canonical = self._manifest_install_dir(manifest, archive)
        yield canonical
        # Forced repairs create immutable sibling generations, not in-place
        # replacement of a binary a running process may still be using.
        yield from sorted(canonical.parent.glob(f"{canonical.name}-*"))

    def resolve_binary(self) -> Path | None:
        if not self.lifecycle_enabled:
            return super().resolve_binary()
        manifest = self._load_manifest(allow_network=False)
        archive = self._manifest_archive_for_platform(manifest) if manifest else None
        if manifest is None or archive is None:
            return None
        for directory in self._manifest_install_candidates(manifest, archive):
            binary = self._verified_manifest_binary(directory, manifest, archive)
            if binary is not None:
                return binary
        return None

    def stage_required(self, **kwargs) -> dict[str, Any]:
        from vibe.model_hub_runtime.journal import JournalError
        from vibe.model_hub_runtime.lifecycle import EngineJournal

        try:
            generation = self.required_generation()
            raw = (
                EngineJournal(self.runtime_dir).provenance(generation.manifest_sha256, kind="manifest")
                if generation is not None else self.bundled_manifest_bytes()
            )
            return self.stage_manifest(raw, generation=generation, **kwargs)
        except (JournalError, OSError) as exc:
            return self._failure("engine_recovery_pending", message=str(exc))

    def stage_manifest(
        self, raw: bytes, *, generation=None, envelope: bytes | None = None,
        minimum: str | None = None, force: bool = False, expected_target=None,
        on_resolved=None, validate_candidate=None,
    ) -> dict[str, Any]:
        """Materialize exact verified files, not execution selection or its hint.

        Candidate admission/signature authorization belongs to the owner. The
        callback runs under the existing installer fence, closing cleanup races.
        """
        from vibe.model_hub_runtime.journal import JournalError, flush_directory, flush_file
        from vibe.model_hub_runtime.lifecycle import EngineGeneration, EngineJournal

        store = EngineJournal(self.runtime_dir)
        stager = _EngineStager(runtime_dir=self.runtime_dir, offline=self.offline, raw=raw)
        manifest = stager._load_manifest(allow_network=False)
        archive = stager._manifest_archive_for_platform(manifest) if manifest else None
        if manifest is None or archive is None:
            return self._failure(stager._install_reason or "engine_manifest_invalid")
        staged: EngineGeneration | None = None

        def persist_stage(binary: Path) -> str | None:
            nonlocal staged
            reason = validate_candidate(binary) if validate_candidate else None
            if reason:
                return reason
            value = store.load() or store.empty()
            digest = store.retain_bytes(raw, kind="manifest")
            envelope_digest = store.retain_bytes(envelope, kind="envelope") if envelope is not None else (
                generation.envelope_sha256 if generation is not None else None
            )
            origin = "channel" if envelope_digest is not None else "bundled"
            staged = EngineGeneration.read({
                "asset_release_tag": str(manifest.payload.get("asset_release_tag") or manifest.runtime_version),
                "runtime_version": manifest.runtime_version, "platform": archive.platform,
                "archive_sha256": archive.sha256, "binary_sha256": archive.binary_sha256,
                "manifest_sha256": digest, "install_dir": str(binary.parents[len(Path(archive.bin_path).parts) - 1]),
                "bin_path": archive.bin_path, "origin": origin,
                "min_avibe_version": minimum if minimum is not None else (
                    generation.min_avibe_version if generation is not None else None
                ), "envelope_sha256": envelope_digest,
            }, self.runtime_dir)
            if staged.document() in value["staged"] or staged.document() in (value["active"], value["previous"]):
                # These immutable bytes were flushed BEFORE their durable role
                # was published, and ensure has just reverified their integrity.
                # Reopening a running Windows image for writes is forbidden.
                return None
            install_dir = Path(staged.install_dir)
            # Flush both files and namespace entries before publishing the role.
            for item in install_dir.rglob("*"):
                if item.is_file():
                    flush_file(item)
            for directory in sorted((p for p in install_dir.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
                flush_directory(directory)
            directory = install_dir
            while directory != self.runtime_dir:
                flush_directory(directory)
                flush_directory(directory.parent)
                directory = directory.parent
            for cached in {
                self.runtime_dir / "downloads" / stager._archive_cache_name(archive),
                self.runtime_dir / "downloads" / archive.name,
            }:
                if managed_runtime._is_regular_file(cached) and stager._downloaded_archive_matches(cached, archive):
                    flush_file(cached)
                    flush_directory(cached.parent)
            if staged.document() not in value["staged"] and staged.document() not in (value["active"], value["previous"]):
                value["staged"].append(staged.document())
            store.save(value)
            return None

        result = ManagedRuntimeManager.ensure(
            stager, force=force, select=False, expected_target=expected_target,
            on_resolved=on_resolved, validate_candidate=persist_stage,
        )
        if result.get("ok") and staged is not None:
            result.update(state="staged", reason="waiting_for_controller", generation=staged.document())
        elif result.get("ok"):
            raise JournalError("engine_stage_not_recorded")
        return result

    def _current_install_dir(self, versions_dir: Path) -> Path | None:
        if not self.lifecycle_enabled:
            return super()._current_install_dir(versions_dir)
        generation = self.required_generation()
        if generation is not None:
            return Path(generation.install_dir)
        manifest = self.bundled_manifest()
        archive = self._manifest_archive_for_platform(manifest) if manifest else None
        if manifest and archive:
            for directory in self._manifest_install_candidates(manifest, archive):
                if self._verified_manifest_binary(directory, manifest, archive) is not None:
                    return directory
        return None

    def _current_archive_sha256(self) -> str:
        if not self.lifecycle_enabled:
            return super()._current_archive_sha256()
        manifest = self._load_manifest(allow_network=False)
        archive = self._manifest_archive_for_platform(manifest) if manifest else None
        return archive.sha256 if archive else ""

    def _clean_locked(self, *, keep_previous: int, dry_run: bool = False, removed=None):
        if self.lifecycle_enabled:
            from vibe.model_hub_runtime.lifecycle import EngineJournal

            # Damage must fail the entire cleanup plan closed, not look empty.
            EngineJournal(self.runtime_dir).load()
        return super()._clean_locked(keep_previous=keep_previous, dry_run=dry_run, removed=removed)

    def _install_dir_is_protected(self, install_dir: Path, protected: set[Path]) -> bool:
        if self.lifecycle_enabled:
            from vibe.model_hub_runtime.lifecycle import EngineJournal

            value = EngineJournal(self.runtime_dir).load()
            if value:
                roles = [value["active"], value["previous"], *value["staged"]]
                if value["pending"]:
                    roles += [value["pending"]["old"], value["pending"]["candidate"]]
                if value["launch"]:
                    roles.append(value["launch"]["generation"])
                if value["preflight"]:
                    roles.append(value["preflight"]["generation"])
                protected = protected | {Path(g["install_dir"]).resolve() for g in roles if g}
            bundled = self.bundled_manifest()
            archive = self._manifest_archive_for_platform(bundled) if bundled else None
            if bundled and archive:
                canonical = self._manifest_install_dir(bundled, archive)
                protected = protected | {canonical.resolve(), *(p.resolve() for p in canonical.parent.glob(f"{canonical.name}-*"))}
        return super()._install_dir_is_protected(install_dir, protected)

    def _clean_downloaded_archives(self, *, archive_provenance, protected_sha256s, dry_run):
        # Released metadata names the upstream archive. New caches key it by
        # digest too. Reuse the generic descriptor/hash-safe cleaner for BOTH
        # names without teaching other managed runtimes this engine's layout.
        aliases = {(name, digest): f"{digest}-{name}" for name, digest in archive_provenance}
        report, terminal = super()._clean_downloaded_archives(
            archive_provenance=archive_provenance | {(alias, digest) for (_name, digest), alias in aliases.items()},
            protected_sha256s=protected_sha256s, dry_run=dry_run,
        )
        for (name, _digest), alias in aliases.items():
            if alias not in terminal:
                # Keep ownership after install removal while its content-
                # addressed copy is young, retained, or failed removal.
                terminal.discard(name)
        return report, terminal

    def _resolve_manifest_archive(self, archive: ManagedRuntimeArchive) -> Path | None:
        # Keep released/preseeded offline caches usable without renaming or
        # overwriting their bytes. New downloads remain digest-addressed.
        cached = self.runtime_dir / "downloads" / self._archive_cache_name(archive)
        if managed_runtime._is_regular_file(cached) and self._downloaded_archive_matches(cached, archive):
            self._install_reason = None
            return cached
        legacy = self.runtime_dir / "downloads" / archive.name
        if managed_runtime._is_regular_file(legacy) and self._downloaded_archive_matches(legacy, archive):
            self._install_reason = None
            return legacy
        return super()._resolve_manifest_archive(archive)

    def _archive_cache_name(self, archive: ManagedRuntimeArchive) -> str:
        # The upstream name is reused between Avibe builds. Never overwrite a
        # rollback archive with another digest that happens to share that name.
        return f"{archive.sha256}-{archive.name}"

    def status(self) -> dict[str, Any]:
        if self.lifecycle_enabled:
            return self._lifecycle_install_status()
        managed = super().status()
        install_state = self.install_state()
        if install_state and install_state.get("state") == "not_installed":
            reason = install_state.get("reason")
            if isinstance(reason, str) and reason and not managed.get("reason"):
                managed["status"] = "error"
                managed["reason"] = reason
        return managed

    def _lifecycle_install_status(self) -> dict[str, Any]:
        from vibe.model_hub_runtime.lifecycle import EngineLifecycleView

        manifest = self._load_manifest(allow_network=False)
        archive = self._manifest_archive_for_platform(manifest) if manifest else None
        binary = self.resolve_binary() if archive else None
        return {
            "id": self.spec.runtime_id, "provider": self.spec.record_provider,
            "engine_lifecycle": EngineLifecycleView.read(self.runtime_dir),
            "platform": self.host_platform(), "installed": binary is not None,
            "version": manifest.runtime_version if binary and manifest else None,
            "selected_version": manifest.runtime_version if manifest else None,
            "matches_manifest": True if binary else None,
            "status": "ready" if binary else "error" if self._install_reason else "missing",
            "path": str(binary) if binary else None,
            "install_dir": str(binary.parents[len(Path(archive.bin_path).parts) - 1]) if binary and archive else None,
            "manifest": self._manifest_status_payload(manifest),
            "archive": self._archive_status_payload(archive),
            "reason": None if binary else self._install_reason,
            "os_errno": None if binary else self._install_failure.os_errno,
            "download_error": self._download_error,
        }

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

    def _normalize_engine_platform(self, platform_tag: str) -> str:
        return self._platform_map.get(platform_tag, platform_tag)

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
            "asset_release_tag": payload.get("asset_release_tag"),
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
        asset_platform = self._platform_map.get(managed_runtime.runtime_platform_tag())
        archive = manifest.archives.get(asset_platform) if asset_platform is not None else None
        if archive is None:
            return ManifestResolution.UNSUPPORTED, None
        return ManifestResolution.RESOLVED, archive

    def _binary_digest_matches_manifest(self, binary: Path, manifest: ManagedRuntimeManifest) -> bool:
        archive = self._manifest_archive_for_platform(manifest)
        return bool(archive and archive.binary_sha256 and managed_runtime.file_sha256(binary) == archive.binary_sha256)

    def _binary_matches_manifest(self, binary: Path, manifest: ManagedRuntimeManifest) -> bool:
        # Lifecycle inspection/staging authenticates bytes; only the owner may
        # execute them in isolated preflight or a fenced real-config start.
        if self.lifecycle_enabled:
            return self._binary_digest_matches_manifest(binary, manifest)
        return super()._binary_matches_manifest(binary, manifest)

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
                **isolated_subprocess_kwargs(console=False),
            )
        except Exception as exc:  # noqa: BLE001
            self._set_install_failure(self._reason("binary_not_runnable"), exc)
            return None
        match = _ENGINE_VERSION_RE.search(f"{result.stdout}\n{result.stderr}")
        if match is None:
            return None
        return f"v{match.group(1).lstrip('v')}"


class _EngineStager(EngineRuntimeManager):
    """An immutable exact-manifest byte installer; it has no selection authority."""

    def __init__(self, *, runtime_dir: Path, offline: bool, raw: bytes) -> None:
        self._raw = raw
        super().__init__(runtime_dir=runtime_dir, offline=offline)

    @property
    def lifecycle_enabled(self) -> bool:
        return False

    def _load_manifest(self, *, allow_network: bool, persist_remote_cache: bool = True):
        return self._parse_manifest(self._raw, loaded_from="lifecycle:staging")

    def _publish_staged_directory(self, staging_dir: Path, install_dir: Path) -> None:
        from vibe.model_hub_runtime.journal import publish_directory

        publish_directory(staging_dir, install_dir)

    def _write_manifest_install_metadata(self, install_dir, manifest, archive, *, binary_sha256):
        from vibe.model_hub_runtime.journal import write_durable

        super()._write_manifest_install_metadata(install_dir, manifest, archive, binary_sha256=binary_sha256)
        metadata = install_dir / self.spec.metadata_filename
        # The generic metadata writer is deliberately best-effort; the engine
        # stage has a stronger completion boundary, including native rename.
        write_durable(metadata, metadata.read_bytes())

    def _binary_matches_manifest(self, binary: Path, manifest: ManagedRuntimeManifest) -> bool:
        return self._binary_digest_matches_manifest(binary, manifest)

    def _current_install_dir(self, versions_dir: Path) -> Path | None:
        # A legacy pointer must never introduce a staging or retention root.
        return None
