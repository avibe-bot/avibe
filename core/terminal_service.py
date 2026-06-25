from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import logging
import os
import re
import signal
import struct
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from config import paths

try:  # POSIX-only; the PTY + tmux terminal is not supported on native Windows.
    import fcntl
    import termios
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]
    termios = None  # type: ignore[assignment]

try:
    from core.tmux_runtime import resolve_tmux_binary
except Exception:  # pragma: no cover - integration branch may not be present.

    def resolve_tmux_binary() -> str | None:
        return None


logger = logging.getLogger(__name__)

# The terminal needs a PTY (os.openpty) + ioctl winsize; all POSIX-only. On native
# Windows the websocket endpoint refuses instead of crashing at import/spawn time.
TERMINAL_SUPPORTED = hasattr(os, "openpty") and fcntl is not None and termios is not None

_SAFE_SESSION_ID_RE = re.compile(r"[^A-Za-z0-9_-]+")


@dataclass
class TerminalConnection:
    session_id: str
    process: asyncio.subprocess.Process
    master_fd: int
    persistent: bool
    attached_at: float
    last_seen: float

    def touch(self) -> None:
        self.last_seen = time.monotonic()


class TerminalService:
    def __init__(self, *, idle_timeout_seconds: int = 3600, max_sessions: int = 8) -> None:
        self.idle_timeout_seconds = max(60, int(idle_timeout_seconds))
        self.max_sessions = max(1, int(max_sessions))
        self._connections: dict[str, TerminalConnection] = {}
        self._detached_tmux_sessions: dict[str, float] = {}
        self._reserved_sessions: set[str] = set()
        self._lock = asyncio.Lock()
        self._reaper_task: asyncio.Task | None = None
        self._closed = False

    def start_reaper(self) -> None:
        if self._closed:
            return
        task = self._reaper_task
        if task is None or task.done():
            self._reaper_task = asyncio.create_task(self._reaper_loop(), name="terminal-session-reaper")

    async def shutdown(self) -> None:
        self._closed = True
        task, self._reaper_task = self._reaper_task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        async with self._lock:
            connections = list(self._connections.values())
            self._connections.clear()
        await asyncio.gather(*(self._cleanup_connection(connection) for connection in connections), return_exceptions=True)

    async def handle_websocket(self, websocket: WebSocket, raw_session_id: str) -> None:
        session_id = sanitize_session_id(raw_session_id)
        connection = await self.open(session_id)
        await websocket.send_text(json.dumps({"type": "ready", "persistent": connection.persistent}))
        output_task = asyncio.create_task(self._pump_output(websocket, connection), name=f"terminal-output-{session_id}")
        input_task = asyncio.create_task(self._pump_input(websocket, connection), name=f"terminal-input-{session_id}")
        wait_task = asyncio.create_task(connection.process.wait(), name=f"terminal-process-{session_id}")
        exit_code: int | None = None
        try:
            while True:
                done, _pending = await asyncio.wait(
                    {output_task, input_task, wait_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if wait_task in done:
                    exit_code = await wait_task
                    input_task.cancel()
                    await asyncio.gather(input_task, return_exceptions=True)
                    try:
                        await asyncio.wait_for(output_task, timeout=0.5)
                    except asyncio.TimeoutError:
                        output_task.cancel()
                        await asyncio.gather(output_task, return_exceptions=True)
                    except WebSocketDisconnect:
                        pass
                    await _send_exit_status(websocket, exit_code)
                    break
                if input_task in done:
                    try:
                        await input_task
                    except WebSocketDisconnect:
                        output_task.cancel()
                        wait_task.cancel()
                        await asyncio.gather(output_task, wait_task, return_exceptions=True)
                        break
                    output_task.cancel()
                    wait_task.cancel()
                    await asyncio.gather(output_task, wait_task, return_exceptions=True)
                    break
                if output_task in done:
                    try:
                        await output_task
                    except WebSocketDisconnect:
                        input_task.cancel()
                        wait_task.cancel()
                        await asyncio.gather(input_task, wait_task, return_exceptions=True)
                        break
                    output_task = asyncio.create_task(
                        self._wait_for_process_exit(websocket, connection),
                        name=f"terminal-output-drain-{session_id}",
                    )
        finally:
            await self.close(connection)

    async def open(self, session_id: str) -> TerminalConnection:
        # If this session id is already attached, replace it — close the old
        # connection first so its PTY/process isn't orphaned (an orphan would
        # also slip past the max_sessions cap).
        async with self._lock:
            existing = self._connections.get(session_id)
        if existing is not None:
            await self.close(existing)
        async with self._lock:
            self._forget_finished_locked()
            # Reclaim this id's detached slot BEFORE the cap check, so reconnecting
            # to an existing (detached) session is never rejected as "too many".
            self._detached_tmux_sessions.pop(session_id, None)
            if session_id in self._reserved_sessions:
                raise TerminalServiceError("session_opening")
            active_count = len(self._connections) + len(self._detached_tmux_sessions) + len(self._reserved_sessions)
            if active_count >= self.max_sessions:
                raise TerminalServiceError("too_many_sessions")
            self._reserved_sessions.add(session_id)
        try:
            persistent = False
            tmux_binary = resolve_tmux_binary()
            if tmux_binary:
                cmd = _tmux_launch_command(tmux_binary, session_id)
                persistent = True
            else:
                cmd = [os.environ.get("SHELL") or "/bin/bash", "-l"]

            master_fd, slave_fd = os.openpty()
            try:
                process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdin=slave_fd,
                    stdout=slave_fd,
                    stderr=slave_fd,
                    cwd=str(Path.home()),
                    env=_spawn_env(persistent=persistent),
                    preexec_fn=os.setsid if hasattr(os, "setsid") else None,
                )
            except Exception:
                _close_fd(master_fd)
                raise
            finally:
                _close_fd(slave_fd)

            connection = TerminalConnection(
                session_id=session_id,
                process=process,
                master_fd=master_fd,
                persistent=persistent,
                attached_at=time.monotonic(),
                last_seen=time.monotonic(),
            )
            async with self._lock:
                self._reserved_sessions.discard(session_id)
                self._connections[session_id] = connection
            return connection
        except Exception:
            async with self._lock:
                self._reserved_sessions.discard(session_id)
            raise

    async def close(self, connection: TerminalConnection) -> None:
        async with self._lock:
            if self._connections.get(connection.session_id) is connection:
                self._connections.pop(connection.session_id, None)
        await self._cleanup_connection(connection)

    async def _cleanup_connection(self, connection: TerminalConnection) -> None:
        _close_fd(connection.master_fd)
        if connection.process.returncode is not None:
            return
        if connection.persistent:
            await _terminate_process(connection.process, signal.SIGHUP)
            async with self._lock:
                self._detached_tmux_sessions[connection.session_id] = time.monotonic()
        else:
            await _terminate_process(connection.process, signal.SIGTERM)

    async def resize(self, connection: TerminalConnection, cols: int, rows: int) -> None:
        rows = max(1, min(int(rows), 1000))
        cols = max(1, min(int(cols), 1000))
        payload = struct.pack("HHHH", rows, cols, 0, 0)
        await asyncio.to_thread(fcntl.ioctl, connection.master_fd, termios.TIOCSWINSZ, payload)
        connection.touch()

    async def _write_all(self, fd: int, data: bytes) -> None:
        # master_fd is non-blocking (see _pump_output), so a single os.write can
        # accept only part of a large frame or raise EAGAIN (easy to hit by pasting
        # a long command). Loop until every byte is written.
        view = memoryview(data)
        while view:
            try:
                written = os.write(fd, view)
            except BlockingIOError:
                await asyncio.sleep(0.005)
                continue
            view = view[written:]

    async def _pump_input(self, websocket: WebSocket, connection: TerminalConnection) -> None:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                raise WebSocketDisconnect(message.get("code", 1000))
            if bytes_payload := message.get("bytes"):
                await self._write_all(connection.master_fd, bytes_payload)
                connection.touch()
            elif text_payload := message.get("text"):
                await self._handle_control_message(connection, text_payload)

    async def _handle_control_message(self, connection: TerminalConnection, payload: str) -> None:
        try:
            message = json.loads(payload)
        except json.JSONDecodeError:
            return
        if not isinstance(message, dict) or message.get("type") != "resize":
            return
        try:
            cols = int(message.get("cols"))
            rows = int(message.get("rows"))
        except (TypeError, ValueError):
            return
        await self.resize(connection, cols, rows)

    async def _pump_output(self, websocket: WebSocket, connection: TerminalConnection) -> None:
        os.set_blocking(connection.master_fd, False)
        while True:
            try:
                chunk = os.read(connection.master_fd, 8192)
            except BlockingIOError:
                if connection.process.returncode is not None:
                    return
                await asyncio.sleep(0.02)
                continue
            except OSError as err:
                if err.errno == errno.EIO:
                    return
                raise
            if not chunk:
                if connection.process.returncode is not None:
                    return
                await asyncio.sleep(0.02)
                continue
            connection.touch()
            await websocket.send_bytes(chunk)

    async def _wait_for_process_exit(self, websocket: WebSocket, connection: TerminalConnection) -> None:
        while connection.process.returncode is None:
            await asyncio.sleep(0.05)

    def _forget_finished_locked(self) -> None:
        for session_id, connection in list(self._connections.items()):
            if connection.process.returncode is not None:
                self._connections.pop(session_id, None)

    async def _reaper_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            await self.reap_idle()

    async def reap_idle(self) -> None:
        cutoff = time.monotonic() - self.idle_timeout_seconds
        async with self._lock:
            expired = [
                connection
                for connection in self._connections.values()
                if connection.last_seen < cutoff or connection.process.returncode is not None
            ]
            for connection in expired:
                self._connections.pop(connection.session_id, None)
            expired_tmux_sessions = [
                session_id for session_id, last_seen in self._detached_tmux_sessions.items() if last_seen < cutoff
            ]
            for session_id in expired_tmux_sessions:
                self._detached_tmux_sessions.pop(session_id, None)
        await asyncio.gather(*(self._cleanup_connection(connection) for connection in expired), return_exceptions=True)
        await asyncio.gather(*(_kill_tmux_session(session_id) for session_id in expired_tmux_sessions), return_exceptions=True)


class TerminalServiceError(Exception):
    pass


def sanitize_session_id(raw_session_id: str) -> str:
    safe = _SAFE_SESSION_ID_RE.sub("_", raw_session_id.strip())[:80].strip("_-")
    return safe or "terminal"


def _tmux_launch_command(tmux_binary: str, session_id: str) -> list[str]:
    return [
        tmux_binary,
        "-L",
        _tmux_socket_name(),
        "-f",
        "/dev/null",
        "new-session",
        "-A",
        "-s",
        session_id,
        ";",
        "set-option",
        "-g",
        "status",
        "off",
    ]


def _spawn_env(*, persistent: bool) -> dict[str, str]:
    env = dict(os.environ)
    env["TERM"] = "xterm-256color"
    if persistent:
        env["TMUX"] = ""
    # macOS lacks a C.UTF-8 locale; fall back to one that exists there so inner
    # programs don't warn on startup. Only override a missing/C/POSIX locale — a
    # real UTF-8 locale inherited from the user is kept as-is.
    utf8_fallback = "en_US.UTF-8" if sys.platform == "darwin" else "C.UTF-8"
    for key in ("LANG", "LC_CTYPE"):
        value = env.get(key, "")
        if not value or value in {"C", "POSIX"}:
            env[key] = utf8_fallback
    return env


async def _send_exit_status(websocket: WebSocket, code: int | None) -> None:
    try:
        await websocket.send_text(json.dumps({"type": "exit", "code": code}))
    except Exception:
        pass


async def _terminate_process(process: asyncio.subprocess.Process, signum: signal.Signals) -> None:
    try:
        if hasattr(os, "killpg") and process.pid:
            os.killpg(os.getpgid(process.pid), signum)
        else:
            process.send_signal(signum)
    except ProcessLookupError:
        return
    except Exception:
        logger.debug("terminal process signal failed", exc_info=True)
    try:
        await asyncio.wait_for(process.wait(), timeout=2)
        return
    except asyncio.TimeoutError:
        pass
    try:
        if hasattr(os, "killpg") and process.pid:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        else:
            process.kill()
    except ProcessLookupError:
        return
    await process.wait()


async def _kill_tmux_session(session_id: str) -> None:
    tmux_binary = resolve_tmux_binary()
    if not tmux_binary:
        return
    process = await asyncio.create_subprocess_exec(
        tmux_binary,
        "-L",
        _tmux_socket_name(),
        "-f",
        "/dev/null",
        "kill-session",
        "-t",
        session_id,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await process.wait()


def _tmux_socket_name() -> str:
    runtime_dir = str(paths.get_runtime_dir())
    digest = hashlib.sha256(runtime_dir.encode("utf-8")).hexdigest()[:12]
    return f"avibe-{digest}"


def _close_fd(fd: int) -> None:
    try:
        os.close(fd)
    except OSError:
        pass
