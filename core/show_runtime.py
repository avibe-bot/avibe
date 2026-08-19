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
import platform
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
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from sysconfig import get_platform
from typing import Any, Iterable, Iterator, Mapping

import httpx

from storage.lock import MigrationFileLock, MigrationLockTimeout, _try_lock as storage_lock_try_lock

from config import paths
from core.dependency_network import dependency_error_details, fetch_bytes, fetch_to_path, probe_url, redact_url
from core.show_pages import SHOW_RUNTIME_RECOVERY_LOADING_DELAY_SECONDS
from core.process_isolation import KILL_SIGNAL, isolated_subprocess_kwargs, signal_process_tree


logger = logging.getLogger(__name__)
_RUNTIME_BIN = "avibe-show-runtime"
_RUNTIME_PACKAGE = "@avibe/show-runtime"
_RUNTIME_ARCHIVE_PREFIX = "vibe-show-runtime-node"
_RUNTIME_ARCHIVE_RELEASE_BASE_URL = "https://github.com/avibe-bot/vibe-show-runtime/releases/latest/download"
_RUNTIME_GITHUB_REPO = "https://github.com/avibe-bot/vibe-show-runtime.git"
_RUNTIME_GITHUB_REF = "main"
_RUNTIME_SOURCE_MANIFEST = "manifest-cache"
_RUNTIME_SOURCE_ARCHIVE = "archive"
_RUNTIME_SOURCE_GITHUB = "github"
_RUNTIME_SOURCE_NPM = "npm"
_RUNTIME_MANIFEST_RESOURCE = "show_runtime_manifest.json"
_PACKAGED_RUNTIME_MANIFEST_SOURCE = f"package:{_RUNTIME_MANIFEST_RESOURCE}"
_MANAGED_RUNTIME_ROLLBACK_INSTALLS = 1
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


class _ArchiveMetadataError(Exception):
    """A retained install's metadata could not be read; destructive cleanup must abort."""


class _ArchiveInspectionError(Exception):
    """The archive cache itself could not be inspected; cleanup must abort."""


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
SHOW_RUNTIME_CONTEXT_KEY_FEATURE = "show-context-key-v1"
_CAPABILITY_RETRY_BASE_SECONDS = 0.25
_CAPABILITY_RETRY_MAX_SECONDS = 5.0
_CAPABILITY_RETRYABLE_STATUS_CODES = {408, 429}
_MISSING = object()


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


@dataclass(frozen=True)
class ShowRuntimeResult:
    available: bool
    base_url: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class ShowRuntimeArchive:
    platform: str
    name: str
    url: str
    sha256: str
    size: int | None = None


