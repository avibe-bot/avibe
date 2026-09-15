"""Owned lifecycle for private EverOS sidecar and one-shot children."""

from __future__ import annotations

import asyncio
import errno
import hashlib
import inspect
import json
import logging
import math
import os
import signal
import stat
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Deque, Protocol, TypeVar, runtime_checkable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib

import psutil

from config import paths
from avibe_memory.attachments import attachment_pin_root
from avibe_memory.confined_filesystem import (
    ConfinedFilesystemError,
    ConfinedRoot,
    create_confined_file,
    ensure_private_directory,
    open_confined_directory,
    open_confined_regular_file,
    remove_anchored_entry,
    required_no_follow_flag,
)
from avibe_memory.everos import (
    EverOSPort,
    MULTIMODAL_EXPLICIT_ENV,
    PROCESSING_PROBE_MAX_DEADLINE_SECONDS,
    processing_probe_deadline_seconds,
)
from avibe_memory.secret_scrubber import scrub_text


logger = logging.getLogger(__name__)


async def _drain_probe_stderr(stream: object) -> bytes:
    tail = bytearray()
    try:
        reader = getattr(stream, "read", None)
        if not callable(reader):
            return b""
        while True:
            chunk = await reader(4096)
            if not chunk:
                break
            tail.extend(chunk if isinstance(chunk, bytes) else str(chunk).encode())
            del tail[:-_PROCESSING_PROBE_STDERR_BYTES]
    except Exception:
        pass
    return bytes(tail)


async def _probe_stderr_tail(task: asyncio.Task[bytes] | None, *, settings: EverOSProcessSettings) -> str:
    if task is None:
        return ""
    try:
        data = await asyncio.wait_for(asyncio.shield(task), timeout=1.0)
        text = data.decode("utf-8", "replace")
        text = scrub_text(
            text,
            base_urls=tuple(
                value for value in (
                    settings.llm_base_url,
                    settings.embedding_base_url,
                    settings.rerank_base_url,
                    settings.multimodal_base_url,
                ) if value
            ),
            exact_values=tuple(
                value for value in (
                    settings.llm_api_key,
                    settings.embedding_api_key,
                    settings.rerank_api_key,
                    settings.multimodal_api_key,
                ) if value
            ),
        )
        compact = " ".join(text.split())
        return compact.encode("utf-8")[-_PROCESSING_PROBE_STDERR_BYTES:].decode(
            "utf-8",
            "ignore",
        )
    except Exception:
        task.cancel()
        return ""

_STARTUP_TIMEOUT_SECONDS = 30.0
_STOP_TIMEOUT_SECONDS = 10.0
# Reconcile's transport budget must cover the worst derived probe-child deadline.
_PROCESSING_PROBE_TIMEOUT_SECONDS = PROCESSING_PROBE_MAX_DEADLINE_SECONDS
_PROCESSING_PROBE_STDERR_BYTES = 2048
_SOCKET_MODE = 0o600
_OWNER_DIR_MODE = 0o700
_TREE_INSPECTION_INTERVAL_SECONDS = 1.0
_SIDECAR_RECORD_FILENAME = "everos.sidecar.json"
_SIDECAR_RECORD_MAX_BYTES = 4 * 1024
_SIDECAR_ENTRYPOINT_MODULE = "avibe_memory.sidecar"
_RELEASED_SIDECAR_ENTRYPOINT_MODULE = "core.memory.sidecar"
_SIDECAR_ENTRYPOINT_MODULES = frozenset(
    {_SIDECAR_ENTRYPOINT_MODULE, _RELEASED_SIDECAR_ENTRYPOINT_MODULE}
)
_PROVIDER_LOCK_PREFIX = "cascade-rebuild-"
_SYNC_RECORD_PREFIX = "cascade-sync-"
_SYNC_RECORD_MAX_BYTES = 16 * 1024
_SYNC_NONCE_ENV = "AVIBE_MEMORY_SYNC_NONCE"
_SYNC_PARENT_PID_ENV = "AVIBE_MEMORY_SYNC_PARENT_PID"
_SYNC_PARENT_CREATE_TIME_ENV = "AVIBE_MEMORY_SYNC_PARENT_CREATE_TIME"
_SYNC_PARENT_UID_ENV = "AVIBE_MEMORY_SYNC_PARENT_UID"
_SYNC_ARGV = ("-I", "-m", "everos.entrypoints.cli.main", "cascade", "sync")
_PROVIDER_LOCK_DIRECTORY = ".avibe-memory-locks"
_PROVIDER_LOCK_RETRY_INTERVAL_SECONDS = 0.05
_SIDECAR_ROLE = "sidecar"
_RELEASED_REBUILD_ROLE = "cascade_rebuild"
_RELEASED_SYNC_ROLE = "cascade_sync"
_RELEASED_REBUILD_ENTRYPOINT_MODULE = "core.memory.rebuild_child"

_IdentityFieldT = TypeVar("_IdentityFieldT")


@dataclass(frozen=True)
class EverOSProcessSettings:
    """Non-persistent launch settings; keys only live in the child environment."""

    llm_base_url: str | None = None
    llm_model: str | None = None
    llm_api_key: str | None = field(default=None, repr=False)
    embedding_base_url: str | None = None
    embedding_model: str | None = None
    embedding_api_key: str | None = field(default=None, repr=False)
    rerank_base_url: str | None = None
    rerank_model: str | None = None
    rerank_api_key: str | None = field(default=None, repr=False)
    rerank_provider: str | None = None
    multimodal_base_url: str | None = None
    multimodal_model: str | None = None
    multimodal_api_key: str | None = field(default=None, repr=False)
    timezone: str | None = None
    profile_enabled: bool = True


class _ProcessKind(Enum):
    SIDECAR = "sidecar"
    PROCESSING_PROBE = "processing_probe"


class _ProviderRootBusy(RuntimeError):
    """Another process owns the provider-root coordination lock."""


class _ProviderRootLock:
    """One private, no-follow file lock anchored beside the provider root."""

    def __init__(self, *, confinement_root: Path, path: Path) -> None:
        self._confinement_root = confinement_root
        self._path = path
        self._descriptor: int | None = None

    def acquire(self) -> None:
        ensure_private_directory(
            self._confinement_root,
            self._path.parent,
            harden_confinement_root=False,
        )
        try:
            descriptor = create_confined_file(
                self._confinement_root,
                self._path,
                read_write=True,
            )
        except ConfinedFilesystemError:
            descriptor = open_confined_regular_file(self._confinement_root, self._path)
        try:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            os.close(descriptor)
            raise _ProviderRootBusy from error
        except BaseException:
            os.close(descriptor)
            raise
        self._descriptor = descriptor

    def release(self) -> None:
        descriptor = self._descriptor
        if descriptor is None:
            return
        self._descriptor = None
        try:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


async def _wait_for_provider_root_lock(
    root_lock: _ProviderRootLock,
    *,
    keep_waiting: Callable[[], bool],
) -> bool:
    """Acquire without blocking the event loop or consuming crash budget."""

    while keep_waiting():
        try:
            root_lock.acquire()
        except _ProviderRootBusy:
            await asyncio.sleep(_PROVIDER_LOCK_RETRY_INTERVAL_SECONDS)
            continue
        return True
    return False


@runtime_checkable
class _ProcessHost(Protocol):
    """Host capabilities needed to supervise one owned sidecar process tree."""

    async def spawn(
        self,
        kind: _ProcessKind,
        python: Path,
        *,
        cwd: Path,
        env: Mapping[str, str],
        socket_path: Path | None = None,
        capture_stderr: bool = False,
    ) -> asyncio.subprocess.Process: ...

    def process_group(self, pid: int) -> int | None: ...

    def inspect_identity(self, pid: int) -> _ProcessIdentity | None: ...

    def capture(self, pid: int) -> psutil.Process | None: ...

    def snapshot_tree(self, pid: int, process_group: int | None, owned=None) -> dict[int, psutil.Process | None]: ...

    def recorded_group_members(
        self,
        process_group: int,
        *,
        socket_path: Path,
        provider_root: Path,
        role: str | None = None,
    ) -> tuple[dict[int, psutil.Process | None], list[int]]: ...

    def find_sidecars(self, *, socket_path: Path) -> dict[int, psutil.Process | None]: ...

    def find_sidecars_by_root(self, *, provider_root: Path) -> dict[int, psutil.Process | None]: ...

    def find_syncs(
        self,
        *,
        provider_root: Path,
        python: Path,
        nonce: str,
    ) -> dict[int, psutil.Process | None]: ...

    def live(self, identities: Mapping[int, psutil.Process | None]) -> dict[int, psutil.Process | None]: ...

    def signal(
        self,
        identities: Mapping[int, psutil.Process | None],
        signum: int,
        *,
        process_group: int | None = None,
        process: asyncio.subprocess.Process | None = None,
    ) -> None: ...

    async def wait_for_exit(
        self,
        identities: dict[int, psutil.Process | None],
        timeout_seconds: float,
        *,
        process_group: int | None = None,
        process: asyncio.subprocess.Process | None = None,
    ) -> bool: ...

    def has_tcp_listener(self, identities: Mapping[int, psutil.Process | None]) -> bool: ...


