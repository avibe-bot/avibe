"""Owned lifecycle for private EverOS sidecar and one-shot children."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
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
from core.memory.attachments import attachment_pin_root
from core.memory.confined_filesystem import (
    ConfinedFilesystemError,
    create_confined_file,
    ensure_private_directory,
    open_confined_directory,
    open_confined_regular_file,
    remove_anchored_entry,
    required_no_follow_flag,
)
from core.memory.everos import EverOSPort
from core.memory.secret_scrubber import scrub_text
from core.memory.types import MemoryErrorCode


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
_HEALTHY_RESET_SECONDS = 5 * 60.0
_RESTART_DELAYS_SECONDS = (1.0, 5.0, 30.0, 120.0)
_MAX_CONSECUTIVE_FAILURES = 5
_PROCESSING_PROBE_TIMEOUT_SECONDS = 20.0
_PROCESSING_PROBE_STDERR_BYTES = 2048
_SOCKET_MODE = 0o600
_OWNER_DIR_MODE = 0o700
_SAFETY_MONITOR_INTERVAL_SECONDS = 0.2
_TREE_INSPECTION_INTERVAL_SECONDS = 1.0
_HEALTH_OBSERVATION_INTERVAL_SECONDS = 5.0
_SIDECAR_RECORD_FILENAME = "everos.sidecar.json"
_SIDECAR_RECORD_MAX_BYTES = 4 * 1024
_SIDECAR_ENTRYPOINT_MODULE = "core.memory.sidecar"
_REBUILD_ENTRYPOINT_MODULE = "core.memory.rebuild_child"
_REBUILD_LOCK_PREFIX = "cascade-rebuild-"
_REBUILD_LOCK_DIRECTORY = ".avibe-memory-locks"
_PROVIDER_LOCK_RETRY_INTERVAL_SECONDS = 0.05
_REBUILD_HANDSHAKE_TIMEOUT_SECONDS = 30.0
_REBUILD_TIMEOUT_SECONDS = 30 * 60.0
_PLANNED_REAP_TOKEN_TTL_SECONDS = _REBUILD_TIMEOUT_SECONDS

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
    multimodal_base_url: str | None = None
    multimodal_model: str | None = None
    multimodal_api_key: str | None = field(default=None, repr=False)
    timezone: str | None = None
    call_log_db_path: Path | None = None


class _ProcessKind(Enum):
    SIDECAR = "sidecar"
    PROCESSING_PROBE = "processing_probe"
    CASCADE_REBUILD = "cascade_rebuild"
    CASCADE_SYNC = "cascade_sync"


class _MemoryChildRole(Enum):
    SIDECAR = "sidecar"
    CASCADE_REBUILD = "cascade_rebuild"
    CASCADE_SYNC = "cascade_sync"


class RebuildProcessResult(str, Enum):
    """Closed outcomes from one owned EverOS cascade rebuild child."""

    COMPLETED = "completed"
    ROOT_BUSY = "root_busy"
    INTERRUPTED = "interrupted"
    TIMED_OUT = "timed_out"
    FAILED = "failed"


class _ProviderRootBusy(RuntimeError):
    """Another process owns the provider root's rebuild lifecycle."""


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


class _PlannedReapTokens:
    """One-shot provider-root handoffs backed by a live issuer lease."""

    def __init__(self, provider_root: Path) -> None:
        self._provider_root = provider_root

    def _layout(self) -> tuple[Path, Path, str]:
        """Resolve the secure namespace only after its provider parent exists."""

        lock_path = _provider_rebuild_lock_path(provider_root=self._provider_root)
        return (
            lock_path.parent.parent,
            lock_path.parent,
            f"{lock_path.name}.handoff-",
        )

    def record(self, pid: int, created_at: float) -> _PlannedReapLease:
        confinement_root, directory, prefix = self._layout()
        path = self._path(directory, prefix, pid, created_at)
        ensure_private_directory(
            confinement_root,
            directory,
            harden_confinement_root=False,
        )
        created = False
        created = False
        try:
            descriptor = create_confined_file(
                confinement_root,
                path,
            )
            created = True
        except ConfinedFilesystemError:
            # Rediscovery can name the same exact sidecar twice. A secure token
            # is idempotent; a symlink, special file, or loose mode still fails.
            descriptor = self._open_writable(confinement_root, directory, path)
        try:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            info = os.fstat(descriptor)
            os.ftruncate(descriptor, 0)
            os.write(descriptor, b"pending\n")
            os.fsync(descriptor)
        except BaseException:
            info = os.fstat(descriptor)
            os.close(descriptor)
            if created:
                self._remove(path, (info.st_dev, info.st_ino))
            raise
        return _PlannedReapLease(
            tokens=self,
            descriptor=descriptor,
            path=path,
            identity=(info.st_dev, info.st_ino),
        )

    @staticmethod
    def _open_writable(confinement_root: Path, directory: Path, path: Path) -> int:
        parent = open_confined_directory(confinement_root, directory)
        try:
            descriptor = os.open(
                path.name,
                os.O_RDWR | required_no_follow_flag() | int(getattr(os, "O_CLOEXEC", 0)),
                dir_fd=parent,
            )
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or stat.S_IMODE(info.st_mode) != 0o600
                or (hasattr(os, "getuid") and info.st_uid != os.getuid())
            ):
                os.close(descriptor)
                raise ConfinedFilesystemError("planned-reap token is unsafe")
            return descriptor
        except OSError as error:
            raise ConfinedFilesystemError("planned-reap token cannot be opened safely") from error
        finally:
            os.close(parent)

    def consume(self, pid: int, created_at: float) -> bool:
        try:
            confinement_root, directory, prefix = self._layout()
            path = self._path(directory, prefix, pid, created_at)
            descriptor = open_confined_regular_file(
                confinement_root,
                path,
            )
        except (ConfinedFilesystemError, OSError):
            return False
        info = os.fstat(descriptor)
        expected_identity = (info.st_dev, info.st_ino)
        active_issuer = False
        acquired = False
        import fcntl

        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                active_issuer = True
            else:
                acquired = True
            os.lseek(descriptor, 0, os.SEEK_SET)
            state = os.read(descriptor, 256).decode("ascii", errors="ignore").strip()
            removed = self._remove(path, expected_identity)
        finally:
            if acquired:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
        committed = False
        if state.startswith("committed:") and not active_issuer:
            try:
                committed = float(state.split(":", 1)[1]) >= time.time()
            except (ValueError, IndexError):
                committed = False
        return removed and (active_issuer or committed)

    def _remove(self, path: Path, expected_identity: tuple[int, int]) -> bool:
        confinement_root, directory, _prefix = self._layout()
        parent: int | None = None
        try:
            parent = open_confined_directory(
                confinement_root,
                directory,
            )
            remove_anchored_entry(
                parent,
                path.name,
                expected_identity=expected_identity,
            )
        except ConfinedFilesystemError:
            return False
        finally:
            if parent is not None:
                os.close(parent)
        return True

    @staticmethod
    def _path(
        directory: Path,
        prefix: str,
        pid: int,
        created_at: float,
    ) -> Path:
        generation = hashlib.sha256(
            f"{pid}:{float(created_at).hex()}".encode("ascii")
        ).hexdigest()
        return directory / f"{prefix}{generation}"