@dataclass(frozen=True)
class ShowRuntimeManifest:
    schema_version: int
    runtime_version: str
    minimum_node: str | None
    archives: dict[str, ShowRuntimeArchive]
    digest: str
    source: str


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
        github_repo: str | None = None,
        github_ref: str | None = None,
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
        if (
            source_value is None
            and not self.manifest_path
            and not self.manifest_url
            and not _packaged_runtime_manifest_exists()
            and _running_from_development_checkout()
        ):
            source_value = _RUNTIME_SOURCE_GITHUB
        self.auto_install = _auto_install_enabled() if auto_install is None else auto_install
        self.package_spec = package_spec or os.environ.get("VIBE_SHOW_RUNTIME_PACKAGE_SPEC") or _RUNTIME_PACKAGE
        self.runtime_source = _normalize_runtime_source(source_value)
        self.archive_url = archive_url if archive_url is not None else os.environ.get(
            "VIBE_SHOW_RUNTIME_ARCHIVE_URL",
            _default_runtime_archive_url(),
        )
        self.github_repo = github_repo or os.environ.get("VIBE_SHOW_RUNTIME_GITHUB_REPO") or _RUNTIME_GITHUB_REPO
        self.github_ref = github_ref or os.environ.get("VIBE_SHOW_RUNTIME_GITHUB_REF") or _RUNTIME_GITHUB_REF
        self.offline = _env_flag_enabled("VIBE_SHOW_RUNTIME_OFFLINE", default=False) if offline is None else offline
        self.force_install = force_install
        self.stdout_path = self.runtime_dir / "stdout.log"
        self.stderr_path = self.runtime_dir / "stderr.log"
        self.install_log_path = self.runtime_dir / "install.log"
        self.cache_root = self.runtime_dir / "vite-cache"
        self._install_attempted = False
        self._install_reason: str | None = None
        self._download_error: dict[str, Any] | None = None
        self._managed_command: list[str] | None = None
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
        self._capability_retry_deadline = 0.0
        self._capability_retry_attempt = 0
        self._capability_generation = 0

    async def ensure(self) -> ShowRuntimeResult:
        if self._base_url and await self._healthy(self._base_url):
            return ShowRuntimeResult(True, self._base_url)
        async with self._lock:
            if self._base_url and await self._healthy(self._base_url):
                return ShowRuntimeResult(True, self._base_url)
            self.stop()
            command = _resolve_command(self.command) if self._command_explicit else None
            if not command:
                command = await self._resolve_managed_command()
            if not command:
                return ShowRuntimeResult(False, reason=self._install_reason or "runtime_command_missing")
            self.runtime_dir.mkdir(parents=True, exist_ok=True)
            self.workspace_root.mkdir(parents=True, exist_ok=True)
            self.cache_root.mkdir(parents=True, exist_ok=True)
            # Reap any orphaned runtime server still bound to this workspace root before
            # spawning ours, so there is a single writer (avibe#813). self.stop() above
            # already released our own tracked child; anything left is a stray from a
            # prior avibe instance that died without reaping it (SIGKILL / crash). Run it
            # off the event loop: the psutil scan + terminate/kill can block for seconds.
            await asyncio.to_thread(self._sweep_orphan_runtime_servers)
            with self.stdout_path.open("w", encoding="utf-8") as stdout, self.stderr_path.open(
                "w", encoding="utf-8"
            ) as stderr:
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
                        str(SHOW_RUNTIME_RECOVERY_LOADING_DELAY_SECONDS),
                    ],
                    stdout=stdout,
                    stderr=stderr,
                    text=True,
                    **isolated_subprocess_kwargs(),
                )
            base_url = await self._read_startup_url()
            if not base_url:
                self.stop()
                return ShowRuntimeResult(False, reason="runtime_start_failed")
            self._base_url = base_url
            return ShowRuntimeResult(True, base_url)

    async def request(
        self,
        method: str,
        path: str,
        *,
        envelope: ShowRuntimeProtocolEnvelope,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
    ) -> httpx.Response:
        ready = await self.ensure()
        if not ready.available or not ready.base_url:
            raise RuntimeError(ready.reason or "show runtime unavailable")
        await self._negotiate_context_key_capability(ready.base_url)
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=5.0)) as client:
            return await client.request(
                method,
                f"{ready.base_url}{path}",
                headers=envelope.headers(headers),
                content=body,
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
        ready = await self.ensure()
        if not ready.available or not ready.base_url:
            raise RuntimeError(ready.reason or "show runtime unavailable")
        blocked = {SHOW_RUNTIME_PROTOCOL_HEADER.lower(), SHOW_RUNTIME_CONTEXT_HEADER.lower()}
        forwarded = {
            key: value
            for key, value in (headers or {}).items()
            if key.lower() not in blocked
        }
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=5.0)) as client:
            return await client.request(method, f"{ready.base_url}{path}", headers=forwarded, content=body)

    async def prewarm_session(
        self,
        session_id: str,
        *,
        context: ShowRuntimeContext,
        base_path: str | None = None,
    ) -> ShowRuntimeResult:
        session_part = urllib.parse.quote(session_id, safe="")
        runtime_path = f"/sessions/{session_part}/app/"
        headers = {"x-vibe-show-base": base_path} if base_path else None
        envelope = ShowRuntimeProtocolEnvelope(context)
        try:
            response = await self.request("GET", runtime_path, envelope=envelope, headers=headers)
            if response.status_code >= 500:
                return ShowRuntimeResult(False, reason=f"session_prewarm_failed:{response.status_code}")
            result = await self._prewarm_session_module_graph(
                session_id,
                runtime_path=runtime_path,
                envelope=envelope,
                headers=headers,
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
        headers: dict[str, str] | None,
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
            response = await self.request("GET", path, envelope=envelope, headers=headers)
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
            raise RuntimeError(ready.reason or "show runtime unavailable")
        await self._negotiate_context_key_capability(ready.base_url)
        url = f"{ready.base_url.replace('http://', 'ws://', 1).replace('https://', 'wss://', 1)}{path}"
        return ShowRuntimeWebSocketTarget(url=url, headers=envelope.headers())

    async def context_key_capability(self) -> ShowRuntimeContextCapability:
        ready = await self.ensure()
        if not ready.available or not ready.base_url:
            return ShowRuntimeContextCapability.TRANSIENT_UNKNOWN
        return await self._negotiate_context_key_capability(ready.base_url)

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
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(2.0, connect=0.5)) as client:
                response = await client.get(f"{base_url}/capabilities")
        except (httpx.TimeoutException, httpx.TransportError):
            return ShowRuntimeContextCapability.TRANSIENT_UNKNOWN

        if response.status_code == 404:
            return ShowRuntimeContextCapability.UNSUPPORTED
        if response.status_code in _CAPABILITY_RETRYABLE_STATUS_CODES or response.status_code >= 500:
            return ShowRuntimeContextCapability.TRANSIENT_UNKNOWN
        if not 200 <= response.status_code < 300:
            return ShowRuntimeContextCapability.TRANSIENT_UNKNOWN
        try:
            payload = response.json()
        except (UnicodeDecodeError, ValueError):
            return ShowRuntimeContextCapability.TRANSIENT_UNKNOWN
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

    def _runtime_identity(self, base_url: str) -> tuple[str, int | None]:
        process = self._process
        return base_url, getattr(process, "pid", None) if process is not None else None

    def _clear_capability_state(self, *, identity: tuple[str, int | None] | None = None) -> None:
        self._capability_identity = identity
        self._context_key_capability = None
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

    async def _read_startup_url(self) -> str | None:
        deadline = asyncio.get_running_loop().time() + 10
        while asyncio.get_running_loop().time() < deadline:
            if self._process and self._process.poll() is not None:
                return None
            try:
                text = self.stdout_path.read_text(encoding="utf-8")
            except FileNotFoundError:
                text = ""
            for line in reversed(text.splitlines()):
                marker = "Vibe Show Runtime listening at "
                if marker in line:
                    return line.split(marker, 1)[1].strip()
            await asyncio.sleep(0.05)
        return None

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

    async def _resolve_managed_command(self) -> list[str] | None:
        if self._command_explicit and self.command != _RUNTIME_BIN:
            self._install_reason = "runtime_command_missing"
            return None
        if self.runtime_source == _RUNTIME_SOURCE_MANIFEST:
            command = None if self.force_install else self._installed_manifest_runtime_command()
            if command:
                self._managed_command = command
                return command
            if self.auto_install and not self._install_attempted:
                self._install_attempted = True
                command = await asyncio.to_thread(self._install_managed_runtime)
                if command:
                    self._managed_command = command
                    return command
            command = self._installed_manifest_runtime_command()
            if command:
                self._managed_command = command
                return command
            if self._managed_command:
                return self._managed_command
            return None
        if self.runtime_source == _RUNTIME_SOURCE_ARCHIVE:
            if self.auto_install and not self._install_attempted:
                self._install_attempted = True
                command = await asyncio.to_thread(self._install_managed_runtime)
                if command:
                    self._managed_command = command
                    return command
            command = self._installed_archive_runtime_command()
            if command:
                self._managed_command = command
                return command
            if self._managed_command:
                return self._managed_command
        else:
            managed = self._managed_bin_path()
            resolved = _resolve_executable_path(managed)
            if resolved:
                return [resolved]
            if self._managed_command:
                return self._managed_command
        if self.runtime_source == _RUNTIME_SOURCE_GITHUB:
            command = self._installed_github_runtime_command()
            if command:
                self._managed_command = command
                return command
        if not self.auto_install:
            self._install_reason = "runtime_command_missing"
            return None
        if self._install_attempted:
            return None
        self._install_attempted = True
        command = await asyncio.to_thread(self._install_managed_runtime)
        if command:
            self._managed_command = command
        return command

    def _install_managed_runtime(self) -> list[str] | None:
        command: list[str] | None
        if self.runtime_source == _RUNTIME_SOURCE_MANIFEST:
            command = self._install_manifest_runtime()
        elif self.runtime_source == _RUNTIME_SOURCE_ARCHIVE:
            command = self._install_archive_runtime()
        elif self.runtime_source == _RUNTIME_SOURCE_GITHUB:
            command = self._install_github_runtime()
        elif self.runtime_source == _RUNTIME_SOURCE_NPM:
            command = self._install_npm_runtime()
        else:
            self._install_reason = "runtime_source_unsupported"
            return None
        if command:
            self._download_error = None
            self._clean_after_managed_install(command)
        return command

    def _clean_after_managed_install(self, command: list[str]) -> None:
        try:
            if self.runtime_source == _RUNTIME_SOURCE_MANIFEST:
                protected_install_dirs = self._manifest_install_dirs_for_command(command)
                removed = self._clean_manifest_install_dirs(
                    keep_previous=_MANAGED_RUNTIME_ROLLBACK_INSTALLS,
                    protected_install_dirs=protected_install_dirs,
                )
                if removed:
                    logger.info("Removed %d stale managed Show Runtime install(s)", len(removed))
            archives = self._clean_downloaded_archives()
            if archives.get("removed_count"):
                logger.info(
                    "Removed %d stale Show Runtime archive(s), reclaimed %d byte(s)",
                    archives["removed_count"],
                    archives["removed_bytes"],
                )
        except Exception:
            # Cache cleanup must never turn a usable runtime install into a failed prepare.
            logger.warning("Failed to clean stale managed Show Runtime installs", exc_info=True)

    def status(self) -> dict[str, Any]:
        configured_command = _resolve_command(self.command) if self._command_explicit else None
        manifest = self._load_runtime_manifest() if self.runtime_source == _RUNTIME_SOURCE_MANIFEST else None
        platform_tag = _runtime_platform_tag()
        node = _resolve_node_command()
        node_version = _node_version(node) if node else None
        node_supported = _node_satisfies_requirement(node_version, manifest.minimum_node) if manifest else None
        installed_command: list[str] | None = configured_command
        installed_dir: Path | None = None
        archive: ShowRuntimeArchive | None = None
        archive_status: dict[str, Any] | None = None
        installed_matches = False
        if not configured_command and manifest:
            archive = manifest.archives.get(platform_tag)
            if archive:
                installed_dir = self._manifest_install_dir(manifest, archive)
                installed_matches = self._manifest_install_matches(installed_dir, manifest, archive)
                if installed_matches and node and node_supported is not False:
                    installed_command = self._manifest_runtime_command(installed_dir, node)
        elif not configured_command and self.runtime_source == _RUNTIME_SOURCE_ARCHIVE:
            installed_dir = self._archive_install_dir()
            installed_command = self._archive_runtime_command(installed_dir, node or ["node"])
            archive_status = {
                "platform": platform_tag,
                "name": _runtime_archive_name(),
                "url": _redact_download_url(self.archive_url) if not self.archive_path else None,
                "path": str(self.archive_path) if self.archive_path else None,
                "sha256": None,
                "size": None,
            }
        elif not configured_command and self.runtime_source == _RUNTIME_SOURCE_GITHUB:
            installed_dir = self._github_source_dir()
            installed_command = self._github_runtime_command(installed_dir, node or ["node"])
        elif not configured_command and self.runtime_source == _RUNTIME_SOURCE_NPM:
            managed = _resolve_executable_path(self._managed_bin_path())
            installed_command = [managed] if managed else None
        return {
            "provider": self.runtime_source,
            "platform": platform_tag,
            "explicit_command": self.command if self._command_explicit else None,
            "node_available": node is not None,
            "node_version": _format_semver(node_version),
            "node_supported": node_supported,
            "manifest": _manifest_status_payload(manifest),
            "archive": archive_status or _archive_status_payload(archive),
            "installed": installed_command is not None,
            "installed_matches_manifest": installed_matches,
            "install_dir": str(installed_dir) if installed_dir else None,
            "command": installed_command,
            "reason": self._install_reason,
            "download_error": self._download_error,
        }

    def probe_archive_reachability(self, *, timeout: float = 10.0) -> dict[str, Any]:
        """Check the selected archive without downloading its body or mutating the cache."""
        archive_url: str | None = None
        if self.runtime_source == _RUNTIME_SOURCE_MANIFEST:
            manifest = self._load_runtime_manifest()
            if not manifest:
                return {
                    "ok": False,
                    "checked": False,
                    "reason": self._install_reason or "runtime_manifest_missing",
                    "download_error": self._download_error,
                }
            archive = self._manifest_archive_for_platform(manifest)
            if not archive:
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
            try:
                os.close(fd)
            except OSError:
                pass
            self._preview_guard_fd = None

    def _preview_busy_reason(self) -> str | None:
        """Read-only busy probe for previews: never creates or locks files.

        Detects an active install (same process via the RLock depth, another
        process via flock on an existing ``.install.lock``) so a preview never
        advertises a live staging directory (``manifest-*``) as removable.
        On POSIX an unopenable-but-existing guard reports unavailable (an
        inspection problem); where flock does not exist (native Windows) a
        staging sentinel — a fresh ``manifest-*``/``prebuilt-*`` directory
        modified within the install guard window — is used instead.

        On POSIX success the probe fd is kept open with the shared lock held
        (stored on the instance) so the caller can hold the guard through
        planning; ``_release_preview_guard`` closes it.
        """
        if self._install_guard_depth > 0:
            return "runtime_install_already_running"
        try:
            import fcntl
        except ImportError:
            # Windows: no flock probe; fall back to the staging sentinel.
            return self._staging_sentinel_reason()
        try:
            self._install_guard_path.stat()
        except FileNotFoundError:
            return None
        except OSError:
            return "runtime_install_guard_unavailable"
        try:
            fd = os.open(self._install_guard_path, os.O_RDONLY)
        except OSError:
            return "runtime_install_guard_unavailable"
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            # Held (shared) until the preview scope ends: an installer's
            # exclusive-lock acquisition now blocks on us and vice versa.
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

    def _preview_raced_busy(self) -> bool:
        """True when an install started after a lock-absent preview probe."""
        if getattr(self, "_preview_guard_fd", None) is not None:
            return False
        if self._staging_sentinel_reason():
            return True
        try:
            self._install_guard_path.stat()
        except FileNotFoundError:
            return False
        except OSError:
            return True
        return True

    def clean(self, *, keep_previous: int = 1, dry_run: bool = False) -> dict[str, Any]:
        try:
            return self._clean_locked(keep_previous=keep_previous, dry_run=dry_run)
        except Exception:
            # A planning failure (e.g. an install dir disappearing mid-scan)
            # must return the structured inspection-failure report, never an
            # exception through the CLI or Doctor paths.
            logger.warning("Show Runtime cache cleanup failed", exc_info=True)
            return {
                "ok": False,
                "dry_run": dry_run,
                "removed": [],
                "archives": self._skipped_archive_report(_SKIPPED_ARCHIVE_REASON_INSPECTION_FAILED),
            }

    def _clean_locked(self, *, keep_previous: int, dry_run: bool) -> dict[str, Any]:
        removed: list[str] = []
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
            removed.extend(self._clean_manifest_install_dirs(keep_previous=keep_previous, dry_run=dry_run))
            # A dry run leaves stale install dirs in place, so their metadata
            # would still read as "protected" below. Skip metadata under the
            # dirs a real run would remove, so the archive preview matches
            # the real outcome. Hold the preview guard through this phase so
            # an install starting after staging enumeration cannot expose an
            # in-flight archive as reclaimable.
            skip_metadata_under = {Path(path) for path in removed} if dry_run else None
            archives = self._clean_downloaded_archives(
                dry_run=dry_run,
                skip_metadata_under=skip_metadata_under,
            )
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
        except Exception as exc:
            raise _ArchiveMetadataError("current.json is unreadable") from exc
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
                    is_claim = bool(_ABANDONED_ARCHIVE_CLAIM_RE.match(entry.name))
                    if not is_claim and not _CONTENT_ADDRESSED_ARCHIVE_RE.match(entry.name):
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
                is_claim = bool(_ABANDONED_ARCHIVE_CLAIM_RE.match(name))
                if not is_claim and not _CONTENT_ADDRESSED_ARCHIVE_RE.match(name):
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
                    if os.name == "nt":
                        downloads_identity = getattr(self, "_downloads_dir_identity", None)
                        for path, size, name, inode in candidates:
                            is_claim = bool(_ABANDONED_ARCHIVE_CLAIM_RE.match(name))
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

    def _clean_manifest_install_dirs(
        self,
        *,
        keep_previous: int,
        manifest_source: str | None = None,
        protected_install_dirs: set[Path] | None = None,
        dry_run: bool = False,
    ) -> list[str]:
        versions_dir = self.runtime_dir / "versions"
        if not versions_dir.is_dir():
            return []
        protected = set(protected_install_dirs or ())
        current_install_dir = self._current_manifest_install_dir(versions_dir)
        if current_install_dir is not None:
            protected.add(current_install_dir)
        install_dirs = self._manifest_install_dirs(versions_dir, manifest_source=manifest_source)
        all_manifest_install_dirs = self._manifest_install_dirs(versions_dir)
        sorted_install_dirs = sorted(install_dirs, key=lambda path: path.stat().st_mtime, reverse=True)
        resolved_install_dirs = {path: path.resolve() for path in install_dirs}
        all_resolved_install_dirs = {path: path.resolve() for path in all_manifest_install_dirs}
        rollback_candidates = [
            path
            for path in sorted_install_dirs
            if not any(
                resolved_install_dirs[path] in other_resolved.parents
                for other, other_resolved in resolved_install_dirs.items()
                if other != path
            )
        ]
        kept_previous = 0
        for path in rollback_candidates:
            path_resolved = resolved_install_dirs[path]
            if self._install_dir_overlaps_protected(path_resolved, protected):
                continue
            if kept_previous < keep_previous:
                kept_previous += 1
                protected.add(path_resolved)
        removed: list[str] = []
        removable_install_dirs = [
            path
            for path, path_resolved in resolved_install_dirs.items()
            if not self._install_dir_overlaps_protected(path_resolved, protected)
        ]
        removable_resolved_install_dirs = {resolved_install_dirs[path] for path in removable_install_dirs}
        safe_removable_install_dirs = [
            path
            for path in removable_install_dirs
            if not any(
                resolved_install_dirs[path] in other_resolved.parents
                and other_resolved not in removable_resolved_install_dirs
                for other_resolved in all_resolved_install_dirs.values()
            )
        ]
        for path in sorted(safe_removable_install_dirs, key=lambda item: len(resolved_install_dirs[item].parts), reverse=True):
            if not path.is_dir():
                continue
            if not dry_run:
                shutil.rmtree(path, ignore_errors=True)
            removed.append(str(path))
        if not dry_run:
            self._prune_empty_manifest_version_dirs(versions_dir)
        return removed

    @staticmethod
    def _install_dir_overlaps_protected(path_resolved: Path, protected: set[Path]) -> bool:
        return any(
            path_resolved == item or path_resolved in item.parents or item in path_resolved.parents
            for item in protected
        )

    def _manifest_install_dirs(self, versions_dir: Path, *, manifest_source: str | None = None) -> set[Path]:
        install_dirs: set[Path] = set()
        for pattern in ("*/*/.vibe-show-runtime.json", "*/*/*/.vibe-show-runtime.json"):
            for metadata_path in versions_dir.glob(pattern):
                if not metadata_path.parent.is_dir():
                    continue
                if manifest_source is not None:
                    try:
                        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                    except Exception:
                        continue
                    if (
                        metadata.get("provider") != _RUNTIME_SOURCE_MANIFEST
                        or metadata.get("manifest_source") != manifest_source
                    ):
                        continue
                install_dirs.add(metadata_path.parent)
        return install_dirs

    def _current_manifest_install_dir(self, versions_dir: Path) -> Path | None:
        try:
            pointer = json.loads((self.runtime_dir / "current.json").read_text(encoding="utf-8"))
            pointer_install_dir = Path(str(pointer.get("install_dir") or "")).resolve()
            if versions_dir.resolve() in pointer_install_dir.parents:
                return pointer_install_dir
        except Exception:
            pass
        return None

    def _manifest_source_for_install_dirs(self, install_dirs: set[Path]) -> str | None:
        for install_dir in install_dirs:
            try:
                metadata = json.loads(self._manifest_metadata_path(install_dir).read_text(encoding="utf-8"))
            except Exception:
                continue
            source = metadata.get("manifest_source")
            if isinstance(source, str) and source:
                return source
        if self.manifest_path is None and self.manifest_url is None:
            return _PACKAGED_RUNTIME_MANIFEST_SOURCE
        return None

    def _manifest_install_dirs_for_command(self, command: list[str]) -> set[Path]:
        versions_dir = self.runtime_dir / "versions"
        if not versions_dir.is_dir():
            return set()
        install_dirs = self._manifest_install_dirs(versions_dir)
        matching_install_dirs: set[Path] = set()
        for command_part in command:
            try:
                command_path = Path(command_part).resolve()
            except (OSError, RuntimeError):
                continue
            for install_dir in install_dirs:
                install_dir_resolved = install_dir.resolve()
                if install_dir_resolved == command_path or install_dir_resolved in command_path.parents:
                    matching_install_dirs.add(install_dir_resolved)
        return {
            path
            for path in matching_install_dirs
            if not any(path in other.parents for other in matching_install_dirs if other != path)
        }

    def _prune_empty_manifest_version_dirs(self, versions_dir: Path) -> None:
        for path in sorted(versions_dir.glob("*/*"), reverse=True):
            if path.is_dir() and not any(path.iterdir()):
                path.rmdir()
        for path in sorted(versions_dir.iterdir(), reverse=True):
            if path.is_dir() and not any(path.iterdir()):
                path.rmdir()

    def prepare(self, *, force: bool | None = None, offline: bool | None = None) -> dict[str, Any]:
        previous_force = self.force_install
        previous_offline = self.offline
        if force is not None:
            self.force_install = force
        if offline is not None:
            self.offline = offline
        try:
            if self._command_explicit:
                command = _resolve_command(self.command)
                self._install_reason = None if command else "runtime_command_missing"
            else:
                command = self._install_managed_runtime()
            return {
                "ok": command is not None,
                "provider": self.runtime_source,
                "platform": _runtime_platform_tag(),
                "command": command,
                "reason": None if command else self._install_reason,
                "status": self.status(),
            }
        finally:
            self.force_install = previous_force
            self.offline = previous_offline

    def _installed_manifest_runtime_command(self) -> list[str] | None:
        node = _resolve_node_command()
        if not node:
            return None
        manifest = self._load_runtime_manifest()
        if not manifest:
            return None
        if not self._manifest_node_supported(node, manifest):
            return None
        archive = self._manifest_archive_for_platform(manifest)
        if not archive:
            return None
        install_dir = self._manifest_install_dir(manifest, archive)
        command = self._verified_manifest_runtime_command(install_dir, manifest, archive, node)
        if command:
            return command
        return self._verified_manifest_runtime_command(self._legacy_manifest_install_dir(manifest, archive), manifest, archive, node)

    @contextlib.contextmanager
    def _install_guard_locked(self, *, timeout_seconds: float = 0.0):
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
                open_stat = os.fstat(lock_fd)
                try:
                    path_stat = self._install_guard_path.lstat()
                except OSError:
                    os.close(lock_fd)
                    yield unavailable
                    return
                if (
                    not _is_exclusive_regular_file(open_stat)
                    or not _is_exclusive_regular_file(path_stat)
                    or (open_stat.st_dev, open_stat.st_ino) != (path_stat.st_dev, path_stat.st_ino)
                ):
                    logger.warning(
                        "Show Runtime install guard descriptor is not an exclusive regular file; refusing",
                    )
                    os.close(lock_fd)
                    yield unavailable
                    return
                file_lock = MigrationFileLock(self._install_guard_path, timeout_seconds=timeout_seconds)
                file_lock._handle = os.fdopen(lock_fd, "a+", encoding="utf-8")
                deadline = time.monotonic() + timeout_seconds
                while True:
                    file_lock._handle.seek(0)
                    if storage_lock_try_lock(file_lock._handle):
                        file_lock._handle.seek(0)
                        file_lock._handle.truncate()
                        file_lock._handle.write(str(os.getpid()))
                        file_lock._handle.flush()
                        break
                    if time.monotonic() >= deadline:
                        file_lock._handle.close()
                        yield busy
                        return
                    time.sleep(0.1)
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

    def _install_manifest_runtime(self) -> list[str] | None:
        with self._install_guard_locked() as (acquired, reason):
            if not acquired:
                self._install_reason = reason
                # An untakeable lock (busy or unavailable) must not break a
                # non-forced prepare that already has a verified install to
                # reuse. A forced reinstall that cannot run is a failure —
                # reporting success would hide that the repair never happened.
                if self.force_install:
                    return None
                return self._reuse_verified_manifest_command()
            return self._install_manifest_runtime_locked()

    def _reuse_verified_manifest_command(self) -> list[str] | None:
        """Best-effort read-only fallback: reuse a verified installed runtime."""
        try:
            node = _resolve_node_command()
            manifest = self._load_runtime_manifest()
            if not node or not manifest:
                return None
            # Mirror the normal install path: an unsupported Node version must
            # not be reported as a usable runtime.
            if not self._manifest_node_supported(node, manifest):
                return None
            archive = self._manifest_archive_for_platform(manifest)
            if not archive:
                return None
            install_dir = self._manifest_install_dir(manifest, archive)
            command = self._verified_manifest_runtime_command(install_dir, manifest, archive, node)
            if not command:
                legacy_install_dir = self._legacy_manifest_install_dir(manifest, archive)
                command = self._verified_manifest_runtime_command(legacy_install_dir, manifest, archive, node)
            return self._reuse_existing_archive_runtime(command)
        except Exception:
            return None

    def _install_manifest_runtime_locked(self) -> list[str] | None:
        node = _resolve_node_command()
        if not node:
            self._install_reason = "runtime_node_missing"
            return None
        manifest = self._load_runtime_manifest()
        if not manifest:
            return None
        if not self._manifest_node_supported(node, manifest):
            return None
        archive = self._manifest_archive_for_platform(manifest)
        if not archive:
            return None
        install_dir = self._manifest_install_dir(manifest, archive)
        verified_existing_command = self._verified_manifest_runtime_command(install_dir, manifest, archive, node)
        if not verified_existing_command:
            legacy_install_dir = self._legacy_manifest_install_dir(manifest, archive)
            verified_existing_command = self._verified_manifest_runtime_command(legacy_install_dir, manifest, archive, node)
        if verified_existing_command and not self.force_install:
            self._install_reason = None
            return verified_existing_command
        archive_path = self._resolve_manifest_archive(archive)
        if not archive_path:
            return self._reuse_existing_archive_runtime(verified_existing_command)
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        tmp_dir = Path(tempfile.mkdtemp(prefix="manifest-", dir=self.runtime_dir))
        try:
            with tarfile.open(archive_path, "r:gz") as tar:
                _safe_extract_tar(tar, tmp_dir)
            command = self._manifest_runtime_command(tmp_dir, node)
            if not command:
                self._install_reason = "runtime_install_missing_bin"
                return self._reuse_existing_archive_runtime(verified_existing_command)
            if install_dir.exists():
                shutil.rmtree(install_dir)
            install_dir.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(tmp_dir), str(install_dir))
            self._write_manifest_install_metadata(install_dir, manifest, archive)
            self._write_current_manifest_pointer(manifest, archive, install_dir)
            self._install_reason = None
            return self._manifest_runtime_command(install_dir, node)
        except Exception:
            logger.exception("Failed to install manifest Show Runtime")
            self._install_reason = "runtime_install_failed"
            return self._reuse_existing_archive_runtime(verified_existing_command)
        finally:
            if tmp_dir.exists():
                shutil.rmtree(tmp_dir, ignore_errors=True)

    def _load_runtime_manifest(self) -> ShowRuntimeManifest | None:
        payload: bytes | None = None
        source = ""
        if self.manifest_path:
            if not self.manifest_path.exists():
                self._install_reason = "runtime_manifest_missing"
                return None
            payload = self.manifest_path.read_bytes()
            source = str(self.manifest_path)
        elif self.manifest_url:
            if self.offline:
                self._install_reason = "runtime_manifest_unavailable_offline"
                return None
            try:
                payload = fetch_bytes(
                    self.manifest_url,
                    timeout=30,
                    opener=urllib.request.urlopen,
                )
                source = self.manifest_url
            except Exception as exc:
                logger.exception("Failed to download Show Runtime manifest from %s", self.manifest_url)
                self._install_reason = "runtime_manifest_download_failed"
                self._download_error = _runtime_download_error(exc, self.manifest_url)
                return None
        else:
            try:
                resource = package_resources.files("vibe").joinpath(_RUNTIME_MANIFEST_RESOURCE)
            except Exception:
                resource = None
            if resource is None or not resource.is_file():
                self._install_reason = "runtime_manifest_missing"
                return None
            payload = resource.read_bytes()
            source = _PACKAGED_RUNTIME_MANIFEST_SOURCE
        digest = hashlib.sha256(payload).hexdigest()
        try:
            data = json.loads(payload.decode("utf-8"))
            archives = {
                platform_tag: ShowRuntimeArchive(
                    platform=platform_tag,
                    name=str(item["name"]),
                    url=str(item["url"]),
                    sha256=str(item["sha256"]),
                    size=int(item["size"]) if item.get("size") is not None else None,
                )
                for platform_tag, item in (data.get("archives") or {}).items()
                if isinstance(item, dict)
            }
            manifest = ShowRuntimeManifest(
                schema_version=int(data.get("schema_version")),
                runtime_version=str(data.get("runtime_version") or ""),
                minimum_node=str(data.get("minimum_node") or "") or None,
                archives=archives,
                digest=digest,
                source=source,
            )
        except Exception:
            self._install_reason = "runtime_manifest_invalid"
            return None
        if manifest.schema_version != 1 or not manifest.runtime_version or not manifest.archives:
            self._install_reason = "runtime_manifest_invalid"
            return None
        return manifest

    def _manifest_node_supported(self, node: list[str], manifest: ShowRuntimeManifest) -> bool:
        if not manifest.minimum_node:
            return True
        version = _node_version(node)
        if _node_satisfies_requirement(version, manifest.minimum_node):
            return True
        self._install_reason = "runtime_node_unsupported"
        return False

    def _manifest_archive_for_platform(self, manifest: ShowRuntimeManifest) -> ShowRuntimeArchive | None:
        platform_tag = _runtime_platform_tag()
        archive = manifest.archives.get(platform_tag)
        if not archive:
            self._install_reason = "runtime_platform_unsupported"
            return None
        return archive

    def _resolve_manifest_archive(self, archive: ShowRuntimeArchive) -> Path | None:
        cached = self.runtime_dir / "downloads" / f"{archive.sha256}.tgz"
        if cached.exists() and self._downloaded_archive_matches(cached, archive):
            self._download_error = None
            return cached
        if self.offline:
            self._install_reason = "runtime_archive_unavailable_offline"
            return None
        parsed = urllib.parse.urlparse(archive.url)
        if parsed.scheme not in {"https", "file"}:
            self._install_reason = "runtime_archive_url_unsupported"
            return None
        tmp_path = cached.with_suffix(".tmp")
        cached.parent.mkdir(parents=True, exist_ok=True)
        try:
            fetch_to_path(
                archive.url,
                tmp_path,
                timeout=60,
                opener=urllib.request.urlopen,
            )
            if not self._downloaded_archive_matches(tmp_path, archive):
                tmp_path.unlink(missing_ok=True)
                return None
            tmp_path.replace(cached)
            self._download_error = None
            return cached
        except Exception as exc:
            logger.exception("Failed to download Show Runtime archive from %s", archive.url)
            tmp_path.unlink(missing_ok=True)
            self._install_reason = "runtime_archive_download_failed"
            self._download_error = _runtime_download_error(exc, archive.url)
            return None

    def _downloaded_archive_matches(self, path: Path, archive: ShowRuntimeArchive) -> bool:
        if archive.size is not None and path.stat().st_size != archive.size:
            self._install_reason = "runtime_archive_size_mismatch"
            return False
        if _file_sha256(path) != archive.sha256:
            self._install_reason = "runtime_archive_checksum_mismatch"
            return False
        return True

    def _manifest_install_dir(self, manifest: ShowRuntimeManifest, archive: ShowRuntimeArchive) -> Path:
        fingerprint = hashlib.sha256(f"{manifest.digest}:{archive.sha256}".encode("utf-8")).hexdigest()[:16]
        return (
            self.runtime_dir
            / "versions"
            / _safe_path_part(manifest.runtime_version)
            / _safe_path_part(archive.platform)
            / fingerprint
        )

    def _legacy_manifest_install_dir(self, manifest: ShowRuntimeManifest, archive: ShowRuntimeArchive) -> Path:
        return self.runtime_dir / "versions" / _safe_path_part(manifest.runtime_version) / _safe_path_part(archive.platform)

    def _manifest_metadata_path(self, install_dir: Path) -> Path:
        return install_dir / ".vibe-show-runtime.json"

    def _verified_manifest_runtime_command(
        self,
        install_dir: Path,
        manifest: ShowRuntimeManifest,
        archive: ShowRuntimeArchive,
        node: list[str],
    ) -> list[str] | None:
        command = self._manifest_runtime_command(install_dir, node)
        if command and self._manifest_install_matches(install_dir, manifest, archive):
            return command
        return None

    def _manifest_install_matches(self, install_dir: Path, manifest: ShowRuntimeManifest, archive: ShowRuntimeArchive) -> bool:
        try:
            payload = json.loads(self._manifest_metadata_path(install_dir).read_text(encoding="utf-8"))
        except Exception:
            return False
        return (
            payload.get("provider") == _RUNTIME_SOURCE_MANIFEST
            and payload.get("manifest_sha256") == manifest.digest
            and payload.get("runtime_version") == manifest.runtime_version
            and payload.get("platform") == archive.platform
            and payload.get("archive_sha256") == archive.sha256
        )

    def _write_manifest_install_metadata(self, install_dir: Path, manifest: ShowRuntimeManifest, archive: ShowRuntimeArchive) -> None:
        self._manifest_metadata_path(install_dir).write_text(
            json.dumps(
                {
                    "provider": _RUNTIME_SOURCE_MANIFEST,
                    "manifest_sha256": manifest.digest,
                    "runtime_version": manifest.runtime_version,
                    "platform": archive.platform,
                    "archive_name": archive.name,
                    "archive_sha256": archive.sha256,
                    "manifest_source": manifest.source,
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    def _write_current_manifest_pointer(self, manifest: ShowRuntimeManifest, archive: ShowRuntimeArchive, install_dir: Path) -> None:
        pointer = self.runtime_dir / "current.json"
        pointer.parent.mkdir(parents=True, exist_ok=True)
        pointer.write_text(
            json.dumps(
                {
                    "provider": _RUNTIME_SOURCE_MANIFEST,
                    "runtime_version": manifest.runtime_version,
                    "platform": archive.platform,
                    "install_dir": str(install_dir),
                    "manifest_sha256": manifest.digest,
                    "archive_sha256": archive.sha256,
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    def _manifest_runtime_command(self, install_dir: Path, node: list[str]) -> list[str] | None:
        return self._archive_runtime_command(install_dir, node)

    def _installed_archive_runtime_command(self) -> list[str] | None:
        node = _resolve_node_command()
        if not node:
            return None
        return self._archive_runtime_command(self._archive_install_dir(), node)

    def _install_archive_runtime(self) -> list[str] | None:
        node = _resolve_node_command()
        if not node:
            self._install_reason = "runtime_node_missing"
            return None
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        install_dir = self._archive_install_dir()
        existing_command = self._archive_runtime_command(install_dir, node)
        archive = self._resolve_prebuilt_archive()
        if not archive:
            return self._reuse_existing_archive_runtime(existing_command)
        archive_digest = _file_sha256(archive)
        if existing_command and self._archive_manifest_matches(archive_digest):
            self._install_reason = None
            return existing_command
        tmp_dir = Path(tempfile.mkdtemp(prefix="prebuilt-", dir=self.runtime_dir))
        try:
            with tarfile.open(archive, "r:gz") as tar:
                _safe_extract_tar(tar, tmp_dir)
            command = self._archive_runtime_command(tmp_dir, node)
            if not command:
                self._install_reason = "runtime_install_missing_bin"
                return self._reuse_existing_archive_runtime(existing_command)
            if install_dir.exists():
                shutil.rmtree(install_dir)
            install_dir.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(tmp_dir), str(install_dir))
            self._write_archive_manifest(archive_digest)
            self._install_reason = None
            return self._archive_runtime_command(install_dir, node)
        except Exception:
            logger.exception("Failed to install prebuilt Show Runtime")
            self._install_reason = "runtime_install_failed"
            return self._reuse_existing_archive_runtime(existing_command)
        finally:
            if tmp_dir.exists():
                shutil.rmtree(tmp_dir, ignore_errors=True)

    def _resolve_prebuilt_archive(self) -> Path | None:
        if self.archive_path:
            if self.archive_path.exists():
                return self.archive_path
            self._install_reason = "runtime_archive_missing"
            return None
        packaged = self._copy_packaged_runtime_archive()
        if packaged:
            return packaged
        if not self.archive_url:
            self._install_reason = "runtime_archive_missing"
            return None
        if self.offline:
            self._install_reason = "runtime_archive_unavailable_offline"
            return None
        return self._download_runtime_archive(self.archive_url)

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

    def _download_runtime_archive(self, archive_url: str) -> Path | None:
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
            self._install_reason = "runtime_archive_download_failed"
            self._download_error = _runtime_download_error(exc, archive_url)
            return None
        return target

    def _archive_install_dir(self) -> Path:
        return self.runtime_dir / "prebuilt" / "current"

    def _archive_manifest_path(self) -> Path:
        return self._archive_install_dir() / ".vibe-show-runtime.json"

    def _archive_manifest_matches(self, archive_digest: str) -> bool:
        try:
            payload = json.loads(self._archive_manifest_path().read_text(encoding="utf-8"))
        except Exception:
            return False
        return payload.get("archive_name") == _runtime_archive_name() and payload.get("sha256") == archive_digest

    def _write_archive_manifest(self, archive_digest: str) -> None:
        self._archive_manifest_path().write_text(
            json.dumps(
                {
                    "archive_name": _runtime_archive_name(),
                    "sha256": archive_digest,
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    def _archive_runtime_command(self, install_dir: Path, node: list[str]) -> list[str] | None:
        cli_path = install_dir / "node_modules" / "@avibe" / "show-runtime" / "dist" / "cli.js"
        if not cli_path.exists():
            return None
        return [*node, str(cli_path)]

    def _reuse_existing_archive_runtime(self, command: list[str] | None) -> list[str] | None:
        if command:
            self._install_reason = None
            return command
        return None

    def _installed_github_runtime_command(self) -> list[str] | None:
        node = _resolve_node_command()
        if not node:
            return None
        return self._github_runtime_command(self._github_source_dir(), node)

    def _install_github_runtime(self) -> list[str] | None:
        node = _resolve_node_command()
        if not node:
            self._install_reason = "runtime_node_missing"
            return None
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        source_dir = self._github_source_dir()
        existing_command = self._github_runtime_command(source_dir, node)
        git = _resolve_command("git")
        npm = _resolve_command("npm")
        if not git:
            if existing_command:
                self._install_reason = None
                return existing_command
            self._install_reason = "runtime_git_missing"
            return None
        if not npm:
            if existing_command:
                self._install_reason = None
                return existing_command
            self._install_reason = "runtime_npm_missing"
            return None
        if not source_dir.exists():
            source_dir.parent.mkdir(parents=True, exist_ok=True)
            if not self._run_install_command([*git, "clone", "--depth", "1", "--branch", self.github_ref, self.github_repo, str(source_dir)]):
                return None
        else:
            if not self._run_install_command([*git, "-C", str(source_dir), "fetch", "--depth", "1", "origin", self.github_ref]):
                return self._reuse_existing_github_runtime(existing_command)
            if not self._run_install_command([*git, "-C", str(source_dir), "checkout", "FETCH_HEAD"]):
                return self._reuse_existing_github_runtime(existing_command)
        if not self._run_install_command([*npm, "ci"], cwd=source_dir):
            return self._reuse_existing_github_runtime(existing_command)
        if not self._run_install_command([*npm, "run", "build"], cwd=source_dir):
            return self._reuse_existing_github_runtime(existing_command)
        command = self._github_runtime_command(source_dir, node)
        if not command:
            self._install_reason = "runtime_install_missing_bin"
            return None
        return command

    def _github_runtime_command(self, source_dir: Path, node: list[str]) -> list[str] | None:
        cli_path = source_dir / "packages" / "runtime" / "dist" / "cli.js"
        if not cli_path.exists():
            return None
        return [*node, str(cli_path)]

    def _reuse_existing_github_runtime(self, command: list[str] | None) -> list[str] | None:
        if command:
            self._install_reason = None
            return command
        return None

    def _install_npm_runtime(self) -> list[str] | None:
        npm = _resolve_command("npm")
        if not npm:
            self._install_reason = "runtime_npm_missing"
            return None
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        install_root = self.runtime_dir / "package"
        install_root.mkdir(parents=True, exist_ok=True)
        package_json = install_root / "package.json"
        if not package_json.exists():
            package_json.write_text('{"private":true,"type":"module"}\n', encoding="utf-8")
        with self.install_log_path.open("w", encoding="utf-8") as log:
            result = subprocess.run(
                [
                    *npm,
                    "install",
                    "--prefix",
                    str(install_root),
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
        if result.returncode != 0:
            self._install_reason = "runtime_install_failed"
            return None
        resolved = _resolve_executable_path(self._managed_bin_path())
        if not resolved:
            self._install_reason = "runtime_install_missing_bin"
            return None
        return [resolved]

    def _managed_bin_path(self) -> Path:
        suffix = ".cmd" if os.name == "nt" else ""
        return self.runtime_dir / "package" / "node_modules" / ".bin" / f"{_RUNTIME_BIN}{suffix}"

    def _github_source_dir(self) -> Path:
        repo_slug = self.github_repo.removesuffix(".git").rstrip("/").rsplit("/", 2)[-2:]
        repo_part = "_".join(repo_slug) if len(repo_slug) == 2 else "vibe-show-runtime"
        ref_part = _safe_path_part(self.github_ref)
        return self.runtime_dir / "source" / "github" / repo_part / ref_part

    def _run_install_command(self, command: list[str], *, cwd: Path | None = None) -> bool:
        with self.install_log_path.open("a", encoding="utf-8") as log:
            log.write(f"$ {' '.join(command)}\n")
            result = subprocess.run(
                command,
                cwd=str(cwd) if cwd else None,
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=300,
                check=False,
                **isolated_subprocess_kwargs(),
            )
        if result.returncode != 0:
            self._install_reason = "runtime_install_failed"
            return False
        return True


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


async def prewarm_show_runtime() -> ShowRuntimeResult:
    return await get_show_runtime_manager().ensure()


async def prewarm_show_page_session(
    session_id: str,
    *,
    context: ShowRuntimeContext,
    base_path: str | None = None,
) -> ShowRuntimeResult:
    return await get_show_runtime_manager().prewarm_session(
        session_id,
        context=context,
        base_path=base_path,
    )


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


def _env_flag_enabled(name: str, *, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() not in _FALSE_VALUES


def _packaged_runtime_manifest_exists() -> bool:
    try:
        resource = package_resources.files("vibe").joinpath(_RUNTIME_MANIFEST_RESOURCE)
    except Exception:
        return False
    return resource.is_file()


def _running_from_development_checkout() -> bool:
    source_root = Path(__file__).resolve().parents[1]
    return (source_root / "pyproject.toml").is_file() and (source_root / "main.py").is_file()


def _normalize_runtime_source(value: str | None) -> str:
    normalized = (value or _RUNTIME_SOURCE_MANIFEST).strip().lower()
    aliases = {
        "manifest": _RUNTIME_SOURCE_MANIFEST,
        "manifest-cache": _RUNTIME_SOURCE_MANIFEST,
        "archive": _RUNTIME_SOURCE_ARCHIVE,
        "prebuilt": _RUNTIME_SOURCE_ARCHIVE,
        "github": _RUNTIME_SOURCE_GITHUB,
        "github-source": _RUNTIME_SOURCE_GITHUB,
        "npm": _RUNTIME_SOURCE_NPM,
    }
    return aliases.get(normalized, normalized or _RUNTIME_SOURCE_MANIFEST)


def _manifest_status_payload(manifest: ShowRuntimeManifest | None) -> dict[str, Any] | None:
    if manifest is None:
        return None
    return {
        "schema_version": manifest.schema_version,
        "runtime_version": manifest.runtime_version,
        "minimum_node": manifest.minimum_node,
        "sha256": manifest.digest,
        "source": manifest.source,
        "platforms": sorted(manifest.archives),
    }


def _redact_download_url(url: str) -> str:
    return redact_url(url)


def _runtime_download_error(exc: BaseException, url: str) -> dict[str, Any]:
    return dependency_error_details(exc, url)


def _archive_status_payload(archive: ShowRuntimeArchive | None) -> dict[str, Any] | None:
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
    return f"{_RUNTIME_ARCHIVE_PREFIX}-{_runtime_platform_tag()}.tgz"


def _default_runtime_archive_url() -> str:
    return f"{_RUNTIME_ARCHIVE_RELEASE_BASE_URL}/{_runtime_archive_name()}"


def _runtime_platform_tag() -> str:
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


def _safe_extract_tar(tar: tarfile.TarFile, destination: Path) -> None:
    destination_resolved = destination.resolve()
    for member in tar.getmembers():
        if not (member.isfile() or member.isdir() or member.issym() or member.islnk()):
            raise ValueError(f"Unsafe archive member type: {member.name}")
        target = (destination / member.name).resolve()
        if target != destination_resolved and destination_resolved not in target.parents:
            raise ValueError(f"Unsafe archive member path: {member.name}")
        if member.issym():
            link_target = (destination / member.name).parent / member.linkname
            link_target_resolved = link_target.resolve()
            if link_target_resolved != destination_resolved and destination_resolved not in link_target_resolved.parents:
                raise ValueError(f"Unsafe archive link target: {member.name}")
        elif member.islnk():
            link_target = destination / member.linkname
            link_target_resolved = link_target.resolve()
            if link_target_resolved != destination_resolved and destination_resolved not in link_target_resolved.parents:
                raise ValueError(f"Unsafe archive link target: {member.name}")
    try:
        tar.extractall(destination, filter="data")
    except TypeError:
        tar.extractall(destination)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_path_part(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in value.strip())
    return cleaned or "main"


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
    if configured:
        return _resolve_command(configured)
    return _resolve_command("node")


def _resolve_executable_path(path: Path) -> str | None:
    expanded = path.expanduser()
    return str(expanded) if expanded.exists() and os.access(expanded, os.X_OK) else None


atexit.register(stop_show_runtime_manager)
