"""Owned Unix-socket lifecycle for the private EverOS sidecar."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import signal
import stat
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import tomllib

import psutil

from config import paths
from core.memory.everos import EverOSPort
from core.memory.types import MemoryErrorCode


logger = logging.getLogger(__name__)

_STARTUP_TIMEOUT_SECONDS = 30.0
_STOP_TIMEOUT_SECONDS = 10.0
_HEALTHY_RESET_SECONDS = 5 * 60.0
_RESTART_DELAYS_SECONDS = (1.0, 5.0, 30.0, 120.0)
_MAX_CONSECUTIVE_FAILURES = 5
_PROCESSING_PROBE_TIMEOUT_SECONDS = 20.0
_SOCKET_MODE = 0o600
_OWNER_DIR_MODE = 0o700
_SAFETY_MONITOR_INTERVAL_SECONDS = 0.2
_HEALTH_OBSERVATION_INTERVAL_SECONDS = 5.0


@dataclass(frozen=True)
class EverOSProcessSettings:
    """Non-persistent launch settings; keys only live in the child environment."""

    llm_base_url: str | None = None
    llm_model: str | None = None
    llm_api_key: str | None = field(default=None, repr=False)
    embedding_base_url: str | None = None
    embedding_model: str | None = None
    embedding_api_key: str | None = field(default=None, repr=False)
    timezone: str | None = None


class EverOSProcess:
    """Launch, supervise, and reap one privately owned EverOS child tree."""

    def __init__(
        self,
        python: Path | str,
        *,
        provider_root: Path | str | None = None,
        effective_home: Path | str | None = None,
        owner_id: str = "",
        settings: EverOSProcessSettings | None = None,
        socket_path: Path | str | None = None,
        startup_timeout_seconds: float = _STARTUP_TIMEOUT_SECONDS,
        stop_timeout_seconds: float = _STOP_TIMEOUT_SECONDS,
        on_ready: Callable[[], Awaitable[None] | None] | None = None,
    ) -> None:
        self._python = Path(python)
        self._effective_home = Path(effective_home) if effective_home is not None else paths.get_vibe_remote_dir()
        self._memory_dir = self._effective_home / "memory"
        self._provider_root = Path(provider_root) if provider_root is not None else self._memory_dir / "everos-root"
        self._socket_path = Path(socket_path) if socket_path is not None else self._memory_dir / ".rt" / "everos.sock"
        self._owner_id = owner_id
        self._settings = settings or EverOSProcessSettings()
        self._startup_timeout_seconds = _positive_timeout(startup_timeout_seconds, _STARTUP_TIMEOUT_SECONDS)
        self._stop_timeout_seconds = _positive_timeout(stop_timeout_seconds, _STOP_TIMEOUT_SECONDS)
        self._lifecycle_lock = asyncio.Lock()
        self._process: asyncio.subprocess.Process | None = None
        self._process_group: int | None = None
        self._watch_task: asyncio.Task[None] | None = None
        self._monitor_task: asyncio.Task[None] | None = None
        self._restart_task: asyncio.Task[None] | None = None
        self._owned_processes: dict[int, float] = {}
        self._on_ready = on_ready
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

    def set_settings(self, settings: EverOSProcessSettings, *, owner_id: str | None = None) -> None:
        """Replace launch settings before an explicit reconciliation/start."""

        self._settings = settings
        if owner_id is not None:
            self._owner_id = owner_id

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
            return await self._start_locked()

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

    async def processing_healthy(self) -> bool:
        """Probe processing from a short-lived child with the scrubbed key env."""

        if not self._python.is_file() or not _settings_complete(self._settings):
            return False
        try:
            probe = await asyncio.create_subprocess_exec(
                str(self._python),
                "-m",
                "core.memory.sidecar",
                "--probe-processing",
                cwd=str(self._effective_home),
                env=self._child_environment(),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
            )
        except (OSError, ValueError):
            logger.warning("EverOS processing probe could not start")
            return False

        process_group = _isolated_process_group(probe.pid)
        owned_processes = _snapshot_owned_processes(probe.pid, process_group)
        try:
            await asyncio.wait_for(probe.wait(), timeout=_PROCESSING_PROBE_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            logger.warning("EverOS processing probe timed out")
            try:
                await self._terminate_owned_tree(
                    probe,
                    process_group=process_group,
                    owned_processes=owned_processes,
                )
            except Exception:
                logger.warning("EverOS processing probe cleanup failed")
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
        return probe.returncode == 0

    async def _start_locked(self) -> bool:
        self._starting = True
        self._down = False
        self._last_error = None
        try:
            self._validate_launch_inputs()
            self._prepare_owned_directories()
            self._write_generated_config()
            self._remove_owned_socket()
            child_env = self._child_environment()
            process = await asyncio.create_subprocess_exec(
                str(self._python),
                "-m",
                "core.memory.sidecar",
                "--uds",
                str(self._socket_path),
                "--owner-id",
                self._owner_id,
                cwd=str(self._memory_dir),
                env=child_env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
            )
            self._process = process
            self._process_group = _isolated_process_group(process.pid)
            self._owned_processes = _snapshot_owned_processes(process.pid, self._process_group)
            if not _owned_process_identity_is_live(process.pid, self._owned_processes):
                raise RuntimeError("could not establish sidecar process ownership")
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
        except Exception:
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
            self._record_start_failure_locked()
            return False

    async def _wait_for_ready(self, process: asyncio.subprocess.Process) -> None:
        deadline = time.monotonic() + self._startup_timeout_seconds
        client = EverOSPort(self._socket_path, sidecar_timeout_seconds=2.0)
        while time.monotonic() < deadline:
            if process.returncode is not None:
                raise RuntimeError("sidecar exited before readiness")
            if not _owned_process_identity_is_live(process.pid, self._owned_processes):
                raise RuntimeError("sidecar ownership changed before readiness")
            _merge_owned_processes(
                self._owned_processes,
                _snapshot_owned_processes(process.pid, self._process_group),
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
            self._starting = False
            if healthy_since is not None and time.monotonic() - healthy_since >= _HEALTHY_RESET_SECONDS:
                self._consecutive_failures = 0
            if not self._desired_running:
                return
            self._record_start_failure_locked()

    async def _monitor_child(self, process: asyncio.subprocess.Process) -> None:
        """Keep tracking descendants and reject any later TCP listener."""

        try:
            client = EverOSPort(self._socket_path, sidecar_timeout_seconds=2.0)
            next_health_observation = time.monotonic()
            while process is self._process and process.returncode is None:
                if not _owned_process_identity_is_live(process.pid, self._owned_processes):
                    raise RuntimeError("sidecar ownership changed during monitoring")
                _merge_owned_processes(
                    self._owned_processes,
                    _snapshot_owned_processes(process.pid, self._process_group),
                )
                self._assert_no_tcp_listener(process.pid)
                observed_at = time.monotonic()
                if observed_at >= next_health_observation:
                    self._record_health_observation(await client.health(), observed_at=observed_at)
                    next_health_observation = observed_at + _HEALTH_OBSERVATION_INTERVAL_SECONDS
                await asyncio.sleep(_SAFETY_MONITOR_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            return
        except Exception:
            logger.warning("EverOS sidecar safety monitor rejected the child tree")
            async with self._lifecycle_lock:
                if process is not self._process:
                    return
                self._desired_running = False
                self._down = True
                self._last_error = "memory_sidecar_unavailable"
                try:
                    await self._terminate_owned_tree(
                        process,
                        process_group=self._process_group,
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
                await self._start_locked()
        except asyncio.CancelledError:
            return

    def _validate_launch_inputs(self) -> None:
        if os.name != "posix" or not self._python.is_file() or not self._owner_id:
            raise RuntimeError("invalid sidecar launch")
        if len(os.fsencode(self._socket_path)) + 1 > _socket_path_limit():
            raise RuntimeError("socket path exceeds sun_path")
        if not _settings_complete(self._settings):
            raise RuntimeError("processing settings incomplete")

    def _prepare_owned_directories(self) -> None:
        for directory in (
            self._memory_dir,
            self._memory_dir / ".rt",
            self._memory_dir / ".child-home",
            self._memory_dir / ".child-home" / ".cache",
            self._memory_dir / ".child-home" / ".config",
            self._memory_dir / ".child-home" / ".local" / "share",
            self._memory_dir / ".child-home" / ".local" / "state",
            self._memory_dir / "generated",
            self._memory_dir / "generated" / ".empty-ingest",
        ):
            _ensure_owner_directory(directory)
        _ensure_owner_directory(self._provider_root)

    def _write_generated_config(self) -> None:
        generated = self._memory_dir / "generated"
        ingest_dir = generated / ".empty-ingest"
        timezone_name = self._timezone_for_root()
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
                f"file_uri_allow_dirs = [{_toml_string(str(ingest_dir))}]",
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
        _validate_generated_config(everos_contents, ome_contents, timezone_name)
        for path, contents in (
            (generated / "everos.toml", everos_contents),
            (generated / "ome.toml", ome_contents),
            # EverOS 1.1.3 discovers its fixed filenames under EVEROS_ROOT.
            (self._provider_root / "everos.toml", everos_contents),
            (self._provider_root / "ome.toml", ome_contents),
        ):
            _write_private_text(path, contents)

    def _child_environment(self) -> dict[str, str]:
        child_home = self._memory_dir / ".child-home"
        source_root = Path(__file__).resolve().parents[2]
        settings = self._settings
        env = {
            "ENV": "prod",
            "HOME": str(child_home),
            "PATH": f"{self._python.parent}:/usr/bin:/bin",
            "PYTHONNOUSERSITE": "1",
            "PYTHONUNBUFFERED": "1",
            "PYTHONPATH": str(source_root),
            "XDG_CACHE_HOME": str(child_home / ".cache"),
            "XDG_CONFIG_HOME": str(child_home / ".config"),
            "XDG_DATA_HOME": str(child_home / ".local" / "share"),
            "XDG_STATE_HOME": str(child_home / ".local" / "state"),
            "EVEROS_ROOT": str(self._provider_root),
            "EVEROS_LLM__BASE_URL": str(settings.llm_base_url),
            "EVEROS_LLM__MODEL": str(settings.llm_model),
            "EVEROS_LLM__API_KEY": str(settings.llm_api_key),
            "EVEROS_EMBEDDING__BASE_URL": str(settings.embedding_base_url),
            "EVEROS_EMBEDDING__MODEL": str(settings.embedding_model),
            "EVEROS_EMBEDDING__API_KEY": str(settings.embedding_api_key),
        }
        # This is an explicit allowlist, so proxy/CA override variables are never
        # inherited from the parent service environment.
        return env

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

    def _assert_no_tcp_listener(self, pid: int) -> None:
        if not _owned_process_identity_is_live(pid, self._owned_processes):
            raise RuntimeError("sidecar ownership changed during listener inspection")
        _merge_owned_processes(
            self._owned_processes,
            _snapshot_owned_processes(pid, self._process_group),
        )
        for process_id in _live_owned_processes(self._owned_processes):
            try:
                connections = psutil.Process(process_id).net_connections(kind="inet")
            except (psutil.NoSuchProcess, psutil.ZombieProcess):
                continue
            except psutil.Error as exc:
                raise RuntimeError("could not inspect sidecar listeners") from exc
            if any(connection.status == psutil.CONN_LISTEN for connection in connections):
                raise RuntimeError("sidecar opened a TCP listener")

    async def _terminate_owned_tree(
        self,
        process: asyncio.subprocess.Process,
        *,
        process_group: int | None,
        owned_processes: Mapping[int, float] | None = None,
    ) -> None:
        identities = dict(owned_processes or {})
        if _owned_process_identity_is_live(process.pid, identities):
            _merge_owned_processes(identities, _snapshot_owned_processes(process.pid, process_group))
        _signal_owned_group_or_process(process, process_group, identities, signal.SIGTERM)
        _signal_owned_processes(identities, signal.SIGTERM)
        if await _wait_for_owned_exit(
            process,
            process_group=process_group,
            identities=identities,
            timeout_seconds=self._stop_timeout_seconds,
        ):
            return

        kill_signal = getattr(signal, "SIGKILL", signal.SIGTERM)
        if _owned_process_identity_is_live(process.pid, identities):
            _merge_owned_processes(identities, _snapshot_owned_processes(process.pid, process_group))
        _signal_owned_group_or_process(process, process_group, identities, kill_signal)
        _signal_owned_processes(identities, kill_signal)
        if await _wait_for_owned_exit(
            process,
            process_group=process_group,
            identities=identities,
            timeout_seconds=min(self._stop_timeout_seconds, 3.0),
        ):
            return
        raise RuntimeError("sidecar process tree did not exit")

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

    def _timezone_for_root(self) -> str:
        configured = _iana_timezone(self._settings.timezone)
        if configured is not None:
            return configured
        existing = _root_timezone(self._provider_root / "everos.toml")
        return existing or _local_iana_timezone()


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
        except (OSError, psutil.Error):
            continue
    return identities


def _merge_owned_processes(identities: dict[int, float], discovered: Mapping[int, float]) -> None:
    """Add newly seen children without changing a captured process identity."""

    for process_id, created_at in discovered.items():
        identities.setdefault(process_id, created_at)


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


def _signal_owned_group_or_process(
    process: asyncio.subprocess.Process,
    process_group: int | None,
    identities: Mapping[int, float],
    signum: int,
) -> None:
    if (
        process_group is not None
        and hasattr(os, "killpg")
        and _group_contains_only_confirmed_owned_processes(process_group, identities)
    ):
        try:
            os.killpg(process_group, signum)
            return
        except ProcessLookupError:
            return
        except OSError:
            pass
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


def _validate_generated_config(everos_contents: str, ome_contents: str, timezone: str) -> None:
    try:
        everos = tomllib.loads(everos_contents)
        ome = tomllib.loads(ome_contents)
    except tomllib.TOMLDecodeError as exc:
        raise RuntimeError("invalid generated EverOS config") from exc
    if (
        everos.get("memory", {}).get("timezone") != timezone
        or everos.get("memorize", {}).get("mode") != "chat"
        or everos.get("rerank", {}).get("model") != ""
        or everos.get("rerank", {}).get("base_url") != ""
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
