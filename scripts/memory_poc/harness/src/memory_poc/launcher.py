from __future__ import annotations

import os
import secrets
import signal
import stat
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping

import psutil

from .environment import ProviderSettings, child_environment, verify_locked_environment
from .errors import LaunchError
from .paths import ensure_owner_directory
from .provider import EverOSClient


def _socket_path_limit() -> int:
    return 104 if sys.platform == "darwin" else 108


def validate_socket_path(path: Path) -> None:
    encoded_length = len(os.fsencode(path)) + 1
    if encoded_length > _socket_path_limit():
        raise LaunchError("uds_path_too_long")


def secure_socket(path: Path) -> None:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISSOCK(info.st_mode):
        raise LaunchError("uds_path_not_socket")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise LaunchError("uds_owner_mismatch")
    os.chmod(path, 0o600)
    verified = path.lstat()
    if stat.S_IMODE(verified.st_mode) != 0o600:
        raise LaunchError("uds_mode_invalid")


def _owned_process_tree_pids(pid: int) -> tuple[int, ...]:
    try:
        root = psutil.Process(pid)
        processes = [root, *root.children(recursive=True)]
    except psutil.NoSuchProcess:
        return ()
    except psutil.Error as exc:
        raise LaunchError("tcp_listener_probe_failed") from exc
    return tuple(dict.fromkeys(process.pid for process in processes))


def assert_no_tcp_listener(
    pid: int,
    *,
    connection_provider: Callable[[int], list[object]] | None = None,
    process_ids_provider: Callable[[int], tuple[int, ...]] | None = None,
) -> None:
    """Reject a listening TCP socket in the owned process tree."""
    provider = connection_provider or (lambda child_pid: psutil.Process(child_pid).net_connections(kind="inet"))
    process_ids = (process_ids_provider or _owned_process_tree_pids)(pid)
    for process_id in process_ids:
        try:
            connections = provider(process_id)
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
        except (psutil.AccessDenied, psutil.Error) as exc:
            raise LaunchError("tcp_listener_probe_failed") from exc
        for connection in connections:
            if getattr(connection, "status", None) == psutil.CONN_LISTEN:
                raise LaunchError("tcp_listener_detected")


def new_socket_path(state_root: Path) -> Path:
    socket_dir = ensure_owner_directory(state_root / "s", anchor=state_root)
    path = socket_dir / f"{secrets.token_hex(8)}.sock"
    validate_socket_path(path)
    return path


def _signal_owned_process_group(process: subprocess.Popen[bytes], signum: int) -> None:
    """Signal the isolated sidecar group, falling back to its direct process."""
    if process.poll() is not None:
        return
    if os.name == "posix" and hasattr(os, "getpgid") and hasattr(os, "killpg"):
        try:
            process_group = os.getpgid(process.pid)
            if process_group != os.getpgrp():
                os.killpg(process_group, signum)
                return
        except ProcessLookupError:
            return
        except OSError:
            pass
    try:
        process.send_signal(signum)
    except ProcessLookupError:
        return


def _snapshot_owned_processes(process: subprocess.Popen[bytes]) -> dict[int, float]:
    """Capture the owned process tree by PID and creation time while it is linked."""
    if process.poll() is not None:
        return {}
    try:
        root = psutil.Process(process.pid)
        candidates = [root, *root.children(recursive=True)]
    except psutil.Error:
        return {}
    identities: dict[int, float] = {}
    for candidate in candidates:
        try:
            identities[candidate.pid] = candidate.create_time()
        except psutil.Error:
            continue
    return identities


def _isolated_process_group(process: subprocess.Popen[bytes]) -> int | None:
    if os.name != "posix" or not hasattr(os, "getpgid") or process.poll() is not None:
        return None
    try:
        process_group = os.getpgid(process.pid)
    except OSError:
        return None
    return process_group if process_group != os.getpgrp() else None


def _snapshot_process_group(process_group: int | None) -> dict[int, float]:
    if process_group is None or os.name != "posix" or not hasattr(os, "getpgid"):
        return {}
    identities: dict[int, float] = {}
    for candidate in psutil.process_iter():
        try:
            if os.getpgid(candidate.pid) != process_group:
                continue
            identities[candidate.pid] = candidate.create_time()
        except (OSError, psutil.Error):
            continue
    return identities


