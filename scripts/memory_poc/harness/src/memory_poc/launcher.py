from __future__ import annotations

import os
import secrets
import signal
import stat
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

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


def assert_no_tcp_listener(pid: int, *, connection_provider: Callable[[int], list[object]] | None = None) -> None:
    provider = connection_provider or (lambda child_pid: psutil.Process(child_pid).net_connections(kind="inet"))
    try:
        connections = provider(pid)
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
        if signum == signal.SIGTERM:
            process.terminate()
        else:
            process.kill()
    except ProcessLookupError:
        return


def terminate_owned_process(process: subprocess.Popen[bytes], *, timeout_seconds: float = 10.0) -> None:
    """Terminate the launcher-owned process and descendants in its private group."""
    if process.poll() is not None:
        return
    _signal_owned_process_group(process, signal.SIGTERM)
    try:
        process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        _signal_owned_process_group(process, getattr(signal, "SIGKILL", signal.SIGTERM))
        process.wait(timeout=timeout_seconds)


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

    def start(self) -> EverOSClient:
        if os.name != "posix":
            raise LaunchError("uds_launcher_requires_posix")
        verify_locked_environment(self.python)
        ensure_owner_directory(self.everos_root, anchor=self.state_root)
        ensure_owner_directory(self.child_home, anchor=self.state_root)
        self.socket_path = new_socket_path(self.state_root)
        child_env = child_environment(
            self.settings,
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
            self._wait_for_socket()
            secure_socket(self.socket_path)
            client = EverOSClient(self.socket_path, timeout_seconds=self.timeout_seconds)
            client.health()
            assert_no_tcp_listener(self.process.pid)
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
        if process is not None:
            terminate_owned_process(process)
        if self.socket_path is not None:
            try:
                info = self.socket_path.lstat()
                if stat.S_ISSOCK(info.st_mode) and (not hasattr(os, "getuid") or info.st_uid == os.getuid()):
                    self.socket_path.unlink()
            except FileNotFoundError:
                pass
            finally:
                self.socket_path = None

    def __enter__(self) -> EverOSClient:
        return self.start()

    def __exit__(self, *_: object) -> None:
        self.stop()
