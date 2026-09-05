from __future__ import annotations

import atexit
import asyncio
import contextlib
import errno
import fnmatch
import hashlib
import importlib.resources as package_resources
import json
import logging
import os
import random
import re
import shlex
import shutil
import signal
import stat
import subprocess
import tarfile
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import weakref
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import IO, Any, Callable, Iterable, Iterator, Mapping
from uuid import uuid4

import httpx

from storage.lock import (
    MigrationFileLock,
    MigrationLockTimeout,
    _try_lock as storage_lock_try_lock,
    fcntl_available,
    try_windows_exclusive_lock,
    unlock_windows_exclusive_lock,
)

from config import paths
from config.atomic_io import write_atomic
from core.dependency_network import dependency_error_details, fetch_to_path, probe_url, redact_url
from core.managed_runtime import (
    ManagedRuntimeArchive,
    ManagedRuntimeManager,
    ManagedRuntimeManifest,
    ManagedRuntimeSpec,
    env_flag_enabled,
    file_sha256,
    runtime_platform_tag,
    safe_extract_tar,
    safe_path_part,
)
from core.process_isolation import KILL_SIGNAL, isolated_subprocess_kwargs, signal_process_tree
from core.show_runtime_failures import (
    ShowRuntimeFailureClass,
    ShowRuntimeFailureDimension,
    ShowRuntimeFailureEvidence,
    ShowRuntimeRecoveryAction,
    classify_show_runtime_failure,
    show_runtime_recovery_action,
)
from core.show_runtime_source import retired_show_runtime_source


logger = logging.getLogger(__name__)
_RUNTIME_BIN = "avibe-show-runtime"
_RUNTIME_PACKAGE = "@avibe/show-runtime"
_RUNTIME_ARCHIVE_PREFIX = "vibe-show-runtime-node"
_RUNTIME_ARCHIVE_RELEASE_BASE_URL = "https://github.com/avibe-bot/vibe-show-runtime/releases/latest/download"
_RUNTIME_SOURCE_MANIFEST = "manifest-cache"
_RUNTIME_SOURCE_ARCHIVE = "archive"
_RUNTIME_SOURCE_NPM = "npm"
_WARNED_RETIRED_RUNTIME_SOURCES: set[str] = set()
_RUNTIME_MANIFEST_RESOURCE = "show_runtime_manifest.json"
_PACKAGED_RUNTIME_MANIFEST_SOURCE = f"package:{_RUNTIME_MANIFEST_RESOURCE}"
_CONTENT_ADDRESSED_ARCHIVE_RE = re.compile(r"^[0-9a-f]{64}\.tgz$")
_ABANDONED_ARCHIVE_CLAIM_RE = re.compile(r"^[0-9a-f]{64}\.tgz\.avibe-removing$")
# Cross-process safety window: between a download finalizing ``<sha256>.tgz``
# and the installing process writing install metadata / ``current.json``, the
# archive is not yet protected. Archives younger than this window are never
# pruned by automatic or manual cleanup; real stale archives are days old.
_ARCHIVE_MTIME_GUARD_SECONDS = 15 * 60
# Skip reasons for archive-cache cleanup; consumers (CLI, Doctor) key off
# ``skipped_reason`` generically instead of matching literal strings.
_SKIPPED_ARCHIVE_REASON_INSTALL_RUNNING = "runtime_install_already_running"
_SKIPPED_ARCHIVE_REASON_INSPECTION_FAILED = "archive_inspection_failed"
_SKIPPED_ARCHIVE_REASON_REMOVAL_FAILED = "archive_removal_failed"
# Every archive-cache report carries exactly one outcome from this set; the
# enumeration test in tests/test_show_runtime_archive_cleanup.py pins CLI and
# Doctor rendering to it, so a new outcome fails the test until wired.
_ARCHIVE_CLEANUP_OUTCOMES = frozenset({"cleaned", "partial", "skipped"})
_INSTALL_REFERENCE_RE = re.compile(r"^[0-9a-f]{32}\.lock$")
_INSTALL_REFERENCE_LOCKS: dict[tuple[str, str], "_ShowRuntimeInstallReference"] = {}
# Allocations inside the guard can collect a manager and run its finalizer
# synchronously on the same thread.
_INSTALL_REFERENCE_LOCKS_GUARD = threading.RLock()


class _ArchiveMetadataError(Exception):
    """A retained install's metadata could not be read; destructive cleanup must abort."""


class _ArchiveInspectionError(Exception):
    """The archive cache itself could not be inspected; cleanup must abort."""


@dataclass
class _ShowRuntimeInstallReference:
    marker: Path
    handle: IO[str]
    owners: set[str]


def _unlock_install_reference(handle: IO[str]) -> None:
    try:
        if os.name == "nt":
            handle.seek(0)
            unlock_windows_exclusive_lock(handle.fileno())
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def _release_install_reference_owner(owner: str) -> None:
    released: list[_ShowRuntimeInstallReference] = []
    with _INSTALL_REFERENCE_LOCKS_GUARD:
        for key, reference in list(_INSTALL_REFERENCE_LOCKS.items()):
            reference.owners.discard(owner)
            if reference.owners:
                continue
            released.append(reference)
            _INSTALL_REFERENCE_LOCKS.pop(key, None)
    for reference in released:
        try:
            _unlock_install_reference(reference.handle)
        finally:
            reference.marker.unlink(missing_ok=True)