def _signal_process_group(process_group: int, signum: int) -> None:
    if os.name != "posix" or not hasattr(os, "killpg") or process_group == os.getpgrp():
        return
    try:
        os.killpg(process_group, signum)
    except ProcessLookupError:
        return


def _live_owned_processes(identities: Mapping[int, float]) -> dict[int, float]:
    live: dict[int, float] = {}
    for process_id, created_at in identities.items():
        try:
            candidate = psutil.Process(process_id)
            if abs(candidate.create_time() - created_at) > 0.001:
                continue
            if candidate.status() == psutil.STATUS_ZOMBIE:
                continue
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied:
            # A child we cannot inspect cannot be assumed to have exited.
            pass
        except psutil.Error:
            continue
        live[process_id] = created_at
    return live


def _signal_owned_processes(identities: Mapping[int, float], signum: int) -> None:
    for process_id in _live_owned_processes(identities):
        try:
            os.kill(process_id, signum)
        except ProcessLookupError:
            continue
        except PermissionError as exc:
            raise LaunchError("sidecar_process_signal_failed") from exc


class _TcpListenerMonitor:
    """Continuously verify that the owned sidecar tree remains UDS-only."""

    def __init__(
        self,
        process: subprocess.Popen[bytes],
        *,
        process_group: int | None = None,
        interval_seconds: float = 0.05,
    ) -> None:
        self._process = process
        self._process_group = process_group
        self._interval_seconds = interval_seconds
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._failure: LaunchError | None = None
        self._known_processes: dict[int, float] = {}
        self._thread = threading.Thread(target=self._run, name="memory-poc-tcp-monitor", daemon=True)

    def start(self) -> None:
        self._check_once()
        self._thread.start()

    def assert_safe(self) -> None:
        self._raise_if_failed()
        self._check_once()
        self._raise_if_failed()

    def stop(self) -> dict[int, float]:
        final_probe_error: LaunchError | None = None
        try:
            self.assert_safe()
        except LaunchError as exc:
            final_probe_error = exc
        self._stop_event.set()
        if self._thread.is_alive():
            self._thread.join(timeout=max(self._interval_seconds * 4, 0.2))
        if final_probe_error is not None:
            raise final_probe_error
        if self._thread.is_alive():
            raise LaunchError("tcp_listener_monitor_shutdown_timeout")
        self._raise_if_failed()
        return self.known_processes()

    def known_processes(self) -> dict[int, float]:
        with self._lock:
            return dict(self._known_processes)

    @property
    def process_group(self) -> int | None:
        return self._process_group

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._check_once()
            except LaunchError as exc:
                with self._lock:
                    self._failure = exc
                if self._process_group is not None and _snapshot_process_group(self._process_group):
                    _signal_process_group(self._process_group, signal.SIGTERM)
                else:
                    _signal_owned_process_group(self._process, signal.SIGTERM)
                return
            self._stop_event.wait(self._interval_seconds)

    def _check_once(self) -> None:
        identities = _snapshot_owned_processes(self._process)
        identities.update(_snapshot_process_group(self._process_group))
        with self._lock:
            self._known_processes.update(identities)
            known_processes = dict(self._known_processes)
        process_ids = tuple(_live_owned_processes(known_processes))
        if process_ids:
            assert_no_tcp_listener(self._process.pid, process_ids_provider=lambda _pid: process_ids)

    def _raise_if_failed(self) -> None:
        with self._lock:
            failure = self._failure
        if failure is not None:
            raise failure