class _PlannedReapLease:
    """Descriptor lease proving a planned-reap issuer is still active."""

    def __init__(
        self,
        *,
        tokens: _PlannedReapTokens,
        descriptor: int,
        path: Path,
        identity: tuple[int, int],
    ) -> None:
        self._tokens = tokens
        self._descriptor = descriptor
        self._path = path
        self._identity = identity
        self._committed = False

    @property
    def committed(self) -> bool:
        return self._committed

    def commit(self) -> None:
        """Authenticate that the planned target was actually signalled."""

        descriptor = self._descriptor
        if descriptor is None:
            raise RuntimeError("planned-reap issuer lease is no longer held")
        payload = f"committed:{time.time() + _PLANNED_REAP_TOKEN_TTL_SECONDS:.6f}\n".encode(
            "ascii"
        )
        os.lseek(descriptor, 0, os.SEEK_SET)
        os.ftruncate(descriptor, 0)
        os.write(descriptor, payload)
        os.fsync(descriptor)
        self._committed = True

    def release(self, *, remove_token: bool = True) -> bool:
        descriptor = self._descriptor
        if descriptor is None:
            return False
        self._descriptor = None
        removed = False
        try:
            if remove_token:
                removed = self._tokens._remove(self._path, self._identity)
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
        return removed


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

    def snapshot_tree(self, pid: int, process_group: int | None) -> dict[int, float]: ...

    def recorded_group_members(
        self,
        process_group: int,
        *,
        socket_path: Path,
        provider_root: Path,
        role: _MemoryChildRole | None = None,
    ) -> tuple[dict[int, float], list[int]]: ...

    def find_sidecars(self, *, socket_path: Path) -> dict[int, float]: ...

    def find_sidecars_by_root(self, *, provider_root: Path) -> dict[int, float]: ...

    def find_rebuilds(
        self,
        *,
        provider_root: Path,
        python: Path | None,
    ) -> dict[int, float]: ...

    def live(self, identities: Mapping[int, float]) -> dict[int, float]: ...

    async def wait_for_stopped(self, pid: int, timeout_seconds: float) -> bool: ...

    def signal(
        self,
        identities: Mapping[int, float],
        signum: int,
        *,
        process_group: int | None = None,
        process: asyncio.subprocess.Process | None = None,
    ) -> None: ...

    async def wait_for_exit(
        self,
        identities: dict[int, float],
        timeout_seconds: float,
        *,
        process_group: int | None = None,
        process: asyncio.subprocess.Process | None = None,
    ) -> bool: ...

    def has_tcp_listener(self, identities: Mapping[int, float]) -> bool: ...


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
        startup_timeout_seconds: float = _STARTUP_TIMEOUT_SECONDS,
        stop_timeout_seconds: float = _STOP_TIMEOUT_SECONDS,
        on_ready: Callable[[], Awaitable[None] | None] | None = None,
        before_start: Callable[[], Awaitable[None] | None] | None = None,
        on_reaped: Callable[[], Awaitable[None] | None] | None = None,
        _host: _ProcessHost | None = None,
    ) -> None:
        self._python = Path(python)
        effective_home_path = (
            Path(effective_home) if effective_home is not None else paths.get_vibe_remote_dir()
        )
        self._effective_home = Path(os.path.abspath(os.fspath(effective_home_path)))
        self._memory_dir = self._effective_home / "memory"
        self._attachments_root = attachment_pin_root(self._effective_home)
        provider_root_path = (
            Path(provider_root) if provider_root is not None else self._memory_dir / "everos-root"
        )
        self._provider_root = Path(os.path.abspath(os.fspath(provider_root_path)))
        self._socket_path = Path(socket_path) if socket_path is not None else self._memory_dir / ".rt" / "everos.sock"
        self._settings = settings or EverOSProcessSettings()
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
        self._restart_task: asyncio.Task[None] | None = None
        self._retained_provider_root_lock: _ProviderRootLock | None = None
        self._owned_processes: dict[int, float] = {}
        self._on_ready = on_ready
        self._before_start = before_start
        self._on_reaped = on_reaped
        self._desired_running = False
        self._starting = False
        self._down = False
        self._consecutive_failures = 0
        self._started_at: float | None = None
        self._healthy_since: float | None = None
        self._last_error: MemoryErrorCode | None = None

    @property
    def socket_path(self) -> Path:
        return self._socket_path


    @property
    def provider_root(self) -> Path:
        return self._provider_root

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    @property
    def starting(self) -> bool:
        return self._starting

    @property
    def down(self) -> bool:
        return self._down

    @property
    def last_error(self) -> MemoryErrorCode | None:
        return self._last_error

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    def set_settings(self, settings: EverOSProcessSettings) -> None:
        """Replace launch settings before an explicit reconciliation/start."""

        self._settings = settings

    async def start(self) -> bool:
        """Request an owned sidecar; failed starts enter bounded supervision."""

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
                self._last_error = "memory_sidecar_unavailable"
                return False
            if self._down:
                # A caller can explicitly retry a down sidecar, but that must
                # not erase the crash budget. Only observed health earns that
                # reset, otherwise repeated settings saves could restart forever.
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
            restart_task = self._restart_task
            self._restart_task = None
            if restart_task is not None and restart_task is not asyncio.current_task():
                restart_task.cancel()
            process = self._process
            process_group = self._process_group
            owned_processes = dict(self._owned_processes)
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
            self._started_at = None
            self._healthy_since = None
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
            if process is not None:
                await self._notify_reaped()

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
                    env=self._child_environment(),
                    capture_stderr=True,
                )
            except TypeError:
                probe = await self._host.spawn(
                    _ProcessKind.PROCESSING_PROBE,
                    self._python,
                    cwd=self._effective_home,
                    env=self._child_environment(),
                )
        except (OSError, ValueError):
            logger.warning("EverOS processing probe could not start; branch=probe_spawn")
            return False

        process_group = self._host.process_group(probe.pid)
        owned_processes = self._host.snapshot_tree(probe.pid, process_group)
        stderr = getattr(probe, "stderr", None)
        stderr_task = asyncio.create_task(_drain_probe_stderr(stderr)) if stderr is not None else None
        try:
            await asyncio.wait_for(probe.wait(), timeout=_PROCESSING_PROBE_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            try:
                await self._terminate_owned_tree(
                    probe,
                    process_group=process_group,
                    owned_processes=owned_processes,
                )
            except Exception:
                logger.warning("EverOS processing probe cleanup failed")
            logger.warning(
                "EverOS processing probe timed out; stderr_tail=%s",
                await _probe_stderr_tail(stderr_task, settings=self._settings),
            )
            return False
        except asyncio.CancelledError:
            # ``MemoryWorker`` bounds this probe independently. Do not let that
            # timeout orphan an owned child with the credential environment.
            try:
                await self._terminate_owned_tree(
                    probe,
                    process_group=process_group,
                    owned_processes=owned_processes,
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
        self._last_error = None
        try:
            self._validate_launch_inputs()
            self._prepare_owned_directories()
            await self._ownership.reap(discover_missing=True)
            self._write_generated_config()
            self._remove_owned_socket()
            child_env = self._child_environment(role=_MemoryChildRole.SIDECAR)
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
            self._process_group = self._host.process_group(process.pid)
            self._owned_processes = self._host.snapshot_tree(process.pid, self._process_group)
            if not _host_identity_is_live(self._host, process.pid, self._owned_processes):
                raise RuntimeError("could not establish sidecar process ownership")
            self._ownership.record_launch(
                process.pid,
                self._owned_processes[process.pid],
                self._process_group,
            )
            if spawn_interrupted:
                raise asyncio.CancelledError
            self._started_at = time.monotonic()
            self._healthy_since = None
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
                            owned_processes=dict(self._owned_processes),
                        )
                    )
                except Exception:
                    # Keep both the child identity and provider-root lock so a
                    # rebuild cannot enter before Stop retries the cleanup.
                    self._down = True
                    self._last_error = "memory_sidecar_unavailable"
                    self._starting = False
                    raise
                self._ownership.retire_if_group_is_clear(process.pid, process_group)
            self._process = None
            self._process_group = None
            self._owned_processes = {}
            self._started_at = None
            self._healthy_since = None
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
            await _finish_cleanup_despite_cancellation(self._notify_reaped())
            raise
        except _ProviderRootBusy:
            self._starting = False
            self._last_error = "memory_provider_root_busy"
            if self._desired_running:
                self._restart_task = asyncio.create_task(
                    self._restart_after(_PROVIDER_LOCK_RETRY_INTERVAL_SECONDS),
                    name="memory-everos-root-busy-retry",
                )
            return False
        except Exception:
            # Every start failure collapses into `memory_sidecar_unavailable`, and
            # some of them are permanent: a recorded orphan that cannot be
            # identified fails each later attempt the same way. Log the cause
            # first, before any cleanup branch can raise or return.
            logger.exception("EverOS sidecar start failed")
            process = self._process
            process_group = self._process_group
            owned_processes = dict(self._owned_processes)
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
                self._last_error = "memory_sidecar_unavailable"
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
            self._started_at = None
            self._healthy_since = None
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
            await self._notify_reaped()
            self._record_start_failure_locked()
            return False
    async def _start_with_provider_lock(self) -> bool:
        """Serialize sidecar admission with every rebuild of this provider root."""

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
                    await self._notify_reaped()
                    self._record_start_failure_locked()
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
            if not _host_identity_is_live(self._host, process.pid, self._owned_processes):
                raise RuntimeError("sidecar ownership changed before readiness")
            _merge_owned_processes(
                self._owned_processes,
                self._host.snapshot_tree(process.pid, self._process_group),
            )
            if self._socket_path.exists():
                self._secure_socket()
                if await client.health():
                    self._record_health_observation(True)
                    return
            await asyncio.sleep(0.05)
        raise RuntimeError("sidecar readiness timed out")

    async def _watch_child(self, process: asyncio.subprocess.Process) -> None:
        await process.wait()
        async with self._lifecycle_lock:
            if process is not self._process:
                return
            healthy_since = self._healthy_since
            process_group = self._process_group
            owned_processes = dict(self._owned_processes)
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
                self._last_error = "memory_sidecar_unavailable"
                self._desired_running = False
                self._starting = False
                return
            self._process = None
            self._process_group = None
            self._owned_processes = {}
            self._started_at = None
            self._healthy_since = None
            self._monitor_task = None
            if monitor_task is not None and monitor_task is not asyncio.current_task():
                monitor_task.cancel()
            self._remove_owned_socket()
            self._ownership.retire_if_group_is_clear(process.pid, process_group)
            self._starting = False
            await self._notify_reaped()
            planned_reap = (
                process.pid in owned_processes
                and self._ownership.consume_planned_reap(
                    process.pid,
                    owned_processes[process.pid],
                )
            )
            if healthy_since is not None and time.monotonic() - healthy_since >= _HEALTHY_RESET_SECONDS:
                self._consecutive_failures = 0
            if not self._desired_running:
                return
            if planned_reap:
                # Provider-root reconciliation terminates a supervised sidecar
                # before taking over its root. That is an ownership handoff, not
                # a child crash: restart through the shared lock without spending
                # the bounded crash budget while the rebuild remains active.
                self._last_error = None
                self._restart_task = asyncio.create_task(
                    self._restart_after(0.0),
                    name="memory-everos-restart",
                )
                return
            self._record_start_failure_locked()

    async def _monitor_child(self, process: asyncio.subprocess.Process) -> None:
        """Keep tracking descendants and reject any later TCP listener."""

        try:
            client = EverOSPort(self._socket_path, sidecar_timeout_seconds=2.0)
            next_tree_inspection = time.monotonic()
            next_health_observation = time.monotonic()
            while process is self._process and process.returncode is None:
                observed_at = time.monotonic()
                if observed_at >= next_tree_inspection:
                    owned_processes = self._refresh_owned_processes(process.pid)
                    self._assert_no_tcp_listener(
                        process.pid,
                        owned_processes=owned_processes,
                    )
                    next_tree_inspection = observed_at + _TREE_INSPECTION_INTERVAL_SECONDS
                elif not _host_identity_is_live(self._host, process.pid, self._owned_processes):
                    raise RuntimeError("sidecar ownership changed during monitoring")
                if observed_at >= next_health_observation:
                    self._record_health_observation(await client.health(), observed_at=observed_at)
                    next_health_observation = observed_at + _HEALTH_OBSERVATION_INTERVAL_SECONDS
                await asyncio.sleep(_SAFETY_MONITOR_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            return
        except Exception:
            if not self._desired_running:
                return
            logger.warning("EverOS sidecar safety monitor rejected the child tree")
            async with self._lifecycle_lock:
                if process is not self._process:
                    return
                self._desired_running = False
                self._down = True
                self._last_error = "memory_sidecar_unavailable"
                process_group = self._process_group
                try:
                    await self._terminate_owned_tree(
                        process,
                        process_group=process_group,
                        owned_processes=dict(self._owned_processes),
                    )
                except Exception:
                    logger.warning("EverOS sidecar safety shutdown did not reap the child tree")
                    return
                self._process = None
                self._process_group = None
                self._owned_processes = {}
                self._started_at = None
                self._healthy_since = None
                self._monitor_task = None
                self._remove_owned_socket()
                self._ownership.retire_if_group_is_clear(process.pid, process_group)
                await self._notify_reaped()

    def _record_start_failure_locked(self) -> None:
        self._consecutive_failures += 1
        self._last_error = "memory_sidecar_unavailable"
        if self._consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
            self._down = True
            return
        self._down = False
        delay = _RESTART_DELAYS_SECONDS[min(self._consecutive_failures - 1, len(_RESTART_DELAYS_SECONDS) - 1)]
        self._restart_task = asyncio.create_task(self._restart_after(delay), name="memory-everos-restart")

    async def _restart_after(self, delay_seconds: float) -> None:
        try:
            await asyncio.sleep(delay_seconds)
            async with self._lifecycle_lock:
                if not self._desired_running or self.running or self._down:
                    return
            if not await self._await_before_start():
                return
            await self._start_with_provider_lock()
        except asyncio.CancelledError:
            return

    def _validate_launch_inputs(self) -> None:
        if os.name != "posix" or not self._python.is_file():
            raise RuntimeError("invalid sidecar launch")
        if len(os.fsencode(self._socket_path)) + 1 > _socket_path_limit():
            raise RuntimeError("socket path exceeds sun_path")
        if not _settings_complete(self._settings):
            raise RuntimeError("processing settings incomplete")

    def _prepare_owned_directories(self) -> None:
        _prepare_memory_child_directories(
            memory_dir=self._memory_dir,
            provider_root=self._provider_root,
            settings=self._settings,
        )

    def _provider_root_lock(self) -> _ProviderRootLock:
        lock_path = _provider_rebuild_lock_path(provider_root=self._provider_root)
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

    def _child_environment(self, *, role: _MemoryChildRole | None = None) -> dict[str, str]:
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

    def _refresh_owned_processes(self, pid: int) -> dict[int, float]:
        _merge_owned_processes(
            self._owned_processes,
            self._host.snapshot_tree(pid, self._process_group),
        )
        unverifiable = {
            process_id: created_at
            for process_id, created_at in self._owned_processes.items()
            if created_at < 0
        }
        self._owned_processes = self._host.live(self._owned_processes)
        # AccessDenied group members use a negative identity sentinel. Retain
        # those for fail-closed group cleanup even though ordinary dead PIDs are
        # pruned from the hot monitor set.
        _merge_owned_processes(self._owned_processes, unverifiable)
        if pid not in self._owned_processes:
            raise RuntimeError("sidecar ownership changed during monitoring")
        return dict(self._owned_processes)

    def _assert_no_tcp_listener(
        self,
        pid: int,
        *,
        owned_processes: Mapping[int, float] | None = None,
    ) -> None:
        live_processes = (
            dict(owned_processes)
            if owned_processes is not None
            else self._refresh_owned_processes(pid)
        )
        if pid not in live_processes:
            raise RuntimeError("sidecar ownership changed during listener inspection")
        if self._host.has_tcp_listener(live_processes):
            raise RuntimeError("sidecar opened a TCP listener")

    async def _terminate_owned_tree(
        self,
        process: asyncio.subprocess.Process,
        *,
        process_group: int | None,
        owned_processes: Mapping[int, float] | None = None,
    ) -> None:
        await _terminate_owned_process_tree(
            self._host,
            process,
            process_group=process_group,
            owned_processes=owned_processes,
            stop_timeout_seconds=self._stop_timeout_seconds,
        )

    def _record_health_observation(self, healthy: bool, *, observed_at: float | None = None) -> None:
        """Track continuous, observed health before resetting crash supervision."""

        now = time.monotonic() if observed_at is None else observed_at
        if not healthy:
            self._healthy_since = None
            return
        if self._healthy_since is None:
            self._healthy_since = now
            return
        if now - self._healthy_since >= _HEALTHY_RESET_SECONDS:
            self._consecutive_failures = 0

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
                    self._record_start_failure_locked()
            await self._notify_reaped()
            return False

    async def _notify_reaped(self) -> None:
        callback = self._on_reaped
        if callback is None:
            return
        try:
            result = callback()
            if inspect.isawaitable(result):
                await result
        except Exception:
            logger.warning("EverOS sidecar reaped callback failed")

    def _timezone_for_root(self) -> str:
        configured = _iana_timezone(self._settings.timezone)
        if configured is not None:
            return configured
        existing = _root_timezone(self._provider_root / "everos.toml")
        return existing or _local_iana_timezone()


class EverOSRebuildProcess:
    """Run and fully reap one role-owned EverOS cascade rebuild."""

    def __init__(
        self,
        python: Path | str | None,
        *,
        provider_root: Path | str | None = None,
        effective_home: Path | str | None = None,
        settings: EverOSProcessSettings | None = None,
        timeout_seconds: float = _REBUILD_TIMEOUT_SECONDS,
        stop_timeout_seconds: float = _STOP_TIMEOUT_SECONDS,
        _host: _ProcessHost | None = None,
    ) -> None:
        self._python = (
            Path(os.path.abspath(os.fspath(python)))
            if python is not None
            else None
        )
        effective_home_path = (
            Path(effective_home) if effective_home is not None else paths.get_vibe_remote_dir()
        )
        self._effective_home = Path(os.path.abspath(os.fspath(effective_home_path)))
        self._memory_dir = self._effective_home / "memory"
        self._attachments_root = attachment_pin_root(self._effective_home)
        provider_root_path = (
            Path(provider_root) if provider_root is not None else self._memory_dir / "everos-root"
        )
        self._provider_root = Path(os.path.abspath(os.fspath(provider_root_path)))
        self._socket_path = self._memory_dir / ".rt" / "everos.sock"
        self._settings = settings or EverOSProcessSettings()
        self._timeout_seconds = _positive_timeout(timeout_seconds, _REBUILD_TIMEOUT_SECONDS)
        self._stop_timeout_seconds = _positive_timeout(
            stop_timeout_seconds,
            _STOP_TIMEOUT_SECONDS,
        )
        self._host = _SystemProcessHost() if _host is None else _host
        self._ownership = SidecarOwnership(
            record_path=sidecar_record_path(self._memory_dir),
            socket_path=self._socket_path,
            provider_root=self._provider_root,
            stop_timeout_seconds=self._stop_timeout_seconds,
            role=_MemoryChildRole.CASCADE_REBUILD,
            python=self._python,
            _host=self._host,
        )

    async def reconcile_orphan(self) -> None:
        """Reap any managed child that could still own this provider root."""

        _ensure_owner_directory(self._memory_dir)
        _require_provider_root_access_path(self._provider_root)
        root_lock = self._provider_root_lock()
        root_lock.acquire()
        try:
            await self._reconcile_orphan_exclusive()
        finally:
            try:
                self._ownership.release_planned_reaps()
            finally:
                root_lock.release()

    async def _reconcile_orphan_exclusive(self) -> None:
        """Reap one managed child while the provider-root lock is held."""

        interrupted = await _finish_cleanup_despite_cancellation(
            self._ownership.reap(discover_missing=True)
        )
        if interrupted:
            raise asyncio.CancelledError

    def _provider_root_lock(self) -> _ProviderRootLock:
        lock_path = _provider_rebuild_lock_path(provider_root=self._provider_root)
        return _ProviderRootLock(
            confinement_root=lock_path.parent.parent,
            path=lock_path,
        )

    async def run(self) -> RebuildProcessResult:
        """Run the pinned rebuild command and release only after tree cleanup."""

        try:
            if (
                os.name != "posix"
                or self._python is None
                or not self._python.is_file()
                or not _rebuild_settings_complete(self._settings)
            ):
                return RebuildProcessResult.FAILED
            # The default provider root is below this Avibe-owned directory, so
            # its parent must exist before the adjacent lock can be resolved.
            # Provider data itself remains untouched until after lock admission.
            _ensure_owner_directory(self._memory_dir)
            _require_provider_root_access_path(self._provider_root)
            root_lock = self._provider_root_lock()
            root_lock.acquire()
        except _ProviderRootBusy:
            return RebuildProcessResult.ROOT_BUSY
        except Exception:
            logger.exception("EverOS cascade rebuild admission failed")
            return RebuildProcessResult.FAILED
        try:
            try:
                _prepare_memory_child_directories(
                    memory_dir=self._memory_dir,
                    provider_root=self._provider_root,
                    settings=self._settings,
                )
            except Exception:
                logger.exception("EverOS cascade rebuild admission failed")
                return RebuildProcessResult.FAILED
            return await self._run_exclusive()
        finally:
            try:
                self._ownership.release_planned_reaps()
            finally:
                root_lock.release()

    async def _run_exclusive(self) -> RebuildProcessResult:
        """Own the provider root from orphan reconciliation through retirement."""

        process: asyncio.subprocess.Process | None = None
        process_group: int | None = None
        identities: dict[int, float] = {}
        try:
            await self._reconcile_orphan_exclusive()
            _write_memory_child_config(
                memory_dir=self._memory_dir,
                provider_root=self._provider_root,
                attachments_root=self._attachments_root,
                settings=self._settings,
            )
            process, spawn_interrupted = await _finish_handoff_despite_cancellation(
                self._host.spawn(
                    _ProcessKind.CASCADE_REBUILD,
                    self._python,
                    cwd=self._memory_dir,
                    env=_memory_child_environment(
                        python=self._python,
                        memory_dir=self._memory_dir,
                        provider_root=self._provider_root,
                        attachments_root=self._attachments_root,
                        settings=self._settings,
                        role=_MemoryChildRole.CASCADE_REBUILD,
                    ),
                ),
            )
            process_group = self._host.process_group(process.pid)
            identities = self._host.snapshot_tree(process.pid, process_group)
            stopped, handshake_interrupted = await _finish_handoff_despite_cancellation(
                self._host.wait_for_stopped(
                    process.pid,
                    _REBUILD_HANDSHAKE_TIMEOUT_SECONDS,
                )
            )
            _merge_owned_processes(
                identities,
                self._host.snapshot_tree(process.pid, process_group),
            )
            if not stopped:
                raise RuntimeError("rebuild child did not enter ownership handshake")
            if not _host_identity_is_live(self._host, process.pid, identities):
                raise RuntimeError("could not establish rebuild process ownership")
            self._ownership.record_launch(
                process.pid,
                identities[process.pid],
                process_group,
            )
            _release_rebuild_child(process)
            if spawn_interrupted or handshake_interrupted:
                result = RebuildProcessResult.INTERRUPTED
            else:
                try:
                    await asyncio.wait_for(process.wait(), timeout=self._timeout_seconds)
                except asyncio.TimeoutError:
                    result = RebuildProcessResult.TIMED_OUT
                except asyncio.CancelledError:
                    result = RebuildProcessResult.INTERRUPTED
                else:
                    result = _rebuild_result_for_exit_code(process.returncode)
        except asyncio.CancelledError:
            result = RebuildProcessResult.INTERRUPTED
        except Exception:
            logger.exception("EverOS cascade rebuild failed")
            result = RebuildProcessResult.FAILED

        if process is None:
            return result
        try:
            cleanup_interrupted = await _finish_cleanup_despite_cancellation(
                _terminate_owned_process_tree(
                    self._host,
                    process,
                    process_group=process_group,
                    owned_processes=identities,
                    stop_timeout_seconds=self._stop_timeout_seconds,
                )
            )
            retire_interrupted = await _finish_cleanup_despite_cancellation(
                self._ownership.retire_reaped_launch(
                    process.pid,
                    process_group=process_group,
                )
            )
        except Exception:
            logger.exception("EverOS cascade rebuild cleanup failed")
            return RebuildProcessResult.FAILED
        if cleanup_interrupted or retire_interrupted:
            return RebuildProcessResult.INTERRUPTED
        return result


def _rebuild_result_for_exit_code(exit_code: int | None) -> RebuildProcessResult:
    if exit_code == 0:
        return RebuildProcessResult.COMPLETED
    if exit_code == 3:
        return RebuildProcessResult.ROOT_BUSY
    if exit_code == 130:
        return RebuildProcessResult.INTERRUPTED
    return RebuildProcessResult.FAILED


def _release_rebuild_child(process: asyncio.subprocess.Process) -> None:
    """Release a bootstrap only after this parent has persisted ownership."""

    try:
        process.send_signal(signal.SIGCONT)
    except ProcessLookupError:
        pass


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
        / _REBUILD_LOCK_DIRECTORY
        / f"{prefix}{root_identity}{suffix}"
    )