def _is_exclusive_regular_file(info: os.stat_result) -> bool:
    """True only for a one-link regular file that is not a reparse point."""
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        return False
    attrs = getattr(info, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return not (reparse and attrs & reparse)


def _is_reparse_point(info: os.stat_result) -> bool:
    if stat.S_ISLNK(info.st_mode):
        return True
    attrs = getattr(info, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(reparse and attrs & reparse)
_FALSE_VALUES = {"0", "false", "no", "off"}
_PREWARM_IMPORT_RE = re.compile(r"""(?P<quote>["'])(?P<path>[^"']+)(?P=quote)""")
_PREWARM_MAX_ASSETS = 64
_PREWARM_MAX_DEPTH = 4
SHOW_RUNTIME_PROTOCOL_VERSION = 1
SHOW_RUNTIME_PROTOCOL_HEADER = "X-Avibe-Show-Protocol"
SHOW_RUNTIME_CONTEXT_HEADER = "X-Avibe-Show-Context"
SHOW_RUNTIME_BASE_HEADER = "x-vibe-show-base"
SHOW_RUNTIME_TARGET_HEADER = "x-vibe-show-target"
SHOW_RUNTIME_CONTEXT_KEY_FEATURE = "show-context-key-v1"
SHOW_RUNTIME_RENDER_MARKDOWN_CAPABILITY = "render_markdown_ssr"
# Phase 2.1 measured ~2 s cold against a 10 s Runtime module-load budget; this margin has no browser-install allowance.
SHOW_RUNTIME_REQUEST_TIMEOUT_SECONDS = 30.0
_CAPABILITY_RETRY_BASE_SECONDS = 0.25
_CAPABILITY_RETRY_MAX_SECONDS = 5.0
_CAPABILITY_RETRYABLE_STATUS_CODES = {408, 429}
_CAPABILITY_ENDPOINT_UNSUPPORTED = object()
SHOW_RUNTIME_CLI_FALLBACK_DELAY_SECONDS = 30
_STARTUP_READY_TIMEOUT_SECONDS = 10.0
_STARTUP_POLL_INTERVAL_SECONDS = 0.05
_STARTUP_URL_TIMEOUT_REASON = "runtime_start_url_timeout"
_STARTUP_PROCESS_UNAVAILABLE_REASON = "runtime_start_process_unavailable"
_STARTUP_HEALTH_TIMEOUT_REASON = "runtime_start_health_timeout"
_STARTUP_COMMAND_UNAVAILABLE_REASON = "runtime_start_command_unavailable"
_STARTUP_COMMAND_INVALID_REASON = "runtime_start_command_invalid"
_STARTUP_ATTEMPT_FAILED_REASON = "runtime_start_attempt_failed"
_MISSING = object()


class ShowRuntimeStartabilityState(str, Enum):
    STARTABLE = "startable"
    NOT_STARTABLE = "not_startable"
    UNDETERMINED = "undetermined"


@dataclass(frozen=True)
class ShowRuntimeStartability:
    state: ShowRuntimeStartabilityState
    reason: str | None = None
    detail: str | None = None

    def __post_init__(self) -> None:
        if self.state is ShowRuntimeStartabilityState.STARTABLE and (self.reason or self.detail):
            raise ValueError("startable outcome cannot carry failure evidence")
        if self.state is ShowRuntimeStartabilityState.NOT_STARTABLE and (
            not self.reason or self.detail
        ):
            raise ValueError("not-startable outcome requires only a reason")
        if self.state is ShowRuntimeStartabilityState.UNDETERMINED and (
            self.reason or not self.detail
        ):
            raise ValueError("undetermined outcome requires only a detail")

    @classmethod
    def startable(cls) -> "ShowRuntimeStartability":
        return cls(ShowRuntimeStartabilityState.STARTABLE)

    @classmethod
    def not_startable(cls, reason: str) -> "ShowRuntimeStartability":
        return cls(ShowRuntimeStartabilityState.NOT_STARTABLE, reason=reason)

    @classmethod
    def undetermined(cls, detail: str) -> "ShowRuntimeStartability":
        return cls(ShowRuntimeStartabilityState.UNDETERMINED, detail=detail)

    def as_payload(self) -> dict[str, str | None]:
        return {
            "state": self.state.value,
            "reason": self.reason,
            "detail": self.detail,
        }


class ShowRuntimeContext(str, Enum):
    PRIVATE = "private"
    SHARED = "shared"


class ShowRuntimeContextCapability(str, Enum):
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    TRANSIENT_UNKNOWN = "transient-unknown"


@dataclass(frozen=True)
class ShowRuntimeProtocolEnvelope:
    context: ShowRuntimeContext

    def __post_init__(self) -> None:
        if not isinstance(self.context, ShowRuntimeContext):
            raise TypeError("Show Runtime protocol 1 requires an explicit private or shared context")

    def headers(self, forwarded: Mapping[str, str] | None = None) -> dict[str, str]:
        blocked = {SHOW_RUNTIME_PROTOCOL_HEADER.lower(), SHOW_RUNTIME_CONTEXT_HEADER.lower()}
        headers = {
            key: value
            for key, value in (forwarded or {}).items()
            if key.lower() not in blocked
        }
        headers[SHOW_RUNTIME_PROTOCOL_HEADER] = str(SHOW_RUNTIME_PROTOCOL_VERSION)
        headers[SHOW_RUNTIME_CONTEXT_HEADER] = self.context.value
        return headers


@dataclass(frozen=True)
class ShowRuntimeWebSocketTarget:
    url: str
    headers: dict[str, str]
    _base_url: str | None = None
    _process: subprocess.Popen[str] | None = None


@dataclass(frozen=True)
class ShowRuntimeResult:
    available: bool
    base_url: str | None = None
    reason: str | None = None


class ShowRuntimeRequestTimeoutError(TimeoutError):
    """A proxied Runtime request exceeded its total request deadline."""


class ShowRuntimeUnavailableError(RuntimeError):
    def __init__(
        self,
        reason: str,
        failure_class: ShowRuntimeFailureClass,
        recovery_action: ShowRuntimeRecoveryAction,
    ):
        self.reason = reason
        self.failure_class = failure_class
        self.recovery_action = recovery_action
        super().__init__(reason)


class ShowRuntimePolicyState(str, Enum):
    ALLOWED = "allowed"
    SKIPPED = "skipped"


class ShowRuntimeInstallState(str, Enum):
    INSTALLED = "installed"
    ABSENT = "absent"
    FAILED = "failed"


class ShowRuntimeServingState(str, Enum):
    SERVING = "serving"
    UNCHECKED = "unchecked"
    START_FAILED = "start_failed"


class _ShowRuntimeOperationState(str, Enum):
    COMPLETED = "completed"
    FAILED = "failed"
    NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True)
class _ShowRuntimeOperationOutcome:
    state: _ShowRuntimeOperationState
    reason: str | None

    def __post_init__(self) -> None:
        if self.state is _ShowRuntimeOperationState.COMPLETED and self.reason:
            raise ValueError("completed operation cannot carry a failure reason")
        if self.state is not _ShowRuntimeOperationState.COMPLETED and not self.reason:
            raise ValueError("incomplete operation requires a reason")

    @property
    def ok(self) -> bool:
        return self.state is _ShowRuntimeOperationState.COMPLETED


@dataclass(frozen=True)
class _ManagedInstallAttempt:
    command: list[str] | None
    operation_reason: str | None = None

    def __post_init__(self) -> None:
        if self.command and self.operation_reason:
            raise ValueError("successful install attempt cannot carry an operation failure")
        if not self.command and not self.operation_reason:
            raise ValueError("failed install attempt requires an operation reason")


@dataclass(frozen=True)
class _ShowRuntimeDiskInstall:
    install_dir: Path
    command: list[str] | None
    metadata: Mapping[str, Any]


@dataclass(frozen=True)
class ShowRuntimeAvailability:
    policy: ShowRuntimePolicyState = ShowRuntimePolicyState.ALLOWED
    install: ShowRuntimeInstallState = ShowRuntimeInstallState.ABSENT
    runtime: ShowRuntimeServingState = ShowRuntimeServingState.UNCHECKED
    command: list[str] | None = None
    base_url: str | None = None
    policy_reason: str | None = None
    policy_failure_class: ShowRuntimeFailureClass | None = None
    policy_recovery_action: ShowRuntimeRecoveryAction | None = None
    install_reason: str | None = None
    install_failure_class: ShowRuntimeFailureClass | None = None
    install_recovery_action: ShowRuntimeRecoveryAction | None = None
    install_dir: str | None = None
    install_runtime_version: str | None = None
    install_matches_manifest: bool | None = None
    runtime_reason: str | None = None
    runtime_failure_class: ShowRuntimeFailureClass | None = None
    runtime_recovery_action: ShowRuntimeRecoveryAction | None = None

    def __post_init__(self) -> None:
        for dimension, reason, failure_class, recovery_action in (
            (
                "policy",
                self.policy_reason,
                self.policy_failure_class,
                self.policy_recovery_action,
            ),
            (
                "install",
                self.install_reason,
                self.install_failure_class,
                self.install_recovery_action,
            ),
            (
                "runtime",
                self.runtime_reason,
                self.runtime_failure_class,
                self.runtime_recovery_action,
            ),
        ):
            evidence = (failure_class, recovery_action)
            if reason is None and any(value is not None for value in evidence):
                raise ValueError(f"{dimension} recovery evidence requires a reason")
            if reason is not None and any(value is None for value in evidence):
                raise ValueError(f"{dimension} reason requires complete recovery evidence")

    @property
    def ok(self) -> bool:
        return self.install is ShowRuntimeInstallState.INSTALLED

    @property
    def available(self) -> bool:
        return self.runtime is ShowRuntimeServingState.SERVING and self.base_url is not None

    @property
    def reason(self) -> str | None:
        return self.runtime_reason or self.install_reason or self.policy_reason

    @property
    def failure_class(self) -> ShowRuntimeFailureClass | None:
        return self.runtime_failure_class or self.install_failure_class or self.policy_failure_class

    @property
    def recovery_action(self) -> ShowRuntimeRecoveryAction | None:
        return self.runtime_recovery_action or self.install_recovery_action or self.policy_recovery_action

    @classmethod
    def from_install(
        cls,
        *,
        command: list[str] | None = None,
        install: ShowRuntimeInstallState | None = None,
        policy_reason: str | None = None,
        install_reason: str | None = None,
        install_evidence: ShowRuntimeFailureEvidence | None = None,
        install_failure_class: ShowRuntimeFailureClass | None = None,
        install_recovery_action: ShowRuntimeRecoveryAction | None = None,
        install_dir: Path | str | None = None,
        install_runtime_version: str | None = None,
        install_matches_manifest: bool | None = None,
    ) -> "ShowRuntimeAvailability":
        if install is None:
            if command:
                install = ShowRuntimeInstallState.INSTALLED
            elif install_reason:
                install = ShowRuntimeInstallState.FAILED
            else:
                install = ShowRuntimeInstallState.ABSENT
        policy_evidence = ShowRuntimeFailureEvidence(
            ShowRuntimeFailureDimension.POLICY,
            policy_reason,
        )
        if install_evidence is None:
            install_evidence = ShowRuntimeFailureEvidence(
                ShowRuntimeFailureDimension.INSTALL,
                install_reason,
            )
        elif (
            install_evidence.dimension is not ShowRuntimeFailureDimension.INSTALL
            or install_evidence.reason != install_reason
        ):
            raise ValueError("install evidence must describe the published install reason")
        resolved_install_failure_class = install_failure_class or (
            classify_show_runtime_failure(install_evidence) if install is ShowRuntimeInstallState.FAILED else None
        )
        return cls(
            policy=(ShowRuntimePolicyState.SKIPPED if policy_reason else ShowRuntimePolicyState.ALLOWED),
            install=install,
            command=command,
            policy_reason=policy_reason,
            policy_failure_class=(classify_show_runtime_failure(policy_evidence) if policy_reason else None),
            policy_recovery_action=(show_runtime_recovery_action(policy_evidence) if policy_reason else None),
            install_reason=install_reason,
            install_failure_class=resolved_install_failure_class,
            install_recovery_action=(
                install_recovery_action or (show_runtime_recovery_action(install_evidence) if install_reason else None)
            ),
            install_dir=str(install_dir) if install_dir is not None else None,
            install_runtime_version=install_runtime_version,
            install_matches_manifest=install_matches_manifest,
        )

    def as_payload(self) -> dict[str, Any]:
        command = list(self.command) if self.command else None
        return {
            "policy": {
                "state": self.policy.value,
                "reason": self.policy_reason,
                "failure_class": self.policy_failure_class.value if self.policy_failure_class else None,
                "recovery_action": self.policy_recovery_action.value if self.policy_recovery_action else None,
            },
            "install": {
                "state": self.install.value,
                "reason": self.install_reason,
                "failure_class": self.install_failure_class.value if self.install_failure_class else None,
                "recovery_action": self.install_recovery_action.value if self.install_recovery_action else None,
                "command": command,
                "install_dir": self.install_dir,
                "runtime_version": self.install_runtime_version,
                "matches_manifest": self.install_matches_manifest,
            },
            "runtime": {
                "state": self.runtime.value,
                "reason": self.runtime_reason,
                "failure_class": self.runtime_failure_class.value if self.runtime_failure_class else None,
                "recovery_action": self.runtime_recovery_action.value if self.runtime_recovery_action else None,
                "base_url": self.base_url,
            },
            # Compatibility fields remain projections of the three dimensions.
            "ok": self.ok,
            "command": command,
            "reason": self.reason,
        }


_SHOW_MANAGED_RUNTIME_SPEC = ManagedRuntimeSpec(
    runtime_id="runtime",
    manifest_resource=_RUNTIME_MANIFEST_RESOURCE,
    version_field="runtime_version",
    default_bin_path="node_modules/@avibe/show-runtime/dist/cli.js",
    binary_artifact=False,
    record_provider=_RUNTIME_SOURCE_MANIFEST,
    metadata_filename_override=".vibe-show-runtime.json",
    allow_legacy_missing_runtime_id=True,
    staging_prefixes=("install-", "manifest-"),
)


class _ShowManifestRuntimeManager(ManagedRuntimeManager):
    """Show policy and released-record declarations over the shared installer."""

    def __init__(self, owner: "ShowRuntimeManager", *, offline: bool) -> None:
        self.owner = owner
        super().__init__(
            spec=_SHOW_MANAGED_RUNTIME_SPEC,
            runtime_dir=owner.runtime_dir,
            manifest_path=owner.manifest_path,
            manifest_url=owner.manifest_url,
            offline=offline,
        )

    def load_manifest(self, *, allow_network: bool) -> ManagedRuntimeManifest | None:
        return self._load_manifest(allow_network=allow_network)

    def archive_for_platform(
        self,
        manifest: ManagedRuntimeManifest,
    ) -> ManagedRuntimeArchive | None:
        return self._manifest_archive_for_platform(manifest)

    def verified_entrypoint(
        self,
        install_dir: Path,
        manifest: ManagedRuntimeManifest,
        archive: ManagedRuntimeArchive,
    ) -> Path | None:
        admitted = self._verified_manifest_binary(install_dir, manifest, archive)
        return self._project_entrypoint(install_dir) if admitted is not None else None

    def installed_result_entrypoint(self, result: Mapping[str, Any]) -> Path | None:
        if not result.get("ok") or not isinstance(result.get("path"), str):
            return None
        install_dir = result.get("install_dir")
        if not isinstance(install_dir, str):
            return None
        entrypoint = self._project_entrypoint(Path(install_dir))
        return entrypoint if entrypoint.is_file() else None

    def resolve_selected_entrypoint(self) -> Path | None:
        manifest = self._load_manifest(allow_network=False)
        if manifest is None or not self._manifest_installable(manifest):
            return None
        archive = self._manifest_archive_for_platform(manifest)
        if archive is None:
            return None
        for install_dir in self._manifest_install_candidates(manifest, archive):
            entrypoint = self._verified_manifest_binary(install_dir, manifest, archive)
            if entrypoint is not None:
                return self._project_entrypoint(install_dir)
        return None

    def _project_entrypoint(self, install_dir: Path) -> Path:
        return install_dir / self.spec.default_bin_path

    def install_candidates(
        self,
        manifest: ManagedRuntimeManifest,
        archive: ManagedRuntimeArchive,
    ) -> Iterator[Path]:
        return self._manifest_install_candidates(manifest, archive)

    def _manifest_installable(self, manifest: ManagedRuntimeManifest) -> bool:
        node = _resolve_node_command()
        if node is None:
            self._install_reason = "runtime_node_missing"
            return False
        minimum_node = self._minimum_node(manifest)
        if minimum_node and not _node_satisfies_requirement(_node_version(node), minimum_node):
            self._install_reason = "runtime_node_unsupported"
            return False
        return True

    def _parse_manifest(
        self,
        payload: bytes,
        *,
        loaded_from: str,
    ) -> ManagedRuntimeManifest | None:
        manifest = super()._parse_manifest(payload, loaded_from=loaded_from)
        if manifest is None:
            return None
        if "minimum_node" in manifest.payload and not isinstance(
            manifest.payload["minimum_node"],
            str,
        ):
            self._install_reason = "runtime_manifest_invalid"
            return None
        return manifest

    def _manifest_archive_for_platform(
        self,
        manifest: ManagedRuntimeManifest,
    ) -> ManagedRuntimeArchive | None:
        archive = super()._manifest_archive_for_platform(manifest)
        if archive is not None and archive.bin_path != self.spec.default_bin_path:
            self._install_reason = "runtime_manifest_invalid"
            return None
        return archive

    def _binary_matches_manifest(
        self,
        binary: Path,
        manifest: ManagedRuntimeManifest,
    ) -> bool:
        del binary, manifest
        return True

    def _manifest_identity_fields(self, manifest: ManagedRuntimeManifest) -> dict[str, str]:
        minimum_node = self._minimum_node(manifest)
        return {"minimum_node": minimum_node} if minimum_node else {}

    def _metadata_matches_install_target(
        self,
        metadata: Mapping[str, Any],
        target: Mapping[str, str],
    ) -> bool:
        if metadata.get("runtime_id") is None:
            target = {key: value for key, value in target.items() if key != "minimum_node"}
        return super()._metadata_matches_install_target(metadata, target)

    def _manifest_install_candidates(
        self,
        manifest: ManagedRuntimeManifest,
        archive: ManagedRuntimeArchive,
    ) -> Iterator[Path]:
        install_dir = self._manifest_install_dir(manifest, archive)
        legacy_parent = install_dir.parent
        yield install_dir
        yield legacy_parent

        try:
            candidates = sorted(legacy_parent.iterdir(), key=lambda path: path.name)
        except OSError:
            return
        for candidate in candidates:
            if candidate == install_dir:
                continue
            try:
                metadata = json.loads(
                    (candidate / self.spec.metadata_filename).read_text(encoding="utf-8")
                )
            except Exception:
                continue
            manifest_sha256 = metadata.get("manifest_sha256")
            if not isinstance(manifest_sha256, str) or not _CONTENT_ADDRESSED_ARCHIVE_RE.fullmatch(
                f"{manifest_sha256}.tgz"
            ):
                continue
            previous_fingerprint = hashlib.sha256(
                f"{manifest_sha256}:{archive.sha256}".encode("utf-8")
            ).hexdigest()[:16]
            if candidate.name == previous_fingerprint:
                yield candidate

    def _archive_cache_name(self, archive: ManagedRuntimeArchive) -> str:
        return f"{archive.sha256}.tgz"

    def _install_record_archive_name(
        self,
        metadata: Mapping[str, Any],
        archive_name: str,
        archive_sha256: str,
    ) -> str:
        del metadata, archive_name
        return f"{archive_sha256}.tgz"

    def _read_archive_provenance(self) -> set[tuple[str, str]]:
        provenance = super()._read_archive_provenance()
        downloads_dir = self.runtime_dir / "downloads"
        try:
            downloads_info = downloads_dir.lstat()
        except FileNotFoundError:
            return provenance
        except OSError as exc:
            raise OSError("downloads directory cannot be inspected") from exc
        if _is_reparse_point(downloads_info) or not stat.S_ISDIR(downloads_info.st_mode):
            raise OSError("downloads directory is not a confined directory")
        try:
            with os.scandir(downloads_dir) as entries:
                for entry in entries:
                    match = _CONTENT_ADDRESSED_ARCHIVE_RE.fullmatch(entry.name)
                    claim = _ABANDONED_ARCHIVE_CLAIM_RE.fullmatch(entry.name)
                    if match is None and claim is None:
                        continue
                    info = entry.stat(follow_symlinks=False)
                    if not _is_exclusive_regular_file(info):
                        continue
                    provenance.add((entry.name, entry.name[:64]))
        except OSError as exc:
            raise OSError("downloads directory cannot be inspected") from exc
        return provenance

    def _archive_candidate_sha256(self, name: str, fd: int) -> str:
        if _CONTENT_ADDRESSED_ARCHIVE_RE.fullmatch(name) or _ABANDONED_ARCHIVE_CLAIM_RE.fullmatch(name):
            return name[:64]
        return super()._archive_candidate_sha256(name, fd)

    def _clean_downloaded_archives(
        self,
        *,
        archive_provenance: set[tuple[str, str]],
        protected_sha256s: set[str],
        dry_run: bool,
    ) -> tuple[dict[str, Any], set[str]]:
        report, terminal_names = super()._clean_downloaded_archives(
            archive_provenance=archive_provenance,
            protected_sha256s=protected_sha256s,
            dry_run=dry_run,
        )
        report["protected_count"] = len(protected_sha256s)
        return report, terminal_names

    def _record_matches_configured_source(self, metadata: Mapping[str, Any]) -> bool:
        source = metadata.get("manifest_source")
        configured_source = self.owner._configured_manifest_source()
        return source == configured_source or (
            isinstance(source, str)
            and self.owner.manifest_url is not None
            and source == f"cache:{self.owner.manifest_url}"
        )

    def _record_install_dir_matches(
        self,
        install_dir: Path,
        metadata: Mapping[str, Any],
    ) -> bool:
        runtime_version = metadata.get("runtime_version")
        platform_tag = metadata.get("platform")
        archive_sha256 = metadata.get("archive_sha256")
        manifest_sha256 = metadata.get("manifest_sha256")
        if not all(
            isinstance(value, str) and value
            for value in (runtime_version, platform_tag, archive_sha256, manifest_sha256)
        ):
            return False
        if not all(
            _CONTENT_ADDRESSED_ARCHIVE_RE.fullmatch(f"{value}.tgz")
            for value in (archive_sha256, manifest_sha256)
        ):
            return False
        try:
            parts = install_dir.relative_to((self.runtime_dir / "versions").resolve()).parts
        except (OSError, ValueError):
            return False
        expected_prefix = (safe_path_part(runtime_version), safe_path_part(platform_tag))
        if parts[:2] != expected_prefix:
            return False
        if len(parts) == 2:
            return True
        if len(parts) != 3:
            return False
        current_fingerprint = hashlib.sha256(
            f"{runtime_version}:{platform_tag}:{archive_sha256}".encode("utf-8")
        ).hexdigest()[:16]
        previous_fingerprint = hashlib.sha256(
            f"{manifest_sha256}:{archive_sha256}".encode("utf-8")
        ).hexdigest()[:16]
        for fingerprint in (current_fingerprint, previous_fingerprint):
            if parts[2] == fingerprint:
                return True
            prefix = f"{fingerprint}-"
            suffix = parts[2][len(prefix) :] if parts[2].startswith(prefix) else ""
            if len(suffix) == 8 and all(
                character.isascii() and (character.isalnum() or character == "_")
                for character in suffix
            ):
                return True
        return False

    def _retention_ranked_installs(self, installs: list[Any], protected: set[Path]) -> list[Any]:
        resolved_by_path = {install.path: install.path.resolve() for install in installs}
        protected.update(
            resolved
            for resolved in resolved_by_path.values()
            if self.owner._install_dir_has_live_reference(resolved)
        )
        return [
            install
            for install in installs
            if not any(
                resolved_by_path[install.path] in other.parents
                for other in resolved_by_path.values()
                if other != resolved_by_path[install.path]
            )
            and not self._install_dir_is_protected(
                resolved_by_path[install.path],
                protected,
            )
        ]

    def _manifest_path_read_error_reason(self) -> str:
        return "runtime_manifest_invalid"

    def _reason(self, suffix: str) -> str:
        aliases = {
            "install_lock_failed": "runtime_install_guard_unavailable",
            "install_missing_binary": "runtime_install_missing_bin",
        }
        return aliases.get(suffix, f"runtime_{suffix}")

    @staticmethod
    def _minimum_node(manifest: ManagedRuntimeManifest) -> str | None:
        value = manifest.payload.get("minimum_node")
        return value if isinstance(value, str) and value else None


class ShowRuntimeManager:
    def __init__(
        self,
        *,
        command: str | None = None,
        workspace_root: Path | None = None,
        runtime_dir: Path | None = None,
        auto_install: bool | None = None,
        package_spec: str | None = None,
        runtime_source: str | None = None,
        archive_path: Path | str | None = None,
        archive_url: str | None = None,
        manifest_path: Path | str | None = None,
        manifest_url: str | None = None,
        offline: bool | None = None,
        force_install: bool = False,
    ) -> None:
        configured_command = command or os.environ.get("VIBE_SHOW_RUNTIME_BIN")
        self.command = configured_command or _RUNTIME_BIN
        self._command_explicit = configured_command is not None
        self.workspace_root = workspace_root or paths.get_show_pages_dir()
        self.runtime_dir = runtime_dir or paths.get_runtime_dir() / "show-runtime"
        archive_path_value = archive_path or os.environ.get("VIBE_SHOW_RUNTIME_ARCHIVE_PATH")
        self.archive_path = Path(archive_path_value).expanduser() if archive_path_value else None
        archive_url_env = os.environ.get("VIBE_SHOW_RUNTIME_ARCHIVE_URL")
        manifest_path_value = manifest_path or os.environ.get("VIBE_SHOW_RUNTIME_MANIFEST_PATH")
        self.manifest_path = Path(manifest_path_value).expanduser() if manifest_path_value else None
        self.manifest_url = manifest_url if manifest_url is not None else os.environ.get("VIBE_SHOW_RUNTIME_MANIFEST_URL")
        source_value = runtime_source or os.environ.get("VIBE_SHOW_RUNTIME_SOURCE")
        if source_value is None and (archive_path_value or archive_url is not None or archive_url_env):
            source_value = _RUNTIME_SOURCE_ARCHIVE
        self.auto_install = _auto_install_enabled() if auto_install is None else auto_install
        self.package_spec = package_spec or os.environ.get("VIBE_SHOW_RUNTIME_PACKAGE_SPEC") or _RUNTIME_PACKAGE
        self.runtime_source = _normalize_runtime_source(source_value)
        configured_archive_url = archive_url if archive_url is not None else archive_url_env
        self.archive_url = configured_archive_url if configured_archive_url is not None else _default_runtime_archive_url()
        self._archive_url_provenance = "configured" if configured_archive_url is not None else "packaged"
        self.offline = env_flag_enabled("VIBE_SHOW_RUNTIME_OFFLINE", default=False) if offline is None else offline
        self.force_install = force_install
        self.stdout_path = self.runtime_dir / "stdout.log"
        self.stderr_path = self.runtime_dir / "stderr.log"
        self.install_log_path = self.runtime_dir / "install.log"
        self.cache_root = self.runtime_dir / "vite-cache"
        self._install_evidence: ShowRuntimeFailureEvidence | None = None
        self._download_error: dict[str, Any] | None = None
        self._managed_command: list[str] | None = None
        self._availability = ShowRuntimeAvailability()
        self._process: subprocess.Popen[str] | None = None
        self._base_url: str | None = None
        self._lock = asyncio.Lock()
        self._capability_lock = asyncio.Lock()
        # Cross-process in-flight install guard for the archive cache: held for
        # the whole resolve-download-validate-extract window so a concurrent
        # ``runtime clean`` (or another process's post-install cleanup) cannot
        # unlink an archive this process has validated but not yet opened.
        # ``flock`` is not re-entrant across file handles, so the depth counter
        # lets the post-install cleanup reuse the installer's lock.
        self._install_guard = threading.RLock()
        self._install_guard_depth = 0
        self._install_guard_path = self.runtime_dir / ".install.lock"
        self._capability_identity: tuple[str, int | None] | None = None
        self._context_key_capability: ShowRuntimeContextCapability | None = None
        self._render_markdown_capability: bool | None = None
        self._render_markdown_retry_deadline = 0.0
        self._render_markdown_retry_attempt = 0
        self._capability_retry_deadline = 0.0
        self._capability_retry_attempt = 0
        self._capability_generation = 0
        self._install_reference_owner = uuid4().hex
        self._install_reference_finalizer = weakref.finalize(
            self,
            _release_install_reference_owner,
            self._install_reference_owner,
        )

    @property
    def _install_reason(self) -> str | None:
        return self._install_evidence.reason if self._install_evidence else None

    @_install_reason.setter
    def _install_reason(self, reason: str | None) -> None:
        self._install_evidence = (
            ShowRuntimeFailureEvidence(ShowRuntimeFailureDimension.INSTALL, reason)
            if reason
            else None
        )

    def _record_install_failure(
        self,
        reason: str,
        *,
        provenance: str | None = None,
        retryable: bool | None = None,
    ) -> None:
        self._install_evidence = ShowRuntimeFailureEvidence(
            ShowRuntimeFailureDimension.INSTALL,
            reason,
            provenance,
            retryable,
        )

    def _record_download_failure(
        self,
        reason: str,
        exc: BaseException,
        url: str,
        *,
        provenance: str,
    ) -> None:
        self._download_error = _runtime_download_error(exc, url)
        retryable = self._download_error.get("retryable")
        self._record_install_failure(
            reason,
            provenance=provenance,
            retryable=retryable if isinstance(retryable, bool) else None,
        )

    def _resolve_explicit_command_availability(self) -> ShowRuntimeAvailability:
        """Resolve the configured command without discarding failure evidence."""
        if not self._command_explicit:
            raise AssertionError("explicit command resolution requires an explicit command")
        try:
            command = _resolve_command(self.command)
        except (OSError, ValueError):
            evidence = ShowRuntimeFailureEvidence(
                ShowRuntimeFailureDimension.RUNTIME,
                _STARTUP_COMMAND_INVALID_REASON,
            )
            return ShowRuntimeAvailability(
                runtime=ShowRuntimeServingState.START_FAILED,
                runtime_reason=evidence.reason,
                runtime_failure_class=classify_show_runtime_failure(evidence),
                runtime_recovery_action=show_runtime_recovery_action(evidence),
            )
        return ShowRuntimeAvailability.from_install(
            command=command,
            install_reason=None if command else "runtime_command_missing",
        )

    def _publish_explicit_command_availability(
        self,
        availability: ShowRuntimeAvailability,
    ) -> ShowRuntimeAvailability:
        self._availability = availability
        return availability

    async def ensure(self, *, automatic: bool = True) -> ShowRuntimeAvailability:
        async with self._lock:
            return await self._admit_runtime_start(automatic=automatic)

    async def _admit_runtime_start(self, *, automatic: bool) -> ShowRuntimeAvailability:
        """Own one start admission through readiness publication."""
        availability: ShowRuntimeAvailability | None = None
        operation: _ShowRuntimeOperationOutcome | None = None
        base_url: str | None = None
        pending_exception: BaseException | None = None
        start_phase = "admission"
        try:
            availability = self._availability
            operation = _ShowRuntimeOperationOutcome(
                _ShowRuntimeOperationState.NOT_APPLICABLE,
                availability.reason or "runtime_unavailable",
            )
            if self._base_url:
                base_url = self._base_url
                operation = _ShowRuntimeOperationOutcome(
                    _ShowRuntimeOperationState.COMPLETED,
                    None,
                )
            else:
                self.stop()
                command: list[str] | None = None
                if self._command_explicit:
                    start_phase = "resolve-command"
                    availability = self._publish_explicit_command_availability(
                        self._resolve_explicit_command_availability()
                    )
                    command = availability.command
                    if availability.runtime_reason:
                        operation = _ShowRuntimeOperationOutcome(
                            _ShowRuntimeOperationState.FAILED,
                            availability.runtime_reason,
                        )
                else:
                    start_phase = "install"
                    availability = await self._resolve_managed_availability(automatic=automatic)
                    command = availability.command
                if command:
                    start_phase = "establish"
                    self.runtime_dir.mkdir(parents=True, exist_ok=True)
                    self.workspace_root.mkdir(parents=True, exist_ok=True)
                    self.cache_root.mkdir(parents=True, exist_ok=True)
                    # Reap any orphaned runtime server still bound to this workspace root before
                    # spawning ours, so there is a single writer (avibe#813). self.stop() above
                    # already released our own tracked child; anything left is a stray from a
                    # prior avibe instance that died without reaping it (SIGKILL / crash). Run it
                    # off the event loop: the psutil scan + terminate/kill can block for seconds.
                    await asyncio.to_thread(self._sweep_orphan_runtime_servers)
                    with (
                        self.stdout_path.open(
                            "w",
                            encoding="utf-8",
                        ) as stdout,
                        self.stderr_path.open("w", encoding="utf-8") as stderr,
                    ):
                        startup_deadline = asyncio.get_running_loop().time() + _STARTUP_READY_TIMEOUT_SECONDS
                        start_phase = "spawn"
                        self._process = subprocess.Popen(
                            [
                                *command,
                                "--workspace-root",
                                str(self.workspace_root),
                                "--cache-root",
                                str(self.cache_root),
                                "--host",
                                "127.0.0.1",
                                "--port",
                                "0",
                                "--fallback-delay-seconds",
                                str(SHOW_RUNTIME_CLI_FALLBACK_DELAY_SECONDS),
                            ],
                            stdout=stdout,
                            stderr=stderr,
                            text=True,
                            **isolated_subprocess_kwargs(),
                        )
                    start_phase = "readiness"
                    base_url = await self._read_startup_url(deadline=startup_deadline)
                    process = self._process
                    if process is None or process.poll() is not None:
                        operation = _ShowRuntimeOperationOutcome(
                            _ShowRuntimeOperationState.FAILED,
                            _STARTUP_PROCESS_UNAVAILABLE_REASON,
                        )
                    elif not base_url:
                        operation = _ShowRuntimeOperationOutcome(
                            _ShowRuntimeOperationState.FAILED,
                            _STARTUP_URL_TIMEOUT_REASON,
                        )
                    elif reason := await self._wait_for_startup_health(
                        base_url,
                        process,
                        deadline=startup_deadline,
                    ):
                        operation = _ShowRuntimeOperationOutcome(
                            _ShowRuntimeOperationState.FAILED,
                            reason,
                        )
                    else:
                        operation = _ShowRuntimeOperationOutcome(
                            _ShowRuntimeOperationState.COMPLETED,
                            None,
                        )
                elif operation.state is not _ShowRuntimeOperationState.FAILED:
                    operation = _ShowRuntimeOperationOutcome(
                        _ShowRuntimeOperationState.NOT_APPLICABLE,
                        availability.reason or "runtime_unavailable",
                    )
        except OSError as exc:
            reason = _STARTUP_ATTEMPT_FAILED_REASON
            if start_phase == "spawn" and exc.errno in {
                errno.EACCES,
                errno.ENOENT,
                errno.ENOEXEC,
            }:
                reason = (
                    _STARTUP_COMMAND_INVALID_REASON if self._command_explicit else _STARTUP_COMMAND_UNAVAILABLE_REASON
                )
            operation = _ShowRuntimeOperationOutcome(
                _ShowRuntimeOperationState.FAILED,
                reason,
            )
            logger.exception("Show Runtime start admission raised during %s", start_phase)
        except BaseException as exc:
            pending_exception = exc
        finally:
            if pending_exception is not None and self._process is not None:
                try:
                    self.stop()
                except OSError:
                    logger.warning("Show Runtime cancellation cleanup failed", exc_info=True)
            if availability is None or operation is None:
                availability = self._availability
                operation = _ShowRuntimeOperationOutcome(
                    _ShowRuntimeOperationState.NOT_APPLICABLE,
                    availability.reason or "runtime_unavailable",
                )
            published = self._complete_runtime_start_admission(
                availability,
                operation,
                base_url=base_url,
            )
        if pending_exception is not None:
            raise pending_exception
        return published

    async def request(
        self,
        method: str,
        path: str,
        *,
        envelope: ShowRuntimeProtocolEnvelope,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        base_path: str | None = None,
        render_target: str | None = None,
        timeout_seconds: float | None = None,
        automatic: bool = True,
    ) -> httpx.Response:
        base_url = self._base_url
        process = self._process
        if base_url is None:
            ready = await self.ensure(automatic=automatic)
            if not ready.available or not ready.base_url:
                raise self._unavailable_error(ready)
            base_url = ready.base_url
            process = self._process
        await self._negotiate_context_key_capability(base_url)
        reserved_headers = {
            SHOW_RUNTIME_BASE_HEADER.lower(),
            SHOW_RUNTIME_TARGET_HEADER.lower(),
        }
        request_headers = {
            key: value
            for key, value in envelope.headers(headers).items()
            if key.lower() not in reserved_headers
        }
        if base_path is not None:
            if (
                not base_path.startswith("/")
                or not base_path.endswith("/")
                or "\r" in base_path
                or "\n" in base_path
            ):
                raise ValueError("Show Runtime base path must be an absolute path ending in '/'")
            request_headers[SHOW_RUNTIME_BASE_HEADER] = base_path
        elif session_part := _show_runtime_app_session_part(path):
            request_headers[SHOW_RUNTIME_BASE_HEADER] = f"/show/{session_part}/"
        if render_target is not None:
            if not render_target.startswith("/") or "\r" in render_target or "\n" in render_target:
                raise ValueError("Show Runtime render target must be an absolute path")
            request_headers[SHOW_RUNTIME_TARGET_HEADER] = render_target
        phase_timeout_seconds = SHOW_RUNTIME_REQUEST_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds
        return await self._request_runtime_transport(
            method,
            base_url,
            path,
            process=process,
            headers=request_headers,
            body=body,
            phase_timeout_seconds=phase_timeout_seconds,
            total_timeout_seconds=timeout_seconds,
        )

    async def request_global(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
    ) -> httpx.Response:
        """Request a capability-independent Runtime resource without an app context."""
        base_url = self._base_url
        process = self._process
        if base_url is None:
            ready = await self.ensure()
            if not ready.available or not ready.base_url:
                raise self._unavailable_error(ready)
            base_url = ready.base_url
            process = self._process
        blocked = {
            SHOW_RUNTIME_PROTOCOL_HEADER.lower(),
            SHOW_RUNTIME_CONTEXT_HEADER.lower(),
            SHOW_RUNTIME_BASE_HEADER.lower(),
            SHOW_RUNTIME_TARGET_HEADER.lower(),
        }
        forwarded = {key: value for key, value in (headers or {}).items() if key.lower() not in blocked}
        return await self._request_runtime_transport(
            method,
            base_url,
            path,
            process=process,
            headers=forwarded,
            body=body,
            phase_timeout_seconds=30.0,
        )

    async def _request_runtime_transport(
        self,
        method: str,
        base_url: str,
        path: str,
        *,
        process: subprocess.Popen[str] | None,
        headers: dict[str, str],
        body: bytes | None,
        phase_timeout_seconds: float,
        total_timeout_seconds: float | None = None,
    ) -> httpx.Response:
        """Own transport failures and publish their recovery evidence."""
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(phase_timeout_seconds, connect=5.0)) as client:
                request = client.request(method, f"{base_url}{path}", headers=headers, content=body)
                if total_timeout_seconds is None:
                    return await request
                try:
                    return await asyncio.wait_for(request, timeout=total_timeout_seconds)
                except (asyncio.TimeoutError, httpx.ReadTimeout) as exc:
                    raise ShowRuntimeRequestTimeoutError(
                        f"Show Runtime request exceeded {total_timeout_seconds:g} seconds"
                    ) from exc
        except (ShowRuntimeRequestTimeoutError, httpx.RequestError) as exc:
            await self._invalidate_runtime_snapshot(base_url, process)
            if isinstance(exc, ShowRuntimeRequestTimeoutError):
                raise
            evidence = ShowRuntimeFailureEvidence(
                ShowRuntimeFailureDimension.RUNTIME,
                "runtime_proxy_failed",
            )
            raise ShowRuntimeUnavailableError(
                "runtime_proxy_failed",
                classify_show_runtime_failure(evidence),
                show_runtime_recovery_action(evidence),
            ) from exc

    async def prewarm_session(
        self,
        session_id: str,
        *,
        context: ShowRuntimeContext,
    ) -> ShowRuntimeResult:
        session_part = urllib.parse.quote(session_id, safe="")
        runtime_path = f"/sessions/{session_part}/app/"
        base_path = f"/show/{session_part}/"
        envelope = ShowRuntimeProtocolEnvelope(context)
        try:
            response = await self.request("GET", runtime_path, envelope=envelope)
            if response.status_code >= 500:
                return ShowRuntimeResult(False, reason=f"session_prewarm_failed:{response.status_code}")
            result = await self._prewarm_session_module_graph(
                session_id,
                runtime_path=runtime_path,
                envelope=envelope,
                seed_responses=[(runtime_path, response)],
                base_path=base_path,
            )
            if not result.available:
                return result
            return ShowRuntimeResult(True, self._base_url)
        except Exception as exc:
            return ShowRuntimeResult(False, reason=f"session_prewarm_failed:{exc}")

    async def _prewarm_session_module_graph(
        self,
        session_id: str,
        *,
        runtime_path: str,
        envelope: ShowRuntimeProtocolEnvelope,
        seed_responses: list[tuple[str, httpx.Response]],
        base_path: str | None,
    ) -> ShowRuntimeResult:
        pending: list[tuple[str, int]] = [(f"{runtime_path}src/main.tsx", 0)]
        visited: set[str] = {path for path, _response in seed_responses}
        for path, response in seed_responses:
            pending.extend(
                (import_path, 1)
                for import_path in _show_runtime_prewarm_import_paths(
                    response,
                    session_id=session_id,
                    runtime_path=runtime_path,
                    base_path=base_path,
                )
            )

        while pending and len(visited) < _PREWARM_MAX_ASSETS:
            path, depth = pending.pop(0)
            if path in visited or depth > _PREWARM_MAX_DEPTH:
                continue
            visited.add(path)
            response = await self.request("GET", path, envelope=envelope)
            if response.status_code >= 500:
                return ShowRuntimeResult(False, reason=f"session_prewarm_module_failed:{response.status_code}:{path}")
            if response.status_code >= 400:
                continue
            if depth >= _PREWARM_MAX_DEPTH:
                continue
            for import_path in _show_runtime_prewarm_import_paths(
                response,
                session_id=session_id,
                runtime_path=runtime_path,
                base_path=base_path,
            ):
                if import_path not in visited:
                    pending.append((import_path, depth + 1))
        return ShowRuntimeResult(True, self._base_url)

    async def websocket_target(
        self,
        path: str,
        *,
        envelope: ShowRuntimeProtocolEnvelope,
    ) -> ShowRuntimeWebSocketTarget:
        ready = await self.ensure()
        if not ready.available or not ready.base_url:
            raise self._unavailable_error(ready)
        base_url = ready.base_url
        process = self._process
        await self._negotiate_context_key_capability(base_url)
        url = f"{base_url.replace('http://', 'ws://', 1).replace('https://', 'wss://', 1)}{path}"
        return ShowRuntimeWebSocketTarget(
            url=url,
            headers=envelope.headers(),
            _base_url=base_url,
            _process=process,
        )

    async def invalidate_websocket_target(self, target: ShowRuntimeWebSocketTarget) -> None:
        if target._base_url is None:
            return
        await self._invalidate_runtime_snapshot(target._base_url, target._process)

    async def _invalidate_runtime_snapshot(
        self,
        base_url: str,
        process: subprocess.Popen[str] | None,
    ) -> None:
        async with self._lock:
            if self._base_url == base_url and self._process is process:
                self._base_url = None
                self._clear_capability_state()

    def _unavailable_error(self, availability: ShowRuntimeAvailability) -> ShowRuntimeUnavailableError:
        reason = availability.reason
        failure_class = availability.failure_class
        recovery_action = availability.recovery_action
        if reason is None or failure_class is None or recovery_action is None:
            raise AssertionError("unavailable Show Runtime must publish complete recovery evidence")
        return ShowRuntimeUnavailableError(
            reason,
            failure_class,
            recovery_action,
        )

    async def context_key_capability(self) -> ShowRuntimeContextCapability:
        ready = await self.ensure()
        if not ready.available or not ready.base_url:
            return ShowRuntimeContextCapability.TRANSIENT_UNKNOWN
        return await self._negotiate_context_key_capability(ready.base_url)

    async def supports_render_markdown(self, *, automatic: bool = True) -> bool:
        """Return whether this Runtime process implements Markdown rendering."""
        ready = await self.ensure(automatic=automatic)
        if not ready.available or not ready.base_url:
            raise RuntimeError(ready.reason or "show runtime unavailable")
        identity = self._runtime_identity(ready.base_url)
        async with self._capability_lock:
            if identity != self._capability_identity:
                self._clear_capability_state(identity=identity)
            if self._render_markdown_capability is not None:
                return self._render_markdown_capability
            if time.monotonic() < self._render_markdown_retry_deadline:
                raise RuntimeError("show runtime capability probe temporarily unavailable")

            generation = self._capability_generation
            outcome = await self._probe_render_markdown_capability(ready.base_url)
            if generation != self._capability_generation or identity != self._runtime_identity(ready.base_url):
                raise RuntimeError("show runtime changed during capability probe")
            if outcome is None:
                self._render_markdown_retry_attempt += 1
                self._render_markdown_retry_deadline = (
                    time.monotonic()
                    + _show_runtime_capability_retry_delay(self._render_markdown_retry_attempt)
                )
                raise RuntimeError("show runtime capability probe unavailable")
            self._render_markdown_capability = outcome
            self._render_markdown_retry_attempt = 0
            self._render_markdown_retry_deadline = 0.0
            return outcome

    async def _negotiate_context_key_capability(
        self,
        base_url: str,
    ) -> ShowRuntimeContextCapability:
        async with self._capability_lock:
            identity = self._runtime_identity(base_url)
            if identity != self._capability_identity:
                self._clear_capability_state(identity=identity)

            if self._context_key_capability in {
                ShowRuntimeContextCapability.SUPPORTED,
                ShowRuntimeContextCapability.UNSUPPORTED,
            }:
                return self._context_key_capability

            now = time.monotonic()
            if (
                self._context_key_capability is ShowRuntimeContextCapability.TRANSIENT_UNKNOWN
                and now < self._capability_retry_deadline
            ):
                return ShowRuntimeContextCapability.TRANSIENT_UNKNOWN

            generation = self._capability_generation
            outcome = await self._probe_context_key_capability(base_url)
            if generation != self._capability_generation or identity != self._runtime_identity(base_url):
                return ShowRuntimeContextCapability.TRANSIENT_UNKNOWN

            self._context_key_capability = outcome
            if outcome is ShowRuntimeContextCapability.TRANSIENT_UNKNOWN:
                self._capability_retry_attempt += 1
                self._capability_retry_deadline = time.monotonic() + _show_runtime_capability_retry_delay(
                    self._capability_retry_attempt
                )
            else:
                self._capability_retry_attempt = 0
                self._capability_retry_deadline = 0.0
            return outcome

    async def _probe_context_key_capability(self, base_url: str) -> ShowRuntimeContextCapability:
        payload = await self._probe_capabilities_payload(base_url)
        if payload is _CAPABILITY_ENDPOINT_UNSUPPORTED:
            return ShowRuntimeContextCapability.UNSUPPORTED
        if not isinstance(payload, dict):
            return ShowRuntimeContextCapability.TRANSIENT_UNKNOWN

        protocol = payload.get("protocol", _MISSING)
        features = payload.get("features", _MISSING)
        if protocol is not _MISSING and (isinstance(protocol, bool) or not isinstance(protocol, int)):
            return ShowRuntimeContextCapability.TRANSIENT_UNKNOWN
        if features is not _MISSING and (
            not isinstance(features, list) or any(not isinstance(feature, str) for feature in features)
        ):
            return ShowRuntimeContextCapability.TRANSIENT_UNKNOWN
        if protocol == SHOW_RUNTIME_PROTOCOL_VERSION and SHOW_RUNTIME_CONTEXT_KEY_FEATURE in (
            features if isinstance(features, list) else []
        ):
            return ShowRuntimeContextCapability.SUPPORTED
        return ShowRuntimeContextCapability.UNSUPPORTED

    async def _probe_capabilities_payload(self, base_url: str) -> dict[str, Any] | object | None:
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(2.0, connect=0.5)) as client:
                response = await client.get(f"{base_url}/capabilities")
        except (httpx.TimeoutException, httpx.TransportError):
            return None

        if response.status_code == 404:
            return _CAPABILITY_ENDPOINT_UNSUPPORTED
        if response.status_code in _CAPABILITY_RETRYABLE_STATUS_CODES or response.status_code >= 500:
            return None
        if not 200 <= response.status_code < 300:
            return None
        try:
            payload = response.json()
        except (UnicodeDecodeError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        return payload

    async def _probe_render_markdown_capability(self, base_url: str) -> bool | None:
        payload = await self._probe_capabilities_payload(base_url)
        if payload is _CAPABILITY_ENDPOINT_UNSUPPORTED:
            return False
        if not isinstance(payload, dict):
            return None

        protocol = payload.get("protocol", _MISSING)
        capability = payload.get(SHOW_RUNTIME_RENDER_MARKDOWN_CAPABILITY, _MISSING)
        return (
            isinstance(protocol, int)
            and not isinstance(protocol, bool)
            and protocol == SHOW_RUNTIME_PROTOCOL_VERSION
            and capability is True
        )

    def _runtime_identity(self, base_url: str) -> tuple[str, int | None]:
        process = self._process
        return base_url, getattr(process, "pid", None) if process is not None else None

    def _clear_capability_state(self, *, identity: tuple[str, int | None] | None = None) -> None:
        self._capability_identity = identity
        self._context_key_capability = None
        self._render_markdown_capability = None
        self._render_markdown_retry_deadline = 0.0
        self._render_markdown_retry_attempt = 0
        self._capability_retry_deadline = 0.0
        self._capability_retry_attempt = 0
        self._capability_generation += 1

    async def _healthy(self, base_url: str) -> bool:
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(2.0, connect=0.5)) as client:
                response = await client.get(f"{base_url}/health")
            return response.status_code == 200
        except Exception:
            return False

    async def _read_startup_url(self, *, deadline: float) -> str | None:
        loop = asyncio.get_running_loop()
        while loop.time() < deadline:
            process = self._process
            if process is None or process.poll() is not None:
                return None
            try:
                text = self.stdout_path.read_text(encoding="utf-8")
            except FileNotFoundError:
                text = ""
            for line in reversed(text.splitlines()):
                marker = "Vibe Show Runtime listening at "
                if marker in line:
                    return line.split(marker, 1)[1].strip()
            await asyncio.sleep(min(_STARTUP_POLL_INTERVAL_SECONDS, max(0.0, deadline - loop.time())))
        return None

    async def _wait_for_startup_health(
        self,
        base_url: str,
        process: subprocess.Popen[str],
        *,
        deadline: float,
    ) -> str | None:
        loop = asyncio.get_running_loop()
        while True:
            if process.poll() is not None:
                return _STARTUP_PROCESS_UNAVAILABLE_REASON
            remaining = deadline - loop.time()
            if remaining <= 0:
                return _STARTUP_HEALTH_TIMEOUT_REASON
            try:
                healthy = await asyncio.wait_for(self._healthy(base_url), timeout=remaining)
            except asyncio.TimeoutError:
                return _STARTUP_HEALTH_TIMEOUT_REASON
            if process.poll() is not None:
                return _STARTUP_PROCESS_UNAVAILABLE_REASON
            if healthy:
                return None
            remaining = deadline - loop.time()
            if remaining <= 0:
                return _STARTUP_HEALTH_TIMEOUT_REASON
            await asyncio.sleep(min(_STARTUP_POLL_INTERVAL_SECONDS, remaining))

    def stop(self) -> None:
        process = self._process
        self._process = None
        self._base_url = None
        self._clear_capability_state()
        if not process or process.poll() is not None:
            return
        signal_process_tree(process, signal.SIGTERM, logger, "show runtime")
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            signal_process_tree(process, KILL_SIGNAL, logger, "show runtime")

    def _sweep_orphan_runtime_servers(self) -> None:
        """Best-effort reap of stray runtime servers bound to our workspace root."""
        keep_pid = self._process.pid if self._process else None
        try:
            sweep_orphan_show_runtime_servers(self.workspace_root, keep_pid=keep_pid)
        except Exception:  # pragma: no cover - defensive; sweeping must never block spawn
            logger.debug("Orphan show runtime sweep skipped", exc_info=True)

    async def _resolve_managed_availability(
        self,
        *,
        automatic: bool = True,
    ) -> ShowRuntimeAvailability:
        if self._command_explicit:
            return self._publish_explicit_command_availability(
                self._resolve_explicit_command_availability()
            )
        if not self.force_install and self._managed_command:
            return self._publish_install_availability(command=self._managed_command)
        if (
            not self.force_install
            and self.runtime_source != _RUNTIME_SOURCE_ARCHIVE
            and not (self.runtime_source == _RUNTIME_SOURCE_MANIFEST and self.manifest_url)
        ):
            command = await asyncio.to_thread(
                self._safe_installed_managed_runtime_command,
                offline=True,
            )
            if command:
                return self._publish_install_availability(command=command)
        admission, _operation = await asyncio.to_thread(
            self._attempt_managed_install,
            force=self.force_install,
            offline=self.offline,
            automatic=automatic,
        )
        return admission

    async def _resolve_managed_command(self, *, automatic: bool = True) -> list[str] | None:
        availability = await self._resolve_managed_availability(automatic=automatic)
        return availability.command

    def _attempt_managed_install(
        self,
        *,
        force: bool,
        offline: bool,
        automatic: bool,
        candidate_validator: Callable[[list[str]], ShowRuntimeStartability] | None = None,
    ) -> tuple[ShowRuntimeAvailability, _ShowRuntimeOperationOutcome]:
        admission: ShowRuntimeAvailability | None = None
        operation: _ShowRuntimeOperationOutcome | None = None
        pending_exception: BaseException | None = None
        stack: contextlib.ExitStack | None = None
        try:
            self._install_evidence = None
            self._download_error = None
            stack = contextlib.ExitStack()
            admission = self._availability
            operation = _ShowRuntimeOperationOutcome(
                _ShowRuntimeOperationState.NOT_APPLICABLE,
                admission.reason or "runtime_unavailable",
            )
            preflight = self._managed_install_preflight(
                force=force,
                automatic=automatic,
            )
            if preflight is not None:
                admission, operation = preflight
            else:
                guard = (
                    contextlib.nullcontext((True, None))
                    if self.runtime_source == _RUNTIME_SOURCE_MANIFEST
                    else self._install_guard_locked()
                )
                acquired, guard_reason = stack.enter_context(guard)
                if not acquired:
                    command = None if force else self._safe_installed_managed_runtime_command(offline=offline)
                    if command:
                        admission = self._publish_install_availability(command=command)
                        operation = _ShowRuntimeOperationOutcome(
                            _ShowRuntimeOperationState.COMPLETED,
                            None,
                        )
                    else:
                        reason = guard_reason or "runtime_install_guard_unavailable"
                        admission = self._publish_install_availability(
                            install_reason=reason,
                        )
                        operation = _ShowRuntimeOperationOutcome(
                            _ShowRuntimeOperationState.NOT_APPLICABLE,
                            reason,
                        )
                else:
                    preflight = self._managed_install_preflight(
                        force=force,
                        automatic=automatic,
                    )
                    if preflight is not None:
                        admission, operation = preflight
                    else:
                        if candidate_validator is None:
                            raw_attempt = self._install_managed_runtime_locked(
                                force=force,
                                offline=offline,
                            )
                        else:
                            raw_attempt = self._install_managed_runtime_locked(
                                force=force,
                                offline=offline,
                                candidate_validator=candidate_validator,
                            )
                        attempt = (
                            raw_attempt
                            if isinstance(raw_attempt, _ManagedInstallAttempt)
                            else _ManagedInstallAttempt(
                                raw_attempt,
                                None if raw_attempt else self._install_reason or "runtime_install_failed",
                            )
                        )
                        if attempt.command:
                            admission = self._publish_install_availability(command=attempt.command)
                            operation = _ShowRuntimeOperationOutcome(
                                _ShowRuntimeOperationState.COMPLETED,
                                None,
                            )
                        else:
                            reason = attempt.operation_reason or "runtime_install_failed"
                            installed_command = self._safe_installed_managed_runtime_command(offline=True)
                            admission = (
                                self._publish_install_availability(command=installed_command)
                                if installed_command
                                else self._publish_install_availability(install_reason=reason)
                            )
                            operation = _ShowRuntimeOperationOutcome(
                                (
                                    _ShowRuntimeOperationState.NOT_APPLICABLE
                                    if reason
                                    in {
                                        "runtime_install_already_running",
                                        "runtime_install_guard_unavailable",
                                    }
                                    else _ShowRuntimeOperationState.FAILED
                                ),
                                reason,
                            )
        except OSError:
            reason = self._install_reason or "runtime_install_failed"
            installed_command = self._safe_installed_managed_runtime_command(offline=True)
            admission = (
                self._publish_install_availability(command=installed_command)
                if installed_command
                else self._publish_install_availability(install_reason=reason)
            )
            operation = _ShowRuntimeOperationOutcome(
                _ShowRuntimeOperationState.FAILED,
                reason,
            )
            logger.exception("Show Runtime install admission raised")
        except BaseException as exc:
            pending_exception = exc
        finally:
            if admission is None or operation is None:
                admission = self._availability
                operation = _ShowRuntimeOperationOutcome(
                    _ShowRuntimeOperationState.NOT_APPLICABLE,
                    admission.reason or "runtime_unavailable",
                )
            if stack is not None:
                stack.close()
        if pending_exception is not None:
            raise pending_exception
        return admission, operation

    def _managed_install_preflight(
        self,
        *,
        force: bool,
        automatic: bool,
    ) -> tuple[ShowRuntimeAvailability, _ShowRuntimeOperationOutcome] | None:
        if self._command_explicit:
            availability = self._publish_explicit_command_availability(
                self._resolve_explicit_command_availability()
            )
            command = availability.command
            reason = availability.reason
            if force:
                operation = _ShowRuntimeOperationOutcome(
                    _ShowRuntimeOperationState.NOT_APPLICABLE,
                    "VIBE_SHOW_RUNTIME_BIN",
                )
            elif command:
                operation = _ShowRuntimeOperationOutcome(
                    _ShowRuntimeOperationState.COMPLETED,
                    None,
                )
            else:
                operation = _ShowRuntimeOperationOutcome(
                    _ShowRuntimeOperationState.FAILED,
                    reason or "runtime_command_missing",
                )
            return availability, operation
        skipped_reason = self._managed_install_opt_out_reason(automatic=automatic)
        if skipped_reason:
            return self._publish_policy_skip(skipped_reason), _ShowRuntimeOperationOutcome(
                _ShowRuntimeOperationState.NOT_APPLICABLE,
                skipped_reason,
            )
        if self._managed_command and not force:
            availability = self._publish_install_availability(command=self._managed_command)
            operation = _ShowRuntimeOperationOutcome(_ShowRuntimeOperationState.COMPLETED, None)
            return availability, operation
        return None

    def _managed_install_opt_out_reason(self, *, automatic: bool) -> str | None:
        if automatic and env_flag_enabled(
            "VIBE_INSTALL_SKIP_SHOW_RUNTIME",
            default=False,
        ):
            return "VIBE_INSTALL_SKIP_SHOW_RUNTIME"
        if automatic and not self.auto_install:
            return "VIBE_SHOW_RUNTIME_AUTO_INSTALL"
        return None

    def _complete_runtime_start_admission(
        self,
        availability: ShowRuntimeAvailability,
        operation: _ShowRuntimeOperationOutcome,
        *,
        base_url: str | None,
    ) -> ShowRuntimeAvailability:
        """Publish exactly one start outcome before an admission can leave."""
        published = availability
        if operation.state is _ShowRuntimeOperationState.COMPLETED:
            if not base_url:
                raise AssertionError("completed Show Runtime start admission requires a base URL")
            self._base_url = base_url
            published = self._publish_runtime_availability(
                ShowRuntimeServingState.SERVING,
                base_url,
            )
        elif operation.state is _ShowRuntimeOperationState.FAILED:
            try:
                self.stop()
            except OSError:  # pragma: no cover - process cleanup must not hide the outcome
                logger.warning("Show Runtime start cleanup failed", exc_info=True)
            published = self._publish_runtime_availability(
                ShowRuntimeServingState.START_FAILED,
                runtime_reason=operation.reason,
            )
        return published

    def _publish_policy_skip(self, reason: str) -> ShowRuntimeAvailability:
        if self._managed_command:
            return self._publish_install_availability(command=self._managed_command, policy_reason=reason)
        if self.runtime_source == _RUNTIME_SOURCE_MANIFEST:
            manager = self._shared_manifest_manager(offline=True)
            disk_install = self._shared_manifest_disk_install(manager)
            return self._publish_install_availability(
                command=disk_install.command if disk_install else None,
                install_state=(ShowRuntimeInstallState.INSTALLED if disk_install else ShowRuntimeInstallState.ABSENT),
                policy_reason=reason,
            )
        command = self._safe_installed_managed_runtime_command(offline=True)
        return self._publish_install_availability(command=command, policy_reason=reason)

    def _publish_install_availability(
        self,
        *,
        command: list[str] | None = None,
        install_state: ShowRuntimeInstallState | None = None,
        policy_reason: str | None = None,
        install_reason: str | None = None,
        install_failure_class: ShowRuntimeFailureClass | None = None,
    ) -> ShowRuntimeAvailability:
        install_evidence = (
            self._install_evidence
            if install_reason is not None and self._install_reason == install_reason
            else None
        )
        if command:
            self._managed_command = command
            self._install_evidence = None
        elif install_reason is not None and install_evidence is None:
            self._install_reason = install_reason
            install_evidence = self._install_evidence
        elif install_reason is None:
            self._install_evidence = None
        availability = ShowRuntimeAvailability.from_install(
            command=command,
            install=install_state,
            policy_reason=policy_reason,
            install_reason=install_reason,
            install_evidence=install_evidence,
            install_failure_class=install_failure_class,
        )
        self._availability = availability
        return availability

    def _publish_runtime_availability(
        self,
        runtime: ShowRuntimeServingState,
        base_url: str | None = None,
        *,
        runtime_reason: str | None = None,
    ) -> ShowRuntimeAvailability:
        command = self._availability.command or self._managed_command
        install = self._availability.install
        if command:
            install = ShowRuntimeInstallState.INSTALLED
        runtime_evidence = ShowRuntimeFailureEvidence(
            ShowRuntimeFailureDimension.RUNTIME,
            runtime_reason,
            provenance="configured" if self._command_explicit else None,
        )
        runtime_failure_class = classify_show_runtime_failure(runtime_evidence) if runtime_reason else None
        availability = replace(
            self._availability,
            install=install,
            command=command,
            runtime=runtime,
            base_url=base_url,
            runtime_reason=runtime_reason,
            runtime_failure_class=runtime_failure_class,
            runtime_recovery_action=(show_runtime_recovery_action(runtime_evidence) if runtime_reason else None),
        )
        self._availability = availability
        return availability

    def _managed_install_operation_command(
        self,
        command: list[str] | None,
        *,
        replacement_required: bool,
        replacement_completed: bool = False,
    ) -> list[str] | None:
        """Return a command only when it satisfies the requested operation.

        An ordinary prepare asks only for an available runtime, so a verified
        existing command is sufficient even when a refresh fails. A forced
        prepare asks for replacement; reusing the old command must preserve the
        failure and report that operation as unsuccessful.
        """
        if not command or (replacement_required and not replacement_completed):
            return None
        self._install_reason = None
        return command

    def _remove_managed_runtime_tree_for_replacement(self, path: Path, *, label: str) -> bool:
        """Invalidate cached install facts before removing managed runtime bytes."""
        self._invalidate_managed_runtime_projection()
        try:
            if os.path.lexists(path):
                shutil.rmtree(path)
        except OSError:
            logger.warning("Failed to remove the %s Show Runtime tree before replacement", label, exc_info=True)
            self._install_reason = "runtime_install_failed"
            return False
        if os.path.lexists(path):
            self._install_reason = "runtime_install_failed"
            return False
        return True

    def _invalidate_managed_runtime_projection(self) -> None:
        self._managed_command = None
        self._availability = replace(
            self._availability,
            install=ShowRuntimeInstallState.ABSENT,
            command=None,
            install_reason=None,
            install_failure_class=None,
            install_recovery_action=None,
        )

    def _install_managed_runtime_locked(
        self,
        *,
        force: bool,
        offline: bool,
        candidate_validator: Callable[[list[str]], ShowRuntimeStartability] | None = None,
    ) -> _ManagedInstallAttempt:
        command: list[str] | None
        if self.runtime_source == _RUNTIME_SOURCE_MANIFEST:
            command = (
                self._install_manifest_runtime_locked(force=force, offline=offline)
                if candidate_validator is None
                else self._install_manifest_runtime_locked(
                    force=force,
                    offline=offline,
                    candidate_validator=candidate_validator,
                )
            )
        elif self.runtime_source == _RUNTIME_SOURCE_ARCHIVE:
            command = (
                self._install_archive_runtime(force=force, offline=offline)
                if candidate_validator is None
                else self._install_archive_runtime(
                    force=force,
                    offline=offline,
                    candidate_validator=candidate_validator,
                )
            )
        elif self.runtime_source == _RUNTIME_SOURCE_NPM:
            command = (
                self._install_npm_runtime(force=force)
                if candidate_validator is None
                else self._install_npm_runtime(
                    force=force,
                    candidate_validator=candidate_validator,
                )
            )
        else:
            self._install_reason = "runtime_source_unsupported"
            return _ManagedInstallAttempt(None, self._install_reason)
        if command:
            self._download_error = None
            return _ManagedInstallAttempt(command)
        return _ManagedInstallAttempt(None, self._install_reason or "runtime_install_failed")

    def _installed_managed_runtime_command(self, *, offline: bool) -> list[str] | None:
        if self.runtime_source == _RUNTIME_SOURCE_MANIFEST:
            return self._installed_manifest_runtime_command(offline=offline)
        if self.runtime_source == _RUNTIME_SOURCE_ARCHIVE:
            return self._installed_archive_runtime_command()
        if self.runtime_source == _RUNTIME_SOURCE_NPM:
            resolved = _resolve_executable_path(self._managed_bin_path())
            command = [resolved] if resolved else None
            return command if command and self._retain_managed_command(command) else None
        return None

    def _safe_installed_managed_runtime_command(self, *, offline: bool) -> list[str] | None:
        evidence = self._install_evidence
        download_error = self._download_error
        try:
            return self._installed_managed_runtime_command(offline=offline)
        except (OSError, ValueError):
            return None
        finally:
            self._install_evidence = evidence
            self._download_error = download_error

    def _managed_install_dir_for_command(self, command: Iterable[str]) -> Path | None:
        try:
            runtime_root = self.runtime_dir.resolve(strict=True)
        except (OSError, RuntimeError):
            return None
        legacy_roots = {
            runtime_root / "prebuilt" / "current",
            runtime_root / "package",
        }
        for value in reversed(tuple(command)):
            try:
                candidate = Path(value).expanduser().resolve(strict=True)
            except (OSError, RuntimeError):
                continue
            parents = (candidate, *candidate.parents) if candidate.is_dir() else candidate.parents
            for parent in parents:
                if parent == runtime_root:
                    break
                if runtime_root not in parent.parents:
                    continue
                if parent in legacy_roots or (parent / ".vibe-show-runtime.json").is_file():
                    return parent
        return None

    def _install_reference_dir(self, install_dir: Path) -> Path:
        identity = hashlib.sha256(str(install_dir).encode("utf-8")).hexdigest()
        return self.runtime_dir / "references" / identity

    def _install_reference_key(self, install_dir: Path) -> tuple[str, str] | None:
        try:
            resolved = install_dir.resolve(strict=True)
            runtime_root = self.runtime_dir.resolve(strict=True)
        except (OSError, RuntimeError):
            return None
        if runtime_root not in resolved.parents:
            return None
        return str(runtime_root), str(resolved)

    def _owns_install_reference(self, key: tuple[str, str]) -> bool:
        with _INSTALL_REFERENCE_LOCKS_GUARD:
            reference = _INSTALL_REFERENCE_LOCKS.get(key)
            return bool(
                reference is not None
                and self._install_reference_owner in reference.owners
            )

    def _release_install_reference(self, key: tuple[str, str]) -> None:
        released: _ShowRuntimeInstallReference | None = None
        with _INSTALL_REFERENCE_LOCKS_GUARD:
            reference = _INSTALL_REFERENCE_LOCKS.get(key)
            if (
                reference is None
                or self._install_reference_owner not in reference.owners
            ):
                return
            reference.owners.remove(self._install_reference_owner)
            if not reference.owners:
                released = _INSTALL_REFERENCE_LOCKS.pop(key)
        if released is None:
            return
        try:
            _unlock_install_reference(released.handle)
        except OSError:
            logger.warning("Failed to release Show Runtime install reference", exc_info=True)
        try:
            released.marker.unlink(missing_ok=True)
        except OSError:
            logger.warning("Failed to remove Show Runtime install reference", exc_info=True)

    @staticmethod
    def _reference_path_matches_handle(path: Path, handle: IO[str]) -> bool:
        try:
            path_info = path.lstat()
            open_info = os.fstat(handle.fileno())
        except OSError:
            return False
        return (
            _is_exclusive_regular_file(path_info)
            and _is_exclusive_regular_file(open_info)
            and (path_info.st_dev, path_info.st_ino) == (open_info.st_dev, open_info.st_ino)
        )

    def _retain_managed_command(self, command: list[str]) -> bool:
        install_dir = self._managed_install_dir_for_command(command)
        if install_dir is None:
            return True
        with self._install_guard_locked(timeout_seconds=None) as (acquired, reason):
            if not acquired:
                self._install_reason = reason or "runtime_install_guard_unavailable"
                return False
            return self._retain_install_dir_locked(install_dir)

    def _retain_install_dir_locked(self, install_dir: Path) -> bool:
        key = self._install_reference_key(install_dir)
        if key is None:
            return False
        runtime_root = Path(key[0])
        resolved = Path(key[1])
        with _INSTALL_REFERENCE_LOCKS_GUARD:
            existing = _INSTALL_REFERENCE_LOCKS.get(key)
            if existing is not None:
                existing.owners.add(self._install_reference_owner)
                return True

            reference_dir = self._install_reference_dir(resolved)
            try:
                reference_dir.mkdir(parents=True, exist_ok=True)
                reference_info = reference_dir.lstat()
                if _is_reparse_point(reference_info) or not stat.S_ISDIR(reference_info.st_mode):
                    raise OSError("reference directory is not confined")
                marker = reference_dir / f"{uuid4().hex}.lock"
                flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
                if hasattr(os, "O_NOFOLLOW"):
                    flags |= os.O_NOFOLLOW
                fd = os.open(marker, flags, 0o600)
                handle = os.fdopen(fd, "r+", encoding="utf-8")
                acquired = False
                try:
                    if not self._reference_path_matches_handle(marker, handle):
                        raise OSError("reference marker path changed")
                    handle.seek(0)
                    if not storage_lock_try_lock(handle):
                        raise OSError("fresh reference marker is already locked")
                    acquired = True
                    handle.seek(0)
                    handle.write(str(os.getpid()))
                    handle.flush()
                    _INSTALL_REFERENCE_LOCKS[key] = _ShowRuntimeInstallReference(
                        marker=marker,
                        handle=handle,
                        owners={self._install_reference_owner},
                    )
                    return True
                except BaseException:
                    if acquired:
                        _unlock_install_reference(handle)
                    else:
                        handle.close()
                    marker.unlink(missing_ok=True)
                    raise
            except Exception:
                logger.warning(
                    "Failed to retain Show Runtime install %s",
                    resolved,
                    exc_info=True,
                )
                self._install_reason = "runtime_install_guard_unavailable"
                return False

    def _install_dir_has_live_reference(self, install_dir: Path) -> bool:
        try:
            resolved = install_dir.resolve(strict=True)
            runtime_root = self.runtime_dir.resolve(strict=True)
        except (OSError, RuntimeError):
            return True
        key = (str(runtime_root), str(resolved))
        with _INSTALL_REFERENCE_LOCKS_GUARD:
            if key in _INSTALL_REFERENCE_LOCKS:
                return True

        reference_dir = self._install_reference_dir(resolved)
        try:
            info = reference_dir.lstat()
        except FileNotFoundError:
            return False
        except OSError:
            return True
        if _is_reparse_point(info) or not stat.S_ISDIR(info.st_mode):
            return True
        try:
            with os.scandir(reference_dir) as entries:
                markers = sorted(
                    reference_dir / entry.name
                    for entry in entries
                    if _INSTALL_REFERENCE_RE.fullmatch(entry.name)
                )
        except OSError:
            return True
        for marker in markers:
            try:
                marker_info = marker.lstat()
                if not _is_exclusive_regular_file(marker_info):
                    return True
                flags = os.O_RDWR | getattr(os, "O_BINARY", 0)
                if hasattr(os, "O_NOFOLLOW"):
                    flags |= os.O_NOFOLLOW
                fd = os.open(marker, flags)
                handle = os.fdopen(fd, "r+", encoding="utf-8")
            except FileNotFoundError:
                continue
            except OSError:
                return True
            handle.seek(0)
            if not storage_lock_try_lock(handle):
                handle.close()
                return True
            if not self._reference_path_matches_handle(marker, handle):
                _unlock_install_reference(handle)
                return True
            _unlock_install_reference(handle)
            marker.unlink(missing_ok=True)
        try:
            reference_dir.rmdir()
        except OSError:
            pass
        return False

    def status(self, *, offline: bool | None = None) -> dict[str, Any]:
        """Inspect install state without turning an unreadable state into absence."""
        try:
            return self._status(offline=offline)
        except (OSError, RecursionError, UnicodeError) as exc:
            logger.warning("Show Runtime status inspection failed", exc_info=True)
            reason = "runtime_install_inspection_failed"
            evidence = ShowRuntimeFailureEvidence(
                ShowRuntimeFailureDimension.INSTALL,
                reason,
            )
            availability = ShowRuntimeAvailability.from_install(
                install_reason=reason,
                install_evidence=evidence,
            )
            payload = availability.as_payload()
            detail = str(exc).strip() or type(exc).__name__
            return {
                "provider": self.runtime_source,
                "platform": runtime_platform_tag(),
                "explicit_command": self.command if self._command_explicit else None,
                "node_available": None,
                "node_version": None,
                "node_supported": None,
                "manifest": None,
                "archive": None,
                "install": payload["install"],
                "runtime": payload["runtime"],
                "command": None,
                "reason": reason,
                "download_error": None,
                "inspection_error": {
                    "kind": type(exc).__name__,
                    "message": detail,
                },
            }

    def _status(self, *, offline: bool | None = None) -> dict[str, Any]:
        explicit_availability = (
            self._resolve_explicit_command_availability()
            if self._command_explicit
            else None
        )
        configured_command = explicit_availability.command if explicit_availability else None
        effective_offline = self.offline if offline is None else offline
        manifest_manager = (
            self._shared_manifest_manager(offline=effective_offline)
            if explicit_availability is None and self.runtime_source == _RUNTIME_SOURCE_MANIFEST
            else None
        )
        manifest = (
            manifest_manager.load_manifest(allow_network=not effective_offline)
            if manifest_manager is not None
            else None
        )
        archive: ManagedRuntimeArchive | None = (
            manifest_manager.archive_for_platform(manifest)
            if manifest_manager is not None and manifest is not None
            else None
        )
        source_manifest_reason = (
            manifest_manager._install_reason if manifest_manager is not None else None
        )
        source_manifest_download_error = (
            manifest_manager._download_error if manifest_manager is not None else None
        )
        disk_install = (
            self._shared_manifest_disk_install(manifest_manager, adopt_evidence=False)
            if manifest_manager is not None
            else None
        )
        disk_install_reason = (
            manifest_manager._install_reason if manifest_manager is not None else None
        )
        manifest_reason = (
            disk_install_reason
            if disk_install_reason == "runtime_install_inspection_failed"
            else source_manifest_reason
        )
        manifest_download_error = (
            source_manifest_download_error
            or (manifest_manager._download_error if manifest_manager is not None else None)
        )
        platform_tag = runtime_platform_tag()
        node = _resolve_node_command()
        node_version = _node_version(node) if node else None
        minimum_node = (
            _ShowManifestRuntimeManager._minimum_node(manifest) if manifest else None
        )
        node_supported = _node_satisfies_requirement(node_version, minimum_node) if manifest else None
        installed_command: list[str] | None = configured_command
        installed_dir: Path | None = None
        archive_status: dict[str, Any] | None = None
        installed_runtime_version: str | None = None
        installed_matches: bool | None = None
        installed = (
            explicit_availability is not None
            and explicit_availability.install is ShowRuntimeInstallState.INSTALLED
        )
        manifest_status = _manifest_status_payload(manifest)
        if explicit_availability is None and self.runtime_source == _RUNTIME_SOURCE_MANIFEST:
            if disk_install:
                installed = True
                installed_dir = disk_install.install_dir
                installed_command = disk_install.command
                installed_runtime_version = _persisted_manifest_runtime_version(disk_install.metadata)
                if manifest is not None and archive is not None:
                    installed_matches = (
                        manifest_manager.verified_entrypoint(
                            disk_install.install_dir,
                            manifest,
                            archive,
                        )
                        is not None
                    )
                if manifest_status is None:
                    manifest_status = _persisted_manifest_status_payload(disk_install.metadata)
                    archive_status = _persisted_archive_status_payload(disk_install.metadata)
            if archive and disk_install is None:
                for candidate in manifest_manager.install_candidates(manifest, archive):
                    entrypoint = manifest_manager.verified_entrypoint(candidate, manifest, archive)
                    if entrypoint is None:
                        continue
                    installed = True
                    installed_dir = candidate
                    installed_runtime_version = manifest.runtime_version
                    installed_matches = True
                    if node and node_supported is not False:
                        installed_command = [*node, str(entrypoint)]
                        if installed_command:
                            break
        elif explicit_availability is None and self.runtime_source == _RUNTIME_SOURCE_ARCHIVE:
            installed_dir = self._archive_install_dir()
            installed_command = self._archive_runtime_command(installed_dir, node or ["node"])
            installed = installed_command is not None
            archive_status = {
                "platform": platform_tag,
                "name": _runtime_archive_name(),
                "url": _redact_download_url(self.archive_url) if not self.archive_path else None,
                "path": str(self.archive_path) if self.archive_path else None,
                "sha256": None,
                "size": None,
            }
        elif explicit_availability is None and self.runtime_source == _RUNTIME_SOURCE_NPM:
            managed = _resolve_executable_path(self._managed_bin_path())
            installed_command = [managed] if managed else None
            installed = installed_command is not None
            installed_dir = self._package_install_dir() if installed else None
        if installed and manifest is not None and archive is not None and installed_matches is not True:
            installed_matches = False
        if (
            explicit_availability is None
            and manifest_reason == "runtime_install_inspection_failed"
        ):
            evidence = ShowRuntimeFailureEvidence(
                ShowRuntimeFailureDimension.INSTALL,
                manifest_reason,
            )
            status_availability = ShowRuntimeAvailability.from_install(
                install_reason=manifest_reason,
                install_evidence=evidence,
            )
        elif explicit_availability is not None:
            status_availability = explicit_availability
        else:
            status_availability = ShowRuntimeAvailability.from_install(
                command=installed_command,
                install=(ShowRuntimeInstallState.INSTALLED if installed else ShowRuntimeInstallState.ABSENT),
                install_dir=installed_dir if installed else None,
                install_runtime_version=installed_runtime_version,
                install_matches_manifest=installed_matches,
            )
        status_payload = status_availability.as_payload()
        install_payload = status_payload["install"]
        runtime_payload = status_payload["runtime"]
        return {
            "provider": self.runtime_source,
            "platform": platform_tag,
            "explicit_command": self.command if self._command_explicit else None,
            "node_available": node is not None,
            "node_version": _format_semver(node_version),
            "node_supported": node_supported,
            "manifest": manifest_status,
            "archive": archive_status or _archive_status_payload(archive),
            "install": install_payload,
            "runtime": runtime_payload,
            "command": installed_command,
            "reason": (
                explicit_availability.reason
                if explicit_availability
                else manifest_reason or self._install_reason
                if manifest_manager is not None
                else self._install_reason
            ),
            "download_error": (
                None
                if explicit_availability
                else manifest_download_error or self._download_error
                if manifest_manager is not None
                else self._download_error
            ),
        }

    def probe_archive_reachability(self, *, timeout: float = 10.0) -> dict[str, Any]:
        """Check the selected archive without downloading its body or mutating the cache."""
        archive_url: str | None = None
        if self.runtime_source == _RUNTIME_SOURCE_MANIFEST:
            manager = self._shared_manifest_manager(offline=self.offline)
            manifest = manager.load_manifest_for_diagnostics()
            if not manifest:
                self._adopt_shared_manifest_evidence(manager)
                return {
                    "ok": False,
                    "checked": False,
                    "reason": self._install_reason or "runtime_manifest_missing",
                    "download_error": self._download_error,
                }
            archive = manager.archive_for_platform(manifest)
            if not archive:
                self._adopt_shared_manifest_evidence(manager)
                return {
                    "ok": False,
                    "checked": False,
                    "reason": self._install_reason or "runtime_platform_unsupported",
                }
            archive_url = archive.url
        elif self.runtime_source == _RUNTIME_SOURCE_ARCHIVE:
            if self.archive_path:
                return {
                    "ok": self.archive_path.is_file(),
                    "checked": True,
                    "kind": "local_file",
                    "path": str(self.archive_path),
                    "reason": None if self.archive_path.is_file() else "runtime_archive_missing",
                }
            archive_url = self.archive_url
        else:
            return {
                "ok": False,
                "checked": False,
                "reason": "runtime_archive_probe_not_applicable",
                "provider": self.runtime_source,
            }

        parsed = urllib.parse.urlparse(archive_url)
        if parsed.scheme == "file":
            path = Path(urllib.request.url2pathname(parsed.path))
            return {
                "ok": path.is_file(),
                "checked": True,
                "kind": "local_file",
                "url": _redact_download_url(archive_url),
                "reason": None if path.is_file() else "runtime_archive_missing",
            }
        if parsed.scheme != "https":
            return {
                "ok": False,
                "checked": False,
                "url": _redact_download_url(archive_url),
                "reason": "runtime_archive_url_unsupported",
            }

        result = probe_url(
            archive_url,
            timeout=timeout,
            opener=urllib.request.urlopen,
            user_agent="avibe-show-runtime-doctor",
        )
        if result.get("reason") == "dependency_probe_unsupported":
            result["reason"] = "runtime_archive_probe_unsupported"
        elif result.get("reason") == "dependency_download_failed":
            result["reason"] = "runtime_archive_download_failed"
        return result

    @contextlib.contextmanager
    def _preview_guard(self):
        """Hold a read-only install guard for the whole preview planning.

        The probe lock stays held while staging loops enumerate candidates, so
        an install starting after the check cannot slip a live ``manifest-*``
        directory into the preview. Yields the busy reason (or ``None`` when
        the preview may proceed).
        """
        busy = self._preview_busy_reason_locked()
        try:
            yield busy
        finally:
            self._release_preview_guard()

    def _preview_busy_reason_locked(self) -> str | None:
        """Take the read-only busy probe and keep it for the preview scope."""
        reason = self._preview_busy_reason()
        if reason is None:
            self._preview_guard_fd = getattr(self, "_preview_guard_fd", None)
        return reason

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

    def _windows_preview_busy_reason(self) -> str | None:
        """Read-only Windows busy probe covering the pre-staging interval.

        A leftover ``.install.lock`` is not itself busy — Windows never
        deletes it. An installer that already holds ``msvcrt.locking`` on
        that file (archive validation, before ``manifest-*`` exists) is.
        Staging remains an additional crash-leftover signal.
        """
        probe = self._preview_lock_probe()
        if probe is not None:
            return probe
        staging = self._staging_sentinel_reason()
        if staging:
            return staging
        if getattr(self, "_preview_lock_was_absent", False):
            return None
        try:
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
            fd = os.open(self._install_guard_path, flags)
        except OSError:
            return "runtime_install_guard_unavailable"
        try:
            if not try_windows_exclusive_lock(fd):
                os.close(fd)
                return "runtime_install_already_running"
            if not self._guard_path_matches_fd(fd):
                os.close(fd)
                return "runtime_install_guard_unavailable"
            self._preview_guard_fd = fd
            self._preview_guard_msvcrt = True
            return None
        except OSError:
            os.close(fd)
            return "runtime_install_guard_unavailable"

    def _preview_busy_reason(self) -> str | None:
        """Read-only busy probe for previews: never creates or rewrites files.

        Detects an active install (same process via the RLock depth, another
        process via an advisory lock on an existing ``.install.lock``) so a
        preview never advertises a live in-use archive or staging directory
        as removable. On POSIX an unopenable-but-existing guard reports
        unavailable (an inspection problem). Native Windows has no flock;
        the probe instead takes a non-blocking ``msvcrt.locking`` on the
        existing lock file (never creating it) and still treats a fresh
        ``manifest-*``/``prebuilt-*`` directory as busy.

        On success the probe fd is kept open with the advisory lock held
        (stored on the instance) so the caller can hold the guard through
        planning; ``_release_preview_guard`` unlocks and closes it.
        """
        if self._install_guard_depth > 0:
            return "runtime_install_already_running"
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
            fd = os.open(self._install_guard_path, flags)
        except OSError:
            return "runtime_install_guard_unavailable"
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            # Held (shared) until the preview scope ends: an installer's
            # exclusive-lock acquisition now blocks on us and vice versa.
            if not self._guard_path_matches_fd(fd):
                os.close(fd)
                return "runtime_install_guard_unavailable"
            self._preview_guard_fd = fd
            return None
        except OSError:
            os.close(fd)
            return "runtime_install_already_running"

    def _staging_sentinel_reason(self) -> str | None:
        """Treat freshly-modified staging dirs as busy (flock-less platforms)."""
        mtime_floor = time.time() - _ARCHIVE_MTIME_GUARD_SECONDS
        try:
            for pattern in ("manifest-*", "prebuilt-*"):
                for path in self.runtime_dir.glob(pattern):
                    try:
                        if path.is_dir() and path.stat().st_mtime > mtime_floor:
                            return "runtime_install_already_running"
                    except OSError:
                        continue
        except OSError:
            return None
        return None

    def _preview_lock_missing(self) -> bool:
        try:
            self._install_guard_path.lstat()
        except FileNotFoundError:
            return True
        except OSError:
            return False
        return False

    def _preview_lock_probe(self) -> str | None:
        """Refuse special files before a blocking preview open."""
        try:
            info = self._install_guard_path.lstat()
        except FileNotFoundError:
            self._preview_lock_was_absent = True
            return None
        except OSError:
            self._preview_lock_was_absent = False
            return "runtime_install_guard_unavailable"
        self._preview_lock_was_absent = False
        if _is_reparse_point(info) or not _is_exclusive_regular_file(info):
            return "runtime_install_guard_unavailable"
        return None

    def _guard_path_matches_fd(self, fd: int) -> bool:
        """True when the live path still names the locked descriptor."""
        try:
            open_stat = os.fstat(fd)
            path_stat = self._install_guard_path.lstat()
        except OSError:
            return False
        return (
            _is_exclusive_regular_file(open_stat)
            and _is_exclusive_regular_file(path_stat)
            and (open_stat.st_dev, open_stat.st_ino) == (path_stat.st_dev, path_stat.st_ino)
        )

    def _preview_raced_busy(self) -> bool:
        """True when an install started after a lock-absent preview probe."""
        if getattr(self, "_preview_guard_fd", None) is not None:
            return False
        if self._staging_sentinel_reason():
            return True
        if not getattr(self, "_preview_lock_was_absent", False):
            return False
        return not self._preview_lock_missing()

    def clean(self, *, keep_previous: int = 1, dry_run: bool = False) -> dict[str, Any]:
        if self.runtime_source == _RUNTIME_SOURCE_MANIFEST:
            result = self._shared_manifest_manager(offline=True).clean(
                keep_previous=keep_previous,
                dry_run=dry_run,
            )
            if "archives" not in result:
                reason = result.get("reason")
                skipped_reason = (
                    _SKIPPED_ARCHIVE_REASON_INSTALL_RUNNING
                    if reason == "runtime_install_already_running"
                    else _SKIPPED_ARCHIVE_REASON_INSPECTION_FAILED
                )
                result["archives"] = self._skipped_archive_report(skipped_reason)
            return {"dry_run": dry_run, **result}
        removed: list[str] = []
        try:
            if dry_run:
                return self._clean_locked(
                    keep_previous=keep_previous,
                    dry_run=True,
                    removed=removed,
                )
            with self._install_guard_locked(timeout_seconds=1.0) as (acquired, reason):
                if not acquired:
                    skipped_reason = (
                        _SKIPPED_ARCHIVE_REASON_INSPECTION_FAILED
                        if reason == "runtime_install_guard_unavailable"
                        else _SKIPPED_ARCHIVE_REASON_INSTALL_RUNNING
                    )
                    return {
                        "ok": False,
                        "dry_run": False,
                        "removed": [],
                        "reason": reason,
                        "archives": self._skipped_archive_report(skipped_reason),
                    }
                return self._clean_locked(
                    keep_previous=keep_previous,
                    dry_run=False,
                    removed=removed,
                )
        except Exception:
            # A planning failure (e.g. an install dir disappearing mid-scan)
            # must return the structured inspection-failure report, never an
            # exception through the CLI or Doctor paths. Staging removals that
            # already happened stay in the result so the CLI does not claim
            # zero items after files were deleted.
            logger.warning("Show Runtime cache cleanup failed", exc_info=True)
            return {
                "ok": False,
                "dry_run": dry_run,
                "removed": list(removed),
                "archives": self._skipped_archive_report(_SKIPPED_ARCHIVE_REASON_INSPECTION_FAILED),
            }

    def _clean_locked(
        self,
        *,
        keep_previous: int,
        dry_run: bool,
        removed: list[str] | None = None,
    ) -> dict[str, Any]:
        if removed is None:
            removed = []
        preview_guard = self._preview_guard() if dry_run else contextlib.nullcontext(None)
        with preview_guard as busy_reason:
            if dry_run and busy_reason:
                # Map both contention and guard-unavailability through the
                # same skip-reason taxonomy the real cleanup uses, so the CLI
                # renders reason-specific guidance either way.
                skip_reason = (
                    _SKIPPED_ARCHIVE_REASON_INSPECTION_FAILED
                    if busy_reason == "runtime_install_guard_unavailable"
                    else _SKIPPED_ARCHIVE_REASON_INSTALL_RUNNING
                )
                return {
                    "ok": False,
                    "dry_run": True,
                    "removed": [],
                    "archives": self._skipped_archive_report(skip_reason),
                }
            for pattern in ("prebuilt-*", "manifest-*"):
                for path in self.runtime_dir.glob(pattern):
                    if path.is_dir():
                        if not dry_run:
                            shutil.rmtree(path, ignore_errors=True)
                        removed.append(str(path))
            if self.runtime_source == _RUNTIME_SOURCE_ARCHIVE:
                self._clean_provider_install_dirs(
                    self.runtime_dir / "prebuilt",
                    provider=_RUNTIME_SOURCE_ARCHIVE,
                    keep_previous=keep_previous,
                    dry_run=dry_run,
                    removed=removed,
                )
            elif self.runtime_source == _RUNTIME_SOURCE_NPM:
                self._clean_provider_install_dirs(
                    self.runtime_dir / "package",
                    provider=_RUNTIME_SOURCE_NPM,
                    keep_previous=keep_previous,
                    dry_run=dry_run,
                    removed=removed,
                )
            archives = self._clean_downloaded_archives(dry_run=dry_run)
            if dry_run and self._preview_raced_busy():
                return {
                    "ok": False,
                    "dry_run": True,
                    "removed": [],
                    "archives": self._skipped_archive_report(_SKIPPED_ARCHIVE_REASON_INSTALL_RUNNING),
                }
            return {
                "ok": True,
                "dry_run": dry_run,
                "removed": removed,
                "archives": archives,
            }

    def _clean_provider_install_dirs(
        self,
        namespace: Path,
        *,
        provider: str,
        keep_previous: int,
        dry_run: bool,
        removed: list[str],
    ) -> None:
        versions_dir = namespace / "versions"
        try:
            versions_info = versions_dir.lstat()
        except FileNotFoundError:
            return
        if _is_reparse_point(versions_info) or not stat.S_ISDIR(versions_info.st_mode):
            raise OSError(f"runtime versions directory is not confined: {versions_dir}")
        pointer_path = namespace / "current.json"
        try:
            pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
            current = Path(pointer["install_dir"]).expanduser().resolve(strict=True)
            resolved_versions = versions_dir.resolve(strict=True)
        except FileNotFoundError:
            return
        except (KeyError, OSError, RuntimeError, TypeError, UnicodeError, ValueError) as exc:
            raise OSError(f"runtime pointer is unreadable: {pointer_path}") from exc
        if pointer.get("provider") != provider or current.parent != resolved_versions:
            raise OSError(f"runtime pointer is not confined: {pointer_path}")

        records: list[tuple[Path, float]] = []
        with os.scandir(versions_dir) as entries:
            for entry in entries:
                try:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                    path = Path(entry.path)
                    resolved = path.resolve(strict=True)
                    if resolved.parent != resolved_versions:
                        continue
                    metadata = json.loads(
                        (path / ".vibe-show-runtime.json").read_text(encoding="utf-8")
                    )
                    if (
                        metadata.get("provider") != provider
                        or metadata.get("runtime_id") != "show-runtime"
                    ):
                        continue
                    records.append((path, entry.stat(follow_symlinks=False).st_mtime))
                except FileNotFoundError:
                    continue
                except (OSError, RuntimeError, UnicodeError, ValueError) as exc:
                    raise OSError(f"runtime install cannot be inspected: {entry.path}") from exc

        protected = {current}
        ranked: list[tuple[Path, float]] = []
        for path, mtime in records:
            resolved = path.resolve(strict=True)
            if self._install_dir_has_live_reference(resolved):
                protected.add(resolved)
            else:
                ranked.append((path, mtime))
        rollback = sorted(
            (item for item in ranked if item[0].resolve(strict=True) != current),
            key=lambda item: item[1],
            reverse=True,
        )
        protected.update(
            path.resolve(strict=True)
            for path, _mtime in rollback[: max(0, keep_previous)]
        )
        for path, _mtime in records:
            if path.resolve(strict=True) in protected:
                continue
            if not dry_run:
                shutil.rmtree(path)
            removed.append(str(path))

    def archive_cache_status(self, *, keep_previous: int = 1) -> dict[str, Any]:
        """Report reclaimable content-addressed archives without deleting anything.

        Simulates the install-dir cleanup a real ``clean(keep_previous=...)``
        would perform, so the reported candidates match what reclamation would
        actually remove. Failures in either phase return the structured
        inspection-failure report so Doctor can render them.
        """
        report = self.clean(keep_previous=keep_previous, dry_run=True)
        archives = report.get("archives") or self._skipped_archive_report(_SKIPPED_ARCHIVE_REASON_INSPECTION_FAILED)
        return archives

    @staticmethod
    def _iter_install_metadata(versions_dir: Path, pattern: str) -> Iterator[Path]:
        """Glob install metadata with error-preserving traversal.

        ``Path.glob`` suppresses per-directory OSError and silently omits
        retained installs whose subtree cannot be scanned; that would unprotect
        their rollback archives, so traversal failures raise instead.
        """
        try:
            parts = pattern.split("/")
            current: Iterable[Path] = [versions_dir]
            for depth, part in enumerate(parts):
                is_last = depth == len(parts) - 1
                next_paths: list[Path] = []
                for parent in current:
                    try:
                        iterator = os.scandir(parent)
                    except FileNotFoundError:
                        continue
                    except NotADirectoryError:
                        # An unrelated non-directory matched a wildcard level
                        # (e.g. .DS_Store under versions/); skip it rather
                        # than disabling archive reporting for the cache.
                        continue
                    except OSError as exc:
                        raise _ArchiveInspectionError(f"versions traversal failed: {parent}") from exc
                    try:
                        for entry in iterator:
                            if not fnmatch.fnmatch(entry.name, part):
                                continue
                            if not is_last and not entry.is_dir(follow_symlinks=False):
                                # Intermediate wildcard levels must be
                                # directories; files/symlinks are not installs.
                                continue
                            child = parent / entry.name
                            next_paths.append(child)
                    except OSError as exc:
                        raise _ArchiveInspectionError(f"versions traversal failed: {parent}") from exc
                    finally:
                        iterator.close()
                current = next_paths
            for path in current:
                if path.name == ".vibe-show-runtime.json":
                    yield path
        except _ArchiveInspectionError:
            raise

    def _protected_archive_sha256s(self, skip_metadata_under: set[Path] | None = None) -> set[str]:
        """SHA-256 digests of archives the current and retained installs still need.

        Sources: the ``current.json`` pointer plus every remaining managed
        install's ``.vibe-show-runtime.json`` metadata. Called after
        install-dir cleanup, so the remaining metadata files are exactly the
        current install plus the retained rollback install(s). Metadata under
        ``skip_metadata_under`` is ignored, so dry runs can simulate the state
        after removing those install dirs.

        Raises ``_ArchiveMetadataError`` when a retained install's metadata is
        unreadable or malformed: silently treating that archive as unprotected
        could delete the artifact a rollback reinstall needs.
        """
        skip_resolved = {path.resolve() for path in (skip_metadata_under or ())}

        def _skipped(metadata_path: Path) -> bool:
            if not skip_resolved:
                return False
            resolved = metadata_path.resolve()
            return any(resolved == item or item in resolved.parents for item in skip_resolved)

        protected: set[str] = set()
        try:
            pointer = json.loads((self.runtime_dir / "current.json").read_text(encoding="utf-8"))
            digest = pointer.get("archive_sha256")
            if isinstance(digest, str) and _CONTENT_ADDRESSED_ARCHIVE_RE.fullmatch(f"{digest}.tgz"):
                protected.add(digest)
        except FileNotFoundError:
            pass
        except Exception as cop:
            raise _ArchiveMetadataError("current.json is unreadable") from cop
        if self.archive_path is not None:
            try:
                archive_name = self.archive_path.resolve().name
            except OSError:
                archive_name = self.archive_path.name
            if _CONTENT_ADDRESSED_ARCHIVE_RE.fullmatch(archive_name):
                protected.add(archive_name[: -len(".tgz")])
        versions_dir = self.runtime_dir / "versions"
        if versions_dir.is_symlink():
            raise _ArchiveMetadataError("versions directory is a symlink")
        try:
            # Path.is_dir()/exists() suppress OSError into False; an
            # uninspectable versions tree must instead fail closed so every
            # retained install keeps its rollback archive protected.
            versions_is_dir = stat.S_ISDIR(versions_dir.stat().st_mode)
        except FileNotFoundError:
            versions_is_dir = False
        except OSError as exc:
            raise _ArchiveInspectionError("versions directory cannot be inspected") from exc
        if versions_is_dir:
            for pattern in ("*/*/.vibe-show-runtime.json", "*/*/*/.vibe-show-runtime.json"):
                for metadata_path in self._iter_install_metadata(versions_dir, pattern):
                    if _skipped(metadata_path):
                        continue
                    try:
                        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                    except Exception as exc:
                        raise _ArchiveMetadataError(f"install metadata is unreadable: {metadata_path}") from exc
                    digest = metadata.get("archive_sha256")
                    if not _CONTENT_ADDRESSED_ARCHIVE_RE.fullmatch(
                        f"{digest}.tgz" if isinstance(digest, str) else ""
                    ):
                        raise _ArchiveMetadataError(
                            f"install metadata archive_sha256 is missing or not a digest: {metadata_path}"
                        )
                    protected.add(digest)
        return protected

    def _archive_cleanup_candidates(self, protected: set[str]) -> list[tuple[Path, int, str, int]]:
        """Completed content-addressed archives outside the protected set.

        Only strict ``<sha256>.tgz`` regular files are candidates. The manifest
        download flow stages into ``<sha256>.tmp`` and atomically renames only
        after size + checksum verification, so a matching name implies a
        completed, verified archive; in-progress ``.tmp`` downloads, symlinks,
        and unknown file names are never candidates. Archives modified within
        the cross-process safety window are also skipped: another process may
        have just finalized a download whose install metadata does not exist
        yet.
        """
        downloads_dir = self.runtime_dir / "downloads"
        # Windows tuples carry an extra inode field for identity checks;
        # POSIX tuples pad it so both branches share one unpack shape.
        candidates: list[tuple[Path, int, str, int]] = []
        try:
            downloads_stat = downloads_dir.lstat()  # error-preserving, no follow
            exists = True
        except FileNotFoundError:
            exists = False
        except OSError as exc:
            raise _ArchiveInspectionError("downloads directory cannot be inspected") from exc
        if not exists:
            return candidates
        # A symlink or reparse-point downloads directory would follow the
        # link and unlink files outside Avibe's runtime state; fail as an
        # inspection error rather than traverse it.
        if _is_reparse_point(downloads_stat) or not stat.S_ISDIR(downloads_stat.st_mode):
            raise _ArchiveInspectionError("downloads directory is a symlink or not a directory")
        mtime_floor = time.time() - _ARCHIVE_MTIME_GUARD_SECONDS
        if os.name == "nt":
            # Directory descriptors (dir_fd) are unsupported on native
            # Windows, so scan/stat/unlink by path. Bind the scan to the
            # validated directory identity (dev/ino) so a junction swap of
            # ``downloads`` cannot redirect enumeration or later deletion.
            downloads_identity = (downloads_stat.st_dev, downloads_stat.st_ino)

            def _require_same_downloads() -> None:
                try:
                    current = downloads_dir.lstat()
                except OSError as exc:
                    raise _ArchiveInspectionError("downloads directory cannot be inspected") from exc
                if _is_reparse_point(current) or not stat.S_ISDIR(current.st_mode):
                    raise _ArchiveInspectionError("downloads directory is a symlink or not a directory")
                if (current.st_dev, current.st_ino) != downloads_identity:
                    raise _ArchiveInspectionError("downloads directory was replaced")

            _require_same_downloads()
            iterator = os.scandir(downloads_dir)
            try:
                for entry in iterator:
                    is_claim = bool(_ABANDONED_ARCHIVE_CLAIM_RE.fullmatch(entry.name))
                    if not is_claim and not _CONTENT_ADDRESSED_ARCHIVE_RE.fullmatch(entry.name):
                        continue
                    try:
                        entry_stat = entry.stat(follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    except OSError as cop:
                        raise _ArchiveInspectionError(f"archive is not stat-able: {entry.name}") from cop
                    if not stat.S_ISREG(entry_stat.st_mode):
                        continue
                    if not is_claim:
                        if entry_stat.st_mtime > mtime_floor:
                            continue
                        if entry.name[: -len(".tgz")] in protected:
                            continue
                    candidates.append(
                        (downloads_dir / entry.name, entry_stat.st_size, entry.name, entry_stat.st_ino)
                    )
            finally:
                iterator.close()
            _require_same_downloads()
            self._downloads_dir_identity = downloads_identity
            return candidates
        # POSIX: bind enumeration and unlinking to the directory we validated
        # so a concurrent path swap (symlink replacing ``downloads`` between
        # the stat above and iterdir/unlink below) cannot redirect operations.
        dir_fd = os.open(downloads_dir, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_DIRECTORY", 0))
        try:
            opened = os.fstat(dir_fd)
            if (opened.st_dev, opened.st_ino) != (downloads_stat.st_dev, downloads_stat.st_ino):
                raise _ArchiveInspectionError("downloads directory was replaced before scan")
            with os.scandir(dir_fd) as entries:
                names = [entry.name for entry in entries]
            for name in sorted(names):
                is_claim = bool(_ABANDONED_ARCHIVE_CLAIM_RE.fullmatch(name))
                if not is_claim and not _CONTENT_ADDRESSED_ARCHIVE_RE.fullmatch(name):
                    continue
                try:
                    stat_result = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                except OSError as cop:
                    raise _ArchiveInspectionError(f"archive is not stat-able: {name}") from cop
                if not stat.S_ISREG(stat_result.st_mode):
                    continue
                if not is_claim:
                    if stat_result.st_mtime > mtime_floor:
                        continue
                    if name[: -len(".tgz")] in protected:
                        continue
                candidates.append((downloads_dir / name, stat_result.st_size, name, 0))
            # Keep the enumeration descriptor until the caller closes it so
            # the removal phase (when any) unlinks through the same fd.
            # Dry runs, Doctor, and empty real scans close it in
            # ``_clean_downloaded_archives``.
            self._downloads_dir_fd = dir_fd
            return candidates
        except BaseException:
            os.close(dir_fd)
            raise

    def _close_downloads_dir_fd(self) -> None:
        fd = getattr(self, "_downloads_dir_fd", None)
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
            self._downloads_dir_fd = None

    def _clean_downloaded_archives(
        self,
        *,
        dry_run: bool = False,
        skip_metadata_under: set[Path] | None = None,
    ) -> dict[str, Any]:
        """Report on / prune the content-addressed archive cache.

        Dry runs are strictly read-only: they never take the install guard,
        never create or rewrite ``.install.lock``, and stay usable on a
        read-only runtime directory (Doctor contract). Real cleanup serializes
        against installs via the guard (re-entrant inside an install) and
        reports one of two skip reasons: lock contention, or inspection
        failure — consumers key off ``skipped_reason`` alone.
        """
        if dry_run:
            try:
                protected = self._protected_archive_sha256s(skip_metadata_under=skip_metadata_under)
                candidates = self._archive_cleanup_candidates(protected)
            except Exception:
                logger.warning("Show Runtime archive cleanup inspection failed", exc_info=True)
                return self._skipped_archive_report(_SKIPPED_ARCHIVE_REASON_INSPECTION_FAILED)
            finally:
                # Dry runs never delete, so the enumeration descriptor (kept
                # for the removal phase) must be closed on every path.
                self._close_downloads_dir_fd()
            return {
                "outcome": "cleaned" if not candidates else "partial",
                "protected_count": len(protected),
                "candidate_count": len(candidates),
                "candidate_bytes": sum(size for _, size, _name, _ino in candidates),
                "removed_count": 0,
                "removed_bytes": 0,
                "failed_count": 0,
            }
        with self._install_guard_locked(timeout_seconds=1.0) as (acquired, guard_reason):
            if not acquired:
                logger.warning("Show Runtime archive cleanup skipped: install guard %s", guard_reason)
                if guard_reason == "runtime_install_guard_unavailable":
                    return self._skipped_archive_report(_SKIPPED_ARCHIVE_REASON_INSPECTION_FAILED)
                return self._skipped_archive_report(_SKIPPED_ARCHIVE_REASON_INSTALL_RUNNING)
            try:
                protected = self._protected_archive_sha256s(skip_metadata_under=skip_metadata_under)
                candidates = self._archive_cleanup_candidates(protected)
                removed_count = 0
                removed_bytes = 0
                failed_count = 0
                # Unlink through the same validated directory on POSIX (no
                # path resolution that a concurrent swap could redirect); on
                # Windows unlink by path. A fresh runtime with no candidates
                # never opens the directory at all.
                if candidates:
                    candidates.sort(key=lambda item: (0 if _ABANDONED_ARCHIVE_CLAIM_RE.fullmatch(item[2]) else 1, item[2]))
                    if os.name == "nt":
                        downloads_identity = getattr(self, "_downloads_dir_identity", None)
                        for path, size, name, inode in candidates:
                            is_claim = bool(_ABANDONED_ARCHIVE_CLAIM_RE.fullmatch(name))
                            claimed = path if is_claim else path.with_name(f"{name}.avibe-removing")
                            try:
                                if downloads_identity is not None:
                                    dir_stat = path.parent.lstat()
                                    if _is_reparse_point(dir_stat) or (
                                        dir_stat.st_dev,
                                        dir_stat.st_ino,
                                    ) != downloads_identity:
                                        raise OSError("downloads directory was replaced")
                                pre_stat = os.stat(path, follow_symlinks=False)
                                if inode and pre_stat.st_ino != inode:
                                    raise OSError("entry was replaced after enumeration")
                                if not stat.S_ISREG(pre_stat.st_mode):
                                    raise OSError("entry replaced by a non-regular file")
                                if is_claim:
                                    os.unlink(path)
                                else:
                                    os.rename(path, claimed)
                                    post_stat = os.stat(claimed, follow_symlinks=False)
                                    if inode and post_stat.st_ino != inode:
                                        os.rename(claimed, path)
                                        raise OSError("rename claimed a different entry")
                                    os.unlink(claimed)
                            except OSError:
                                logger.warning("Failed to remove stale Show Runtime archive %s", path, exc_info=True)
                                failed_count += 1
                                try:
                                    if not is_claim and claimed.exists():
                                        os.rename(claimed, path)
                                except OSError:
                                    pass
                                continue
                            removed_count += 1
                            removed_bytes += size
                    else:
                        dir_fd = getattr(self, "_downloads_dir_fd", None)
                        try:
                            for path, size, name, _inode in candidates:
                                try:
                                    os.unlink(name, dir_fd=dir_fd)
                                except OSError:
                                    logger.warning("Failed to remove stale Show Runtime archive %s", path, exc_info=True)
                                    failed_count += 1
                                    continue
                                removed_count += 1
                                removed_bytes += size
                        finally:
                            if dir_fd is not None:
                                os.close(dir_fd)
                                self._downloads_dir_fd = None
                report = {
                    "protected_count": len(protected),
                    "candidate_count": len(candidates),
                    "candidate_bytes": sum(size for _, size, _name, _ino in candidates),
                    "removed_count": removed_count,
                    "removed_bytes": removed_bytes,
                    "failed_count": failed_count,
                }
                if failed_count:
                    report["outcome"] = "skipped" if removed_count == 0 else "partial"
                    report["skipped_reason"] = _SKIPPED_ARCHIVE_REASON_REMOVAL_FAILED
                else:
                    report["outcome"] = "cleaned"
                return report
            except Exception:
                logger.warning("Show Runtime archive cleanup failed", exc_info=True)
                return self._skipped_archive_report(_SKIPPED_ARCHIVE_REASON_INSPECTION_FAILED)
            finally:
                # Empty real scans never enter the unlink branch that closes
                # the enumeration descriptor; close it on every remaining path.
                self._close_downloads_dir_fd()

    @staticmethod
    def _skipped_archive_report(reason: str) -> dict[str, Any]:
        return {
            "outcome": "skipped",
            "protected_count": 0,
            "candidate_count": 0,
            "candidate_bytes": 0,
            "removed_count": 0,
            "removed_bytes": 0,
            "skipped_reason": reason,
        }

    def prepare(
        self,
        *,
        force: bool | None = None,
        offline: bool | None = None,
        automatic: bool = False,
    ) -> dict[str, Any]:
        return self._prepare(
            force=force,
            offline=offline,
            automatic=automatic,
        )

    def _prepare(
        self,
        *,
        force: bool | None,
        offline: bool | None,
        automatic: bool,
        candidate_validator: Callable[[list[str]], ShowRuntimeStartability] | None = None,
    ) -> dict[str, Any]:
        effective_force = self.force_install if force is None else force
        effective_offline = self.offline if offline is None else offline
        if candidate_validator is None:
            admission, operation = self._attempt_managed_install(
                force=effective_force,
                offline=effective_offline,
                automatic=automatic,
            )
        else:
            admission, operation = self._attempt_managed_install(
                force=effective_force,
                offline=effective_offline,
                automatic=automatic,
                candidate_validator=candidate_validator,
            )
        payload = admission.as_payload()
        payload["ok"] = operation.ok
        if not operation.ok:
            payload["reason"] = operation.reason
        status_offline = True if admission.policy is ShowRuntimePolicyState.SKIPPED else effective_offline
        payload.update(
            {
                "provider": self.runtime_source,
                "platform": runtime_platform_tag(),
                "status": self.status(offline=status_offline),
            }
        )
        return payload

    def _verify_startability(self, command_value: Any) -> ShowRuntimeStartability:
        command = command_value if isinstance(command_value, list) else []
        if not command:
            return ShowRuntimeStartability.not_startable("runtime_command_missing")
        try:
            with tempfile.TemporaryDirectory(
                prefix="avibe-show-runtime-repair-"
            ) as verification_root_value:
                verification_root = Path(verification_root_value)
                verifier = ShowRuntimeManager(
                    command=shlex.join(str(part) for part in command),
                    workspace_root=verification_root / "show",
                    runtime_dir=verification_root / "runtime",
                    auto_install=False,
                )
                try:
                    result = asyncio.run(verifier.ensure())
                finally:
                    verifier.stop()
        except Exception as exc:  # noqa: BLE001
            detail = str(exc).strip() or type(exc).__name__
            return ShowRuntimeStartability.undetermined(detail)
        if result.available:
            return ShowRuntimeStartability.startable()
        return ShowRuntimeStartability.not_startable(
            result.reason or "runtime_start_failed"
        )

    def repair(self) -> dict[str, Any]:
        """Verify startability, then publish only a startable replacement."""

        before = self.status()
        before_install = (
            before.get("install") if isinstance(before.get("install"), dict) else {}
        )
        before_installed = before_install.get("state") == "installed"
        before_install_dir = before_install.get("install_dir")
        provider = before.get("provider")
        platform_tag = before.get("platform")

        def outcome(
            state: str,
            *,
            ok: bool,
            reason: str | None = None,
            verification: ShowRuntimeStartability | None = None,
            verification_phase: str | None = None,
            repair_attempted: bool = False,
            prepared: Mapping[str, Any] | None = None,
        ) -> dict[str, Any]:
            prepared_status = (
                prepared.get("status")
                if prepared is not None and isinstance(prepared.get("status"), dict)
                else {}
            )
            prepared_install = (
                prepared_status.get("install")
                if isinstance(prepared_status.get("install"), dict)
                else before_install
            )
            download_error = (
                prepared_status.get("download_error")
                if isinstance(prepared_status.get("download_error"), dict)
                else before.get("download_error")
            )
            return {
                "ok": ok,
                "outcome": state,
                "reason": reason,
                "verification": verification.as_payload() if verification else None,
                "verification_phase": verification_phase,
                "repair_attempted": repair_attempted,
                "was_installed": before_installed,
                "provider": (
                    prepared.get("provider") if prepared is not None else provider
                ),
                "platform": (
                    prepared.get("platform") if prepared is not None else platform_tag
                ),
                "install_dir": prepared_install.get("install_dir"),
                "installed": prepared_install.get("state") == "installed",
                "command": (
                    prepared_status.get("command")
                    if prepared is not None
                    else before.get("command")
                ),
                "download_error": download_error,
                "start_error": verification.detail if verification else None,
                "explicit_command": before.get("explicit_command"),
                "archive_url": (
                    before.get("archive", {}).get("url")
                    if isinstance(before.get("archive"), dict)
                    else None
                ),
                "before_install_dir": before_install_dir,
                "inspection_error": before.get("inspection_error"),
            }

        if (
            before_install.get("state") == "failed"
            and before.get("reason") == "runtime_install_inspection_failed"
        ):
            return outcome(
                "failed",
                ok=False,
                reason="runtime_install_inspection_failed",
            )

        if before_installed:
            command = before.get("command")
            if isinstance(command, list) and not self._retain_managed_command(command):
                verification = ShowRuntimeStartability.undetermined(
                    self._install_reason or "runtime_install_guard_unavailable"
                )
                return outcome(
                    "failed",
                    ok=False,
                    reason="runtime_start_verification_failed",
                    verification=verification,
                    verification_phase="before",
                )
            verification = self._verify_startability(command)
            if verification.state is ShowRuntimeStartabilityState.UNDETERMINED:
                return outcome(
                    "failed",
                    ok=False,
                    reason="runtime_start_verification_failed",
                    verification=verification,
                    verification_phase="before",
                )
            if verification.state is ShowRuntimeStartabilityState.STARTABLE:
                return outcome(
                    "healthy",
                    ok=True,
                    verification=verification,
                    verification_phase="before",
                )
            if before.get("explicit_command"):
                return outcome(
                    "failed",
                    ok=False,
                    reason=verification.reason or "runtime_start_failed",
                    verification=verification,
                    verification_phase="before",
                )

        archive = before.get("archive") if isinstance(before.get("archive"), dict) else {}
        archive_url = str(archive.get("url") or "")
        if (
            before.get("provider") == _RUNTIME_SOURCE_ARCHIVE
            and "github.com/avibe-bot/vibe-show-runtime/releases/latest/download/"
            in archive_url
        ):
            return outcome(
                "failed",
                ok=False,
                reason="runtime_legacy_archive_unavailable",
                verification=(verification if before_installed else None),
                verification_phase=("before" if before_installed else None),
            )

        candidate_verification: ShowRuntimeStartability | None = None

        def validate_candidate(command: list[str]) -> ShowRuntimeStartability:
            nonlocal candidate_verification
            candidate_verification = self._verify_startability(command)
            return candidate_verification

        prepared = self._prepare(
            force=before_installed,
            offline=None,
            automatic=False,
            candidate_validator=validate_candidate,
        )
        if prepared.get("ok") and candidate_verification is not None:
            return outcome(
                "repaired",
                ok=True,
                verification=candidate_verification,
                verification_phase="after",
                repair_attempted=True,
                prepared=prepared,
            )
        reason = str(prepared.get("reason") or "runtime_prepare_failed")
        return outcome(
            "failed",
            ok=False,
            reason=reason,
            verification=(
                candidate_verification
                if candidate_verification is not None
                else verification
                if before_installed
                else None
            ),
            verification_phase=(
                "after"
                if candidate_verification is not None
                else "before"
                if before_installed
                else None
            ),
            repair_attempted=True,
            prepared=prepared,
        )

    def _configured_manifest_source(self) -> str:
        if self.manifest_path is not None:
            return str(self.manifest_path)
        if self.manifest_url is not None:
            return self.manifest_url
        return _PACKAGED_RUNTIME_MANIFEST_SOURCE

    def _installed_manifest_runtime_command(self, *, offline: bool | None = None) -> list[str] | None:
        node = _resolve_node_command()
        if not node:
            return None
        manager = self._shared_manifest_manager(offline=self.offline if offline is None else offline)
        entrypoint = manager.resolve_selected_entrypoint()
        if entrypoint is not None:
            self._adopt_shared_manifest_evidence(manager)
            command = [*node, str(entrypoint)]
            return command if self._retain_managed_command(command) else None

        source_reason = manager._install_reason
        source_download_error = manager._download_error
        if source_reason not in {
            "runtime_manifest_download_failed",
            "runtime_manifest_invalid",
            "runtime_manifest_missing",
            "runtime_manifest_unavailable_offline",
        }:
            self._adopt_shared_manifest_evidence(manager)
            return None

        disk_install = self._shared_manifest_disk_install(manager, adopt_evidence=False)
        manager._install_reason = source_reason
        manager._download_error = source_download_error
        self._adopt_shared_manifest_evidence(manager)
        command = disk_install.command if disk_install else None
        return command if command and self._retain_managed_command(command) else None

    def _shared_manifest_disk_install(
        self,
        manager: _ShowManifestRuntimeManager,
        *,
        adopt_evidence: bool = True,
    ) -> _ShowRuntimeDiskInstall | None:
        admitted_entrypoint = manager.resolve_binary()
        if adopt_evidence:
            self._adopt_shared_manifest_evidence(manager)
        if admitted_entrypoint is None:
            return None
        try:
            pointer = json.loads((self.runtime_dir / "current.json").read_text(encoding="utf-8"))
            install_dir = Path(pointer["install_dir"]).resolve(strict=True)
            metadata = json.loads(
                (install_dir / manager.spec.metadata_filename).read_text(encoding="utf-8")
            )
            status_metadata = dict(metadata)
            status_metadata["manifest_sha256"] = pointer["manifest_sha256"]
        except (KeyError, OSError, RuntimeError, TypeError, ValueError):
            return None
        entrypoint = manager._project_entrypoint(install_dir)
        if not entrypoint.is_file():
            return None
        node = _resolve_node_command()
        command = [*node, str(entrypoint)] if node else None
        return _ShowRuntimeDiskInstall(
            install_dir=install_dir,
            command=command,
            metadata=status_metadata,
        )

    def _shared_manifest_manager(self, *, offline: bool) -> _ShowManifestRuntimeManager:
        return _ShowManifestRuntimeManager(self, offline=offline)

    def _adopt_shared_manifest_evidence(
        self,
        manager: _ShowManifestRuntimeManager,
        result: Mapping[str, Any] | None = None,
    ) -> None:
        reason = result.get("reason") if result is not None else manager._install_reason
        download_error = (
            result.get("download_error") if result is not None else manager._download_error
        )
        self._download_error = download_error if isinstance(download_error, dict) else None
        if reason:
            provenance = "configured" if self.manifest_path or self.manifest_url else "packaged"
            retryable = (
                self._download_error.get("retryable")
                if self._download_error is not None
                else None
            )
            self._record_install_failure(
                str(reason),
                provenance=provenance,
                retryable=retryable if isinstance(retryable, bool) else None,
            )
        else:
            self._install_reason = None

    @contextlib.contextmanager
    def _install_guard_locked(self, *, timeout_seconds: float | None = 0.0):
        """Serialize installs and archive cleanup, across processes too.

        Yields a ``(acquired, reason)`` pair. ``acquired`` is True when the
        guard is held; otherwise ``reason`` distinguishes contention
        (``runtime_install_already_running``) from a guard that cannot exist
        (``runtime_install_guard_unavailable``: read-only/full directory,
        symlinked or hard-linked lock file). The outermost caller in this
        process takes the cross-process file lock; nested calls on the same
        thread (post-install cleanup runs inside the install) reuse it, so the
        non-re-entrant ``flock`` never deadlocks against itself.
        """
        unavailable = (False, "runtime_install_guard_unavailable")
        busy = (False, "runtime_install_already_running")
        with self._install_guard:
            if self._install_guard_depth > 0:
                self._install_guard_depth += 1
                try:
                    yield (True, None)
                finally:
                    self._install_guard_depth -= 1
                return
            # A symlinked lock path would make the lock truncate/rewrite a
            # file outside the runtime directory; a hard link shares its
            # inode with unrelated content. Refuse both before any open.
            try:
                guard_lstat = self._install_guard_path.lstat()
                if not _is_exclusive_regular_file(guard_lstat):
                    logger.warning(
                        "Show Runtime install guard %s is a symlink or hard link; refusing to lock",
                        self._install_guard_path,
                    )
                    yield unavailable
                    return
            except FileNotFoundError:
                pass
            except OSError:
                logger.warning(
                    "Show Runtime install guard %s cannot be inspected; refusing to lock",
                    self._install_guard_path,
                    exc_info=True,
                )
                yield unavailable
                return
            # Open the guard ourselves with no-follow semantics so a raced
            # replacement (symlink/hard link swapped in after the lstat) can
            # never be truncated by MigrationFileLock's append-open; we then
            # validate the descriptor and hand the same handle's fd to the
            # lock so validation and locking cover one identical inode.
            guard_dir = self._install_guard_path.parent
            try:
                guard_dir.mkdir(parents=True, exist_ok=True)
                guard_flags = os.O_RDWR | os.O_CREAT
                if hasattr(os, "O_NOFOLLOW"):
                    guard_flags |= os.O_NOFOLLOW
                lock_fd = os.open(self._install_guard_path, guard_flags, 0o644)
            except OSError as exc:
                if exc.errno == errno.ELOOP:
                    logger.warning("Show Runtime install guard %s is a symlink; refusing to lock", self._install_guard_path)
                    yield unavailable
                    return
                logger.warning(
                    "Show Runtime install guard %s is unavailable",
                    self._install_guard_path,
                    exc_info=True,
                )
                yield unavailable
                return
            try:
                if not self._guard_path_matches_fd(lock_fd):
                    logger.warning(
                        "Show Runtime install guard descriptor is not an exclusive regular file; refusing",
                    )
                    os.close(lock_fd)
                    yield unavailable
                    return
                file_lock = MigrationFileLock(self._install_guard_path, timeout_seconds=timeout_seconds)
                file_lock._handle = os.fdopen(lock_fd, "a+", encoding="utf-8")
                deadline = (
                    None
                    if timeout_seconds is None
                    else time.monotonic() + timeout_seconds
                )
                while True:
                    file_lock._handle.seek(0)
                    if storage_lock_try_lock(file_lock._handle):
                        file_lock._handle.seek(0)
                        file_lock._handle.truncate()
                        file_lock._handle.write(str(os.getpid()))
                        file_lock._handle.flush()
                        break
                    if deadline is not None and time.monotonic() >= deadline:
                        file_lock._handle.close()
                        yield busy
                        return
                    time.sleep(0.1)
                if not self._guard_path_matches_fd(file_lock._handle.fileno()):
                    logger.warning(
                        "Show Runtime install guard path was replaced after lock acquisition; refusing",
                    )
                    file_lock.release()
                    yield unavailable
                    return
            except OSError:
                logger.warning(
                    "Show Runtime install guard %s could not be locked",
                    self._install_guard_path,
                    exc_info=True,
                )
                yield unavailable
                return
            except OSError:
                # A lock file that cannot be created/opened (read-only or full
                # runtime directory) is a structured "cannot serialize" outcome,
                # never an exception through prepare/clean.
                logger.warning(
                    "Show Runtime install guard %s is unavailable; treating as busy",
                    self._install_guard_path,
                    exc_info=True,
                )
                yield unavailable
                return
            self._install_guard_depth = 1
            try:
                yield (True, None)
            finally:
                self._install_guard_depth = 0
                try:
                    file_lock.release()
                except Exception:
                    logger.warning("Failed to release Show Runtime install guard", exc_info=True)

    def _install_manifest_runtime_locked(
        self,
        *,
        force: bool,
        offline: bool,
        candidate_validator: Callable[[list[str]], ShowRuntimeStartability] | None = None,
    ) -> list[str] | None:
        node = _resolve_node_command()
        if not node:
            self._install_reason = "runtime_node_missing"
            return None
        if force:
            self._invalidate_managed_runtime_projection()
        manager = self._shared_manifest_manager(offline=offline)
        retained_publication_key: tuple[str, str] | None = None

        def validate_entrypoint(entrypoint: Path) -> str | None:
            nonlocal retained_publication_key
            command = [*node, str(entrypoint)]
            if candidate_validator is not None:
                outcome = candidate_validator(command)
                if outcome.state is ShowRuntimeStartabilityState.NOT_STARTABLE:
                    return outcome.reason or "runtime_start_failed"
                if outcome.state is ShowRuntimeStartabilityState.UNDETERMINED:
                    return "runtime_start_verification_failed"
            install_dir = self._managed_install_dir_for_command(command)
            key = self._install_reference_key(install_dir) if install_dir else None
            if key is None:
                return "runtime_install_guard_unavailable"
            already_owned = self._owns_install_reference(key)
            if not self._retain_install_dir_locked(install_dir):
                return self._install_reason or "runtime_install_guard_unavailable"
            if not already_owned:
                retained_publication_key = key
            return None

        result = manager.ensure(
            force=force,
            validate_candidate=validate_entrypoint,
        )
        if not result.get("ok") and retained_publication_key is not None:
            self._release_install_reference(retained_publication_key)
        self._adopt_shared_manifest_evidence(manager, result)
        entrypoint = manager.installed_result_entrypoint(result)
        if (
            not force
            and result.get("reason") == "runtime_install_guard_unavailable"
        ):
            entrypoint = manager.resolve_selected_entrypoint()
        command = [*node, str(entrypoint)] if entrypoint is not None else None
        return self._managed_install_operation_command(
            command,
            replacement_required=force,
            replacement_completed=bool(force and result.get("changed")),
        )

    def _installed_archive_runtime_command(self) -> list[str] | None:
        node = _resolve_node_command()
        if not node:
            return None
        command = self._archive_runtime_command(self._archive_install_dir(), node)
        return command if command and self._retain_managed_command(command) else None

    def _install_archive_runtime(
        self,
        *,
        force: bool,
        offline: bool | None = None,
        candidate_validator: Callable[[list[str]], ShowRuntimeStartability] | None = None,
    ) -> list[str] | None:
        node = _resolve_node_command()
        if not node:
            self._install_reason = "runtime_node_missing"
            return None
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        install_dir = self._archive_install_dir()
        existing_command = self._archive_runtime_command(install_dir, node)
        if existing_command and not self._retain_managed_command(existing_command):
            existing_command = None
        archive = self._resolve_prebuilt_archive(offline=offline)
        if not archive:
            return self._managed_install_operation_command(
                existing_command,
                replacement_required=force,
            )
        archive_digest = file_sha256(archive)
        if not force and existing_command and self._archive_manifest_matches(archive_digest):
            if candidate_validator is not None:
                validation = candidate_validator(existing_command)
                if validation.state is not ShowRuntimeStartabilityState.STARTABLE:
                    self._install_reason = self._startability_failure_reason(validation)
                    return None
            return self._managed_install_operation_command(
                existing_command,
                replacement_required=False,
            )
        tmp_dir = Path(tempfile.mkdtemp(prefix="prebuilt-", dir=self.runtime_dir))
        candidate_dir: Path | None = None
        retained_publication_key: tuple[str, str] | None = None
        try:
            with tarfile.open(archive, "r:gz") as tar:
                safe_extract_tar(tar, tmp_dir)
            command = self._archive_runtime_command(tmp_dir, node)
            if not command:
                self._install_reason = "runtime_install_missing_bin"
                return self._managed_install_operation_command(
                    existing_command,
                    replacement_required=force,
                )
            versions_dir = self.runtime_dir / "prebuilt" / "versions"
            versions_dir.mkdir(parents=True, exist_ok=True)
            candidate_dir = Path(
                tempfile.mkdtemp(
                    prefix=f"{archive_digest[:16]}-",
                    dir=versions_dir,
                )
            )
            candidate_dir.rmdir()
            shutil.move(str(tmp_dir), str(candidate_dir))
            self._write_archive_manifest(archive_digest, install_dir=candidate_dir)
            installed_command = self._archive_runtime_command(candidate_dir, node)
            if not installed_command:
                self._install_reason = "runtime_install_missing_bin"
                return self._managed_install_operation_command(
                    existing_command,
                    replacement_required=force,
                )
            if candidate_validator is not None:
                validation = candidate_validator(installed_command)
                if validation.state is not ShowRuntimeStartabilityState.STARTABLE:
                    self._install_reason = self._startability_failure_reason(validation)
                    return self._managed_install_operation_command(
                        existing_command,
                        replacement_required=force,
                    )
            reference_key = self._install_reference_key(candidate_dir)
            already_owned = bool(
                reference_key is not None
                and self._owns_install_reference(reference_key)
            )
            if not self._retain_managed_command(installed_command):
                self._install_reason = self._install_reason or "runtime_install_guard_unavailable"
                return self._managed_install_operation_command(
                    existing_command,
                    replacement_required=force,
                )
            if reference_key is not None and not already_owned:
                retained_publication_key = reference_key
            self._write_provider_pointer(
                self.runtime_dir / "prebuilt",
                candidate_dir,
                provider=_RUNTIME_SOURCE_ARCHIVE,
            )
            candidate_dir = None
            return self._managed_install_operation_command(
                installed_command,
                replacement_required=force,
                replacement_completed=force and installed_command is not None,
            )
        except Exception:
            logger.exception("Failed to install prebuilt Show Runtime")
            self._install_reason = "runtime_install_failed"
            return self._managed_install_operation_command(
                existing_command,
                replacement_required=force,
            )
        finally:
            if candidate_dir is not None:
                if retained_publication_key is not None:
                    self._release_install_reference(retained_publication_key)
                shutil.rmtree(candidate_dir, ignore_errors=True)
            if tmp_dir.exists():
                shutil.rmtree(tmp_dir, ignore_errors=True)

    def _resolve_prebuilt_archive(self, *, offline: bool | None = None) -> Path | None:
        if self.archive_path:
            if self.archive_path.exists():
                return self.archive_path
            self._record_install_failure("runtime_archive_missing", provenance="configured")
            return None
        packaged = self._copy_packaged_runtime_archive()
        if packaged:
            return packaged
        if not self.archive_url:
            self._record_install_failure("runtime_archive_missing", provenance="packaged")
            return None
        if self.offline if offline is None else offline:
            self._install_reason = "runtime_archive_unavailable_offline"
            return None
        return self._download_runtime_archive(
            self.archive_url,
            provenance=self._archive_url_provenance,
        )

    def _copy_packaged_runtime_archive(self) -> Path | None:
        try:
            resource = package_resources.files("vibe").joinpath("show_runtime", _runtime_archive_name())
        except Exception:
            return None
        if not resource.is_file():
            return None
        target = self.runtime_dir / "downloads" / _runtime_archive_name()
        target.parent.mkdir(parents=True, exist_ok=True)
        with resource.open("rb") as source, target.open("wb") as destination:
            shutil.copyfileobj(source, destination)
        return target

    def _download_runtime_archive(self, archive_url: str, *, provenance: str) -> Path | None:
        target = self.runtime_dir / "downloads" / _runtime_archive_name()
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            fetch_to_path(
                archive_url,
                target,
                timeout=60,
                opener=urllib.request.urlopen,
            )
            self._download_error = None
        except Exception as exc:
            logger.exception("Failed to download prebuilt Show Runtime from %s", archive_url)
            self._record_download_failure(
                "runtime_archive_download_failed",
                exc,
                archive_url,
                provenance=provenance,
            )
            return None
        return target

    def _archive_install_dir(self) -> Path:
        return self._selected_provider_install_dir(
            self.runtime_dir / "prebuilt",
            legacy=self.runtime_dir / "prebuilt" / "current",
            provider=_RUNTIME_SOURCE_ARCHIVE,
        )

    def _archive_manifest_path(self, install_dir: Path | None = None) -> Path:
        return (install_dir or self._archive_install_dir()) / ".vibe-show-runtime.json"

    def _archive_manifest_matches(
        self,
        archive_digest: str,
        *,
        install_dir: Path | None = None,
    ) -> bool:
        try:
            payload = json.loads(
                self._archive_manifest_path(install_dir).read_text(encoding="utf-8")
            )
        except Exception:
            return False
        return payload.get("archive_name") == _runtime_archive_name() and payload.get("sha256") == archive_digest

    def _write_archive_manifest(self, archive_digest: str, *, install_dir: Path) -> None:
        write_atomic(
            self._archive_manifest_path(install_dir),
            json.dumps(
                {
                    "provider": _RUNTIME_SOURCE_ARCHIVE,
                    "runtime_id": "show-runtime",
                    "archive_name": _runtime_archive_name(),
                    "sha256": archive_digest,
                },
                sort_keys=True,
            )
            + "\n",
        )

    def _archive_runtime_command(self, install_dir: Path, node: list[str]) -> list[str] | None:
        cli_path = install_dir / "node_modules" / "@avibe" / "show-runtime" / "dist" / "cli.js"
        if not cli_path.exists():
            return None
        return [*node, str(cli_path)]

    def _install_npm_runtime(
        self,
        *,
        force: bool | None = None,
        candidate_validator: Callable[[list[str]], ShowRuntimeStartability] | None = None,
    ) -> list[str] | None:
        replacement_required = self.force_install if force is None else force
        try:
            npm = _resolve_command("npm")
        except (OSError, ValueError):
            npm = None
        if not npm:
            self._install_reason = "runtime_npm_missing"
            return self._managed_install_operation_command(
                None,
                replacement_required=replacement_required,
            )
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        existing_command_value = _resolve_executable_path(self._managed_bin_path())
        existing_command = [existing_command_value] if existing_command_value else None
        if existing_command and not self._retain_managed_command(existing_command):
            existing_command = None
        staging_root = Path(tempfile.mkdtemp(prefix="npm-", dir=self.runtime_dir))
        package_json = staging_root / "package.json"
        package_json.write_text('{"private":true,"type":"module"}\n', encoding="utf-8")
        candidate_dir: Path | None = None
        retained_publication_key: tuple[str, str] | None = None
        try:
            with self.install_log_path.open("w", encoding="utf-8") as log:
                result = subprocess.run(
                    [
                        *npm,
                        "install",
                        "--prefix",
                        str(staging_root),
                        "--no-audit",
                        "--no-fund",
                        self.package_spec,
                    ],
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    text=True,
                    timeout=180,
                    check=False,
                    **isolated_subprocess_kwargs(),
                )
        except (OSError, subprocess.SubprocessError):
            logger.warning("Failed to install the npm Show Runtime", exc_info=True)
            self._install_reason = "runtime_install_failed"
            shutil.rmtree(staging_root, ignore_errors=True)
            return self._managed_install_operation_command(
                existing_command,
                replacement_required=replacement_required,
            )
        if result.returncode != 0:
            self._install_reason = "runtime_install_failed"
            shutil.rmtree(staging_root, ignore_errors=True)
            return self._managed_install_operation_command(
                existing_command,
                replacement_required=replacement_required,
            )
        try:
            resolved = _resolve_executable_path(self._npm_bin_path(staging_root))
            if not resolved:
                self._install_reason = "runtime_install_missing_bin"
                return self._managed_install_operation_command(
                    existing_command,
                    replacement_required=replacement_required,
                )
            versions_dir = self.runtime_dir / "package" / "versions"
            versions_dir.mkdir(parents=True, exist_ok=True)
            candidate_dir = Path(tempfile.mkdtemp(prefix="npm-", dir=versions_dir))
            candidate_dir.rmdir()
            shutil.move(str(staging_root), str(candidate_dir))
            installed_value = _resolve_executable_path(self._npm_bin_path(candidate_dir))
            if not installed_value:
                self._install_reason = "runtime_install_missing_bin"
                return self._managed_install_operation_command(
                    existing_command,
                    replacement_required=replacement_required,
                )
            installed_command = [installed_value]
            write_atomic(
                candidate_dir / ".vibe-show-runtime.json",
                json.dumps(
                    {
                        "provider": _RUNTIME_SOURCE_NPM,
                        "runtime_id": "show-runtime",
                        "package_spec": self.package_spec,
                    },
                    sort_keys=True,
                )
                + "\n",
            )
            if candidate_validator is not None:
                validation = candidate_validator(installed_command)
                if validation.state is not ShowRuntimeStartabilityState.STARTABLE:
                    self._install_reason = self._startability_failure_reason(validation)
                    return self._managed_install_operation_command(
                        existing_command,
                        replacement_required=replacement_required,
                    )
            reference_key = self._install_reference_key(candidate_dir)
            already_owned = bool(
                reference_key is not None
                and self._owns_install_reference(reference_key)
            )
            if not self._retain_managed_command(installed_command):
                self._install_reason = self._install_reason or "runtime_install_guard_unavailable"
                return self._managed_install_operation_command(
                    existing_command,
                    replacement_required=replacement_required,
                )
            if reference_key is not None and not already_owned:
                retained_publication_key = reference_key
            self._write_provider_pointer(
                self.runtime_dir / "package",
                candidate_dir,
                provider=_RUNTIME_SOURCE_NPM,
            )
            candidate_dir = None
            return self._managed_install_operation_command(
                installed_command,
                replacement_required=replacement_required,
                replacement_completed=replacement_required,
            )
        finally:
            if candidate_dir is not None:
                if retained_publication_key is not None:
                    self._release_install_reference(retained_publication_key)
                shutil.rmtree(candidate_dir, ignore_errors=True)
            if staging_root.exists():
                shutil.rmtree(staging_root, ignore_errors=True)

    def _managed_bin_path(self) -> Path:
        return self._npm_bin_path(self._package_install_dir())

    def _package_install_dir(self) -> Path:
        return self._selected_provider_install_dir(
            self.runtime_dir / "package",
            legacy=self.runtime_dir / "package",
            provider=_RUNTIME_SOURCE_NPM,
        )

    @staticmethod
    def _npm_bin_path(install_dir: Path) -> Path:
        suffix = ".cmd" if os.name == "nt" else ""
        return install_dir / "node_modules" / ".bin" / f"{_RUNTIME_BIN}{suffix}"

    @staticmethod
    def _startability_failure_reason(validation: ShowRuntimeStartability) -> str:
        if validation.state is ShowRuntimeStartabilityState.NOT_STARTABLE:
            return validation.reason or "runtime_start_failed"
        return "runtime_start_verification_failed"

    def _selected_provider_install_dir(
        self,
        namespace: Path,
        *,
        legacy: Path,
        provider: str,
    ) -> Path:
        pointer_path = namespace / "current.json"
        try:
            payload = json.loads(pointer_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return legacy
        except (OSError, UnicodeError, ValueError) as exc:
            raise OSError(f"runtime pointer is unreadable: {pointer_path}") from exc
        try:
            install_dir = Path(payload["install_dir"]).expanduser().resolve(strict=True)
            versions_dir = (namespace / "versions").resolve(strict=True)
        except (KeyError, OSError, RuntimeError, TypeError) as exc:
            raise OSError(f"runtime pointer is unreadable: {pointer_path}") from exc
        if payload.get("provider") != provider or install_dir.parent != versions_dir:
            raise OSError(f"runtime pointer is not confined: {pointer_path}")
        return install_dir

    @staticmethod
    def _write_provider_pointer(namespace: Path, install_dir: Path, *, provider: str) -> None:
        write_atomic(
            namespace / "current.json",
            json.dumps(
                {
                    "provider": provider,
                    "runtime_id": "show-runtime",
                    "install_dir": str(install_dir),
                },
                sort_keys=True,
            )
            + "\n",
        )


_manager: ShowRuntimeManager | None = None


def get_show_runtime_manager() -> ShowRuntimeManager:
    global _manager
    if _manager is None:
        _manager = ShowRuntimeManager()
    return _manager


def stop_show_runtime_manager() -> None:
    if _manager is not None:
        _manager.stop()


def _is_runtime_server_cmdline(cmdline: list[str], workspace_root: str) -> bool:
    """True if ``cmdline`` is a Show Runtime server bound to ``workspace_root``.

    Requires the exact ``--workspace-root <workspace_root>`` arg pair AND a runtime
    signature (the always-present ``--fallback-delay-seconds`` flag, the ``cli.js``
    entrypoint, the managed bin, or a ``show-runtime`` path token), so an unrelated
    process that merely mentions the path is never matched.
    """
    if not cmdline:
        return False
    bound = any(
        token == "--workspace-root" and index + 1 < len(cmdline) and cmdline[index + 1] == workspace_root
        for index, token in enumerate(cmdline)
    )
    if not bound:
        return False
    return any(
        token == "--fallback-delay-seconds"
        or token.endswith("cli.js")
        or token.endswith(_RUNTIME_BIN)
        or "show-runtime" in token
        for token in cmdline
    )


def sweep_orphan_show_runtime_servers(
    workspace_root: Path | str | None = None,
    *,
    keep_pid: int | None = None,
) -> list[int]:
    """Terminate any Show Runtime server still bound to ``workspace_root``.

    A prior avibe instance that died without reaping its child (SIGKILL / crash —
    ``atexit`` does not run) leaves a Node ``cli.js`` orphan reparented to init, still
    listening on its old port and able to warm/mutate this workspace root with stale
    in-memory templates (avibe-bot/avibe#813). The single-service-instance lock makes
    this process the only legitimate owner of the root, so any *other* process bound to
    it is an orphan and safe to reap.

    Best-effort and spawn-agnostic: complements the runtime's own parent-death self-exit
    (vibe-show-runtime#30) by also clearing orphans from builds that predate that
    backstop. Returns the pids swept (for logging/tests).
    """
    root = str(workspace_root) if workspace_root is not None else str(paths.get_show_pages_dir())
    try:
        import psutil
    except Exception:  # pragma: no cover - psutil is a hard dependency in practice
        return []

    own_pid = os.getpid()
    swept: list[int] = []
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            pid = proc.info.get("pid")
            if pid is None or pid == own_pid or (keep_pid is not None and pid == keep_pid):
                continue
            if not _is_runtime_server_cmdline(proc.info.get("cmdline") or [], root):
                continue
            victims = proc.children(recursive=True)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        except Exception:  # pragma: no cover - never let a stray psutil error block callers
            logger.debug("Failed to inspect process while sweeping show runtime orphans", exc_info=True)
            continue
        victims.append(proc)
        logger.warning(
            "Sweeping orphaned show runtime server pid=%s bound to workspace_root=%s", pid, root
        )
        for victim in victims:
            try:
                victim.terminate()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        _gone, alive = psutil.wait_procs(victims, timeout=3)
        for victim in alive:
            try:
                victim.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        swept.append(pid)
    return swept


async def prewarm_show_runtime() -> ShowRuntimeAvailability:
    return await get_show_runtime_manager().ensure()


async def prewarm_show_page_session(
    session_id: str,
    *,
    context: ShowRuntimeContext,
) -> ShowRuntimeResult:
    return await get_show_runtime_manager().prewarm_session(
        session_id,
        context=context,
    )


def _show_runtime_app_session_part(path: str) -> str | None:
    match = re.match(r"^/sessions/([^/]+)/app(?:/|$)", path)
    return match.group(1) if match else None


def set_show_runtime_manager_for_tests(manager: ShowRuntimeManager | None) -> None:
    global _manager
    previous = _manager
    # Stop the manager we are replacing before dropping the reference. Serving-path
    # tests that never install a fake cause get_show_runtime_manager() to lazily
    # create the real manager, which spawns a Node cli.js + esbuild subprocess tree
    # when a runtime is installed locally. If a later test swaps the global without
    # stopping it first, the reference is lost, the atexit cleanup at process exit
    # can no longer reap it, and the subprocess tree leaks for the machine's lifetime.
    if previous is not None and previous is not manager:
        try:
            previous.stop()
        except Exception:  # pragma: no cover - defensive cleanup
            logger.debug("failed to stop previous show runtime manager", exc_info=True)
    _manager = manager


def _show_runtime_prewarm_import_paths(
    response: httpx.Response,
    *,
    session_id: str,
    runtime_path: str,
    base_path: str | None,
) -> list[str]:
    content_type = response.headers.get("content-type", "")
    if "javascript" not in content_type and "html" not in content_type and "css" not in content_type:
        return []
    try:
        text = response.text
    except UnicodeDecodeError:
        return []
    paths: list[str] = []
    seen: set[str] = set()
    for match in _PREWARM_IMPORT_RE.finditer(text):
        value = match.group("path")
        path = _show_runtime_prewarm_runtime_path(
            value,
            session_id=session_id,
            runtime_path=runtime_path,
            base_path=base_path,
        )
        if path and path not in seen:
            seen.add(path)
            paths.append(path)
    return paths


def _show_runtime_prewarm_runtime_path(
    value: str,
    *,
    session_id: str,
    runtime_path: str,
    base_path: str | None,
) -> str | None:
    if not value or value.startswith(("http://", "https://", "data:", "blob:", "#")):
        return None
    raw_path, separator, query = value.partition("?")
    if not _show_runtime_prewarm_asset_path_allowed(raw_path):
        return None
    session_prefixes = [f"/show/{session_id}/", f"/p/{session_id}/"]
    if base_path:
        session_prefixes.insert(0, base_path.rstrip("/") + "/")
    for prefix in session_prefixes:
        if raw_path.startswith(prefix):
            asset_path = raw_path[len(prefix):]
            return _join_show_runtime_prewarm_path(runtime_path, asset_path, separator, query)
    # The shared vendor bundle (`/_show-runtime/vendor/...`) is session-independent and
    # the runtime warms it itself, so it is intentionally not prewarmed per session here.
    if raw_path.startswith("/src/") or raw_path.startswith("/@") or raw_path.startswith("/node_modules/"):
        return _join_show_runtime_prewarm_path(runtime_path, raw_path.lstrip("/"), separator, query)
    if raw_path.startswith("./"):
        return _join_show_runtime_prewarm_path(runtime_path, raw_path[2:], separator, query)
    if raw_path.startswith(("src/", "@", "node_modules/")):
        return _join_show_runtime_prewarm_path(runtime_path, raw_path, separator, query)
    return None


def _show_runtime_prewarm_asset_path_allowed(path: str) -> bool:
    if not path:
        return False
    if path.startswith(("/home/", "/Users/", "/tmp/", "/var/", "/private/")):
        return False
    return path.endswith((".js", ".mjs", ".ts", ".tsx", ".css")) or path in {
        "/@vite/client",
        "/@react-refresh",
    }


def _join_show_runtime_prewarm_path(runtime_path: str, asset_path: str, separator: str, query: str) -> str:
    path = f"{runtime_path}{urllib.parse.quote(asset_path.lstrip('/'), safe='/@:-._~')}"
    if separator:
        path = f"{path}?{query}"
    return path


def _show_runtime_capability_retry_delay(attempt: int) -> float:
    exponent = max(0, min(attempt - 1, 16))
    ceiling = min(_CAPABILITY_RETRY_MAX_SECONDS, _CAPABILITY_RETRY_BASE_SECONDS * (2**exponent))
    return ceiling * (0.5 + random.random() * 0.5)


def _auto_install_enabled() -> bool:
    value = os.environ.get("VIBE_SHOW_RUNTIME_AUTO_INSTALL")
    return value is None or value.strip().lower() not in _FALSE_VALUES


def _packaged_runtime_manifest_exists() -> bool:
    try:
        resource = package_resources.files("vibe").joinpath(_RUNTIME_MANIFEST_RESOURCE)
    except Exception:
        return False
    return resource.is_file()


def _normalize_runtime_source(value: str | None) -> str:
    retired_source = retired_show_runtime_source(value)
    normalized = retired_source or (value or _RUNTIME_SOURCE_MANIFEST).strip().lower()
    aliases = {
        "manifest": _RUNTIME_SOURCE_MANIFEST,
        "manifest-cache": _RUNTIME_SOURCE_MANIFEST,
        "archive": _RUNTIME_SOURCE_ARCHIVE,
        "prebuilt": _RUNTIME_SOURCE_ARCHIVE,
        "npm": _RUNTIME_SOURCE_NPM,
    }
    if retired_source is not None and retired_source not in _WARNED_RETIRED_RUNTIME_SOURCES:
        _WARNED_RETIRED_RUNTIME_SOURCES.add(retired_source)
        logger.warning(
            "VIBE_SHOW_RUNTIME_SOURCE=%s is retired; using %s instead",
            retired_source,
            _RUNTIME_SOURCE_MANIFEST,
        )
    if retired_source is not None:
        return _RUNTIME_SOURCE_MANIFEST
    return aliases.get(normalized, normalized or _RUNTIME_SOURCE_MANIFEST)


def _manifest_status_payload(manifest: ManagedRuntimeManifest | None) -> dict[str, Any] | None:
    if manifest is None:
        return None
    return {
        "schema_version": manifest.schema_version,
        "runtime_version": manifest.runtime_version,
        "minimum_node": _ShowManifestRuntimeManager._minimum_node(manifest),
        "sha256": manifest.digest,
        "source": manifest.loaded_from,
        "platforms": sorted(manifest.archives),
    }


def _persisted_manifest_status_payload(metadata: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": None,
        "runtime_version": metadata.get("runtime_version"),
        "minimum_node": None,
        "sha256": metadata.get("manifest_sha256"),
        "source": metadata.get("manifest_source"),
        "platforms": [metadata.get("platform")],
    }


def _persisted_manifest_runtime_version(metadata: Mapping[str, Any]) -> str | None:
    runtime_version = metadata.get("runtime_version")
    return runtime_version if isinstance(runtime_version, str) and runtime_version else None


def _persisted_archive_status_payload(metadata: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "platform": metadata.get("platform"),
        "name": metadata.get("archive_name"),
        "url": None,
        "sha256": metadata.get("archive_sha256"),
        "size": None,
    }


def _redact_download_url(url: str) -> str:
    return redact_url(url)


def _runtime_download_error(exc: BaseException, url: str) -> dict[str, Any]:
    return dependency_error_details(exc, url)


def _archive_status_payload(archive: ManagedRuntimeArchive | None) -> dict[str, Any] | None:
    if archive is None:
        return None
    return {
        "platform": archive.platform,
        "name": archive.name,
        "url": _redact_download_url(archive.url),
        "sha256": archive.sha256,
        "size": archive.size,
    }


def _runtime_archive_name() -> str:
    return f"{_RUNTIME_ARCHIVE_PREFIX}-{runtime_platform_tag()}.tgz"


def _default_runtime_archive_url() -> str:
    return f"{_RUNTIME_ARCHIVE_RELEASE_BASE_URL}/{_runtime_archive_name()}"


def _node_version(node: list[str]) -> tuple[int, int, int] | None:
    try:
        result = subprocess.run(
            [*node, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            **isolated_subprocess_kwargs(),
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return _parse_semver(result.stdout.strip())


def _parse_semver(value: str) -> tuple[int, int, int] | None:
    match = re.search(r"v?(\d+)\.(\d+)\.(\d+)", value)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def _format_semver(version: tuple[int, int, int] | None) -> str | None:
    if version is None:
        return None
    return ".".join(str(part) for part in version)


def _node_satisfies_requirement(version: tuple[int, int, int] | None, requirement: str | None) -> bool | None:
    if not requirement:
        return None
    if version is None:
        return False
    return any(_node_satisfies_clause(version, clause.strip()) for clause in requirement.split("||") if clause.strip())


def _node_satisfies_clause(version: tuple[int, int, int], clause: str) -> bool:
    if clause.startswith(">="):
        minimum = _parse_semver(clause[2:].strip())
        return minimum is not None and version >= minimum
    if clause.startswith("^"):
        minimum = _parse_semver(clause[1:].strip())
        if minimum is None or version < minimum:
            return False
        major, minor, patch = minimum
        if major > 0:
            ceiling = (major + 1, 0, 0)
        elif minor > 0:
            ceiling = (major, minor + 1, 0)
        else:
            ceiling = (major, minor, patch + 1)
        return version < ceiling
    exact = _parse_semver(clause)
    return exact is not None and version == exact


def _resolve_command(command: str) -> list[str] | None:
    parts = shlex.split(command)
    if not parts:
        return None
    executable = parts[0]
    if os.path.sep in executable or (os.altsep is not None and os.altsep in executable):
        path = Path(executable).expanduser()
        resolved = str(path) if path.exists() and os.access(path, os.X_OK) else None
    else:
        resolved = shutil.which(executable)
    if not resolved:
        return None
    return [resolved, *parts[1:]]


def _resolve_node_command() -> list[str] | None:
    configured = os.environ.get("VIBE_SHOW_RUNTIME_NODE_BIN")
    try:
        if configured:
            return _resolve_command(configured)
        return _resolve_command("node")
    except (OSError, ValueError):
        return None


def _resolve_executable_path(path: Path) -> str | None:
    expanded = path.expanduser()
    return str(expanded) if expanded.exists() and os.access(expanded, os.X_OK) else None


atexit.register(stop_show_runtime_manager)