class EverOSProcess:
    """Launch, supervise, and reap one privately owned EverOS child tree."""

    def __init__(
        self,
        python: Path | str,
        *,
        provider_root: Path | str | None = None,
        effective_home: Path | str | None = None,
        settings: EverOSProcessSettings | None = None,
        socket_path: Path | str | None = None,
        provider_root_guard: Callable[[], None] | None = None,
        startup_timeout_seconds: float = _STARTUP_TIMEOUT_SECONDS,
        stop_timeout_seconds: float = _STOP_TIMEOUT_SECONDS,
        on_ready: Callable[[], Awaitable[None] | None] | None = None,
        before_start: Callable[[], Awaitable[None] | None] | None = None,
        on_unexpected_exit: Callable[[], Awaitable[None] | None] | None = None,
        _host: _ProcessHost | None = None,
    ) -> None:
        self._python = Path(python)
        effective_home_path = (
            Path(effective_home) if effective_home is not None else paths.get_vibe_remote_dir()
        )
        filesystem_root = ConfinedRoot.from_home(effective_home_path)
        self._effective_home = filesystem_root.physical_home
        self._memory_dir = self._effective_home / "memory"
        self._attachments_root = attachment_pin_root(self._effective_home)
        provider_root_path = (
            Path(provider_root) if provider_root is not None else self._memory_dir / "everos-root"
        )
        self._provider_root = (
            filesystem_root.confine_if_child(provider_root_path)
            or Path(os.path.abspath(os.fspath(provider_root_path)))
        )
        socket_path_value = (
            Path(socket_path)
            if socket_path is not None
            else self._memory_dir / ".rt" / "everos.sock"
        )
        self._socket_path = (
            filesystem_root.confine_if_child(socket_path_value)
            or Path(os.path.abspath(os.fspath(socket_path_value)))
        )
        self._settings = settings or EverOSProcessSettings()
        self._provider_root_guard = provider_root_guard
        self._host = _SystemProcessHost() if _host is None else _host
        self._ownership = SidecarOwnership(
            record_path=sidecar_record_path(self._memory_dir),
            socket_path=self._socket_path,
            provider_root=self._provider_root,
            stop_timeout_seconds=stop_timeout_seconds,
            python=self._python,
            _host=self._host,
        )
        self._startup_timeout_seconds = _positive_timeout(startup_timeout_seconds, _STARTUP_TIMEOUT_SECONDS)
        self._stop_timeout_seconds = _positive_timeout(stop_timeout_seconds, _STOP_TIMEOUT_SECONDS)
        self._lifecycle_lock = asyncio.Lock()
        self._process: asyncio.subprocess.Process | None = None
        self._process_group: int | None = None
        self._watch_task: asyncio.Task[None] | None = None
        self._monitor_task: asyncio.Task[None] | None = None
        self._retained_provider_root_lock: _ProviderRootLock | None = None
        self._owned_processes: dict[int, psutil.Process | None] = {}
        self._on_ready = on_ready
        self._before_start = before_start
        self._on_unexpected_exit = on_unexpected_exit
        self._desired_running = False
        self._starting = False
        self._down = False

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    @property
    def retains_active_config(self) -> bool:
        """Whether this adapter retains execution that Stop must still prove ended."""

        return bool(
            self._process is not None
            or self._owned_processes
        )

    async def start(self) -> bool:
        """Attempt one owned sidecar launch; the supervisor owns all retries."""

        async with self._lifecycle_lock:
            self._desired_running = True
            if self.running and not self._down:
                return True
            # A failed startup or watcher may retain a direct-child reference
            # when its tree could not be proven reaped. Never launch beside it
            # or mistake it for a ready sidecar; Stop must finish cleanup first.
            if self._process is not None:
                self._desired_running = False
                self._down = True
                return False
            if self._down:
                self._down = False
        if not await self._await_before_start():
            return False
        async with self._lifecycle_lock:
            if not self._desired_running or self.running or self._down or self._process is not None:
                return self.running
        return await self._start_with_provider_lock()

    async def stop(self) -> None:
        """Stop this object’s child group and every descendant it owns."""

        async with self._lifecycle_lock:
            self._desired_running = False
            process = self._process
            process_group = self._process_group
            owned_processes = self._owned_processes
            watch_task = self._watch_task
            monitor_task = self._monitor_task
            self._starting = False
            if process is not None:
                await self._terminate_owned_tree(
                    process,
                    process_group=process_group,
                    owned_processes=owned_processes,
                )
                # Only a reaped tracked child retires the record, and only once
                # its group is clear. Stopping a supervisor that holds no child
                # must leave any recorded orphan discoverable by the next launch.
                self._ownership.retire_if_group_is_clear(process.pid, process_group)
            self._process = None
            self._process_group = None
            self._owned_processes = {}
            self._watch_task = None
            self._monitor_task = None
            if watch_task is not None and watch_task is not asyncio.current_task():
                watch_task.cancel()
            if monitor_task is not None and monitor_task is not asyncio.current_task():
                monitor_task.cancel()
            self._remove_owned_socket()
            retained_root_lock = self._retained_provider_root_lock
            self._retained_provider_root_lock = None
            if retained_root_lock is not None:
                retained_root_lock.release()

    async def processing_healthy(self) -> bool:
        """Probe processing from a short-lived child with the scrubbed key env."""

        if not self._python.is_file() or not _settings_complete(self._settings):
            return False
        try:
            try:
                probe = await self._host.spawn(
                    _ProcessKind.PROCESSING_PROBE,
                    self._python,
                    cwd=self._effective_home,
                    env=self._child_environment(role=_ProcessKind.PROCESSING_PROBE.value),
                    capture_stderr=True,
                )
            except TypeError:
                probe = await self._host.spawn(
                    _ProcessKind.PROCESSING_PROBE,
                    self._python,
                    cwd=self._effective_home,
                    env=self._child_environment(role=_ProcessKind.PROCESSING_PROBE.value),
                )
        except (OSError, ValueError):
            logger.warning("EverOS processing probe could not start; branch=probe_spawn")
            return False

        owned_processes = {probe.pid: self._host.capture(probe.pid)}
        process_group = self._host.process_group(probe.pid)
        _refresh_owned_process_tree(self._host, owned_processes, probe.pid, process_group)
        stderr = getattr(probe, "stderr", None)
        stderr_task = asyncio.create_task(_drain_probe_stderr(stderr)) if stderr is not None else None
        try:
            await asyncio.wait_for(
                probe.wait(),
                timeout=_processing_probe_timeout_seconds(self._settings),
            )
        except asyncio.TimeoutError:
            try:
                await self._terminate_owned_tree(
                    probe,
                    process_group=process_group,
                    owned_processes=owned_processes,
                    role=_ProcessKind.PROCESSING_PROBE.value,
                )
            except Exception:
                logger.warning("EverOS processing probe cleanup failed")
            logger.warning(
                "EverOS processing probe timed out; stderr_tail=%s",
                await _probe_stderr_tail(stderr_task, settings=self._settings),
            )
            return False
        except asyncio.CancelledError:
            # The bounded Memory writer bounds this probe independently. Do not let that
            # timeout orphan an owned child with the credential environment.
            try:
                await self._terminate_owned_tree(
                    probe,
                    process_group=process_group,
                    owned_processes=owned_processes,
                    role=_ProcessKind.PROCESSING_PROBE.value,
                )
            except Exception:
                logger.warning("EverOS processing probe cleanup failed")
            await _probe_stderr_tail(stderr_task, settings=self._settings)
            raise

        try:
            # A probe must not leave an untracked helper alive, even when its
            # direct child already exited successfully.
            await self._terminate_owned_tree(
                probe,
                process_group=process_group,
                owned_processes=owned_processes,
                role=_ProcessKind.PROCESSING_PROBE.value,
            )
        except Exception:
            logger.warning("EverOS processing probe cleanup failed")
            return False
        if probe.returncode != 0:
            logger.warning(
                "EverOS processing probe failed exit_code=%s stderr_tail=%s",
                probe.returncode,
                await _probe_stderr_tail(stderr_task, settings=self._settings),
            )
        else:
            await _probe_stderr_tail(stderr_task, settings=self._settings)
        return probe.returncode == 0

    async def _start_locked(self) -> bool:
        self._starting = True
        self._down = False
        try:
            self._validate_launch_inputs()
            await self._ownership.reap(discover_missing=True)
            self._require_owned_provider_root()
            self._prepare_owned_directories()
            self._write_generated_config()
            self._remove_owned_socket()
            child_env = self._child_environment(role=_SIDECAR_ROLE)
            process, spawn_interrupted = await _finish_handoff_despite_cancellation(
                self._host.spawn(
                    _ProcessKind.SIDECAR,
                    self._python,
                    cwd=self._memory_dir,
                    env=child_env,
                    socket_path=self._socket_path,
                )
            )
            self._process = process
            self._owned_processes = {process.pid: self._host.capture(process.pid)}
            self._process_group = self._host.process_group(process.pid)
            _refresh_owned_process_tree(self._host, self._owned_processes, process.pid, self._process_group)
            identity = _inspect_captured_identity(self._host, process.pid, self._owned_processes[process.pid])
            if identity is None:
                raise RuntimeError("sidecar exited before ownership could be recorded")
            self._ownership.record_launch(
                process.pid,
                identity.stamp,
                self._process_group,
            )
            if spawn_interrupted:
                raise asyncio.CancelledError
            await self._wait_for_ready(process)
            self._secure_socket()
            self._assert_no_tcp_listener(process.pid)
            self._watch_task = asyncio.create_task(self._watch_child(process), name="memory-everos-watch")
            self._monitor_task = asyncio.create_task(self._monitor_child(process), name="memory-everos-safety")
            self._starting = False
            await self._notify_ready()
            return True
        except asyncio.CancelledError:
            self._desired_running = False
            process = self._process
            process_group = self._process_group
            if process is not None:
                try:
                    await _finish_cleanup_despite_cancellation(
                        self._terminate_owned_tree(
                            process,
                            process_group=process_group,
                            owned_processes=self._owned_processes,
                        )
                    )
                except Exception:
                    # Keep both the child identity and provider-root lock so a
                    # No other owner can enter before Stop retries cleanup.
                    self._down = True
                    self._starting = False
                    raise
                self._ownership.retire_if_group_is_clear(process.pid, process_group)
            self._process = None
            self._process_group = None
            self._owned_processes = {}
            watch_task = self._watch_task
            self._watch_task = None
            monitor_task = self._monitor_task
            self._monitor_task = None
            if watch_task is not None and watch_task is not asyncio.current_task():
                watch_task.cancel()
            if monitor_task is not None and monitor_task is not asyncio.current_task():
                monitor_task.cancel()
            self._remove_owned_socket()
            self._starting = False
            raise
        except _ProviderRootBusy:
            self._starting = False
            self._desired_running = False
            self._down = True
            return False
        except Exception:
            # Every start failure collapses into `memory_sidecar_unavailable`, and
            # some of them are permanent: a recorded orphan that cannot be
            # identified fails each later attempt the same way. Log the cause
            # first, before any cleanup branch can raise or return.
            logger.exception("EverOS sidecar start failed")
            process = self._process
            process_group = self._process_group
            owned_processes = self._owned_processes
            cleanup_failed = False
            if process is not None:
                try:
                    await self._terminate_owned_tree(
                        process,
                        process_group=process_group,
                        owned_processes=owned_processes,
                    )
                except Exception:
                    logger.warning("EverOS child cleanup failed after unsuccessful startup")
                    cleanup_failed = True
            if cleanup_failed:
                # Keep all ownership references so Stop can retry. A new child
                # here could overlap the unreaped one and share its root/socket.
                self._desired_running = False
                self._down = True
                self._starting = False
                return False
            if process is not None:
                # Retire the record only for a child this attempt actually
                # reaped, and only once its group is clear. A startup that failed
                # while reaping a recorded orphan must leave that orphan
                # discoverable by the next attempt.
                self._ownership.retire_if_group_is_clear(process.pid, process_group)
            self._process = None
            self._process_group = None
            self._owned_processes = {}
            watch_task = self._watch_task
            self._watch_task = None
            monitor_task = self._monitor_task
            self._monitor_task = None
            if watch_task is not None and watch_task is not asyncio.current_task():
                watch_task.cancel()
            if monitor_task is not None and monitor_task is not asyncio.current_task():
                monitor_task.cancel()
            self._remove_owned_socket()
            self._starting = False
            # A launch may fail before subprocess creation. The runtime still
            # needs the handoff: ``before_start`` already released host
            # retention and cleanup has proved this supervisor owns no child.
            self._desired_running = False
            self._down = True
            return False

    async def _start_with_provider_lock(self) -> bool:
        """Serialize sidecar admission with other provider-root owners."""

        async with self._lifecycle_lock:
            if not self._desired_running or self.running or self._down or self._process is not None:
                return self.running
            self._starting = True
        root_lock: _ProviderRootLock | None = None
        try:
            # The default provider root lives below this Avibe-owned directory.
            # Only its parent is needed to resolve the adjacent lock; provider
            # data remains untouched until lock admission succeeds.
            _ensure_owner_directory(self._memory_dir)
            _require_provider_root_access_path(self._provider_root)
            root_lock = self._provider_root_lock()
            acquired = await _wait_for_provider_root_lock(
                root_lock,
                keep_waiting=lambda: self._desired_running,
            )
        except asyncio.CancelledError:
            async with self._lifecycle_lock:
                self._starting = False
            raise
        except Exception:
            logger.exception("EverOS sidecar provider-root admission failed")
            async with self._lifecycle_lock:
                self._starting = False
                if self._desired_running:
                    self._desired_running = False
                    self._down = True
            return False
        if not acquired:
            async with self._lifecycle_lock:
                self._starting = False
                return self.running
        try:
            async with self._lifecycle_lock:
                if (
                    not self._desired_running
                    or self.running
                    or self._down
                    or self._process is not None
                ):
                    self._starting = False
                    return self.running
                return await self._start_locked()
        finally:
            if self._process is not None and self._down:
                self._retained_provider_root_lock = root_lock
            else:
                root_lock.release()

    async def _wait_for_ready(self, process: asyncio.subprocess.Process) -> None:
        deadline = time.monotonic() + self._startup_timeout_seconds
        client = EverOSPort(self._socket_path, sidecar_timeout_seconds=2.0)
        while time.monotonic() < deadline:
            if process.returncode is not None:
                raise RuntimeError("sidecar exited before readiness")
            if self._socket_path.exists():
                self._secure_socket()
                if await client.health():
                    return
            await asyncio.sleep(0.05)
        raise RuntimeError("sidecar readiness timed out")

    async def _watch_child(self, process: asyncio.subprocess.Process) -> None:
        await process.wait()
        async with self._lifecycle_lock:
            if process is not self._process:
                return
            process_group = self._process_group
            owned_processes = self._owned_processes
            monitor_task = self._monitor_task
            try:
                await self._terminate_owned_tree(
                    process,
                    process_group=process_group,
                    owned_processes=owned_processes,
                )
            except Exception:
                # A direct child that exits can still leave a same-group helper
                # alive. Never overlap a fresh sidecar with an unreaped tree.
                self._down = True
                self._desired_running = False
                self._starting = False
                self._schedule_unexpected_exit_notification()
                return
            try:
                self._ownership.retire_if_group_is_clear(process.pid, process_group)
            except Exception:
                self._down = True
                self._desired_running = False
                self._starting = False
                self._schedule_unexpected_exit_notification()
                return
            self._process = None
            self._process_group = None
            self._owned_processes = {}
            self._monitor_task = None
            if monitor_task is not None and monitor_task is not asyncio.current_task():
                monitor_task.cancel()
            self._remove_owned_socket()
            self._starting = False
            if not self._desired_running:
                return
            self._desired_running = False
            self._down = True
        await self._notify_unexpected_exit()

    async def _monitor_child(self, process: asyncio.subprocess.Process) -> None:
        """Keep tracking descendants and reject any later TCP listener."""

        try:
            while process is self._process and process.returncode is None:
                owned_processes = self._refresh_owned_processes(process.pid)
                self._assert_no_tcp_listener(process.pid, owned_processes=owned_processes)
                await asyncio.sleep(_TREE_INSPECTION_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            return
        except Exception:
            if not self._desired_running:
                return
            notify_unexpected_exit = False
            logger.exception("EverOS sidecar safety monitor rejected the child tree (pid %s)", process.pid)
            async with self._lifecycle_lock:
                if process is not self._process:
                    return
                self._desired_running = False
                self._down = True
                process_group = self._process_group
                try:
                    await self._terminate_owned_tree(
                        process,
                        process_group=process_group,
                        owned_processes=self._owned_processes,
                    )
                except Exception:
                    logger.warning("EverOS sidecar safety shutdown did not reap the child tree")
                    self._schedule_unexpected_exit_notification()
                    return
                try:
                    self._ownership.retire_if_group_is_clear(
                        process.pid,
                        process_group,
                    )
                except Exception:
                    self._schedule_unexpected_exit_notification()
                    return
                self._process = None
                self._process_group = None
                self._owned_processes = {}
                self._monitor_task = None
                self._remove_owned_socket()
                notify_unexpected_exit = self._on_unexpected_exit is not None
            if notify_unexpected_exit:
                await self._notify_unexpected_exit()

    def _validate_launch_inputs(self) -> None:
        if os.name != "posix" or not self._python.is_file():
            raise RuntimeError("invalid sidecar launch")
        if len(os.fsencode(self._socket_path)) + 1 > _socket_path_limit():
            raise RuntimeError("socket path exceeds sun_path")
        if not _settings_complete(self._settings):
            raise RuntimeError("processing settings incomplete")

    def _require_owned_provider_root(self) -> None:
        guard = self._provider_root_guard
        if guard is None:
            raise RuntimeError("memory provider root is not claimed")
        guard()

    def _prepare_owned_directories(self) -> None:
        _prepare_memory_child_directories(
            memory_dir=self._memory_dir,
            provider_root=self._provider_root,
            settings=self._settings,
        )

    def _provider_root_lock(self) -> _ProviderRootLock:
        lock_path = _provider_root_lock_path(provider_root=self._provider_root)
        return _ProviderRootLock(
            confinement_root=lock_path.parent.parent,
            path=lock_path,
        )

    def _write_generated_config(self) -> None:
        _write_memory_child_config(
            memory_dir=self._memory_dir,
            provider_root=self._provider_root,
            attachments_root=self._attachments_root,
            settings=self._settings,
        )

    def _child_environment(self, *, role: str | None = None) -> dict[str, str]:
        return _memory_child_environment(
            python=self._python,
            memory_dir=self._memory_dir,
            provider_root=self._provider_root,
            attachments_root=self._attachments_root,
            settings=self._settings,
            role=role,
        )


    def _secure_socket(self) -> None:
        info = self._socket_path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISSOCK(info.st_mode):
            raise RuntimeError("sidecar socket is unsafe")
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise RuntimeError("sidecar socket owner mismatch")
        os.chmod(self._socket_path, _SOCKET_MODE)
        verified = self._socket_path.lstat()
        if stat.S_IMODE(verified.st_mode) != _SOCKET_MODE:
            raise RuntimeError("sidecar socket mode mismatch")

    def _remove_owned_socket(self) -> None:
        try:
            info = self._socket_path.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISSOCK(info.st_mode):
            return
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            return
        try:
            self._socket_path.unlink()
        except FileNotFoundError:
            return

    def _refresh_owned_processes(self, pid: int) -> dict[int, psutil.Process | None]:
        _refresh_owned_process_tree(self._host, self._owned_processes, pid, self._process_group)
        return dict(self._owned_processes)

    def _assert_no_tcp_listener(
        self,
        pid: int,
        *,
        owned_processes: Mapping[int, psutil.Process | None] | None = None,
    ) -> None:
        live_processes = (
            dict(owned_processes)
            if owned_processes is not None
            else self._refresh_owned_processes(pid)
        )
        if self._host.has_tcp_listener(live_processes):
            raise RuntimeError("sidecar opened a TCP listener")

    async def _terminate_owned_tree(
        self,
        process: asyncio.subprocess.Process,
        *,
        process_group: int | None,
        owned_processes: Mapping[int, psutil.Process | None] | None = None,
        role: str = _SIDECAR_ROLE,
    ) -> None:
        await _terminate_owned_process_tree(
            self._host,
            process,
            process_group=process_group,
            owned_processes=owned_processes,
            stop_timeout_seconds=self._stop_timeout_seconds,
            socket_path=self._socket_path, provider_root=self._provider_root, role=role,
        )

    async def _notify_ready(self) -> None:
        callback = self._on_ready
        if callback is None:
            return
        try:
            result = callback()
            if inspect.isawaitable(result):
                await result
        except Exception:
            logger.warning("EverOS sidecar ready callback failed")

    async def _notify_unexpected_exit(self) -> None:
        callback = self._on_unexpected_exit
        if callback is None:
            return
        try:
            result = callback()
            if inspect.isawaitable(result):
                await result
        except Exception:
            logger.warning("EverOS sidecar unexpected-exit callback failed")

    def _schedule_unexpected_exit_notification(self) -> None:
        if self._on_unexpected_exit is None:
            return
        asyncio.create_task(
            self._notify_unexpected_exit(),
            name="memory-everos-unexpected-exit-notification",
        )

    async def _notify_before_start(self) -> None:
        callback = self._before_start
        if callback is None:
            return
        result = callback()
        if inspect.isawaitable(result):
            await result

    async def _await_before_start(self) -> bool:
        """Run the host-ownership handoff without holding process lifecycle state."""

        try:
            await self._notify_before_start()
            return True
        except Exception:
            logger.exception("EverOS sidecar pre-start callback failed")
            async with self._lifecycle_lock:
                if self._process is None and self._desired_running:
                    self._starting = False
                    self._desired_running = False
                    self._down = True
            return False

    def _timezone_for_root(self) -> str:
        configured = _iana_timezone(self._settings.timezone)
        if configured is not None:
            return configured
        existing = _root_timezone(self._provider_root / "everos.toml")
        return existing or _local_iana_timezone()


class _RecordedSidecarReaper:
    """Reap an authenticated sidecar record when no supervisor exists."""

    def __init__(
        self,
        *,
        provider_root: Path | str,
        effective_home: Path | str,
        stop_timeout_seconds: float = _STOP_TIMEOUT_SECONDS,
        _host: _ProcessHost | None = None,
    ) -> None:
        filesystem_root = ConfinedRoot.from_home(effective_home)
        self._effective_home = filesystem_root.physical_home
        self._memory_dir = self._effective_home / "memory"
        self._provider_root = filesystem_root.confine(provider_root)
        self._socket_path = self._memory_dir / ".rt" / "everos.sock"
        self._host = _SystemProcessHost() if _host is None else _host
        self._ownership = SidecarOwnership(
            record_path=sidecar_record_path(self._memory_dir),
            socket_path=self._socket_path,
            provider_root=self._provider_root,
            stop_timeout_seconds=_positive_timeout(
                stop_timeout_seconds,
                _STOP_TIMEOUT_SECONDS,
            ),
            python=None,
            _host=self._host,
        )

    async def reconcile_orphan(self) -> None:
        _ensure_owner_directory(self._memory_dir)
        _require_provider_root_access_path(self._provider_root)
        root_lock = self._provider_root_lock()
        root_lock.acquire()
        try:
            interrupted = await _finish_cleanup_despite_cancellation(
                self._ownership.reap(discover_missing=True)
            )
            if interrupted:
                raise asyncio.CancelledError
        finally:
            root_lock.release()

    def _provider_root_lock(self) -> _ProviderRootLock:
        lock_path = _provider_root_lock_path(provider_root=self._provider_root)
        return _ProviderRootLock(
            confinement_root=lock_path.parent.parent,
            path=lock_path,
        )


def _provider_root_coordination_path(
    *,
    provider_root: Path | str,
    prefix: str,
    suffix: str,
) -> Path:
    """Bind one coordination artifact to the canonical root, outside provider data."""

    canonical_root = _canonical_provider_root(provider_root)
    # A sync ownership path is derived before a first-run home creates its
    # provider-root parent. Existing parents still retain their physical spelling.
    canonical_parent = _physical_existing_path(canonical_root.parent.resolve(strict=False))
    root_identity_path = (
        _physical_existing_path(canonical_root)
        if canonical_root.exists()
        else canonical_root
    )
    if sys.platform == "darwin" and not canonical_root.exists():
        # APFS/HFS volumes are commonly case-insensitive. Folding only the
        # not-yet-created final entry avoids alias races without changing
        # Linux's case-sensitive path semantics.
        root_identity_path = canonical_parent / canonical_root.name.casefold()
    root_identity = f"path:{root_identity_path}"
    root_identity = hashlib.sha256(root_identity.encode("utf-8")).hexdigest()
    return (
        canonical_parent
        / _PROVIDER_LOCK_DIRECTORY
        / f"{prefix}{root_identity}{suffix}"
    )


def _provider_root_lock_path(*, provider_root: Path) -> Path:
    """Bind cross-version provider serialization to the canonical root."""

    return _provider_root_coordination_path(
        provider_root=provider_root,
        prefix=_PROVIDER_LOCK_PREFIX,
        suffix=".lock",
    )


def _canonical_provider_root(provider_root: Path | str) -> Path:
    """Resolve a read-only physical identity for locks and process matching."""

    return Path(os.path.abspath(os.fspath(provider_root))).resolve(strict=False)


def _physical_existing_path(path: Path) -> Path:
    """Recover the directory-entry spelling for an existing physical path."""

    current = Path(path.anchor)
    for component in path.parts[1:]:
        candidate = current / component
        try:
            target = os.stat(candidate, follow_symlinks=False)
        except FileNotFoundError:
            return path
        match = component
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    if entry.name == component:
                        match = entry.name
                        break
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if (info.st_dev, info.st_ino) == (target.st_dev, target.st_ino):
                        match = entry.name
                        break
        except OSError:
            return path
        current /= match
    return current


def _require_provider_root_access_path(provider_root: Path) -> None:
    """Reject symlink traversal before any provider-root access or mutation."""

    access_root = Path(os.path.abspath(os.fspath(provider_root)))
    current = access_root
    while True:
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            pass
        except OSError as error:
            raise RuntimeError("memory provider root chain is unavailable") from error
        else:
            if stat.S_ISLNK(info.st_mode):
                raise RuntimeError("memory provider root chain contains a symlink")
            if current != access_root and not stat.S_ISDIR(info.st_mode):
                raise RuntimeError("memory provider root parent is unsafe")
        if current == current.parent:
            return
        current = current.parent


def _paths_match(value: object, expected: Path) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        observed = _canonical_provider_root(value)
        configured = _canonical_provider_root(expected)
        if observed == configured:
            return True
        observed_info = os.stat(observed, follow_symlinks=True)
        configured_info = os.stat(configured, follow_symlinks=True)
        return (observed_info.st_dev, observed_info.st_ino) == (
            configured_info.st_dev,
            configured_info.st_ino,
        )
    except (OSError, RuntimeError):
        return False


def _provider_roots_match(value: object, expected: Path) -> bool:
    return _paths_match(value, expected)


def sidecar_record_path(memory_dir: Path | str) -> Path:
    """Where a home keeps its sidecar ownership record.

    Exported so a caller that owns no supervisor -- the runtime, on a boot that
    never launches one -- can still reach the record without duplicating the
    layout.
    """

    return Path(memory_dir) / ".rt" / _SIDECAR_RECORD_FILENAME


class SidecarOwnership:
    """The record of who owns a home's sidecar, and the recovery it drives.

    Split out of ``EverOSProcess`` because recovery has to be reachable when no
    sidecar can be launched at all. A boot that finds Memory disabled, or whose
    runtime artifact or credentials fail preflight, never constructs a
    supervisor, yet it is exactly the boot that may face an orphan from the run
    before it. None of the work here needs a Python interpreter or launch
    settings: the record path, the socket, the provider root, and a stop timeout
    are the whole of it.
    """

    def __init__(
        self,
        *,
        record_path: Path,
        socket_path: Path,
        provider_root: Path,
        stop_timeout_seconds: float = _STOP_TIMEOUT_SECONDS,
        python: Path | None = None,
        _host: _ProcessHost | None = None,
    ) -> None:
        self.record_path = Path(record_path)
        self._socket_path = Path(socket_path)
        self._provider_root = Path(provider_root)
        self._stop_timeout_seconds = _positive_timeout(stop_timeout_seconds, _STOP_TIMEOUT_SECONDS)
        self._python = Path(python) if python is not None else None
        self._host = _SystemProcessHost() if _host is None else _host

    def record_launch(self, pid: int, created_at: float, process_group: int | None) -> None:
        """Persist the launched child's identity so a later boot can reap an orphan.

        Failing to persist ownership fails the launch. ``_start_locked`` already
        treats unestablished *in-memory* ownership as a start failure, and the same
        rule has to hold for persisted ownership: without this record, a later
        crash leaves an orphan the next boot cannot see, and that boot starts a
        replacement beside it on the same provider root. Raising here hands the
        just-spawned child to ``_start_locked``'s cleanup instead of leaking it.

        The isolated process group is recorded alongside the pid because the pid
        alone stops identifying the tree once the leader exits: its helpers stay in
        the group and keep the provider root open.
        """

        if not _is_identity_stamp(created_at):
            # Released records still require a readable diagnostic stamp. A
            # failed record write retains the live reference for startup cleanup.
            raise RuntimeError("could not verify the sidecar creation time to record")
        if self._python is None:
            raise RuntimeError("could not verify the EverOS child interpreter to record")
        self._persist_record(
            pid,
            created_at,
            process_group,
            role=_SIDECAR_ROLE,
            python=self._python,
        )

    def _persist_record(
        self,
        pid: int,
        created_at: float,
        process_group: int | None,
        *,
        role: str | None,
        python: Path | None,
    ) -> None:
        record: dict[str, object] = {
            "pid": pid,
            "create_time": created_at,
            "process_group": process_group,
            "socket_path": str(self._socket_path),
            "provider_root": str(self._provider_root),
        }
        if _uses_linux_starttime_stamp():
            # In-memory identity is boot-relative starttime ticks. Keep the
            # historical wall-clock field so older readers still parse the file,
            # and persist the stamp so a later boot does not depend on CLOCK_REALTIME.
            record["starttime_ticks"] = created_at
            wall_create_time = _process_wall_create_time(pid)
            # Never store ticks in create_time: older readers treat that field
            # as epoch seconds. Omit a wall value rather than invent one.
            record["create_time"] = wall_create_time
        if role is not None:
            record["role"] = role
            record["python"] = str(python) if python is not None else None
        payload = json.dumps(
            record,
            separators=(",", ":"),
            sort_keys=True,
        )
        try:
            _ensure_owner_directory(self.record_path.parent)
            _write_private_text(self.record_path, payload)
        except OSError as exc:
            raise RuntimeError("could not persist sidecar process ownership") from exc

    async def reap(self, *, discover_missing: bool = False) -> None:
        """Terminate a sidecar a previous Avibe run left behind.

        ``start_new_session=True`` means a crashed or killed service does not
        take its child down with it: the orphan keeps serving the socket and
        holding handles on provider data a later Clear may already have deleted.
        Reap it before a replacement child shares the same root, and refuse to
        launch beside one that will not exit -- the same fail-closed rule
        ``start`` already applies to an unreaped direct child.

        A recorded leader that already exited is not the end of it: the helpers it
        spawned stay in the group it led and hold the same root, so that group is
        swept as well. See ``_reap_recorded_group_without_leader``.
        """

        _require_provider_root_access_path(self._provider_root)
        record = _read_sidecar_record(self.record_path)
        pid = _recorded_sidecar_pid(record)
        if pid is None or pid == os.getpid():
            if _sidecar_record_exists(self.record_path) or discover_missing:
                # A record that is present but unusable is the opposite of an
                # absent one: a previous run did launch a sidecar, and the only
                # pointer at it is what has been lost. See
                # ``_reap_unidentified_sidecar``.
                await self._reap_unidentified_child()
            _remove_sidecar_record(self.record_path)
            return
        recorded_role = _recorded_child_role(record)
        if recorded_role not in {_SIDECAR_ROLE, _RELEASED_REBUILD_ROLE}:
            raise RuntimeError(
                "recorded EverOS child role could not be verified "
                f"(pid {pid}, record {self.record_path})"
            )
        group_match_role = (
            None if isinstance(record, dict) and record.get("role") is None else recorded_role
        )
        reference = self._host.capture(pid)
        identity = _inspect_captured_identity(self._host, pid, reference)
        verdict = _classify_recorded_child(
            record,
            identity,
            socket_path=self._socket_path,
            provider_root=self._provider_root,
            role=recorded_role,
        )
        if verdict is _RecordedSidecar.NOT_OURS:
            # Either the process is already gone, or this pid provably belongs to
            # something else. Dropping the record is the only safe action; a
            # process Avibe cannot positively identify is never signaled.
            if identity is None:
                # "Gone" is not the same as "clean": the leader exited but its
                # helpers stayed in the group it led. A recycled pid needs no such
                # sweep -- the kernel only reuses a pid once its group is empty, so
                # a live process at that pid proves nothing of ours is left there.
                await self._reap_recorded_group_without_leader(
                    record,
                    leader_pid=pid,
                    role=group_match_role,
                )
            _remove_sidecar_record(self.record_path)
            if discover_missing:
                await self._reap_unidentified_child()
                _remove_sidecar_record(self.record_path)
            return
        confirmed_create_time = identity.stamp if identity is not None else None
        if verdict is _RecordedSidecar.UNVERIFIABLE or confirmed_create_time is None:
            # A live pid the OS will not describe well enough to rule out as our
            # own sidecar. Keep the record and fail the launch, exactly as for an
            # orphan that refuses to exit: a second sidecar on the same provider
            # root is worse than a start that reports unavailable. Name the pid
            # and the record, because no later attempt can clear this by itself.
            raise RuntimeError(
                "recorded sidecar identity could not be verified "
                f"(pid {pid}, record {self.record_path})"
            )

        logger.warning(
            "Reaping an orphaned EverOS %s left by a previous Avibe run",
            recorded_role,
        )
        terminated = await self._terminate_orphan_tree(
            pid,
            reference,
            process_group=_recorded_sidecar_group(
                record, socket_path=self._socket_path, provider_root=self._provider_root,
            ),
            role=group_match_role,
        )
        if not terminated:
            raise RuntimeError(f"orphaned sidecar did not exit (pid {pid}, record {self.record_path})")
        # The recorded root is gone, but a helper it spawned after the last
        # rediscovery is not among the identities that proved it. With the leader
        # dead, that is exactly the sweep below, and it fails the launch rather
        # than spawning a replacement beside whatever it could not clear.
        await self._reap_recorded_group_without_leader(
            record,
            leader_pid=pid,
            role=group_match_role,
        )
        _remove_sidecar_record(self.record_path)
        if discover_missing:
            await self._reap_unidentified_child()
            _remove_sidecar_record(self.record_path)

    async def _terminate_orphan_tree(
        self, pid: int, reference: psutil.Process | None,
        *, process_group: int | None, role: str | None,
    ) -> bool:
        """Reap an orphan's whole tree, not just the pid the record names.

        The sidecar may have spawned helpers before the service died, and those
        keep the provider root open just as the root process does. Discovery and
        signalling therefore reuse the same helpers as ``_terminate_owned_tree``
        -- descendants plus the isolated process group, with a group-wide signal
        only once every member is confirmed owned. The one difference is that no
        ``asyncio`` child handle exists for a process this run did not spawn, so
        liveness is decided purely from the captured identities.
        """

        identities: dict[int, psutil.Process | None] = {pid: reference}
        rounds = (
            (signal.SIGTERM, self._stop_timeout_seconds),
            (getattr(signal, "SIGKILL", signal.SIGTERM), min(self._stop_timeout_seconds, 3.0)),
        )
        process_group = _captured_process_group(self._host, pid, reference, process_group)
        for signum, timeout_seconds in rounds:
            _refresh_terminating_process_tree(
                self._host, identities, pid, process_group,
                socket_path=self._socket_path, provider_root=self._provider_root, role=role,
            )
            self._host.signal(identities, signum, process_group=process_group)
            if await self._host.wait_for_exit(identities, timeout_seconds, process_group=process_group):
                return True
        return False

    async def _reap_recorded_group_without_leader(
        self,
        record: object,
        *,
        leader_pid: int,
        role: str | None,
    ) -> None:
        """Reap what an exited recorded leader left behind in its own group.

        A gone leader used to retire the record with no scan at all, yet
        ``start_new_session=True`` put every helper the sidecar spawned into the
        leader's own group, where they keep serving the socket and holding the
        provider root open while a replacement sidecar starts.

        Group membership alone cannot stand in for the leader's identity here. A pid
        is held out of reuse only while its group still has members (Linux defers
        ``free_pid`` while ``pid_has_task(pid, PIDTYPE_PGID)``; XNU's fork retries
        past any pid that is still a pgid or sid), so a group that did empty out may
        since have been recreated by an unrelated process that took the same pid and
        called ``setsid``. Every member therefore has to tie *itself* to this
        installation before it is signaled; the rest are logged and left running,
        because a group Avibe cannot claim must not block its own startup forever.
        """

        group = _recorded_sidecar_group(
            record,
            socket_path=self._socket_path,
            provider_root=self._provider_root,
        )
        if group is None:
            # A record written by an older build carries no group, and its dead
            # leader is the only identity it holds. There is nothing safe to scan,
            # which leaves exactly the behavior that build already had.
            return
        if hasattr(os, "getpgrp") and group == os.getpgrp():
            # Signalling this group would take Avibe itself down.
            # ``_isolated_process_group`` never records our own group, so a record
            # naming it was not written by a launch of ours.
            logger.warning("Ignoring a recorded sidecar group that is Avibe's own process group")
            return
        owned, foreign = self._host.recorded_group_members(
            group,
            socket_path=self._socket_path,
            provider_root=self._provider_root,
            role=role,
        )
        if foreign:
            logger.warning(
                "Leaving %s process(es) in recorded sidecar group %s alone: %s",
                len(foreign),
                group,
                foreign,
            )
        if not owned:
            if foreign:
                raise RuntimeError(
                    "orphaned sidecar group could not be verified "
                    f"(leader pid {leader_pid}, group {group}, record {self.record_path})"
                )
            return
        logger.warning(
            "Reaping EverOS sidecar processes left in group %s by a previous Avibe run",
            group,
        )
        terminated, later_foreign = await self._terminate_claimed_processes(
            group,
            owned,
            role=role,
        )
        foreign = sorted(set(foreign).union(later_foreign))
        if not terminated:
            raise RuntimeError(
                "orphaned sidecar group did not exit "
                f"(leader pid {leader_pid}, group {group}, record {self.record_path})"
            )
        if foreign:
            raise RuntimeError(
                "orphaned sidecar group could not be verified "
                f"(leader pid {leader_pid}, group {group}, record {self.record_path})"
            )

    def retire_if_group_is_clear(
        self,
        leader_pid: int,
        process_group: int | None,
    ) -> None:
        """Retire the ownership record, unless the group still holds one of ours.

        A successful ``_terminate_owned_tree`` proves that every identity it
        captured is gone, and nothing more. A helper the sidecar spawned after the
        monitor's last snapshot is not in that set, and once the leader has exited
        nothing puts it there: rediscovery is anchored on the live leader, and the
        group-wide signal is refused because the unknown member cannot be
        confirmed. The wait then reports success over the identities it does hold.

        Retiring the record on that evidence discards the next launch's only route
        to the survivor -- ``_reap_recorded_group_without_leader`` needs the
        recorded group -- so the replacement sidecar comes up beside it on the same
        provider root. Keeping the record instead leaves the sweep to the next
        launch, which fails closed if the group still will not clear.

        Keeping it is safe to act on later: the record names the leader's pid, and
        a pid is not reused while its group still has members, so the boot that
        reads this record cannot find a stranger at that pid. A successful launch
        overwrites the record as it always has.
        """

        record = _read_sidecar_record(self.record_path)
        if (
            _recorded_sidecar_pid(record) != leader_pid
            or _recorded_child_role(record) != _SIDECAR_ROLE
            or _recorded_sidecar_group(
                record,
                socket_path=self._socket_path,
                provider_root=self._provider_root,
            )
            != process_group
        ):
            return
        if process_group is not None:
            claimed, foreign = self._host.recorded_group_members(
                process_group,
                socket_path=self._socket_path,
                provider_root=self._provider_root,
                role=_SIDECAR_ROLE,
            )
            if claimed or foreign:
                logger.warning(
                    "Keeping the EverOS ownership record: process group %s still holds claimed=%s unverifiable=%s, record %s",
                    process_group,
                    sorted(claimed),
                    foreign,
                    self.record_path,
                )
                if foreign:
                    raise RuntimeError(
                        "sidecar process group could not be verified "
                        f"(group {process_group}, record {self.record_path})"
                    )
                raise RuntimeError(
                    "sidecar process group did not exit "
                    f"(group {process_group}, record {self.record_path})"
                )
        _remove_sidecar_record(self.record_path)

    async def _reap_unidentified_child(self) -> None:
        """Re-establish ownership from live processes when the record cannot.

        ``_read_sidecar_record`` answers ``None`` both for "no previous run
        recorded anything" and for "a record is there, but it is truncated,
        oversized, or unreadable". Those demand opposite actions, and treating the
        second as the first launches a replacement beside a sidecar that may still
        be serving this socket -- the overlap the record exists to prevent.

        Failing closed on an unusable record instead would be its own trap: nothing
        repairs a corrupt file, so every later start would fail with no way out.
        Ownership is therefore rebuilt from observable facts, which need no record
        at all: if nothing on this machine is running our sidecar entrypoint
        against our socket, the unusable record describes something already gone
        and the launch continues; if something is, it is reaped like any other
        orphan, and a tree that will not exit fails the launch and keeps the record.
        """

        socket_anchors = self._host.find_sidecars(socket_path=self._socket_path)
        root_anchors = self._host.find_sidecars_by_root(
            provider_root=self._provider_root
        )
        root_only = set(root_anchors).difference(socket_anchors)
        _merge_owned_processes(socket_anchors, root_anchors)
        if not socket_anchors:
            return
        for pid, reference in sorted(socket_anchors.items()):
            identity = _inspect_captured_identity(self._host, pid, reference)
            if identity is None:
                if not await self._terminate_orphan_tree(pid, reference, process_group=pid, role=None):
                    raise RuntimeError("orphaned sidecar group did not exit")
                continue
            if pid in root_only:
                if (
                    identity.cmdline is None
                    or not _cmdline_matches_role(
                        identity.cmdline,
                        role=_SIDECAR_ROLE,
                        socket_path=self._socket_path,
                    )
                ):
                    raise _ProviderRootBusy(
                        "a live sidecar already owns this provider root"
                    )
            logger.warning(
                "Reaping an EverOS sidecar an unusable ownership record could not identify (pid %s)",
                pid,
            )
            identities = {pid: reference}
            # Helpers are reached through the anchor's own group rather than by
            # widening the machine-wide test, because membership is what makes the
            # looser per-member claim safe.
            group = _captured_process_group(self._host, pid, reference, pid)
            self._persist_record(
                pid,
                identity.stamp,
                group,
                role=None,
                python=None,
            )
            foreign: list[int] = []
            if group is not None:
                claimed, foreign = self._host.recorded_group_members(
                    group,
                    socket_path=self._socket_path,
                    provider_root=self._provider_root,
                    role=None,
                )
                _merge_owned_processes(identities, claimed)
                if foreign:
                    logger.warning(
                        "Leaving %s process(es) in EverOS sidecar group %s alone: %s",
                        len(foreign),
                        group,
                        foreign,
                    )
            terminated, later_foreign = await self._terminate_claimed_processes(
                group,
                identities,
                role=_SIDECAR_ROLE,
            )
            foreign = sorted(set(foreign).union(later_foreign))
            if not terminated:
                raise RuntimeError(
                    "EverOS sidecar left by an unusable record did not exit "
                    f"(pid {pid}, record {self.record_path})"
                )
            if foreign:
                raise RuntimeError(
                    "orphaned sidecar group could not be verified "
                    f"(leader pid {pid}, group {group}, record {self.record_path})"
                )

    async def _terminate_claimed_processes(
        self,
        process_group: int | None,
        identities: dict[int, psutil.Process | None],
        *,
        role: str | None = None,
    ) -> tuple[bool, list[int]]:
        """Signal claimed processes until none of them is left.

        Mirrors ``_terminate_orphan_tree``, minus the recorded root: whatever this
        run claimed is all there is to work from. A process group, when one is
        known, is both the rediscovery anchor and the only thing that permits a
        group-wide signal. Late members must pass contextual classification;
        terminal retained references are never replaced by these discoveries.
        The result carries both the claimed-tree death proof and every unverifiable
        group member observed during those rediscovery rounds.
        """

        rounds = (
            (signal.SIGTERM, self._stop_timeout_seconds),
            (getattr(signal, "SIGKILL", signal.SIGTERM), min(self._stop_timeout_seconds, 3.0)),
        )
        foreign: set[int] = set()
        for signum, timeout_seconds in rounds:
            if process_group is not None:
                discovered, round_foreign = self._host.recorded_group_members(
                    process_group,
                    socket_path=self._socket_path,
                    provider_root=self._provider_root,
                    role=role,
                )
                _merge_owned_processes(identities, discovered)
                foreign.update(round_foreign)
            self._host.signal(identities, signum, process_group=process_group)
            if await self._host.wait_for_exit(identities, timeout_seconds, process_group=process_group):
                return True, sorted(foreign)
        return False, sorted(foreign)


def legacy_sync_record_path(provider_root: Path | str) -> Path:
    """Return the released provider-root-scoped sync ownership path."""

    return _provider_root_coordination_path(
        provider_root=provider_root,
        prefix=_SYNC_RECORD_PREFIX,
        suffix=".json",
    )


class _ReleasedSyncReaper:
    """Consume released sync ownership without restoring sync orchestration."""

    def __init__(
        self,
        *,
        provider_root: Path | str,
        effective_home: Path | str,
        stop_timeout_seconds: float = _STOP_TIMEOUT_SECONDS,
        _host: _ProcessHost | None = None,
    ) -> None:
        filesystem_root = ConfinedRoot.from_home(effective_home)
        self._effective_home = filesystem_root.physical_home
        self._memory_dir = self._effective_home / "memory"
        self._provider_root = filesystem_root.confine(provider_root)
        self._socket_path = self._memory_dir / ".rt" / "everos.sock"
        self._host = _SystemProcessHost() if _host is None else _host
        self._record_path = legacy_sync_record_path(self._provider_root)
        self._stop_timeout_seconds = _positive_timeout(
            stop_timeout_seconds,
            _STOP_TIMEOUT_SECONDS,
        )

    async def reconcile_orphan(self) -> None:
        _ensure_owner_directory(self._memory_dir)
        _require_provider_root_access_path(self._provider_root)
        lock_path = _provider_root_lock_path(provider_root=self._provider_root)
        lock = _ProviderRootLock(confinement_root=lock_path.parent.parent, path=lock_path)
        lock.acquire()
        try:
            record = _read_sidecar_record(
                self._record_path,
                max_bytes=_SYNC_RECORD_MAX_BYTES,
            )
            if record is None:
                if _sidecar_record_exists(self._record_path):
                    raise RuntimeError("released sync ownership record is unsafe")
                return
            validated = _validate_legacy_sync_record(
                record,
                provider_root=self._provider_root,
                socket_path=self._socket_path,
            )
            if self._recorded_parent_is_live(validated):
                raise RuntimeError("released sync is still owned by a live parent")
            interrupted = await _finish_cleanup_despite_cancellation(
                self._reconcile_exclusive(validated)
            )
            if interrupted:
                raise asyncio.CancelledError
        finally:
            lock.release()

    def _recorded_parent_is_live(self, record: Mapping[str, Any]) -> bool:
        identity = self._host.inspect_identity(int(record["parent_pid"]))
        if identity is None or record.get("cleanup_failed") is True:
            return False
        expected_uid = record.get("parent_uid")
        if expected_uid is not None:
            if identity.uid is None:
                raise RuntimeError("released sync parent identity is unavailable")
            if identity.uid != expected_uid:
                return False
        if "parent_starttime_ticks" in record:
            if not _is_identity_stamp(identity.stamp):
                raise RuntimeError("released sync parent identity is unavailable")
            return float(identity.stamp) == float(record["parent_starttime_ticks"])
        if not _is_identity_stamp(identity.wall_create_time):
            raise RuntimeError("released sync parent identity is unavailable")
        return float(identity.wall_create_time) == float(record["parent_create_time"])

    async def _reconcile_exclusive(self, record: Mapping[str, Any]) -> None:
        identities: dict[int, psutil.Process | None] = {}
        if record["state"] == "pending":
            candidates = self._host.find_syncs(
                provider_root=self._provider_root,
                python=Path(record["argv"][0]),
                nonce=str(record["nonce"]),
            )
            if not candidates:
                _remove_legacy_sync_record(self._record_path)
                return
            if len(candidates) != 1:
                raise RuntimeError("released pending sync ownership is ambiguous")
            pid, reference = next(iter(candidates.items()))
            identity = _inspect_captured_identity(self._host, pid, reference)
            if identity is not None:
                _validate_legacy_sync_identity(
                    identity, record, provider_root=self._provider_root, require_argv=True,
                )
            # Released sync launches use setsid; never read a replacement PID's group.
            group = _captured_process_group(self._host, pid, reference, pid)
            if group is None:
                raise RuntimeError("released pending sync process group is unavailable")
            identities[pid] = reference
        else:
            pid = int(record["pid"])
            group = int(record["process_group"])
            reference = self._host.capture(pid)
            identity = _inspect_captured_identity(self._host, pid, reference)
            if identity is not None:
                verdict = _classify_recorded_child(
                    record,
                    identity,
                    socket_path=self._socket_path,
                    provider_root=self._provider_root,
                    role=_RELEASED_SYNC_ROLE,
                )
                if verdict is _RecordedSidecar.NOT_OURS:
                    _remove_legacy_sync_record(self._record_path)
                    return
                if verdict is _RecordedSidecar.UNVERIFIABLE:
                    raise RuntimeError("released finalized sync identity is unavailable")
                if not _is_identity_stamp(identity.stamp):
                    raise RuntimeError("released finalized sync identity is unavailable")
                _validate_legacy_sync_identity(
                    identity,
                    record,
                    provider_root=self._provider_root,
                    require_argv=True,
                )
                identities[pid] = reference

        if hasattr(os, "getpgrp") and group == os.getpgrp():
            raise RuntimeError("released sync process group is unsafe")
        self._merge_validated_group(record, group, identities)
        await self._terminate_group(record, group, identities)
        remaining: dict[int, psutil.Process | None] = {}
        self._merge_validated_group(record, group, remaining)
        if self._host.live(remaining):
            raise RuntimeError("released sync process group did not exit")
        _remove_legacy_sync_record(self._record_path)

    def _merge_validated_group(
        self,
        record: Mapping[str, Any],
        group: int,
        identities: dict[int, psutil.Process | None],
    ) -> None:
        claimed, foreign = self._host.recorded_group_members(
            group,
            socket_path=self._socket_path,
            provider_root=self._provider_root,
            role=_RELEASED_SYNC_ROLE,
        )
        if foreign:
            raise RuntimeError("released sync process group is unverifiable")
        for pid, reference in claimed.items():
            identity = _inspect_captured_identity(self._host, pid, reference)
            if identity is None:
                continue
            _validate_legacy_sync_identity(
                identity,
                record,
                provider_root=self._provider_root,
                require_argv=pid == record.get("pid"),
            )
            identities.setdefault(pid, reference)

    async def _terminate_group(
        self,
        record: Mapping[str, Any],
        group: int,
        identities: dict[int, psutil.Process | None],
    ) -> None:
        rounds = (
            (signal.SIGTERM, self._stop_timeout_seconds),
            (
                getattr(signal, "SIGKILL", signal.SIGTERM),
                min(self._stop_timeout_seconds, 3.0),
            ),
        )
        for signum, timeout_seconds in rounds:
            self._merge_validated_group(record, group, identities)
            self._host.signal(identities, signum, process_group=group)
            if await self._host.wait_for_exit(identities, timeout_seconds, process_group=group):
                return
        raise RuntimeError("released sync process group did not exit")


class ReleasedEverOSOrphanReconciler:
    """One compatibility boundary for process records shipped before PR 4.

    This adapter only consumes released ownership records. It never launches a
    rebuild or sync child and never writes workflow state.
    """

    def __init__(
        self,
        *,
        provider_root: Path | str,
        effective_home: Path | str,
        stop_timeout_seconds: float = _STOP_TIMEOUT_SECONDS,
        _host: _ProcessHost | None = None,
    ) -> None:
        host = _SystemProcessHost() if _host is None else _host
        arguments = {
            "provider_root": provider_root,
            "effective_home": effective_home,
            "stop_timeout_seconds": stop_timeout_seconds,
            "_host": host,
        }
        self._released_sync = _ReleasedSyncReaper(**arguments)
        self._sidecar = _RecordedSidecarReaper(**arguments)

    async def reconcile_orphans(self) -> None:
        await self._released_sync.reconcile_orphan()
        await self._sidecar.reconcile_orphan()


def _validate_legacy_sync_record(
    record: object,
    *,
    provider_root: Path,
    socket_path: Path,
) -> Mapping[str, Any]:
    if not isinstance(record, dict):
        raise RuntimeError("released sync ownership record is invalid")
    if (
        record.get("role") != _RELEASED_SYNC_ROLE
        or not _provider_roots_match(record.get("provider_root"), provider_root)
        or not _paths_match(record.get("socket_path"), socket_path)
        or record.get("state") not in {"pending", "finalized"}
    ):
        raise RuntimeError("released sync ownership record is invalid")
    nonce = record.get("nonce")
    argv = record.get("argv")
    cleanup_failed = record.get("cleanup_failed", False)
    if (
        not isinstance(cleanup_failed, bool)
        or not isinstance(nonce, str)
        or len(nonce) != 64
        or any(character not in "0123456789abcdef" for character in nonce)
        or not isinstance(argv, list)
        or len(argv) != len(_SYNC_ARGV) + 1
        or not isinstance(argv[0], str)
        or not Path(argv[0]).is_absolute()
        or tuple(argv[1:]) != _SYNC_ARGV
    ):
        raise RuntimeError("released sync ownership record is invalid")
    parent_pid = record.get("parent_pid")
    parent_uid = record.get("parent_uid")
    create_time = record.get("create_time")
    if (
        not isinstance(parent_pid, int)
        or isinstance(parent_pid, bool)
        or parent_pid <= 1
        or not _is_identity_stamp(record.get("parent_create_time"))
        or (create_time is not None and not _is_identity_stamp(create_time))
        or (
            parent_uid is not None
            and (
                not isinstance(parent_uid, int)
                or isinstance(parent_uid, bool)
                or parent_uid < 0
            )
        )
        or (
            "parent_starttime_ticks" in record
            and not _is_identity_stamp(record.get("parent_starttime_ticks"))
        )
    ):
        raise RuntimeError("released sync ownership record is invalid")
    if record["state"] == "finalized" and (
        not isinstance(record.get("pid"), int)
        or isinstance(record.get("pid"), bool)
        or int(record["pid"]) <= 1
        or not isinstance(record.get("process_group"), int)
        or isinstance(record.get("process_group"), bool)
        or int(record["process_group"]) <= 1
        or (
            "starttime_ticks" not in record
            and not _is_identity_stamp(record.get("create_time"))
        )
    ):
        raise RuntimeError("released sync ownership record is invalid")
    if "starttime_ticks" in record and not _is_identity_stamp(
        record.get("starttime_ticks")
    ):
        raise RuntimeError("released sync ownership record is invalid")
    return record


def _validate_legacy_sync_identity(
    identity: _ProcessIdentity | None,
    record: Mapping[str, Any],
    *,
    provider_root: Path,
    require_argv: bool,
) -> None:
    if identity is None or not _is_identity_stamp(identity.stamp):
        raise RuntimeError("released sync identity is unavailable")
    if require_argv and identity.cmdline != tuple(record["argv"]):
        raise RuntimeError("released sync identity is unavailable")
    expected_uid = record.get("parent_uid")
    if expected_uid is not None and identity.uid != expected_uid:
        raise RuntimeError("released sync identity is unavailable")
    environment = identity.environment
    if environment is None:
        raise RuntimeError("released sync identity is unavailable")
    expected_parent_uid = "" if expected_uid is None else str(expected_uid)
    if (
        not _provider_roots_match(environment.get("EVEROS_ROOT"), provider_root)
        or environment.get("AVIBE_MEMORY_CHILD_ROLE")
        != _RELEASED_SYNC_ROLE
        or environment.get(_SYNC_NONCE_ENV) != record["nonce"]
        or environment.get(_SYNC_PARENT_PID_ENV) != str(record["parent_pid"])
        or environment.get(_SYNC_PARENT_CREATE_TIME_ENV)
        != float(record["parent_create_time"]).hex()
        or environment.get(_SYNC_PARENT_UID_ENV) != expected_parent_uid
    ):
        raise RuntimeError("released sync identity is unavailable")


def _settings_complete(settings: EverOSProcessSettings) -> bool:
    return all(
        isinstance(value, str) and bool(value.strip())
        for value in (
            settings.llm_base_url,
            settings.llm_model,
            settings.llm_api_key,
            settings.embedding_base_url,
            settings.embedding_model,
            settings.embedding_api_key,
        )
    )


def _processing_probe_timeout_seconds(settings: EverOSProcessSettings) -> float:
    rerank = None
    if _endpoint_settings_complete(
        settings.rerank_base_url,
        settings.rerank_model,
        settings.rerank_api_key,
    ):
        rerank = (settings.rerank_base_url, settings.rerank_api_key)
    multimodal = None
    if _endpoint_settings_complete(
        settings.multimodal_base_url,
        settings.multimodal_model,
        settings.multimodal_api_key,
    ):
        multimodal = (settings.multimodal_base_url, settings.multimodal_api_key)
    return processing_probe_deadline_seconds(
        llm=(settings.llm_base_url, settings.llm_api_key),
        embedding=(settings.embedding_base_url, settings.embedding_api_key),
        rerank=rerank,
        multimodal=multimodal,
    )


def _endpoint_settings_complete(*values: str | None) -> bool:
    return all(isinstance(value, str) and bool(value.strip()) for value in values)


def _prepare_memory_child_directories(
    *,
    memory_dir: Path,
    provider_root: Path,
    settings: EverOSProcessSettings,
) -> None:
    directories = [
        memory_dir,
        memory_dir / ".rt",
        memory_dir / ".child-home",
        memory_dir / ".child-home" / ".cache",
        memory_dir / ".child-home" / ".config",
        memory_dir / ".child-home" / ".local",
        memory_dir / ".child-home" / ".local" / "share",
        memory_dir / ".child-home" / ".local" / "state",
        memory_dir / "generated",
    ]
    for directory in directories:
        _ensure_owner_directory(directory)
    _require_private_provider_root(provider_root)


def _require_private_provider_root(provider_root: Path) -> None:
    _require_provider_root_access_path(provider_root)
    try:
        info = provider_root.lstat()
    except OSError as error:
        raise RuntimeError("memory provider root is not claimed") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("memory provider root is unsafe")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise RuntimeError("memory provider root owner mismatch")
    if stat.S_IMODE(info.st_mode) != _OWNER_DIR_MODE:
        raise RuntimeError("memory provider root mode mismatch")


def _write_memory_child_config(
    *,
    memory_dir: Path,
    provider_root: Path,
    attachments_root: Path,
    settings: EverOSProcessSettings,
) -> None:
    generated = memory_dir / "generated"
    timezone_name = (
        _iana_timezone(settings.timezone)
        or _root_timezone(provider_root / "everos.toml")
        or _local_iana_timezone()
    )
    timezone = _toml_string(timezone_name)
    everos_contents = "\n".join(
        (
            "# Generated by Avibe. No API keys are stored here.",
            "[memory]",
            f"timezone = {timezone}",
            "",
            "[memorize]",
            'mode = "chat"',
            "",
            "[rerank]",
            'model = ""',
            'base_url = ""',
            "",
            "[multimodal]",
            f"file_uri_allow_dirs = [{_toml_string(str(attachments_root))}]",
            "file_uri_max_bytes = 26214400",
            "",
        )
    )
    ome_contents = "\n".join(
        (
            "# Generated by Avibe.",
            "[strategies.reflect_episodes]",
            "enabled = false",
            "",
            "[strategies.extract_foresight]",
            "enabled = false",
            "",
            "[strategies.trigger_profile_clustering]",
            f"enabled = {str(settings.profile_enabled).lower()}",
            "",
            "[strategies.extract_user_profile]",
            f"enabled = {str(settings.profile_enabled).lower()}",
            "",
        )
    )
    _validate_generated_config(everos_contents, ome_contents, timezone_name, settings)
    for path, contents in (
        (generated / "everos.toml", everos_contents),
        (generated / "ome.toml", ome_contents),
        (provider_root / "everos.toml", everos_contents),
        (provider_root / "ome.toml", ome_contents),
    ):
        _write_private_text(path, contents)


def _memory_child_environment(
    *,
    python: Path,
    memory_dir: Path,
    provider_root: Path,
    attachments_root: Path,
    settings: EverOSProcessSettings,
    role: str | None,
) -> dict[str, str]:
    child_home = memory_dir / ".child-home"
    env = {
        "ENV": "prod",
        "HOME": str(child_home),
        "PATH": f"{python.parent}:/usr/bin:/bin",
        "PYTHONNOUSERSITE": "1",
        "PYTHONUNBUFFERED": "1",
        "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
        "XDG_CACHE_HOME": str(child_home / ".cache"),
        "XDG_CONFIG_HOME": str(child_home / ".config"),
        "XDG_DATA_HOME": str(child_home / ".local" / "share"),
        "XDG_STATE_HOME": str(child_home / ".local" / "state"),
        "EVEROS_ROOT": str(provider_root),
        "AVIBE_MEMORY_ATTACHMENTS_ROOT": str(attachments_root),
    }
    optional = {
        "EVEROS_LLM__BASE_URL": settings.llm_base_url,
        "EVEROS_LLM__MODEL": settings.llm_model,
        "EVEROS_LLM__API_KEY": settings.llm_api_key,
        # Workbench keeps one compatibility cycle of implicit LLM inheritance.
        # IM capture is gated separately by the explicit persisted endpoint.
        "EVEROS_MULTIMODAL__BASE_URL": settings.multimodal_base_url or settings.llm_base_url,
        "EVEROS_MULTIMODAL__MODEL": settings.multimodal_model or settings.llm_model,
        "EVEROS_MULTIMODAL__API_KEY": settings.multimodal_api_key or settings.llm_api_key,
        "EVEROS_EMBEDDING__BASE_URL": settings.embedding_base_url,
        "EVEROS_EMBEDDING__MODEL": settings.embedding_model,
        "EVEROS_EMBEDDING__API_KEY": settings.embedding_api_key,
        "EVEROS_RERANK__BASE_URL": settings.rerank_base_url,
        "EVEROS_RERANK__MODEL": settings.rerank_model,
        "EVEROS_RERANK__API_KEY": settings.rerank_api_key,
        "EVEROS_RERANK__PROVIDER": (
            (settings.rerank_provider or "deepinfra")
            if all(
                (
                    settings.rerank_base_url,
                    settings.rerank_model,
                    settings.rerank_api_key,
                )
            )
            else None
        ),
    }
    env.update({key: value for key, value in optional.items() if value is not None})
    if all(
        isinstance(value, str) and bool(value.strip())
        for value in (
            settings.multimodal_base_url,
            settings.multimodal_model,
            settings.multimodal_api_key,
        )
    ):
        env[MULTIMODAL_EXPLICIT_ENV] = "1"
    if role is not None:
        env["AVIBE_MEMORY_CHILD_ROLE"] = role
    return env


async def _terminate_owned_process_tree(
    host: _ProcessHost,
    process: asyncio.subprocess.Process,
    *,
    process_group: int | None,
    owned_processes: Mapping[int, psutil.Process | None] | None,
    stop_timeout_seconds: float,
    socket_path: Path,
    provider_root: Path,
    role: str | None = _SIDECAR_ROLE,
) -> None:
    identities = owned_processes if isinstance(owned_processes, dict) else dict(owned_processes or {})
    rounds = (
        (signal.SIGTERM, stop_timeout_seconds),
        (getattr(signal, "SIGKILL", signal.SIGTERM), min(stop_timeout_seconds, 3.0)),
    )
    for signum, timeout_seconds in rounds:
        _refresh_terminating_process_tree(
            host, identities, process.pid, process_group,
            socket_path=socket_path, provider_root=provider_root, role=role,
        )
        host.signal(identities, signum, process_group=process_group, process=process)
        if await host.wait_for_exit(
            identities, timeout_seconds, process_group=process_group, process=process,
        ):
            return
    raise RuntimeError("EverOS child process tree did not exit")


async def _finish_cleanup_despite_cancellation(cleanup: Awaitable[None]) -> bool:
    task = asyncio.create_task(cleanup, name="memory-everos-owned-cleanup")
    interrupted = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            interrupted = True
            continue
    await task
    return interrupted


async def _finish_handoff_despite_cancellation(
    handoff: Awaitable[_IdentityFieldT],
) -> tuple[_IdentityFieldT, bool]:
    """Finish an ownership-bearing handoff before honoring cancellation."""

    task = asyncio.create_task(handoff, name="memory-everos-owned-handoff")
    interrupted = False
    while not task.done():
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError:
            interrupted = True
            continue
        return result, interrupted
    return await task, interrupted


def _socket_path_limit() -> int:
    return 104 if sys.platform == "darwin" else 108


def _isolated_process_group(pid: int) -> int | None:
    if os.name != "posix" or not hasattr(os, "getpgid"):
        return None
    try:
        group = os.getpgid(pid)
    except OSError:
        return None
    return group if group != os.getpgrp() else None


@dataclass(frozen=True)
class _ProcessIdentity:
    """The observable facts that must match before Avibe signals a recorded pid.

    Every field is independently optional because the OS may disclose some facts
    about a process and withhold others: macOS reads ``create_time`` and ``uids``
    for any pid but refuses ``cmdline`` outside the caller's own uid. ``None``
    therefore means "not disclosed", never "does not match".

    ``stamp`` is the identity value from ``_process_creation_stamp``:
    Linux starttime ticks, or ``psutil.Process.create_time()`` elsewhere.
    ``wall_create_time`` retains the epoch value needed to compare a live Linux
    process with a legacy record that predates ``starttime_ticks``.
    """

    stamp: float | None
    cmdline: tuple[str, ...] | None
    uid: int | None
    environment: Mapping[str, str] | None = None
    wall_create_time: float | None = None


def _uses_linux_starttime_stamp() -> bool:
    """Whether this host identifies processes by boot-relative starttime ticks."""

    return sys.platform.startswith("linux")


def _parse_proc_stat_starttime(stat_data: bytes) -> float:
    """Parse ``/proc/<pid>/stat`` field 22 (starttime, clock ticks).

    Field 2 (``comm``) is parenthesized and may contain spaces or parentheses,
    so tokens after the last ``)`` are field 3 onward.
    """

    close = stat_data.rfind(b")")
    if close < 0:
        raise ValueError("proc stat comm field is missing")
    fields = stat_data[close + 1 :].split()
    try:
        return float(fields[19])
    except (IndexError, ValueError) as exc:
        raise ValueError("proc stat starttime field is missing") from exc


def _read_linux_starttime_ticks(pid: int) -> float:
    """Read boot-relative starttime ticks for ``pid`` from ``/proc``."""

    try:
        data = Path(f"/proc/{pid}/stat").read_bytes()
    except FileNotFoundError as exc:
        raise psutil.NoSuchProcess(pid=pid) from exc
    except PermissionError as exc:
        raise psutil.AccessDenied(pid=pid) from exc
    except OSError as exc:
        if exc.errno in {errno.EACCES, errno.EPERM}:
            raise psutil.AccessDenied(pid=pid) from exc
        if exc.errno in {errno.ENOENT, errno.ESRCH}:
            raise psutil.NoSuchProcess(pid=pid) from exc
        raise
    try:
        return _parse_proc_stat_starttime(data)
    except ValueError as exc:
        raise psutil.AccessDenied(pid=pid) from exc


def _process_creation_stamp(process: psutil.Process) -> float:
    """Stable process-identity stamp.

    On Linux this is ``/proc/<pid>/stat`` starttime ticks, which do not move
    when CLOCK_REALTIME is stepped. Other platforms keep the spawn-time
    ``psutil.Process.create_time()`` value, which those kernels record
    absolutely and do not drift. Missing ``/proc`` falls back to
    ``create_time()`` so test doubles without a real pid still work.
    """

    if _uses_linux_starttime_stamp():
        try:
            return _read_linux_starttime_ticks(int(process.pid))
        except psutil.NoSuchProcess:
            return float(process.create_time())
    return float(process.create_time())


def _process_wall_create_time(pid: int) -> float | None:
    """Wall-clock ``psutil`` create_time, or ``None`` when the OS withholds it."""

    try:
        return float(psutil.Process(pid).create_time())
    except psutil.Error:
        return None


def _is_identity_stamp(value: object) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        stamp = float(value)
    except (OverflowError, ValueError):
        return False
    return math.isfinite(stamp) and stamp >= 0.0


class _RecordedSidecar(Enum):
    """What a recorded pid turned out to be, and so what the launch may do.

    ``NOT_OURS`` is both "already gone" and "provably somebody else's": the
    record can be retired and the launch continues. ``UNVERIFIABLE`` is a live
    pid that cannot be excluded as our own sidecar, which must fail the launch.
    """

    OURS = "ours"
    NOT_OURS = "not_ours"
    UNVERIFIABLE = "unverifiable"


def _inspect_process_identity(pid: int) -> _ProcessIdentity | None:
    """Read a live process' identity, or ``None`` when it is confirmed gone.

    Fields are read one by one so an undisclosed field cannot collapse the whole
    process to "gone". Only ``NoSuchProcess`` -- which ``ZombieProcess`` derives
    from -- and an explicit zombie status prove the pid no longer runs.
    """

    try:
        process = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return None
    except psutil.Error:
        return _ProcessIdentity(stamp=None, cmdline=None, uid=None, environment=None)
    try:
        if process.status() == psutil.STATUS_ZOMBIE:
            return None
    except psutil.NoSuchProcess:
        return None
    except psutil.Error:
        pass
    try:
        cmdline = _disclosed_identity_field(process.cmdline)
        return _ProcessIdentity(
            stamp=_disclosed_identity_field(lambda: _process_creation_stamp(process)),
            cmdline=None if cmdline is None else tuple(str(value) for value in cmdline),
            uid=_process_real_uid(process),
            environment=_disclosed_process_environment(process),
            wall_create_time=_disclosed_identity_field(process.create_time),
        )
    except psutil.NoSuchProcess:
        # The process exited between the reads. That is "gone", which retires the
        # record, not "undisclosed", which would fail the launch for nothing.
        return None


def _disclosed_identity_field(read: Callable[[], _IdentityFieldT]) -> _IdentityFieldT | None:
    """Read one identity field, or ``None`` when the OS will not disclose it.

    ``NoSuchProcess`` deliberately propagates: a pid that disappears mid-read is a
    different verdict from a field the OS refuses to hand over.
    """

    try:
        return read()
    except psutil.NoSuchProcess:
        raise
    except psutil.Error:
        return None


def _process_real_uid(process: psutil.Process) -> int | None:
    """The real uid, or ``None`` when the platform or the OS will not disclose it.

    ``psutil`` declares ``uids`` on every platform but delegates it to a platform
    object that only implements it on POSIX, so on Windows the *call* raises
    ``AttributeError``. ``os.getuid`` is missing there too, so the caller applies
    no uid check rather than failing closed.
    """

    try:
        uids = _disclosed_identity_field(process.uids)
    except AttributeError:
        return None
    return None if uids is None else int(uids.real)


def _recorded_sidecar_pid(record: object) -> int | None:
    """Extract a plausible pid from a persisted record, rejecting anything else."""

    if not isinstance(record, dict):
        return None
    pid = record.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
        return None
    return pid


def _recorded_child_role(record: object) -> str | None:
    if not isinstance(record, dict):
        return None
    role = record.get("role")
    if role is None:
        # Records written before role-aware ownership always describe a sidecar.
        return _SIDECAR_ROLE
    return (
        role
        if role in {_SIDECAR_ROLE, _RELEASED_REBUILD_ROLE, _RELEASED_SYNC_ROLE}
        else None
    )


def _recorded_child_python(record: object) -> Path | None:
    if not isinstance(record, dict):
        return None
    value = record.get("python")
    if isinstance(value, str) and value:
        return Path(value)
    argv = record.get("argv")
    if (
        record.get("role") == _RELEASED_SYNC_ROLE
        and isinstance(argv, list)
        and len(argv) == len(_SYNC_ARGV) + 1
        and isinstance(argv[0], str)
        and tuple(argv[1:]) == _SYNC_ARGV
    ):
        return Path(argv[0])
    return None


def _record_for_this_installation(
    record: object,
    *,
    socket_path: Path,
    provider_root: Path,
) -> Mapping[str, Any] | None:
    """The record, but only when it was written for this home's runtime.

    A record naming a different socket or provider root describes another
    installation's sidecar, whose processes this launch may neither signal nor
    reason about.
    """

    if not isinstance(record, dict):
        return None
    if (
        not _paths_match(record.get("socket_path"), socket_path)
        or not _provider_roots_match(record.get("provider_root"), provider_root)
    ):
        return None
    return record


def _recorded_sidecar_create_time(
    record: object,
    *,
    socket_path: Path,
    provider_root: Path,
) -> float | None:
    """The identity stamp a record can be matched against, or ``None``.

    New Linux records persist ``starttime_ticks`` and that stamp is preferred.
    Legacy records keep wall-clock ``create_time``. A malformed stamp can never
    be matched by any process, so it yields nothing this launch may act on.
    """

    matched = _record_for_this_installation(record, socket_path=socket_path, provider_root=provider_root)
    if matched is None:
        return None
    if "starttime_ticks" in matched:
        ticks = matched.get("starttime_ticks")
        return float(ticks) if _is_identity_stamp(ticks) else None
    created_at = matched.get("create_time")
    if not _is_identity_stamp(created_at):
        return None
    return float(created_at)


def _recorded_sidecar_has_starttime_ticks(
    record: object,
    *,
    socket_path: Path,
    provider_root: Path,
) -> bool:
    matched = _record_for_this_installation(record, socket_path=socket_path, provider_root=provider_root)
    return matched is not None and _is_identity_stamp(matched.get("starttime_ticks"))


def _recorded_sidecar_group(
    record: object,
    *,
    socket_path: Path,
    provider_root: Path,
) -> int | None:
    """The isolated process group a record names, or ``None``.

    ``None`` covers a record written before this field existed and a launch whose
    child never got a group of its own. Neither leaves anything a later boot may
    scan, which is exactly what the previous build did with every record.
    """

    matched = _record_for_this_installation(record, socket_path=socket_path, provider_root=provider_root)
    if matched is None:
        return None
    group = matched.get("process_group")
    if not isinstance(group, int) or isinstance(group, bool) or group <= 1:
        return None
    return group


def _recorded_group_members(
    process_group: int,
    *,
    socket_path: Path,
    provider_root: Path,
    role: str | None = None,
) -> tuple[dict[int, psutil.Process | None], list[int]]:
    """Split a recorded group's live members into ours and ones to leave alone.

    Returns retained public Process references plus the pids that could not
    be tied to this installation, so the caller can log what it deliberately spared.
    """

    claimed: dict[int, psutil.Process | None] = {}
    foreign: list[int] = []
    own_pid = os.getpid()
    for pid, reference in _snapshot_process_group(process_group).items():
        if pid == own_pid or _reference_state(reference, pid) is False:
            continue
        if _reference_state(reference, pid) is True and _process_names_owned_runtime(
            pid,
            socket_path=socket_path,
            provider_root=provider_root,
            role=role,
        ) and _reference_state(reference, pid) is True:
            claimed[pid] = reference
        elif _reference_state(reference, pid) is not False:
            # Either unreadable or not attributable; never signal it.
            foreign.append(pid)
    return claimed, sorted(foreign)


def _process_names_owned_runtime(
    pid: int,
    *,
    socket_path: Path,
    provider_root: Path,
    role: str | None = None,
) -> bool:
    """Whether a live process ties itself to this installation's sidecar runtime.

    Needed where a recorded identity cannot decide ownership, because the process
    was spawned by the sidecar rather than by Avibe. Both facts checked here are
    produced only by our own launch: the socket path on the sidecar's command line,
    and the ``EVEROS_ROOT`` that ``_child_environment`` hands to every descendant.
    A field the OS withholds is never read as a match.
    """

    try:
        process = psutil.Process(pid)
        getuid = getattr(os, "getuid", None)
        own_uid = getuid() if callable(getuid) else None
        if own_uid is not None and _process_real_uid(process) != own_uid:
            return False
        cmdline = _disclosed_identity_field(process.cmdline)
        if role is None and cmdline is not None and (
            _cmdline_serves_socket(cmdline, socket_path)
            or str(provider_root) in cmdline
        ):
            return True
        environment = _disclosed_process_environment(process)
    except psutil.Error:
        # Includes the ``NoSuchProcess`` a field read re-raises: a process that is
        # gone needs no signal, and one that discloses nothing earns none.
        return False
    if environment is None or not _provider_roots_match(
        environment.get("EVEROS_ROOT"),
        provider_root,
    ):
        return False
    return role is None or environment.get("AVIBE_MEMORY_CHILD_ROLE") == role


def _disclosed_process_environment(process: psutil.Process) -> Mapping[str, str] | None:
    """The child environment, or ``None`` when the platform or OS withholds it.

    ``psutil`` exposes ``environ`` on the platforms Avibe supports but delegates it
    to a platform object, so an unsupported build raises ``AttributeError`` from the
    call itself rather than a ``psutil.Error``.
    """

    reader = getattr(process, "environ", None)
    if not callable(reader):
        return None
    try:
        return _disclosed_identity_field(reader)
    except AttributeError:
        return None


def _cmdline_serves_socket(cmdline: tuple[str, ...], socket_path: Path) -> bool:
    if not _SIDECAR_ENTRYPOINT_MODULES.intersection(cmdline) or "--uds" not in cmdline:
        return False
    index = cmdline.index("--uds")
    return index + 1 < len(cmdline) and _paths_match(cmdline[index + 1], socket_path)


def _cmdline_is_sidecar(cmdline: tuple[str, ...]) -> bool:
    return (
        len(cmdline) == 5
        and cmdline[1] == "-m"
        and cmdline[2] in _SIDECAR_ENTRYPOINT_MODULES
        and cmdline[3] == "--uds"
    )


def _cmdline_matches_role(
    cmdline: tuple[str, ...],
    *,
    role: str,
    socket_path: Path,
    python: Path | None = None,
) -> bool:
    if not cmdline:
        return False
    if python is not None and cmdline[0] != str(python):
        return False
    if role == _SIDECAR_ROLE:
        return (
            len(cmdline) == 5
            and cmdline[1] == "-m"
            and cmdline[2] in _SIDECAR_ENTRYPOINT_MODULES
            and cmdline[3] == "--uds"
            and _paths_match(cmdline[4], socket_path)
        )
    if role == _RELEASED_SYNC_ROLE:
        return cmdline[1:] == (
            "-I",
            "-m",
            "everos.entrypoints.cli.main",
            "cascade",
            "sync",
        )
    if role == _RELEASED_REBUILD_ROLE:
        return cmdline[1:] == (
            "-m",
            _RELEASED_REBUILD_ENTRYPOINT_MODULE,
            "cascade",
            "rebuild",
            "--yes",
        )
    return False


def _legacy_sidecar_record(record: object) -> bool:
    return isinstance(record, dict) and record.get("role") is None


def _recorded_child_python_missing(record: object) -> bool:
    return not _legacy_sidecar_record(record) and _recorded_child_python(record) is None


def _cmdline_matches_recorded_child(
    record: object,
    cmdline: tuple[str, ...] | None,
    *,
    socket_path: Path,
    role: str,
) -> bool | None:
    """Whether a disclosed cmdline matches the record.

    ``None`` means the command line was not disclosed.
    """

    if cmdline is None:
        return None
    if _legacy_sidecar_record(record):
        return _cmdline_serves_socket(cmdline, socket_path)
    recorded_python = _recorded_child_python(record)
    if recorded_python is None:
        return False
    return _cmdline_matches_role(
        cmdline,
        role=role,
        socket_path=socket_path,
        python=recorded_python,
    )


def _legacy_recorded_wall_create_time(identity: _ProcessIdentity) -> float | None:
    """Wall-clock create_time captured with the rest of the identity snapshot."""

    wall = identity.wall_create_time
    if wall is not None and not _is_identity_stamp(wall):
        return None
    return wall


def _legacy_create_time_mismatch_verdict(
    record: object,
    identity: _ProcessIdentity,
    *,
    socket_path: Path,
    provider_root: Path,
    role: str,
) -> _RecordedSidecar | None:
    """Resolve a legacy wall-clock ``create_time`` mismatch.

    Legacy wall-clock values can move by any amount after a clock correction,
    so their magnitude is not identity evidence. Returns ``None`` only when
    the exact command, uid, and provider root still prove this installation's
    child. A disclosed contradiction is ``NOT_OURS`` and a withheld deciding
    fact is ``UNVERIFIABLE``.
    """

    if _recorded_child_python_missing(record):
        return _RecordedSidecar.UNVERIFIABLE
    command_match = _cmdline_matches_recorded_child(
        record,
        identity.cmdline,
        socket_path=socket_path,
        role=role,
    )
    if command_match is None:
        return _RecordedSidecar.UNVERIFIABLE
    if not command_match:
        return _RecordedSidecar.NOT_OURS
    getuid = getattr(os, "getuid", None)
    own_uid = getuid() if callable(getuid) else None
    if own_uid is not None:
        if identity.uid is None:
            return _RecordedSidecar.UNVERIFIABLE
        if identity.uid != own_uid:
            return _RecordedSidecar.NOT_OURS
    if identity.environment is None:
        return _RecordedSidecar.UNVERIFIABLE
    if not _provider_roots_match(identity.environment.get("EVEROS_ROOT"), provider_root):
        return _RecordedSidecar.NOT_OURS
    return None


def _classify_recorded_child(
    record: object,
    identity: _ProcessIdentity | None,
    *,
    socket_path: Path,
    provider_root: Path,
    role: str,
) -> _RecordedSidecar:
    recorded_create_time = _recorded_sidecar_create_time(
        record,
        socket_path=socket_path,
        provider_root=provider_root,
    )
    if identity is None or recorded_create_time is None:
        return _RecordedSidecar.NOT_OURS
    has_starttime_ticks = _recorded_sidecar_has_starttime_ticks(
        record,
        socket_path=socket_path,
        provider_root=provider_root,
    )
    live_recorded_stamp = (
        identity.stamp if has_starttime_ticks else _legacy_recorded_wall_create_time(identity)
    )
    getuid = getattr(os, "getuid", None)
    own_uid = getuid() if callable(getuid) else None
    if identity.uid is not None and own_uid is not None and identity.uid != own_uid:
        return _RecordedSidecar.NOT_OURS
    if live_recorded_stamp is not None and not _is_identity_stamp(live_recorded_stamp):
        return _RecordedSidecar.UNVERIFIABLE
    if live_recorded_stamp is not None and live_recorded_stamp != recorded_create_time:
        if has_starttime_ticks:
            return _RecordedSidecar.NOT_OURS
        drift_verdict = _legacy_create_time_mismatch_verdict(
            record,
            identity,
            socket_path=socket_path,
            provider_root=provider_root,
            role=role,
        )
        if drift_verdict is not None:
            return drift_verdict
    if _recorded_child_python_missing(record):
        return _RecordedSidecar.UNVERIFIABLE
    command_match = _cmdline_matches_recorded_child(
        record,
        identity.cmdline,
        socket_path=socket_path,
        role=role,
    )
    if command_match is False:
        return _RecordedSidecar.NOT_OURS
    if not _legacy_sidecar_record(record) and identity.environment is not None:
        if (
            not _provider_roots_match(
                identity.environment.get("EVEROS_ROOT"),
                provider_root,
            )
            or identity.environment.get("AVIBE_MEMORY_CHILD_ROLE") != role
        ):
            return _RecordedSidecar.NOT_OURS
    if live_recorded_stamp is None or command_match is None:
        return _RecordedSidecar.UNVERIFIABLE
    if own_uid is not None and identity.uid is None:
        return _RecordedSidecar.UNVERIFIABLE
    if not _legacy_sidecar_record(record) and identity.environment is None:
        return _RecordedSidecar.UNVERIFIABLE
    return _RecordedSidecar.OURS


def _classify_recorded_sidecar(
    record: object,
    identity: _ProcessIdentity | None,
    *,
    socket_path: Path,
    provider_root: Path,
) -> _RecordedSidecar:
    """Decide what a recorded pid is, so the caller knows what it may do.

    ``OURS`` is the only verdict that permits a signal, and it still demands that
    the identity stamp, the real uid, and the exact ``-m`` entrypoint plus
    ``--uds`` argument all agree with the record. New records prefer
    ``starttime_ticks``; a legacy wall-clock ``create_time`` mismatch may still
    be ``OURS`` when cmdline, uid, and ``EVEROS_ROOT`` all match. Any single
    disclosed fact that contradicts the record
    settles the matter as ``NOT_OURS`` -- a recycled pid or another user's
    process is safe to stop worrying about. What must not be waved through is
    a live pid whose deciding facts were never disclosed: treating it as gone
    would start a replacement sidecar beside it.
    """

    return _classify_recorded_child(
        record,
        identity,
        socket_path=socket_path,
        provider_root=provider_root,
        role=_SIDECAR_ROLE,
    )


def _read_sidecar_record(
    path: Path,
    *,
    max_bytes: int = _SIDECAR_RECORD_MAX_BYTES,
) -> object | None:
    try:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            return None
        if info.st_size > max_bytes:
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None


def _sidecar_record_exists(path: Path) -> bool:
    """Whether a record file is there at all, usable or not.

    ``_read_sidecar_record`` answers ``None`` for a missing record and for one it
    cannot parse, and the caller must tell those apart: only the first means no
    previous run ever recorded ownership.
    """

    try:
        path.lstat()
    except OSError:
        return False
    return True


def _remove_legacy_sync_record(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise RuntimeError("released sync ownership record cannot be inspected") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise RuntimeError("released sync ownership record is unsafe")
    try:
        path.unlink()
    except OSError as exc:
        raise RuntimeError("released sync ownership record cannot be retired") from exc


def _processes_serving_owned_socket(*, socket_path: Path) -> dict[int, psutil.Process | None]:
    """Live processes running this home's sidecar entrypoint against its socket.

    The anchor for a recovery that has no usable record to work from, so unlike
    ``_process_names_owned_runtime`` this test is applied to every process on the
    machine and has to hold there: our own uid, our exact ``-m`` entrypoint, and
    our ``--uds`` argument. A process that merely mentions the provider root -- a
    shell command, an editor, a backup job -- must never be mistaken for a sidecar
    and killed.

    Inherited-environment matching is deliberately not used as an anchor either:
    the short-lived processing probe carries the same ``EVEROS_ROOT``, and helpers
    are reached from the anchor's own process group instead, where membership is
    what makes the looser per-member claim safe.
    """

    claimed: dict[int, psutil.Process | None] = {}
    own_pid = os.getpid()
    getuid = getattr(os, "getuid", None)
    own_uid = getuid() if callable(getuid) else None
    for observed in psutil.process_iter():
        # process_iter may return a cached generation. Capture afresh for this
        # discovery, then bracket classification without adopting old references.
        reference = _capture_process(observed.pid)
        if reference is not None and _reference_state(reference, observed.pid) is False:
            continue
        candidate = reference if reference is not None else observed
        if candidate.pid == own_pid:
            continue
        try:
            if own_uid is not None and _process_real_uid(candidate) != own_uid:
                continue
            cmdline = _disclosed_identity_field(candidate.cmdline)
            if cmdline is None or not _cmdline_serves_socket(tuple(str(value) for value in cmdline), socket_path):
                continue
        except psutil.Error:
            continue
        state = _reference_state(reference, candidate.pid)
        if state is False:
            continue
        if state is None and reference is not None:
            raise RuntimeError("sidecar unreadable during ownership classification")
        claimed[candidate.pid] = reference
    return claimed


def _processes_serving_owned_root(*, provider_root: Path) -> dict[int, psutil.Process | None]:
    """Live exact sidecar entrypoints owned by this uid and provider root."""

    claimed: dict[int, psutil.Process | None] = {}
    own_pid = os.getpid()
    getuid = getattr(os, "getuid", None)
    own_uid = getuid() if callable(getuid) else None
    for observed in psutil.process_iter():
        # process_iter may return a cached generation. Capture afresh for this
        # discovery, then bracket classification without adopting old references.
        reference = _capture_process(observed.pid)
        if reference is not None and _reference_state(reference, observed.pid) is False:
            continue
        candidate = reference if reference is not None else observed
        if candidate.pid == own_pid:
            continue
        try:
            uid = _process_real_uid(candidate)
            if own_uid is not None and uid != own_uid:
                continue
            cmdline = _disclosed_identity_field(candidate.cmdline)
            if cmdline is None:
                continue
            rendered = tuple(str(value) for value in cmdline)
            if not _cmdline_is_sidecar(rendered):
                continue
            environment = _disclosed_process_environment(candidate)
        except psutil.NoSuchProcess:
            continue
        except psutil.Error:
            continue
        if environment is None:
            raise RuntimeError(
                f"sidecar identity could not be verified (pid {candidate.pid})"
            )
        if not _provider_roots_match(
            environment.get("EVEROS_ROOT"),
            provider_root,
        ):
            continue
        role = environment.get("AVIBE_MEMORY_CHILD_ROLE")
        if role not in (None, _SIDECAR_ROLE):
            continue
        state = _reference_state(reference, candidate.pid)
        if state is False:
            continue
        if state is None and reference is not None:
            raise RuntimeError("sidecar unreadable during ownership classification")
        claimed[candidate.pid] = reference
    return claimed


def _processes_syncing_owned_root(
    *,
    provider_root: Path,
    python: Path,
    nonce: str,
) -> dict[int, psutil.Process | None]:
    """Discover one exact released nonce-bearing sync child."""

    claimed: dict[int, psutil.Process | None] = {}
    own_pid = os.getpid()
    getuid = getattr(os, "getuid", None)
    own_uid = getuid() if callable(getuid) else None
    for observed in psutil.process_iter():
        # process_iter may return a cached generation. Capture afresh for this
        # discovery, then bracket classification without adopting old references.
        reference = _capture_process(observed.pid)
        if reference is not None and _reference_state(reference, observed.pid) is False:
            continue
        candidate = reference if reference is not None else observed
        if candidate.pid == own_pid:
            continue
        try:
            uid = _process_real_uid(candidate)
            if own_uid is not None and uid is not None and uid != own_uid:
                continue
            cmdline = _disclosed_identity_field(candidate.cmdline)
        except psutil.NoSuchProcess:
            continue
        except psutil.Error as exc:
            raise RuntimeError(
                f"sync child identity could not be verified (pid {candidate.pid})"
            ) from exc
        if cmdline is None:
            raise RuntimeError(
                f"sync child command line could not be verified (pid {candidate.pid})"
            )
        rendered = tuple(str(value) for value in cmdline)
        if not _cmdline_matches_role(
            rendered,
            role=_RELEASED_SYNC_ROLE,
            socket_path=Path(),
            python=python,
        ):
            continue
        try:
            created_at = _disclosed_identity_field(
                lambda: _process_creation_stamp(candidate)
            )
            environment = _disclosed_process_environment(candidate)
        except psutil.NoSuchProcess:
            continue
        except psutil.Error as exc:
            raise RuntimeError(
                f"sync child identity could not be verified (pid {candidate.pid})"
            ) from exc
        if own_uid is not None and uid is None:
            raise RuntimeError(
                f"sync child uid could not be verified (pid {candidate.pid})"
            )
        if created_at is None or environment is None:
            raise RuntimeError(
                f"sync child identity could not be verified (pid {candidate.pid})"
            )
        if (
            not _provider_roots_match(
                environment.get("EVEROS_ROOT"),
                provider_root,
            )
            or environment.get("AVIBE_MEMORY_CHILD_ROLE")
            != _RELEASED_SYNC_ROLE
            or environment.get(_SYNC_NONCE_ENV) != nonce
        ):
            continue
        state = _reference_state(reference, candidate.pid)
        if state is False:
            continue
        if state is None and reference is not None:
            raise RuntimeError("sync child unreadable during ownership classification")
        claimed[candidate.pid] = reference
    return claimed


def _remove_sidecar_record(path: Path) -> None:
    try:
        info = path.lstat()
    except OSError:
        return
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        return
    try:
        path.unlink()
    except OSError:
        return


async def _wait_for_identities_exit(identities: Mapping[int, psutil.Process | None], timeout_seconds: float, process_group: int | None = None) -> bool:
    """Poll recorded identities until none is live or the bound expires."""

    deadline = time.monotonic() + max(timeout_seconds, 0.1)
    while time.monotonic() < deadline:
        if not _live_owned_processes(identities) and not _snapshot_process_group(process_group):
            return True
        await asyncio.sleep(0.05)
    return not _live_owned_processes(identities) and not _snapshot_process_group(process_group)


def _inspect_captured_identity(
    host: _ProcessHost,
    pid: int,
    reference: psutil.Process | None,
) -> _ProcessIdentity | None:
    state = _reference_state(reference, pid)
    if state is False:
        return None
    if state is None:
        raise RuntimeError("process unreadable before ownership classification")
    identity = host.inspect_identity(pid)
    state = _reference_state(reference, pid)
    if state is False:
        return None
    if state is None:
        raise RuntimeError("process unreadable during ownership classification")
    return identity


def _captured_process_group(
    host: _ProcessHost, pid: int, reference: psutil.Process | None, known_group: int | None,
) -> int | None:
    """Keep the established cleanup scope if its leader exits during lookup."""
    state = _reference_state(reference, pid)
    if state is False:
        return known_group
    if state is None:
        raise RuntimeError("process group is unreadable")
    group = host.process_group(pid)
    state = _reference_state(reference, pid)
    if state is False:
        return known_group
    if state is None:
        raise RuntimeError("process group is unreadable")
    return group


def _capture_process(pid: int) -> psutil.Process | None:
    try:
        # Retain the public reference even if later reads are temporarily denied.
        # State and signal checks decide authority at their existing boundaries.
        return psutil.Process(pid)
    except psutil.Error:
        return None


def _reference_state(reference: psutil.Process | None, pid: int) -> bool | None:
    """True is live, False is gone/reused, None is unresolved presence."""
    try:
        if reference is None:
            return None if psutil.pid_exists(pid) else False
        if not reference.is_running():
            return False
        return reference.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False
    except (psutil.Error, OSError):
        return None


def _snapshot_owned_processes(
    pid: int,
    process_group: int | None,
    owned: Mapping[int, psutil.Process | None] | None = None,
) -> dict[int, psutil.Process | None]:
    identities = dict(owned) if owned is not None else {pid: _capture_process(pid)}
    # Children are observed through retained parents, even after reparenting or
    # leaving the original group. Never refresh a captured PID into a new birth.
    for parent_pid, parent in list(identities.items()):
        if _reference_state(parent, parent_pid) is not True:
            continue
        try:
            children = parent.children(recursive=True)
            group = _snapshot_process_group(process_group) if parent_pid == pid else {}
            if _reference_state(parent, parent_pid) is not True:
                continue
            discovered = {child.pid: child for child in children}
            if parent_pid == pid and _isolated_process_group(pid) == process_group:
                discovered.update(group)
            _merge_owned_processes(identities, discovered)
        except (psutil.Error, OSError):
            # Keep captured members on read errors; listener inspection and the
            # independent group-clear check still fail closed on unknown presence.
            continue
    return identities


def _snapshot_process_group(process_group: int | None) -> dict[int, psutil.Process | None]:
    if process_group is None or os.name != "posix" or not hasattr(os, "getpgid"):
        return {}
    identities = {}
    for candidate in psutil.process_iter():
        try:
            if os.getpgid(candidate.pid) == process_group:
                reference = _capture_process(candidate.pid)
                if (
                    _reference_state(reference, candidate.pid) is not False
                    and os.getpgid(candidate.pid) == process_group
                ):
                    identities[candidate.pid] = reference
        except (PermissionError, psutil.AccessDenied):
            identities[candidate.pid] = None
        except (ProcessLookupError, psutil.NoSuchProcess):
            continue
    return identities


def _merge_owned_processes(
    identities: dict[int, psutil.Process | None],
    discovered: Mapping[int, psutil.Process | None],
) -> None:
    for pid, reference in discovered.items():
        if pid not in identities or identities[pid] is None:
            identities[pid] = reference


def _refresh_owned_process_tree(
    host: _ProcessHost,
    identities: dict[int, psutil.Process | None],
    process_id: int,
    process_group: int | None,
) -> None:
    discovered = host.snapshot_tree(process_id, process_group, identities)
    _merge_owned_processes(identities, discovered)


def _refresh_terminating_process_tree(
    host: _ProcessHost,
    identities: dict[int, psutil.Process | None],
    pid: int,
    process_group: int | None,
    *,
    socket_path: Path,
    provider_root: Path,
    role: str | None,
) -> None:
    _refresh_owned_process_tree(host, identities, pid, process_group)
    if process_group is not None and _reference_state(identities.get(pid), pid) is False:
        # A helper born after the last scan has no retained reference. Only the
        # existing ownership classifier can attribute it after its leader exits.
        claimed, _foreign = host.recorded_group_members(
            process_group, socket_path=socket_path, provider_root=provider_root, role=role,
        )
        for member_pid, reference in claimed.items():
            if member_pid != pid:
                identities.setdefault(member_pid, reference)
        # Foreign/unknown members remain visible to group signaling and wait proof.


def _live_owned_processes(identities: Mapping[int, psutil.Process | None]) -> dict[int, psutil.Process | None]:
    return {pid: ref for pid, ref in identities.items() if _reference_state(ref, pid) is not False}


def _confirmed_owned_processes(identities: Mapping[int, psutil.Process | None]) -> dict[int, psutil.Process]:
    return {pid: ref for pid, ref in identities.items() if ref is not None and _reference_state(ref, pid) is True}


def _group_contains_only_confirmed_owned_processes(
    process_group: int | None,
    identities: Mapping[int, psutil.Process | None],
) -> bool:
    if process_group is None or (hasattr(os, "getpgrp") and process_group == os.getpgrp()):
        return False
    members = _snapshot_process_group(process_group)
    confirmed = _confirmed_owned_processes(identities)
    return bool(members) and all(ref is not None and confirmed.get(pid) == ref for pid, ref in members.items())


def _signal_owned_group(
    process_group: int | None,
    identities: Mapping[int, psutil.Process | None],
    signum: int,
) -> bool:
    if not hasattr(os, "killpg") or not _group_contains_only_confirmed_owned_processes(process_group, identities):
        return False
    try:
        os.killpg(process_group, signum)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return True


def _signal_owned_processes(
    identities: Mapping[int, psutil.Process | None], signum: int,
    *, delivered_group: int | None = None,
) -> None:
    for reference in _confirmed_owned_processes(identities).values():
        try:
            if delivered_group is not None:
                if _reference_state(reference, reference.pid) is not True:
                    continue
                group = os.getpgid(reference.pid)
                if _reference_state(reference, reference.pid) is not True or group == delivered_group:
                    continue
            reference.send_signal(signum)
        except (psutil.Error, OSError):
            continue


async def _wait_for_owned_exit(
    host: _ProcessHost,
    process: asyncio.subprocess.Process,
    *,
    process_group: int | None,
    identities: dict[int, psutil.Process | None],
    timeout_seconds: float,
) -> bool:
    """Reap the direct child AND prove retained descendants and group are clear."""
    deadline = time.monotonic() + max(timeout_seconds, 0.1)
    waiter = asyncio.create_task(process.wait(), name="memory-everos-reap")
    try:
        while time.monotonic() < deadline:
            _refresh_owned_process_tree(host, identities, process.pid, process_group)
            if waiter.done() and not host.live(identities) and not _snapshot_process_group(process_group):
                await waiter
                return True
            await asyncio.sleep(0.05)
        return False
    finally:
        if waiter.done():
            try:
                waiter.result()
            except (asyncio.CancelledError, ProcessLookupError):
                pass
        else:
            waiter.cancel()


def _iana_timezone(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate or len(candidate.encode("utf-8")) > 128 or any(ord(char) < 32 for char in candidate):
        return None
    try:
        return ZoneInfo(candidate).key
    except ZoneInfoNotFoundError:
        return None


def _root_timezone(path: Path) -> str | None:
    try:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_size > 16 * 1024:
            return None
        data: Any = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    memory = data.get("memory")
    return _iana_timezone(memory.get("timezone")) if isinstance(memory, dict) else None


def _local_iana_timezone() -> str:
    candidates = [os.environ.get("TZ", "").lstrip(":"), getattr(datetime.now().astimezone().tzinfo, "key", "")]
    try:
        localtime = Path("/etc/localtime").resolve()
        marker = "zoneinfo/"
        rendered = str(localtime)
        if marker in rendered:
            candidates.append(rendered.split(marker, 1)[1])
    except OSError:
        pass
    for candidate in candidates:
        resolved = _iana_timezone(candidate)
        if resolved is not None:
            return resolved
    return "UTC"


def _validate_generated_config(
    everos_contents: str,
    ome_contents: str,
    timezone: str,
    settings: EverOSProcessSettings,
) -> None:
    try:
        everos = tomllib.loads(everos_contents)
        ome = tomllib.loads(ome_contents)
    except tomllib.TOMLDecodeError as exc:
        raise RuntimeError("invalid generated EverOS config") from exc
    rerank_settings = (
        settings.rerank_base_url,
        settings.rerank_model,
        settings.rerank_api_key,
    )
    if any(rerank_settings) and not all(rerank_settings):
        raise RuntimeError("Generated EverOS config received partial rerank settings")
    multimodal_settings = (
        settings.multimodal_base_url,
        settings.multimodal_model,
        settings.multimodal_api_key,
    )
    if any(multimodal_settings) and not all(multimodal_settings):
        raise RuntimeError("Generated EverOS config received partial multimodal settings")
    if (
        everos.get("memory", {}).get("timezone") != timezone
        or everos.get("memorize", {}).get("mode") != "chat"
        # EverOS 1.2.3 gives env settings precedence over TOML. Keep these
        # blank so configured rerank values have one child-process source.
        or everos.get("rerank", {}).get("model") != ""
        or everos.get("rerank", {}).get("base_url") != ""
        or "api_key" in everos.get("rerank", {})
        or everos.get("multimodal", {}).get("file_uri_max_bytes") != 26214400
        or "api_key" in everos.get("multimodal", {})
        or ome.get("strategies", {}).get("reflect_episodes", {}).get("enabled") is not False
        or ome.get("strategies", {}).get("extract_foresight", {}).get("enabled") is not False
    ):
        raise RuntimeError("invalid generated EverOS config")


def _ensure_owner_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("unsafe memory runtime directory")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise RuntimeError("memory runtime directory owner mismatch")
    os.chmod(path, _OWNER_DIR_MODE)


def _write_private_text(path: Path, contents: str) -> None:
    if path.parent.exists():
        parent = path.parent.lstat()
        if stat.S_ISLNK(parent.st_mode) or not stat.S_ISDIR(parent.st_mode):
            raise RuntimeError("unsafe generated config directory")
    temporary = path.with_name(f".{path.name}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _SOCKET_MODE)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(contents)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, _SOCKET_MODE)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=True)


def _positive_timeout(value: float, fallback: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed > 0 else fallback


class _SystemProcessHost:
    """Production adapter for the process capabilities the supervisor owns."""

    async def spawn(
        self,
        kind: _ProcessKind,
        python: Path,
        *,
        cwd: Path,
        env: Mapping[str, str],
        socket_path: Path | None = None,
        capture_stderr: bool = False,
    ) -> asyncio.subprocess.Process:
        arguments = [str(python), "-m", _SIDECAR_ENTRYPOINT_MODULE]
        if kind is _ProcessKind.SIDECAR:
            if socket_path is None:
                raise ValueError("sidecar launch requires a socket path")
            arguments.extend(("--uds", str(socket_path)))
        elif kind is _ProcessKind.PROCESSING_PROBE:
            arguments.append("--probe-processing")
        return await asyncio.create_subprocess_exec(
            *arguments,
            cwd=str(cwd),
            env=dict(env),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE if capture_stderr else asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )

    def process_group(self, pid: int) -> int | None:
        return _isolated_process_group(pid)

    def inspect_identity(self, pid: int) -> _ProcessIdentity | None:
        return _inspect_process_identity(pid)

    def capture(self, pid: int) -> psutil.Process | None:
        return _capture_process(pid)

    def snapshot_tree(self, pid: int, process_group: int | None, owned=None) -> dict[int, psutil.Process | None]:
        return _snapshot_owned_processes(pid, process_group, owned)

    def recorded_group_members(
        self,
        process_group: int,
        *,
        socket_path: Path,
        provider_root: Path,
        role: str | None = None,
    ) -> tuple[dict[int, psutil.Process | None], list[int]]:
        return _recorded_group_members(
            process_group,
            socket_path=socket_path,
            provider_root=provider_root,
            role=role,
        )

    def find_sidecars(self, *, socket_path: Path) -> dict[int, psutil.Process | None]:
        return _processes_serving_owned_socket(socket_path=socket_path)

    def find_sidecars_by_root(self, *, provider_root: Path) -> dict[int, psutil.Process | None]:
        return _processes_serving_owned_root(provider_root=provider_root)

    def find_syncs(
        self,
        *,
        provider_root: Path,
        python: Path,
        nonce: str,
    ) -> dict[int, psutil.Process | None]:
        return _processes_syncing_owned_root(
            provider_root=provider_root,
            python=python,
            nonce=nonce,
        )

    def live(self, identities: Mapping[int, psutil.Process | None]) -> dict[int, psutil.Process | None]:
        return _live_owned_processes(identities)

    def signal(
        self,
        identities: Mapping[int, psutil.Process | None],
        signum: int,
        *,
        process_group: int | None = None,
        process: asyncio.subprocess.Process | None = None,
    ) -> None:
        delivered = _signal_owned_group(process_group, identities, signum)
        # With stable membership these sets are disjoint. Group movement and
        # delivery are not atomic; retained-reference checks still fence reuse.
        _signal_owned_processes(
            identities, signum, delivered_group=process_group if delivered else None,
        )

    async def wait_for_exit(
        self,
        identities: dict[int, psutil.Process | None],
        timeout_seconds: float,
        *,
        process_group: int | None = None,
        process: asyncio.subprocess.Process | None = None,
    ) -> bool:
        if process is None:
            return await _wait_for_identities_exit(identities, timeout_seconds, process_group)
        return await _wait_for_owned_exit(
            self, process,
            process_group=process_group,
            identities=identities,
            timeout_seconds=timeout_seconds,
        )

    def has_tcp_listener(self, identities: Mapping[int, psutil.Process | None]) -> bool:
        for pid, reference in identities.items():
            state = _reference_state(reference, pid)
            if state is False:
                continue
            if state is None:
                raise RuntimeError("could not inspect sidecar listeners")
            try:
                connections = reference.net_connections(kind="inet")
                if _reference_state(reference, pid) is False:
                    continue
            except (psutil.NoSuchProcess, psutil.ZombieProcess):
                continue
            except psutil.Error as exc:
                raise RuntimeError("could not inspect sidecar listeners") from exc
            if any(connection.status == psutil.CONN_LISTEN for connection in connections):
                return True
        return False


@runtime_checkable
class EverOSProcessPort(Protocol):
    """The narrow child seam owned exclusively by ``EverOSSupervisor``.

    Deliberately five members over the concrete process adapter: callers never
    inspect the child tree, generated config, or signal handling.
    Keeping those out of this interface is what lets tests substitute a fake
    instead of patching ``psutil``, ``os``, and private attributes.
    """

    @property
    def running(self) -> bool: ...

    @property
    def retains_active_config(self) -> bool: ...

    async def start(self) -> bool: ...

    async def stop(self) -> None: ...

    async def processing_healthy(self) -> bool: ...


class EverOSProcessFactory(Protocol):
    """Construct one private child for the supervisor.

    A factory rather than an instance keeps process test injection below the
    capability boundary. Mirrors ``EverOSProcess.__init__``.
    """

    def __call__(
        self,
        python: Path | str,
        *,
        provider_root: Path | str | None = None,
        effective_home: Path | str | None = None,
        settings: EverOSProcessSettings | None = None,
        socket_path: Path | str | None = None,
        provider_root_guard: Callable[[], None] | None = None,
        on_ready: Callable[[], Awaitable[None] | None] | None = None,
        before_start: Callable[[], Awaitable[None] | None] | None = None,
        on_unexpected_exit: Callable[[], Awaitable[None] | None] | None = None,
    ) -> EverOSProcessPort: ...


@dataclass
class FakeEverOSProcess:
    """In-process child fake for supervisor contract tests.

    Mirrors the real supervisor's observable contract: a successful ``start``
    fires ``on_ready``, exactly as ``EverOSProcess`` does once its child answers
    ``/health``. Tests drive outcomes through ``start_results`` /
    ``processing_healthy_results`` instead of patching ``psutil`` and ``os``.
    """

    start_results: Deque[bool] = field(default_factory=deque)
    processing_healthy_results: Deque[bool] = field(default_factory=deque)
    start_failure: BaseException | None = None
    stop_failure: BaseException | None = None
    processing_healthy_flag: bool = True
    on_ready: Callable[[], Awaitable[None] | None] | None = None
    before_start: Callable[[], Awaitable[None] | None] | None = None
    on_unexpected_exit: Callable[[], Awaitable[None] | None] | None = None
    # Launch inputs the factory captured, for tests asserting on child settings.
    python: Path | None = None
    provider_root: Path | None = None
    provider_root_guard: Callable[[], None] | None = None
    settings: EverOSProcessSettings | None = None
    starts: int = 0
    stops: int = 0
    stopped: bool = False
    _running: bool = False
    _process_tree_retained: bool = False

    @property
    def running(self) -> bool:
        return self._running

    @property
    def retains_active_config(self) -> bool:
        return self._running or self._process_tree_retained

    async def start(self) -> bool:
        self.starts += 1
        self._process_tree_retained = False
        before_start = self.before_start
        if before_start is not None:
            result = before_start()
            if inspect.isawaitable(result):
                await result
        if self.start_failure is not None:
            self._running = False
            raise self.start_failure
        started = self.start_results.popleft() if self.start_results else True
        self._running = started
        if started:
            await self.ready()
        return started

    async def stop(self) -> None:
        self.stops += 1
        self.stopped = True
        owned_execution = self._running or self._process_tree_retained
        self._running = False
        if self.stop_failure is not None:
            self._process_tree_retained = owned_execution
            raise self.stop_failure
        self._process_tree_retained = False

    async def unexpected_exit(self) -> None:
        """Simulate a reaped child that the supervisor did not stop."""

        self._running = False
        callback = self.on_unexpected_exit
        if callback is not None:
            result = callback()
            if inspect.isawaitable(result):
                await result

    async def processing_healthy(self) -> bool:
        if self.processing_healthy_results:
            return self.processing_healthy_results.popleft()
        return self.processing_healthy_flag

    async def ready(self) -> None:
        """Fire the runtime's readiness callback as a recovered child would."""

        if self.on_ready is None:
            return
        result = self.on_ready()
        if inspect.isawaitable(result):
            await result


@dataclass
class FakeEverOSProcessFactory:
    """Hand out ``FakeEverOSProcess`` instances and remember every one.

    Satisfies ``EverOSProcessFactory``. ``supervised`` holds only the sidecars the
    supervisor owns; a process built without ``on_ready`` is the
    short-lived enablement probe, not a managed child.
    """

    template: Callable[[], FakeEverOSProcess] = FakeEverOSProcess
    #: Every process handed out, probes included.
    created: list[FakeEverOSProcess] = field(default_factory=list)
    #: Only the supervised sidecars, in creation order. A live list, so a test may
    #: bind it once and watch it grow across reconciliations.
    supervised: list[FakeEverOSProcess] = field(default_factory=list)

    def __call__(
        self,
        python: Path | str,
        *,
        provider_root: Path | str | None = None,
        effective_home: Path | str | None = None,
        settings: EverOSProcessSettings | None = None,
        socket_path: Path | str | None = None,
        provider_root_guard: Callable[[], None] | None = None,
        on_ready: Callable[[], Awaitable[None] | None] | None = None,
        before_start: Callable[[], Awaitable[None] | None] | None = None,
        on_unexpected_exit: Callable[[], Awaitable[None] | None] | None = None,
    ) -> EverOSProcessPort:
        del effective_home, socket_path
        process = self.template()
        process.on_ready = on_ready
        process.before_start = before_start
        process.on_unexpected_exit = on_unexpected_exit
        process.python = Path(python)
        process.provider_root = Path(provider_root) if provider_root is not None else None
        process.provider_root_guard = provider_root_guard
        process.settings = settings
        self.created.append(process)
        if on_ready is not None:
            self.supervised.append(process)
        return process

    @property
    def last(self) -> FakeEverOSProcess | None:
        return self.created[-1] if self.created else None