def _provider_rebuild_lock_path(*, provider_root: Path) -> Path:
    """Bind rebuild serialization to the canonical root location."""

    return _provider_root_coordination_path(
        provider_root=provider_root,
        prefix=_REBUILD_LOCK_PREFIX,
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


def _provider_roots_match(value: object, expected: Path) -> bool:
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
        role: _MemoryChildRole = _MemoryChildRole.SIDECAR,
        python: Path | None = None,
        _host: _ProcessHost | None = None,
    ) -> None:
        self.record_path = Path(record_path)
        self._socket_path = Path(socket_path)
        self._provider_root = Path(provider_root)
        self._stop_timeout_seconds = _positive_timeout(stop_timeout_seconds, _STOP_TIMEOUT_SECONDS)
        self._role = role
        self._python = Path(python) if python is not None else None
        self._host = _SystemProcessHost() if _host is None else _host
        self._planned_reaps = _PlannedReapTokens(self._provider_root)
        self._planned_reap_leases: dict[tuple[int, str], _PlannedReapLease] = {}

    def record_planned_reap(self, pid: int, created_at: float) -> None:
        """Persist an exact one-shot handoff before terminating a live sidecar."""

        key = self._planned_reap_key(pid, created_at)
        if key not in self._planned_reap_leases:
            self._planned_reap_leases[key] = self._planned_reaps.record(
                pid,
                created_at,
            )

    def commit_planned_reap(self, pid: int, created_at: float) -> None:
        """Persist authenticated success before releasing the issuer lease."""

        lease = self._planned_reap_leases.get(
            self._planned_reap_key(pid, created_at),
        )
        if lease is None:
            raise RuntimeError("planned-reap issuer lease is missing")
        lease.commit()

    def consume_planned_reap(self, pid: int, created_at: float) -> bool:
        """Consume this process generation's handoff, if one was recorded."""

        lease = self._planned_reap_leases.pop(
            self._planned_reap_key(pid, created_at),
            None,
        )
        if lease is not None:
            return lease.release()
        return self._planned_reaps.consume(pid, created_at)

    def cancel_planned_reap(self, pid: int, created_at: float) -> None:
        """Revoke a handoff that failed before signalling its exact target."""

        lease = self._planned_reap_leases.pop(
            self._planned_reap_key(pid, created_at),
            None,
        )
        if lease is not None:
            lease.release()

    def release_planned_reaps(self) -> None:
        """End every remaining issuer lease before releasing root ownership."""

        leases = tuple(self._planned_reap_leases.values())
        self._planned_reap_leases.clear()
        for lease in leases:
            try:
                lease.release(remove_token=not lease.committed)
            except Exception:
                # Closing the descriptor still revokes the issuer lease. An
                # unlocked leftover token is stale and cannot suppress a crash.
                logger.warning("Could not remove a stale planned-reap token")

    @staticmethod
    def _planned_reap_key(pid: int, created_at: float) -> tuple[int, str]:
        return pid, float(created_at).hex()

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

        if created_at < 0:
            # A negative sentinel means the OS would not disclose the creation time,
            # and the liveness check above this call should already have rejected
            # that. Recording it would produce a record nothing can ever match, so
            # fail rather than launch a child no later boot can identify.
            raise RuntimeError("could not verify the sidecar creation time to record")
        if self._python is None:
            raise RuntimeError("could not verify the EverOS child interpreter to record")
        self._persist_record(
            pid,
            created_at,
            process_group,
            role=self._role,
            python=self._python,
        )

    def _persist_record(
        self,
        pid: int,
        created_at: float,
        process_group: int | None,
        *,
        role: _MemoryChildRole | None,
        python: Path | None,
    ) -> None:
        record: dict[str, object] = {
            "pid": pid,
            "create_time": created_at,
            "process_group": process_group,
            "socket_path": str(self._socket_path),
            "provider_root": str(self._provider_root),
        }
        if role is not None:
            record["role"] = role.value
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
        if recorded_role is None:
            raise RuntimeError(
                "recorded EverOS child role could not be verified "
                f"(pid {pid}, record {self.record_path})"
            )
        group_match_role = (
            None if isinstance(record, dict) and record.get("role") is None else recorded_role
        )
        identity = self._host.inspect_identity(pid)
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
        confirmed_create_time = identity.create_time if identity is not None else None
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

        logger.warning("Reaping an orphaned EverOS %s left by a previous Avibe run", recorded_role.value)
        planned_sidecar_reap = (
            self._role is _MemoryChildRole.CASCADE_REBUILD
            and recorded_role is _MemoryChildRole.SIDECAR
        )
        if planned_sidecar_reap:
            self.record_planned_reap(pid, confirmed_create_time)
        terminated = False
        try:
            terminated = await self._terminate_orphan_tree(
                pid,
                confirmed_create_time,
            )
        finally:
            if planned_sidecar_reap and not terminated:
                self.cancel_planned_reap(pid, confirmed_create_time)
        if not terminated:
            raise RuntimeError(f"orphaned sidecar did not exit (pid {pid}, record {self.record_path})")
        if planned_sidecar_reap:
            self.commit_planned_reap(pid, confirmed_create_time)
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

    async def _terminate_orphan_tree(self, pid: int, created_at: float) -> bool:
        """Reap an orphan's whole tree, not just the pid the record names.

        The sidecar may have spawned helpers before the service died, and those
        keep the provider root open just as the root process does. Discovery and
        signalling therefore reuse the same helpers as ``_terminate_owned_tree``
        -- descendants plus the isolated process group, with a group-wide signal
        only once every member is confirmed owned. The one difference is that no
        ``asyncio`` child handle exists for a process this run did not spawn, so
        liveness is decided purely from the captured identities.
        """

        identities: dict[int, float] = {pid: created_at}
        rounds = (
            (signal.SIGTERM, self._stop_timeout_seconds),
            (getattr(signal, "SIGKILL", signal.SIGTERM), min(self._stop_timeout_seconds, 3.0)),
        )
        for signum, timeout_seconds in rounds:
            if _host_identity_is_live(self._host, pid, identities):
                # Only rediscover while the recorded root is still the process we
                # identified; a dead root's pid may already have been recycled.
                process_group = self._host.process_group(pid)
                _merge_owned_processes(identities, self._host.snapshot_tree(pid, process_group))
            else:
                process_group = None
            self._host.signal(identities, signum, process_group=process_group)
            if await self._host.wait_for_exit(identities, timeout_seconds):
                return True
        return False

    async def _reap_recorded_group_without_leader(
        self,
        record: object,
        *,
        leader_pid: int,
        role: _MemoryChildRole | None,
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
            if foreign and role is _MemoryChildRole.CASCADE_REBUILD:
                raise RuntimeError(
                    "orphaned rebuild group could not be verified "
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
        if foreign and role is _MemoryChildRole.CASCADE_REBUILD:
            raise RuntimeError(
                "orphaned rebuild group could not be verified "
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
            or _recorded_child_role(record) is not self._role
            or _recorded_sidecar_group(
                record,
                socket_path=self._socket_path,
                provider_root=self._provider_root,
            )
            != process_group
        ):
            return
        if process_group is not None:
            claimed, _foreign = self._host.recorded_group_members(
                process_group,
                socket_path=self._socket_path,
                provider_root=self._provider_root,
                role=self._role,
            )
            if claimed:
                logger.warning(
                    "Keeping the EverOS ownership record: process group %s still holds %s of ours (%s), record %s",
                    process_group,
                    len(claimed),
                    sorted(claimed),
                    self.record_path,
                )
                return
        _remove_sidecar_record(self.record_path)

    async def retire_reaped_launch(
        self,
        leader_pid: int,
        *,
        process_group: int | None,
    ) -> None:
        """Sweep late owned helpers before retiring a completed one-shot child."""

        record = _read_sidecar_record(self.record_path)
        if (
            _recorded_sidecar_pid(record) != leader_pid
            or _recorded_child_role(record) is not self._role
        ):
            raise RuntimeError(
                "active EverOS child ownership record changed before cleanup "
                f"(pid {leader_pid}, record {self.record_path})"
            )
        recorded_group = _recorded_sidecar_group(
            record,
            socket_path=self._socket_path,
            provider_root=self._provider_root,
        )
        if recorded_group != process_group:
            raise RuntimeError(
                "active EverOS child process group changed before cleanup "
                f"(pid {leader_pid}, record {self.record_path})"
            )
        await self._reap_recorded_group_without_leader(
            record,
            leader_pid=leader_pid,
            role=self._role,
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

        sidecar_anchors = self._host.find_sidecars(socket_path=self._socket_path)
        root_sidecar_anchors = self._host.find_sidecars_by_root(
            provider_root=self._provider_root
        )
        root_only_sidecars = set(root_sidecar_anchors).difference(sidecar_anchors)
        sidecar_anchors.update(root_sidecar_anchors)
        sidecars = [
            (_MemoryChildRole.SIDECAR, pid, created_at)
            for pid, created_at in sidecar_anchors.items()
        ]
        rebuilds = [
            (_MemoryChildRole.CASCADE_REBUILD, pid, created_at)
            for pid, created_at in self._host.find_rebuilds(
                provider_root=self._provider_root,
                python=None,
            ).items()
        ]
        candidates = rebuilds or sidecars
        if not candidates:
            return
        if rebuilds and (sidecars or len(rebuilds) != 1):
            raise RuntimeError(
                "ambiguous EverOS child ownership discovery "
                f"(pids {sorted(pid for _role, pid, _created_at in rebuilds + sidecars)})"
            )
        for role, pid, created_at in sorted(candidates, key=lambda candidate: candidate[1]):
            if (
                self._role is _MemoryChildRole.SIDECAR
                and role is _MemoryChildRole.SIDECAR
                and pid in root_only_sidecars
            ):
                identity = self._host.inspect_identity(pid)
                if (
                    identity is None
                    or identity.create_time != created_at
                    or identity.cmdline is None
                    or not _cmdline_matches_role(
                        identity.cmdline,
                        role=role,
                        socket_path=self._socket_path,
                    )
                ):
                    raise _ProviderRootBusy(
                        "a live sidecar already owns this provider root"
                    )
            logger.warning(
                "Reaping an EverOS %s an unusable ownership record could not identify (pid %s)",
                role.value,
                pid,
            )
            identities = {pid: created_at}
            discovered_python = self._python
            if role is _MemoryChildRole.CASCADE_REBUILD:
                identity = self._host.inspect_identity(pid)
                getuid = getattr(os, "getuid", None)
                own_uid = getuid() if callable(getuid) else None
                if (
                    identity is None
                    or identity.create_time != created_at
                    or identity.cmdline is None
                    or not identity.cmdline
                    or not _cmdline_matches_role(
                        identity.cmdline,
                        role=role,
                        socket_path=self._socket_path,
                        python=None,
                    )
                    or (own_uid is not None and identity.uid != own_uid)
                    or identity.environment is None
                    or not _provider_roots_match(
                        identity.environment.get("EVEROS_ROOT"),
                        self._provider_root,
                    )
                    or identity.environment.get("AVIBE_MEMORY_CHILD_ROLE") != role.value
                ):
                    raise RuntimeError(
                        f"rebuild child identity could not be verified (pid {pid})"
                    )
                discovered_python = Path(identity.cmdline[0])
            # Helpers are reached through the anchor's own group rather than by
            # widening the machine-wide test, because membership is what makes the
            # looser per-member claim safe.
            group = self._host.process_group(pid)
            self._persist_record(
                pid,
                created_at,
                group,
                role=None if role is _MemoryChildRole.SIDECAR else role,
                python=(
                    discovered_python
                    if role is _MemoryChildRole.CASCADE_REBUILD
                    else None
                ),
            )
            foreign: list[int] = []
            if group is not None:
                claimed, foreign = self._host.recorded_group_members(
                    group,
                    socket_path=self._socket_path,
                    provider_root=self._provider_root,
                    role=None if role is _MemoryChildRole.SIDECAR else role,
                )
                _merge_owned_processes(identities, claimed)
                if foreign:
                    logger.warning(
                        "Leaving %s process(es) in EverOS %s group %s alone: %s",
                        len(foreign),
                        role.value,
                        group,
                        foreign,
                    )
            planned_sidecar_reap = (
                self._role is _MemoryChildRole.CASCADE_REBUILD
                and role is _MemoryChildRole.SIDECAR
            )
            if planned_sidecar_reap:
                self.record_planned_reap(pid, created_at)
            terminated = False
            try:
                terminated, later_foreign = await self._terminate_claimed_processes(
                    group,
                    identities,
                    role=role,
                )
            finally:
                if planned_sidecar_reap and not terminated:
                    self.cancel_planned_reap(pid, created_at)
            foreign = sorted(set(foreign).union(later_foreign))
            if not terminated:
                raise RuntimeError(
                    f"EverOS {role.value} left by an unusable record did not exit "
                    f"(pid {pid}, record {self.record_path})"
                )
            if planned_sidecar_reap:
                self.commit_planned_reap(pid, created_at)
            if foreign and role is _MemoryChildRole.CASCADE_REBUILD:
                raise RuntimeError(
                    "orphaned rebuild group could not be verified "
                    f"(leader pid {pid}, group {group}, record {self.record_path})"
                )

    async def _terminate_claimed_processes(
        self,
        process_group: int | None,
        identities: dict[int, float],
        *,
        role: _MemoryChildRole | None = None,
    ) -> tuple[bool, list[int]]:
        """Signal claimed processes until none of them is left.

        Mirrors ``_terminate_orphan_tree``, minus the recorded root: whatever this
        run claimed is all there is to work from. A process group, when one is
        known, is both the rediscovery anchor and the only thing that permits a
        group-wide signal; rediscovery runs only while an already-claimed process
        is alive to prove the group has not emptied out from under the scan.
        The result carries both the claimed-tree death proof and every unverifiable
        group member observed during those rediscovery rounds.
        """

        rounds = (
            (signal.SIGTERM, self._stop_timeout_seconds),
            (getattr(signal, "SIGKILL", signal.SIGTERM), min(self._stop_timeout_seconds, 3.0)),
        )
        foreign: set[int] = set()
        for signum, timeout_seconds in rounds:
            if process_group is not None and self._host.live(identities):
                discovered, round_foreign = self._host.recorded_group_members(
                    process_group,
                    socket_path=self._socket_path,
                    provider_root=self._provider_root,
                    role=role,
                )
                _merge_owned_processes(identities, discovered)
                foreign.update(round_foreign)
            self._host.signal(identities, signum, process_group=process_group)
            if await self._host.wait_for_exit(identities, timeout_seconds):
                return True, sorted(foreign)
        return False, sorted(foreign)


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


def _rebuild_settings_complete(settings: EverOSProcessSettings) -> bool:
    return all(
        isinstance(value, str) and bool(value.strip())
        for value in (
            settings.embedding_base_url,
            settings.embedding_model,
            settings.embedding_api_key,
        )
    )


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
    if settings.call_log_db_path is not None:
        directories.append(settings.call_log_db_path.parent)
    for directory in directories:
        _ensure_owner_directory(directory)
    _require_provider_root_access_path(provider_root)
    ensure_private_directory(
        provider_root.parent,
        provider_root,
        harden_confinement_root=False,
    )


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
    role: _MemoryChildRole | None,
) -> dict[str, str]:
    child_home = memory_dir / ".child-home"
    env = {
        "ENV": "prod",
        "HOME": str(child_home),
        "PATH": f"{python.parent}:/usr/bin:/bin",
        "PYTHONNOUSERSITE": "1",
        "PYTHONUNBUFFERED": "1",
        "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
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
    }
    env.update({key: value for key, value in optional.items() if value is not None})
    if settings.call_log_db_path is not None:
        env["AVIBE_MEMORY_CALL_LOG_DB"] = str(settings.call_log_db_path)
    if role is not None:
        env["AVIBE_MEMORY_CHILD_ROLE"] = role.value
    return env


async def _terminate_owned_process_tree(
    host: _ProcessHost,
    process: asyncio.subprocess.Process,
    *,
    process_group: int | None,
    owned_processes: Mapping[int, float] | None,
    stop_timeout_seconds: float,
) -> None:
    identities = dict(owned_processes or {})
    if _host_identity_is_live(host, process.pid, identities):
        _merge_owned_processes(identities, host.snapshot_tree(process.pid, process_group))
    host.signal(
        identities,
        signal.SIGTERM,
        process_group=process_group,
        process=process,
    )
    if await host.wait_for_exit(
        identities,
        stop_timeout_seconds,
        process_group=process_group,
        process=process,
    ):
        return

    kill_signal = getattr(signal, "SIGKILL", signal.SIGTERM)
    if _host_identity_is_live(host, process.pid, identities):
        _merge_owned_processes(identities, host.snapshot_tree(process.pid, process_group))
    host.signal(
        identities,
        kill_signal,
        process_group=process_group,
        process=process,
    )
    if await host.wait_for_exit(
        identities,
        min(stop_timeout_seconds, 3.0),
        process_group=process_group,
        process=process,
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
    """

    create_time: float | None
    cmdline: tuple[str, ...] | None
    uid: int | None
    environment: Mapping[str, str] | None = None


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
        return _ProcessIdentity(create_time=None, cmdline=None, uid=None, environment=None)
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
            create_time=_disclosed_identity_field(process.create_time),
            cmdline=None if cmdline is None else tuple(str(value) for value in cmdline),
            uid=_process_real_uid(process),
            environment=_disclosed_process_environment(process),
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


def _recorded_child_role(record: object) -> _MemoryChildRole | None:
    if not isinstance(record, dict):
        return None
    role = record.get("role")
    if role is None:
        # Records written before role-aware ownership always describe a sidecar.
        return _MemoryChildRole.SIDECAR
    try:
        return _MemoryChildRole(role)
    except (TypeError, ValueError):
        return None


def _recorded_child_python(record: object) -> Path | None:
    if not isinstance(record, dict):
        return None
    value = record.get("python")
    return Path(value) if isinstance(value, str) and value else None


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
        record.get("socket_path") != str(socket_path)
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
    """The creation time a record can be matched against, or ``None``.

    A malformed creation time can never be matched by any process, so it yields
    nothing this launch may act on.
    """

    matched = _record_for_this_installation(record, socket_path=socket_path, provider_root=provider_root)
    if matched is None:
        return None
    created_at = matched.get("create_time")
    if not isinstance(created_at, (int, float)) or isinstance(created_at, bool):
        return None
    return float(created_at)


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
    role: _MemoryChildRole | None = None,
) -> tuple[dict[int, float], list[int]]:
    """Split a recorded group's live members into ours and ones to leave alone.

    Returns claimed ``(pid, create_time)`` identities plus the pids that could not
    be tied to this installation, so the caller can log what it deliberately spared.
    """

    claimed: dict[int, float] = {}
    foreign: list[int] = []
    own_pid = os.getpid()
    for pid, created_at in _snapshot_process_group(process_group).items():
        if pid == own_pid:
            continue
        if created_at >= 0 and _process_names_owned_runtime(
            pid,
            socket_path=socket_path,
            provider_root=provider_root,
            role=role,
        ):
            claimed[pid] = created_at
        else:
            # Either the identity is unreadable (the negative sentinel) or nothing
            # observable ties the process to this installation.
            foreign.append(pid)
    return claimed, sorted(foreign)


def _process_names_owned_runtime(
    pid: int,
    *,
    socket_path: Path,
    provider_root: Path,
    role: _MemoryChildRole | None = None,
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
            str(socket_path) in cmdline or str(provider_root) in cmdline
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
    return role is None or environment.get("AVIBE_MEMORY_CHILD_ROLE") == role.value


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
    return _SIDECAR_ENTRYPOINT_MODULE in cmdline and "--uds" in cmdline and str(socket_path) in cmdline


def _cmdline_is_sidecar(cmdline: tuple[str, ...]) -> bool:
    return len(cmdline) == 5 and cmdline[1:4] == (
        "-m",
        _SIDECAR_ENTRYPOINT_MODULE,
        "--uds",
    )


def _cmdline_matches_role(
    cmdline: tuple[str, ...],
    *,
    role: _MemoryChildRole,
    socket_path: Path,
    python: Path | None = None,
) -> bool:
    if not cmdline:
        return False
    if python is not None and cmdline[0] != str(python):
        return False
    if role is _MemoryChildRole.SIDECAR:
        return cmdline[1:] == (
            "-m",
            _SIDECAR_ENTRYPOINT_MODULE,
            "--uds",
            str(socket_path),
        )
    if role is _MemoryChildRole.CASCADE_SYNC:
        return cmdline[1:] == (
            "-I",
            "-m",
            "everos.entrypoints.cli.main",
            "cascade",
            "sync",
        )
    return cmdline[1:] == (
        "-m",
        _REBUILD_ENTRYPOINT_MODULE,
        "cascade",
        "rebuild",
        "--yes",
    )


def _classify_recorded_child(
    record: object,
    identity: _ProcessIdentity | None,
    *,
    socket_path: Path,
    provider_root: Path,
    role: _MemoryChildRole,
) -> _RecordedSidecar:
    recorded_create_time = _recorded_sidecar_create_time(
        record,
        socket_path=socket_path,
        provider_root=provider_root,
    )
    if identity is None or recorded_create_time is None:
        return _RecordedSidecar.NOT_OURS
    getuid = getattr(os, "getuid", None)
    own_uid = getuid() if callable(getuid) else None
    if identity.uid is not None and own_uid is not None and identity.uid != own_uid:
        return _RecordedSidecar.NOT_OURS
    if identity.create_time is not None and identity.create_time != recorded_create_time:
        return _RecordedSidecar.NOT_OURS
    legacy_sidecar = isinstance(record, dict) and record.get("role") is None
    recorded_python = None if legacy_sidecar else _recorded_child_python(record)
    if not legacy_sidecar and recorded_python is None:
        return _RecordedSidecar.UNVERIFIABLE
    if identity.cmdline is not None:
        matches_command = (
            _cmdline_serves_socket(identity.cmdline, socket_path)
            if legacy_sidecar
            else _cmdline_matches_role(
                identity.cmdline,
                role=role,
                socket_path=socket_path,
                python=recorded_python,
            )
        )
        if not matches_command:
            return _RecordedSidecar.NOT_OURS
    if not legacy_sidecar and identity.environment is not None:
        if (
            not _provider_roots_match(
                identity.environment.get("EVEROS_ROOT"),
                provider_root,
            )
            or identity.environment.get("AVIBE_MEMORY_CHILD_ROLE") != role.value
        ):
            return _RecordedSidecar.NOT_OURS
    if identity.create_time is None or identity.cmdline is None:
        return _RecordedSidecar.UNVERIFIABLE
    if own_uid is not None and identity.uid is None:
        return _RecordedSidecar.UNVERIFIABLE
    if not legacy_sidecar and identity.environment is None:
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
    the creation time, the real uid, and the exact ``-m`` entrypoint plus
    ``--uds`` argument all agree with the record. Any single disclosed fact that
    contradicts the record settles the matter as ``NOT_OURS`` -- a recycled pid
    or another user's process is safe to stop worrying about. What must not be
    waved through is a live pid whose deciding facts were never disclosed:
    treating it as gone would start a replacement sidecar beside it.
    """

    return _classify_recorded_child(
        record,
        identity,
        socket_path=socket_path,
        provider_root=provider_root,
        role=_MemoryChildRole.SIDECAR,
    )


def _read_sidecar_record(path: Path) -> object | None:
    try:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            return None
        if info.st_size > _SIDECAR_RECORD_MAX_BYTES:
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


def _processes_serving_owned_socket(*, socket_path: Path) -> dict[int, float]:
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

    claimed: dict[int, float] = {}
    own_pid = os.getpid()
    getuid = getattr(os, "getuid", None)
    own_uid = getuid() if callable(getuid) else None
    for candidate in psutil.process_iter():
        if candidate.pid == own_pid:
            continue
        try:
            if own_uid is not None and _process_real_uid(candidate) != own_uid:
                continue
            cmdline = _disclosed_identity_field(candidate.cmdline)
            if cmdline is None or not _cmdline_serves_socket(tuple(str(value) for value in cmdline), socket_path):
                continue
            created_at = _disclosed_identity_field(candidate.create_time)
        except psutil.Error:
            continue
        # A claimed process whose creation time is withheld carries the negative
        # sentinel: it can never be signaled by identity, and it never counts as
        # reaped, so it fails the launch closed instead of being written off.
        claimed[candidate.pid] = -1.0 if created_at is None else float(created_at)
    return claimed


def _processes_serving_owned_root(*, provider_root: Path) -> dict[int, float]:
    """Live exact sidecar entrypoints owned by this uid and provider root."""

    claimed: dict[int, float] = {}
    own_pid = os.getpid()
    getuid = getattr(os, "getuid", None)
    own_uid = getuid() if callable(getuid) else None
    for candidate in psutil.process_iter():
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
            created_at = _disclosed_identity_field(candidate.create_time)
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
        if role not in (None, _MemoryChildRole.SIDECAR.value):
            continue
        claimed[candidate.pid] = -1.0 if created_at is None else float(created_at)
    return claimed


def _processes_rebuilding_owned_root(
    *,
    provider_root: Path,
    python: Path | None = None,
) -> dict[int, float]:
    """Discover exact role-bearing rebuild children for crash recovery."""

    claimed: dict[int, float] = {}
    own_pid = os.getpid()
    getuid = getattr(os, "getuid", None)
    own_uid = getuid() if callable(getuid) else None
    for candidate in psutil.process_iter():
        if candidate.pid == own_pid:
            continue
        try:
            uid = _process_real_uid(candidate)
            if own_uid is not None and uid is not None and uid != own_uid:
                continue
            cmdline = _disclosed_identity_field(candidate.cmdline)
            if cmdline is None:
                continue
            rendered = tuple(str(value) for value in cmdline)
            if not _cmdline_matches_role(
                rendered,
                role=_MemoryChildRole.CASCADE_REBUILD,
                socket_path=Path(),
                python=python,
            ):
                continue
            created_at = _disclosed_identity_field(candidate.create_time)
            environment = _disclosed_process_environment(candidate)
        except psutil.NoSuchProcess:
            continue
        except psutil.Error:
            continue
        if own_uid is not None and uid is None:
            raise RuntimeError(f"rebuild child uid could not be verified (pid {candidate.pid})")
        if created_at is None or environment is None:
            raise RuntimeError(f"rebuild child identity could not be verified (pid {candidate.pid})")
        if (
            not _provider_roots_match(
                environment.get("EVEROS_ROOT"),
                provider_root,
            )
            or environment.get("AVIBE_MEMORY_CHILD_ROLE")
            != _MemoryChildRole.CASCADE_REBUILD.value
        ):
            continue
        claimed[candidate.pid] = float(created_at)
    return claimed


def _processes_syncing_owned_root(
    *,
    provider_root: Path,
    python: Path,
    nonce: str,
) -> dict[int, float]:
    """Discover the exact nonce-bearing sync child for pending recovery."""

    claimed: dict[int, float] = {}
    own_pid = os.getpid()
    getuid = getattr(os, "getuid", None)
    own_uid = getuid() if callable(getuid) else None
    for candidate in psutil.process_iter():
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
            role=_MemoryChildRole.CASCADE_SYNC,
            socket_path=Path(),
            python=python,
        ):
            continue
        try:
            created_at = _disclosed_identity_field(candidate.create_time)
            environment = _disclosed_process_environment(candidate)
        except psutil.NoSuchProcess:
            continue
        except psutil.Error as exc:
            raise RuntimeError(
                f"sync child identity could not be verified (pid {candidate.pid})"
            ) from exc
        if own_uid is not None and uid is None:
            raise RuntimeError(f"sync child uid could not be verified (pid {candidate.pid})")
        if created_at is None or environment is None:
            raise RuntimeError(
                f"sync child identity could not be verified (pid {candidate.pid})"
            )
        if (
            not _provider_roots_match(environment.get("EVEROS_ROOT"), provider_root)
            or environment.get("AVIBE_MEMORY_CHILD_ROLE") != _MemoryChildRole.CASCADE_SYNC.value
            or environment.get("AVIBE_MEMORY_SYNC_NONCE") != nonce
        ):
            continue
        claimed[candidate.pid] = float(created_at)
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


async def _wait_for_identities_exit(identities: Mapping[int, float], timeout_seconds: float) -> bool:
    """Poll recorded identities until none is live or the bound expires."""

    deadline = time.monotonic() + max(timeout_seconds, 0.1)
    while time.monotonic() < deadline:
        if not _live_owned_processes(identities):
            return True
        await asyncio.sleep(0.05)
    return not _live_owned_processes(identities)


def _snapshot_owned_processes(pid: int, process_group: int | None) -> dict[int, float]:
    """Record `(pid, create_time)` identities while the child is still owned."""

    identities: dict[int, float] = {}
    try:
        root = psutil.Process(pid)
        candidates = [root, *root.children(recursive=True)]
    except psutil.Error:
        candidates = []
    for candidate in candidates:
        try:
            identities.setdefault(candidate.pid, candidate.create_time())
        except psutil.Error:
            continue
    _merge_owned_processes(identities, _snapshot_process_group(process_group))
    return identities


def _snapshot_process_group(process_group: int | None) -> dict[int, float]:
    if process_group is None or os.name != "posix" or not hasattr(os, "getpgid"):
        return {}
    identities: dict[int, float] = {}
    for candidate in psutil.process_iter():
        try:
            if os.getpgid(candidate.pid) == process_group:
                identities[candidate.pid] = candidate.create_time()
        except psutil.AccessDenied:
            # The member exists but its identity cannot be verified. Keep it with a
            # sentinel so the "all confirmed" check sees an unverifiable member and
            # fails closed (no killpg) rather than silently dropping it.
            identities[candidate.pid] = -1.0
        except (OSError, psutil.Error):
            continue
    return identities


def _merge_owned_processes(identities: dict[int, float], discovered: Mapping[int, float]) -> None:
    """Add newly seen children without changing a captured process identity."""

    for process_id, created_at in discovered.items():
        identities.setdefault(process_id, created_at)


def _host_identity_is_live(
    host: _ProcessHost,
    process_id: int,
    identities: Mapping[int, float],
) -> bool:
    created_at = identities.get(process_id)
    return created_at is not None and process_id in host.live({process_id: created_at})


def _owned_process_identity_is_live(process_id: int, identities: Mapping[int, float]) -> bool:
    created_at = identities.get(process_id)
    if created_at is None:
        return False
    return process_id in _live_owned_processes({process_id: created_at})


def _live_owned_processes(identities: Mapping[int, float]) -> dict[int, float]:
    live: dict[int, float] = {}
    for process_id, created_at in identities.items():
        try:
            candidate = psutil.Process(process_id)
            if candidate.create_time() != created_at:
                continue
            if candidate.status() == psutil.STATUS_ZOMBIE:
                continue
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied:
            # An uninspectable descendant cannot be treated as cleanly reaped.
            pass
        except psutil.Error:
            continue
        live[process_id] = created_at
    return live


def _confirmed_owned_processes(identities: Mapping[int, float]) -> dict[int, float]:
    """Return identities whose current creation time is readable and unchanged."""

    confirmed: dict[int, float] = {}
    for process_id, created_at in identities.items():
        try:
            candidate = psutil.Process(process_id)
            if candidate.create_time() != created_at or candidate.status() == psutil.STATUS_ZOMBIE:
                continue
        except psutil.Error:
            # AccessDenied is live-but-unverified: retain it for reaping, but
            # never use it as authority to signal a numeric PID.
            continue
        confirmed[process_id] = created_at
    return confirmed


def _group_contains_only_confirmed_owned_processes(
    process_group: int | None,
    identities: Mapping[int, float],
) -> bool:
    """Whether a group can be signaled without bypassing PID identity checks."""

    if process_group is None:
        return False
    group_members = _snapshot_process_group(process_group)
    confirmed = _confirmed_owned_processes(identities)
    return bool(group_members) and all(
        confirmed.get(process_id) == created_at for process_id, created_at in group_members.items()
    )


def _signal_owned_group(
    process_group: int | None,
    identities: Mapping[int, float],
    signum: int,
) -> bool:
    """Signal a whole isolated group, but only if every member is confirmed owned.

    Returns whether the group signal settled the delivery, so a caller holding a
    direct child handle can fall back to it without widening the blast radius: a
    group with an unverifiable member is never signaled group-wide.
    """

    if (
        process_group is None
        or not hasattr(os, "killpg")
        or not _group_contains_only_confirmed_owned_processes(process_group, identities)
    ):
        return False
    try:
        os.killpg(process_group, signum)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return True


def _signal_owned_group_or_process(
    process: asyncio.subprocess.Process,
    process_group: int | None,
    identities: Mapping[int, float],
    signum: int,
) -> None:
    if _signal_owned_group(process_group, identities, signum):
        return
    if process.returncode is not None:
        return
    created_at = identities.get(process.pid)
    if created_at is None or process.pid not in _confirmed_owned_processes({process.pid: created_at}):
        return
    try:
        process.send_signal(signum)
    except ProcessLookupError:
        return


def _signal_owned_processes(identities: Mapping[int, float], signum: int) -> None:
    for process_id, created_at in _confirmed_owned_processes(identities).items():
        try:
            candidate = psutil.Process(process_id)
            if candidate.create_time() != created_at:
                continue
            candidate.send_signal(signum)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        except psutil.Error:
            continue


async def _wait_for_owned_exit(
    process: asyncio.subprocess.Process,
    *,
    process_group: int | None,
    identities: dict[int, float],
    timeout_seconds: float,
) -> bool:
    """Wait for the direct child and every discovered descendant to disappear."""

    deadline = time.monotonic() + max(timeout_seconds, 0.1)
    waiter = asyncio.create_task(process.wait(), name="memory-everos-reap")
    try:
        while time.monotonic() < deadline:
            if _owned_process_identity_is_live(process.pid, identities):
                _merge_owned_processes(identities, _snapshot_owned_processes(process.pid, process_group))
            if waiter.done() and not _live_owned_processes(identities):
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
        if kind is _ProcessKind.CASCADE_REBUILD:
            arguments = [
                str(python),
                "-m",
                _REBUILD_ENTRYPOINT_MODULE,
                "cascade",
                "rebuild",
                "--yes",
            ]
        elif kind is _ProcessKind.CASCADE_SYNC:
            arguments = [
                str(python),
                "-I",
                "-m",
                "everos.entrypoints.cli.main",
                "cascade",
                "sync",
            ]
        else:
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

    def snapshot_tree(self, pid: int, process_group: int | None) -> dict[int, float]:
        return _snapshot_owned_processes(pid, process_group)

    def recorded_group_members(
        self,
        process_group: int,
        *,
        socket_path: Path,
        provider_root: Path,
        role: _MemoryChildRole | None = None,
    ) -> tuple[dict[int, float], list[int]]:
        return _recorded_group_members(
            process_group,
            socket_path=socket_path,
            provider_root=provider_root,
            role=role,
        )

    def find_sidecars(self, *, socket_path: Path) -> dict[int, float]:
        return _processes_serving_owned_socket(socket_path=socket_path)

    def find_sidecars_by_root(self, *, provider_root: Path) -> dict[int, float]:
        return _processes_serving_owned_root(provider_root=provider_root)

    def find_rebuilds(
        self,
        *,
        provider_root: Path,
        python: Path | None,
    ) -> dict[int, float]:
        return _processes_rebuilding_owned_root(
            provider_root=provider_root,
            python=python,
        )

    def find_syncs(
        self,
        *,
        provider_root: Path,
        python: Path,
        nonce: str,
    ) -> dict[int, float]:
        return _processes_syncing_owned_root(
            provider_root=provider_root,
            python=python,
            nonce=nonce,
        )

    def live(self, identities: Mapping[int, float]) -> dict[int, float]:
        return _live_owned_processes(identities)

    async def wait_for_stopped(self, pid: int, timeout_seconds: float) -> bool:
        deadline = asyncio.get_running_loop().time() + max(timeout_seconds, 0.0)
        while True:
            try:
                status = psutil.Process(pid).status()
            except (psutil.NoSuchProcess, psutil.ZombieProcess):
                return False
            except psutil.Error as exc:
                raise RuntimeError("could not verify rebuild child handshake") from exc
            if status == psutil.STATUS_STOPPED:
                return True
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(0.01, remaining))

    def signal(
        self,
        identities: Mapping[int, float],
        signum: int,
        *,
        process_group: int | None = None,
        process: asyncio.subprocess.Process | None = None,
    ) -> None:
        if process is None:
            _signal_owned_group(process_group, identities, signum)
        else:
            _signal_owned_group_or_process(process, process_group, identities, signum)
        _signal_owned_processes(identities, signum)

    async def wait_for_exit(
        self,
        identities: dict[int, float],
        timeout_seconds: float,
        *,
        process_group: int | None = None,
        process: asyncio.subprocess.Process | None = None,
    ) -> bool:
        if process is None:
            return await _wait_for_identities_exit(identities, timeout_seconds)
        return await _wait_for_owned_exit(
            process,
            process_group=process_group,
            identities=identities,
            timeout_seconds=timeout_seconds,
        )

    def has_tcp_listener(self, identities: Mapping[int, float]) -> bool:
        for process_id in identities:
            try:
                connections = psutil.Process(process_id).net_connections(kind="inet")
            except (psutil.NoSuchProcess, psutil.ZombieProcess):
                continue
            except psutil.Error as exc:
                raise RuntimeError("could not inspect sidecar listeners") from exc
            if any(connection.status == psutil.CONN_LISTEN for connection in connections):
                return True
        return False


@runtime_checkable
class EverOSProcessPort(Protocol):
    """What the runtime needs from a supervised sidecar, and nothing more.

    Deliberately five members over ``EverOSProcess``'s ~990 lines: the runtime
    never inspects the child tree, the generated config, or the signal handling.
    Keeping those out of this interface is what lets tests substitute a fake
    instead of patching ``psutil``, ``os``, and private attributes.
    """

    @property
    def running(self) -> bool: ...

    @property
    def starting(self) -> bool: ...

    async def start(self) -> bool: ...

    async def stop(self) -> None: ...

    async def processing_healthy(self) -> bool: ...


class EverOSProcessFactory(Protocol):
    """Construct one supervised sidecar per reconciliation.

    A factory rather than an instance because the runtime builds a fresh child
    for every enabled reconciliation, and a short-lived one for the enablement
    probe. Mirrors ``EverOSProcess.__init__``.
    """

    def __call__(
        self,
        python: Path | str,
        *,
        provider_root: Path | str | None = None,
        effective_home: Path | str | None = None,
        settings: EverOSProcessSettings | None = None,
        socket_path: Path | str | None = None,
        on_ready: Callable[[], Awaitable[None] | None] | None = None,
        before_start: Callable[[], Awaitable[None] | None] | None = None,
        on_reaped: Callable[[], Awaitable[None] | None] | None = None,
    ) -> EverOSProcessPort: ...


@dataclass
class FakeEverOSProcess:
    """In-process sidecar fake for runtime contract tests.

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
    on_reaped: Callable[[], Awaitable[None] | None] | None = None
    # Launch inputs the factory captured, for tests asserting on child settings.
    python: Path | None = None
    provider_root: Path | None = None
    settings: EverOSProcessSettings | None = None
    starts: int = 0
    stops: int = 0
    stopped: bool = False
    _running: bool = True
    _starting: bool = False

    @property
    def running(self) -> bool:
        return self._running

    @property
    def starting(self) -> bool:
        return self._starting

    async def start(self) -> bool:
        self.starts += 1
        before_start = self.before_start
        if before_start is not None:
            result = before_start()
            if inspect.isawaitable(result):
                await result
        if self.start_failure is not None:
            self._running = False
            await self._notify_reaped()
            raise self.start_failure
        started = self.start_results.popleft() if self.start_results else True
        self._running = started
        self._starting = False
        if started:
            await self.ready()
        else:
            await self._notify_reaped()
        return started

    async def stop(self) -> None:
        self.stops += 1
        self.stopped = True
        self._running = False
        self._starting = False
        if self.stop_failure is not None:
            raise self.stop_failure
        await self._notify_reaped()

    async def _notify_reaped(self) -> None:
        on_reaped = self.on_reaped
        if on_reaped is None:
            return
        result = on_reaped()
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
    runtime actually supervises — a process built without ``on_ready`` is the
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
        on_ready: Callable[[], Awaitable[None] | None] | None = None,
        before_start: Callable[[], Awaitable[None] | None] | None = None,
        on_reaped: Callable[[], Awaitable[None] | None] | None = None,
    ) -> EverOSProcessPort:
        del effective_home, socket_path
        process = self.template()
        process.on_ready = on_ready
        process.before_start = before_start
        process.on_reaped = on_reaped
        process.python = Path(python)
        process.provider_root = Path(provider_root) if provider_root is not None else None
        process.settings = settings
        self.created.append(process)
        if on_ready is not None:
            self.supervised.append(process)
        return process

    @property
    def last(self) -> FakeEverOSProcess | None:
        return self.created[-1] if self.created else None


def __getattr__(name: str) -> object:
    """Lazily expose the auxiliary sync process without a module cycle."""

    if name in {"EverOSSyncProcess", "SyncProcessResult", "sync_record_path"}:
        from core.memory import sync_process

        return getattr(sync_process, name)
    raise AttributeError(name)
