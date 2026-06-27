"""Resident avault agent socket client.

The resident agent is the protected-tier delivery boundary: Python sends names,
sealed envelopes, scopes, and browser-sealed DEK blind boxes over a Unix socket.
Plaintext and DEKs stay inside ``avault``.
"""

from __future__ import annotations

import json
import socket
import subprocess
import stat
import threading
import time
from pathlib import Path
from typing import Any, Callable

from config import paths
from core.process_isolation import isolated_subprocess_kwargs
from vibe.i18n import t as backend_t

MAX_AGENT_FRAME_BYTES = 1024 * 1024
DEFAULT_AGENT_SOCKET_TIMEOUT = 20.0
DEFAULT_AGENT_START_TIMEOUT = 5.0
DEFAULT_AGENT_IDLE_TIMEOUT_SECS = 900


class AvaultAgentError(Exception):
    """The resident avault agent failed a request."""


class AvaultAgentClient:
    """Length-prefixed JSON client for ``avault agent``."""

    def __init__(
        self,
        socket_path: str | Path | None = None,
        *,
        timeout: float | None = DEFAULT_AGENT_SOCKET_TIMEOUT,
        ensure_agent: Callable[[], None] | None = None,
    ) -> None:
        self.socket_path = Path(socket_path) if socket_path is not None else default_agent_socket_path()
        self.timeout = timeout
        self._ensure_agent = ensure_agent

    def request(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise AvaultAgentError("agent request payload must be an object")
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        if not body or len(body) > MAX_AGENT_FRAME_BYTES:
            raise AvaultAgentError("agent request frame size is invalid")
        response = self._round_trip(body)
        if not isinstance(response, dict):
            raise AvaultAgentError("agent returned malformed response")
        if response.get("ok") is not True:
            error = response.get("error")
            raise AvaultAgentError(str(error or "avault agent request failed"))
        result = response.get("result")
        if not isinstance(result, dict):
            raise AvaultAgentError("agent returned malformed response")
        return result

    def _round_trip(self, body: bytes) -> Any:
        last_error: OSError | None = None
        for attempt in range(2):
            if attempt == 1 and self._ensure_agent is None:
                break
            if attempt == 1:
                self._ensure_agent()
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                    if self.timeout is not None:
                        sock.settimeout(self.timeout)
                    sock.connect(str(self.socket_path))
                    _write_frame(sock, body)
                    return _read_json_frame(sock)
            except FileNotFoundError as exc:
                last_error = exc
            except ConnectionRefusedError as exc:
                last_error = exc
            except OSError as exc:
                last_error = exc
                if attempt == 1:
                    break
        detail = f": {last_error}" if last_error else ""
        raise AvaultAgentError(f"failed to connect to avault agent{detail}")

    def pubkey(self) -> dict[str, Any]:
        return self.request({"type": "pubkey"})

    def grant(
        self,
        *,
        scope_type: str,
        scope_ref: str,
        ttl_secs: int,
        deks: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return self.request(
            {
                "type": "grant",
                "scope_type": scope_type,
                "scope_ref": scope_ref,
                "ttl_secs": ttl_secs,
                "deks": deks,
            }
        )

    def release(self, *, scope_type: str, scope_ref: str) -> dict[str, Any]:
        return self.request({"type": "release", "scope_type": scope_type, "scope_ref": scope_ref})

    def deliver_run(
        self,
        *,
        scope_type: str,
        scope_ref: str,
        command: list[str],
        secrets: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return self.request(
            {
                "type": "deliver",
                "scope_type": scope_type,
                "scope_ref": scope_ref,
                "mode": "run",
                "command": command,
                "secrets": secrets,
            }
        )

    def deliver_fetch(
        self,
        *,
        scope_type: str,
        scope_ref: str,
        name: str,
        envelope: dict[str, Any],
        request: dict[str, Any],
    ) -> dict[str, Any]:
        return self.request(
            {
                "type": "deliver",
                "scope_type": scope_type,
                "scope_ref": scope_ref,
                "mode": "fetch",
                "name": name,
                "envelope": envelope,
                "request": request,
            }
        )

    def deliver_inject(
        self,
        *,
        scope_type: str,
        scope_ref: str,
        path: str,
        fmt: str,
        secrets: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return self.request(
            {
                "type": "deliver",
                "scope_type": scope_type,
                "scope_ref": scope_ref,
                "mode": "inject",
                "path": path,
                "format": fmt,
                "secrets": secrets,
            }
        )


class AvaultAgentManager:
    """Start-on-demand supervisor for the long-lived resident agent."""

    def __init__(
        self,
        *,
        socket_path: str | Path | None = None,
        binary_resolver: Callable[[], str] | None = None,
        command_env: Callable[[str], dict[str, str] | None] | None = None,
        idle_timeout_secs: int = DEFAULT_AGENT_IDLE_TIMEOUT_SECS,
        start_timeout: float = DEFAULT_AGENT_START_TIMEOUT,
    ) -> None:
        self.socket_path = Path(socket_path) if socket_path is not None else default_agent_socket_path()
        self._binary_resolver = binary_resolver or _missing_binary_resolver
        self._command_env = command_env
        self.idle_timeout_secs = idle_timeout_secs
        self.start_timeout = start_timeout
        self._lock = threading.RLock()
        self._output_lock = threading.Lock()
        self._process: subprocess.Popen | None = None
        self._stdout = bytearray()
        self._stderr = bytearray()

    def client(self, *, timeout: float | None = DEFAULT_AGENT_SOCKET_TIMEOUT) -> AvaultAgentClient:
        return AvaultAgentClient(self.socket_path, timeout=timeout, ensure_agent=self.ensure_running)

    def ensure_running(self) -> None:
        if self._socket_responds():
            return
        with self._lock:
            if self._socket_responds():
                return
            if self._process is not None and self._process.poll() is None:
                self._terminate_process_locked()
            self._spawn_locked()
            self._wait_for_socket_locked()

    def reset(self) -> None:
        with self._lock:
            self._terminate_process_locked()

    def _spawn_locked(self) -> None:
        binary = self._binary_resolver()
        _ensure_agent_socket_parent(self.socket_path.parent)
        _remove_stale_agent_socket(self.socket_path)
        try:
            self._process = subprocess.Popen(
                [
                    binary,
                    "agent",
                    "--store",
                    "file",
                    "--socket",
                    str(self.socket_path),
                    "--idle-timeout-secs",
                    str(self.idle_timeout_secs),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self._command_env(binary) if self._command_env else None,
                **isolated_subprocess_kwargs(),
            )
        except FileNotFoundError as exc:
            raise AvaultAgentError("avault binary not found") from exc
        _start_pipe_reader(self._process.stdout, self._stdout, self._output_lock)
        _start_pipe_reader(self._process.stderr, self._stderr, self._output_lock)

    def _wait_for_socket_locked(self) -> None:
        deadline = time.monotonic() + self.start_timeout
        while time.monotonic() < deadline:
            if self._process is not None and self._process.poll() is not None:
                code = self._process.returncode
                self._process = None
                raise AvaultAgentError(f"avault agent exited during startup with code {code}")
            if self._socket_responds():
                return
            time.sleep(0.05)
        self._terminate_process_locked()
        raise AvaultAgentError("timed out waiting for avault agent socket")

    def _socket_responds(self) -> bool:
        try:
            client = AvaultAgentClient(self.socket_path, timeout=0.5)
            client.pubkey()
            return True
        except AvaultAgentError:
            return False

    def _terminate_process_locked(self) -> None:
        proc = self._process
        self._process = None
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)

    def request_with_output(self, request: Callable[[AvaultAgentClient], dict[str, Any]]) -> tuple[dict[str, Any], dict[str, bytes]]:
        with self._lock:
            self._clear_output()
            result = request(self.client(timeout=None))
            output = self._drain_output()
        return result, output

    def _clear_output(self) -> None:
        with self._output_lock:
            self._stdout.clear()
            self._stderr.clear()

    def _drain_output(self) -> dict[str, bytes]:
        _wait_for_output_idle(self._output_lock, self._stdout, self._stderr)
        with self._output_lock:
            output = {"stdout": bytes(self._stdout), "stderr": bytes(self._stderr)}
            self._stdout.clear()
            self._stderr.clear()
        return output


def default_agent_socket_path() -> Path:
    return paths.get_vibe_remote_dir() / "run" / "avault.sock"


def _ensure_agent_socket_parent(path: Path) -> None:
    avibe_home = paths.get_vibe_remote_dir()
    avibe_home.mkdir(parents=True, exist_ok=True)
    avibe_home.chmod(0o700)
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)


def _remove_stale_agent_socket(path: Path) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(mode):
        return
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.2)
            sock.connect(str(path))
            return
    except OSError:
        path.unlink(missing_ok=True)


def _missing_binary_resolver() -> str:
    raise AvaultAgentError(backend_t("dependencies.avault.missing"))


def _write_frame(sock: socket.socket, body: bytes) -> None:
    sock.sendall(len(body).to_bytes(4, "big"))
    sock.sendall(body)


def _read_json_frame(sock: socket.socket) -> Any:
    length_bytes = _read_exact(sock, 4)
    length = int.from_bytes(length_bytes, "big")
    if length <= 0 or length > MAX_AGENT_FRAME_BYTES:
        raise AvaultAgentError("agent response frame size is invalid")
    body = _read_exact(sock, length)
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise AvaultAgentError("agent returned malformed JSON") from exc


def _read_exact(sock: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise AvaultAgentError("agent closed the socket unexpectedly")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _start_pipe_reader(pipe, buffer: bytearray, lock: threading.Lock) -> None:
    if pipe is None:
        return

    def _reader() -> None:
        with pipe:
            while chunk := pipe.read(8192):
                with lock:
                    buffer.extend(chunk)

    threading.Thread(target=_reader, daemon=True).start()


def _wait_for_output_idle(lock: threading.Lock, stdout: bytearray, stderr: bytearray) -> None:
    previous = (-1, -1)
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        with lock:
            current = (len(stdout), len(stderr))
        if current == previous:
            return
        previous = current
        time.sleep(0.01)