def terminate_owned_process(
    process: subprocess.Popen[bytes],
    *,
    timeout_seconds: float = 10.0,
    known_processes: Mapping[int, float] | None = None,
    process_group: int | None = None,
) -> None:
    """Terminate the launcher-owned process and every tracked descendant."""
    identities = dict(known_processes or {})
    owned_group = process_group if process_group is not None else _isolated_process_group(process)
    identities.update(_snapshot_owned_processes(process))
    group_members = _snapshot_process_group(owned_group)
    identities.update(group_members)
    deadline = time.monotonic() + max(timeout_seconds, 0.1)

    if group_members and owned_group is not None:
        _signal_process_group(owned_group, signal.SIGTERM)
    else:
        _signal_owned_process_group(process, signal.SIGTERM)
    _signal_owned_processes(identities, signal.SIGTERM)
    while time.monotonic() < deadline:
        identities.update(_snapshot_owned_processes(process))
        identities.update(_snapshot_process_group(owned_group))
        if not _live_owned_processes(identities):
            process.poll()
            return
        time.sleep(0.05)

    kill_signal = getattr(signal, "SIGKILL", signal.SIGTERM)
    group_members = _snapshot_process_group(owned_group)
    if group_members and owned_group is not None:
        _signal_process_group(owned_group, kill_signal)
    else:
        _signal_owned_process_group(process, kill_signal)
    _signal_owned_processes(identities, kill_signal)
    kill_deadline = time.monotonic() + max(min(timeout_seconds, 2.0), 0.5)
    while time.monotonic() < kill_deadline:
        identities.update(_snapshot_process_group(owned_group))
        if not _live_owned_processes(identities):
            process.poll()
            return
        time.sleep(0.05)
    raise LaunchError("sidecar_process_termination_failed")


@dataclass
class EverOSProcess:
    python: Path
    everos_root: Path
    child_home: Path
    state_root: Path
    settings: ProviderSettings
    metrics_path: Path
    owner_id: str
    timeout_seconds: float = 30.0
    process: subprocess.Popen[bytes] | None = field(default=None, init=False)
    socket_path: Path | None = field(default=None, init=False)
    tcp_monitor: _TcpListenerMonitor | None = field(default=None, init=False)

    def start(self) -> EverOSClient:
        if os.name != "posix":
            raise LaunchError("uds_launcher_requires_posix")
        verify_locked_environment(self.python)
        ensure_owner_directory(self.everos_root, anchor=self.state_root)
        ensure_owner_directory(self.child_home, anchor=self.state_root)
        self.socket_path = new_socket_path(self.state_root)
        child_env = child_environment(
            self.settings,
            python=self.python,
            everos_root=self.everos_root,
            child_home=self.child_home,
            metrics_path=self.metrics_path,
            owner_id=self.owner_id,
            anchor=self.state_root,
        )
        try:
            self.process = subprocess.Popen(
                [str(self.python), "-m", "memory_poc.sidecar", "--uds", str(self.socket_path)],
                cwd=self.everos_root.parent,
                env=child_env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            self.tcp_monitor = _TcpListenerMonitor(self.process, process_group=_isolated_process_group(self.process))
            self.tcp_monitor.start()
            self._wait_for_socket()
            secure_socket(self.socket_path)
            client = EverOSClient(
                self.socket_path,
                timeout_seconds=self.timeout_seconds,
                safety_check=self.tcp_monitor.assert_safe,
            )
            client.health()
            self.tcp_monitor.assert_safe()
            return client
        except Exception:
            self.stop()
            raise

    def _wait_for_socket(self) -> None:
        assert self.process is not None
        assert self.socket_path is not None
        deadline = time.monotonic() + self.timeout_seconds
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise LaunchError("sidecar_exited_before_ready")
            if self.socket_path.exists():
                return
            time.sleep(0.05)
        raise LaunchError("sidecar_socket_timeout")

    def stop(self) -> None:
        process = self.process
        self.process = None
        monitor = self.tcp_monitor
        self.tcp_monitor = None
        known_processes: dict[int, float] = {}
        monitor_error: LaunchError | None = None
        if monitor is not None:
            try:
                known_processes = monitor.stop()
            except LaunchError as exc:
                monitor_error = exc
                known_processes = monitor.known_processes()
        termination_error: LaunchError | None = None
        try:
            if process is not None:
                terminate_owned_process(
                    process,
                    known_processes=known_processes,
                    process_group=monitor.process_group if monitor is not None else None,
                )
        except LaunchError as exc:
            termination_error = exc
        finally:
            if self.socket_path is not None:
                try:
                    info = self.socket_path.lstat()
                    if stat.S_ISSOCK(info.st_mode) and (not hasattr(os, "getuid") or info.st_uid == os.getuid()):
                        self.socket_path.unlink()
                except FileNotFoundError:
                    pass
                finally:
                    self.socket_path = None
        if termination_error is not None:
            raise termination_error
        if monitor_error is not None:
            raise monitor_error

    def __enter__(self) -> EverOSClient:
        return self.start()

    def __exit__(self, *_: object) -> None:
        self.stop()
