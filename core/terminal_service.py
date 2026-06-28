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
from enum import Enum
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


class _Phase(Enum):
    OPENING = "opening"    # open() in progress; no usable connection yet
    ATTACHED = "attached"  # a live websocket client is connected
    CLOSING = "closing"    # tearing the client down; still counts until reconciled
    DETACHED = "detached"  # tmux session alive but no client (reattachable)


@dataclass
class _Session:
    """One terminal session id, always in exactly one phase.

    The whole service tracks sessions in a single dict keyed by id, so an id can never be
    double-counted or land in two collections out of sync — the root cause of an entire
    class of bookkeeping races. Capacity is just len(self._sessions); a reconnect reuses an
    existing entry, only a brand-new id is checked against the cap.
    """

    session_id: str
    phase: _Phase
    persistent: bool = False
    connection: TerminalConnection | None = None  # set while ATTACHED
    detached_at: float | None = None              # set while DETACHED


class TerminalService:
    def __init__(self, *, idle_timeout_seconds: int = 3600, max_sessions: int = 8) -> None:
        self.idle_timeout_seconds = max(60, int(idle_timeout_seconds))
        self.max_sessions = max(1, int(max_sessions))
        # One entry per session id, each in exactly one phase. Capacity is simply
        # len(self._sessions) — no cross-collection arithmetic to keep in sync.
        self._sessions: dict[str, _Session] = {}
        self._terminating: set[str] = set()
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
            sessions = list(self._sessions.values())
            self._sessions.clear()
        # Kill every tmux session for real so `vibe stop` leaves no orphaned server, and reap
        # any live client process. An in-flight open() finds its slot gone and tears its own
        # child down.
        await asyncio.gather(*(self._shutdown_session(s) for s in sessions), return_exceptions=True)
        # Tracked teardown above only covers this process's registry. Also kill the whole tmux
        # server on our private socket so sessions orphaned by a prior crashed/restarted process
        # (absent from _sessions, so untouched above) don't survive `vibe stop`.
        await _kill_tmux_server()

    async def _shutdown_session(self, session: _Session) -> None:
        if session.connection is not None:
            await self._teardown_client(session.connection, kill_session=True)
        elif session.persistent:
            await _kill_tmux_session(session.session_id)

    async def handle_websocket(self, websocket: WebSocket, raw_session_id: str) -> None:
        session_id = sanitize_session_id(raw_session_id)
        connection = await self.open(session_id)
        try:
            await websocket.send_text(json.dumps({"type": "ready", "persistent": connection.persistent}))
        except BaseException:
            # The browser can drop right after accept; open() already registered the PTY, so
            # close it here or it leaks against max_sessions until the service restarts.
            await self.close(connection)
            raise
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
            # Structural backstop: guarantee no pump/wait task outlives the connection,
            # however the loop exits. The per-branch logic above cancels siblings on the
            # expected paths (clean disconnect, process exit); this covers the rest — e.g. a
            # pump raising a non-WebSocketDisconnect error (EBADF when a fast reconnect or
            # shutdown closes the PTY fd) would otherwise propagate out of the loop leaving
            # input/wait still reading the old socket or awaiting the old process. Cancelling
            # an already-finished task is a no-op, so this is safe over the happy paths too.
            for task in (output_task, input_task, wait_task):
                task.cancel()
            await asyncio.gather(output_task, input_task, wait_task, return_exceptions=True)
            await self.close(connection)

    async def open(self, session_id: str) -> TerminalConnection:
        # Claim the id as OPENING under the lock. A single registry entry per id means a
        # concurrent open for the same id is rejected here (the slot is already OPENING), and
        # reconnecting an existing attached/detached id reuses its slot rather than allocating
        # a new one — so the cap is just len(self._sessions), no per-collection arithmetic.
        async with self._lock:
            self._forget_finished_locked()
            current = self._sessions.get(session_id)
            if session_id in self._terminating:
                raise TerminalServiceError("session_opening")
            if current is not None and current.phase in (_Phase.OPENING, _Phase.CLOSING):
                # Either an open is in progress, or the session is mid-teardown (e.g. the
                # reaper is awaiting a kill-session). Reject rather than reuse the id — a
                # reconnect must not attach to a session that is being killed (which the
                # delayed kill would then terminate). The client retries; once teardown
                # finishes the slot is gone and the retry opens a fresh session.
                raise TerminalServiceError("session_opening")
            if current is None and len(self._sessions) >= self.max_sessions:
                raise TerminalServiceError("too_many_sessions")
            # A live client for this id will be replaced; its tmux session stays alive and
            # the new client reattaches to it.
            replaced = current.connection if (current is not None and current.phase is _Phase.ATTACHED) else None
            # Carry the persistent flag onto the OPENING placeholder: if shutdown or a cancel
            # lands in the reconnect window (after we overwrite the slot, before the new client
            # registers), shutdown must still see this id as persistent and kill its existing
            # tmux session — otherwise the replaced session leaks untracked after `vibe stop`.
            was_persistent = current is not None and current.persistent
            opening_slot = _Session(session_id, _Phase.OPENING, persistent=was_persistent)
            self._sessions[session_id] = opening_slot
        registered = False
        spawned: TerminalConnection | None = None
        try:
            if replaced is not None:
                await self._teardown_client(replaced, kill_session=False)

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
                    env=_spawn_env(),
                    # start_new_session runs setsid() in the child via C (post-fork, pre-exec)
                    # instead of a Python preexec_fn — a Python preexec_fn can deadlock the
                    # forked child if another thread (FastAPI threadpool, dependency jobs) holds
                    # a lock at fork time, which would hang open() with the slot reserved.
                    start_new_session=True,
                )
            except BaseException:
                # Close the PTY master on cancellation too (CancelledError is a BaseException,
                # not Exception) — otherwise a cancelled spawn leaks the fd.
                _close_fd(master_fd)
                raise
            finally:
                _close_fd(slave_fd)

            spawned = TerminalConnection(
                session_id=session_id,
                process=process,
                master_fd=master_fd,
                persistent=persistent,
                attached_at=time.monotonic(),
                last_seen=time.monotonic(),
            )
            async with self._lock:
                slot = self._sessions.get(session_id)
                if slot is opening_slot and slot.phase is _Phase.OPENING:
                    slot.phase = _Phase.ATTACHED
                    slot.connection = spawned
                    slot.persistent = persistent
                    registered = True
            if not registered:
                # The OPENING slot was taken over or the service shut down while we spawned;
                # we lost the race — tear our own child down (below) and report busy.
                raise TerminalServiceError("session_opening")
            return spawned
        except BaseException:
            if not registered:
                await self._abandon_open(session_id, opening_slot, spawned)
            raise

    async def _abandon_open(
        self,
        session_id: str,
        opening_slot: _Session,
        spawned: TerminalConnection | None,
    ) -> None:
        # Reconcile an open that never registered. The slot object is the open-attempt token:
        # a same-id slot created later is not ours, so a stale abandon must not remove it or
        # kill its tmux session by id.
        terminating_open = False
        killed_session = False
        async with self._lock:
            slot = self._sessions.get(session_id)
            owns_slot = slot is opening_slot and slot.phase in (_Phase.OPENING, _Phase.CLOSING)
            terminating_open = owns_slot and slot.phase is _Phase.CLOSING
            slot_missing = slot is None
        try:
            kill_spawned_session = slot_missing or terminating_open
            if spawned is not None:
                await self._teardown_client(spawned, kill_session=kill_spawned_session)
                killed_session = kill_spawned_session and spawned.persistent
            # Re-track only when our slot is still ours AND a tmux session for this id is alive —
            # which covers a failed reconnect to an existing DETACHED session (its tmux server is
            # still up) regardless of whether our own child spawned. A session that does not exist
            # (fresh open whose spawn failed before creating one) is simply dropped.
            async with self._lock:
                slot = self._sessions.get(session_id)
                owns_slot = slot is opening_slot and slot.phase in (_Phase.OPENING, _Phase.CLOSING)
                current_terminating_open = owns_slot and slot.phase is _Phase.CLOSING
                terminating_open = terminating_open or current_terminating_open
            if not owns_slot:
                return
            if current_terminating_open:
                if not killed_session:
                    await _kill_tmux_session(session_id)
                    killed_session = True
                return

            alive = await _tmux_has_session(session_id)
            needs_terminate_kill = False
            async with self._lock:
                slot = self._sessions.get(session_id)
                if slot is opening_slot and slot.phase is _Phase.CLOSING:
                    terminating_open = True
                    needs_terminate_kill = not killed_session
                elif slot is opening_slot and slot.phase is _Phase.OPENING:
                    if alive:
                        opening_slot.phase = _Phase.DETACHED
                        opening_slot.connection = None
                        opening_slot.persistent = True
                        opening_slot.detached_at = time.monotonic()
                    else:
                        self._sessions.pop(session_id, None)
            if needs_terminate_kill:
                await _kill_tmux_session(session_id)
                killed_session = True
        finally:
            if terminating_open:
                async with self._lock:
                    if self._sessions.get(session_id) is opening_slot:
                        self._sessions.pop(session_id, None)
                    self._terminating.discard(session_id)

    async def close(self, connection: TerminalConnection) -> None:
        async with self._lock:
            slot = self._sessions.get(connection.session_id)
            superseded = slot is None or slot.connection is not connection
            if not superseded:
                # Mark CLOSING but keep the entry in place so the session keeps counting
                # toward the cap across the async teardown; _finalize_connection settles it.
                slot.phase = _Phase.CLOSING
        if superseded:
            # A newer connection already owns this id (fast reconnect): just reap our own
            # client process; the registry belongs to the live owner.
            await self._teardown_client(connection, kill_session=False)
            return
        await self._finalize_connection(connection)

    async def terminate(self, session_id: str) -> bool:
        """End a terminal session by id and remove it from capacity accounting.

        Used by windowed terminals whose UI lifetime is intentionally ephemeral. A
        normal websocket close detaches from tmux for reconnect durability; this path
        kills the backing session so closing a window frees the backend slot.
        """

        safe_session_id = sanitize_session_id(session_id)
        async with self._lock:
            if safe_session_id in self._terminating:
                return False
            slot = self._sessions.get(safe_session_id)
            tracked = slot is not None
            self._terminating.add(safe_session_id)
            terminating_slot = slot
            connection = slot.connection if slot is not None else None
            pending_open = slot is not None and slot.phase is _Phase.OPENING
            if slot is not None:
                slot.phase = _Phase.CLOSING

        try:
            if pending_open:
                return tracked
            if connection is not None:
                await self._teardown_client(connection, kill_session=True)
            else:
                await _kill_tmux_session(safe_session_id)
        finally:
            async with self._lock:
                if not (pending_open and self._sessions.get(safe_session_id) is terminating_slot):
                    if terminating_slot is not None and self._sessions.get(safe_session_id) is terminating_slot:
                        self._sessions.pop(safe_session_id, None)
                    self._terminating.discard(safe_session_id)
        return tracked

    async def _finalize_connection(self, connection: TerminalConnection) -> None:
        # Detach the client, then settle the CLOSING slot against tmux reality. The slot stays
        # in _sessions (CLOSING) for the whole teardown, so there is no window where the
        # session is uncounted and a concurrent open could overfill max_sessions. A reconnect
        # that grabbed the id (slot no longer our CLOSING entry) wins and we leave it be.
        await self._teardown_client(connection, kill_session=False)
        alive = connection.persistent and await _tmux_has_session(connection.session_id)
        async with self._lock:
            slot = self._sessions.get(connection.session_id)
            if slot is None or slot.connection is not connection or slot.phase is not _Phase.CLOSING:
                return
            if alive:
                slot.phase = _Phase.DETACHED
                slot.connection = None
                slot.detached_at = time.monotonic()
            else:
                self._sessions.pop(connection.session_id, None)

    async def _teardown_client(self, connection: TerminalConnection, *, kill_session: bool) -> None:
        """Release a connection's OS resources only — never touches the registry (the caller
        owns the _sessions transition). SIGHUP detaches a tmux client so its session survives
        for reattach; SIGTERM ends an ephemeral shell. kill_session=True also tears the tmux
        session down (shutdown / idle expiry) so nothing is left running.

        Idempotent: a fast reconnect tears down the replaced connection, then os.openpty() for
        the replacement can reuse that same fd number. If the superseded connection's stale
        close() then ran a second teardown it would close the NEW terminal's fd — so we poison
        master_fd to -1 on first teardown and a repeat call closes nothing."""
        fd, connection.master_fd = connection.master_fd, -1
        if fd >= 0:
            _close_fd(fd)
        if kill_session and connection.persistent:
            await _kill_tmux_session(connection.session_id)
        if connection.process.returncode is None:
            signum = signal.SIGHUP if connection.persistent else signal.SIGTERM
            await _terminate_process(connection.process, signum)

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
        # Drop EPHEMERAL attached sessions whose client has exited — they have no tmux session
        # to preserve. A persistent client can exit while its tmux session is still alive (the
        # user detached with the prefix key), so we must NOT pop those here: that would untrack
        # a live session. Leave them for the reaper's finalize, which checks has-session and
        # reconciles to DETACHED or removes them.
        for session_id, session in list(self._sessions.items()):
            if (
                session.phase is _Phase.ATTACHED
                and session.connection is not None
                and session.connection.process.returncode is not None
                and not session.persistent
            ):
                self._sessions.pop(session_id, None)

    async def _reaper_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            try:
                await self.reap_idle()
            except asyncio.CancelledError:
                raise
            except Exception:
                # One bad session (e.g. a kill-session subprocess that fails because the
                # vendored tmux binary went missing) must not kill the reaper for good, or
                # idle/detached sessions would never be reclaimed until a restart.
                logger.exception("terminal reaper cycle failed; continuing")

    async def reap_idle(self) -> None:
        cutoff = time.monotonic() - self.idle_timeout_seconds
        async with self._lock:
            closing: list[TerminalConnection] = []
            expired_detached: list[str] = []
            for session_id, session in list(self._sessions.items()):
                if session.phase is _Phase.ATTACHED and session.connection is not None:
                    conn = session.connection
                    if conn.process.returncode is not None or conn.last_seen < cutoff:
                        # Mark CLOSING (keep counted) and run it through the same finalize path
                        # as close(): a dead OR idle client is reconciled against has-session,
                        # so a still-alive tmux session is re-tracked as DETACHED rather than
                        # silently dropped (and an idle client's session is preserved).
                        session.phase = _Phase.CLOSING
                        closing.append(conn)
                elif session.phase is _Phase.DETACHED and (session.detached_at or 0.0) < cutoff:
                    # Keep the entry registered (CLOSING) across the async kill so it stays
                    # counted and a reconnect in the gap can't be silently killed.
                    session.phase = _Phase.CLOSING
                    expired_detached.append(session_id)
        for conn in closing:
            await self._finalize_connection(conn)
        # Kill detached sessions that outlived the idle window — but only if a reconnect has
        # not grabbed the id meanwhile (it would have flipped the phase away from CLOSING), so
        # we never kill a freshly reattached terminal. Drop the entry once the kill completes.
        for session_id in expired_detached:
            async with self._lock:
                slot = self._sessions.get(session_id)
                if slot is None or slot.phase is not _Phase.CLOSING or slot.connection is not None:
                    continue
            await _kill_tmux_session(session_id)
            async with self._lock:
                slot = self._sessions.get(session_id)
                if slot is not None and slot.phase is _Phase.CLOSING and slot.connection is None:
                    self._sessions.pop(session_id, None)


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
        # tmux drives the outer terminal's ALTERNATE screen, so the browser xterm keeps no
        # scrollback of its own — without mouse mode the wheel does nothing and earlier output
        # is unreachable. `mouse on` routes the wheel into tmux copy-mode (scroll history), and
        # `set-clipboard on` lets a mouse selection reach the system clipboard via OSC 52 so
        # copy still works once tmux owns the mouse.
        ";",
        "set-option",
        "-g",
        "mouse",
        "on",
        ";",
        "set-option",
        "-g",
        "set-clipboard",
        "on",
    ]


def _spawn_env() -> dict[str, str]:
    env = dict(os.environ)
    env["TERM"] = "xterm-256color"
    # Drop any tmux context inherited from the Avibe daemon (which may itself have been
    # started from inside tmux). Otherwise the spawned terminal — the vendored tmux client
    # OR a plain fallback shell — points at the operator's tmux socket and nesting/targeting
    # breaks (e.g. running tmux/byobu inside the fallback shell). tmux sets the correct
    # TMUX/TMUX_PANE itself inside the persistent panes it creates.
    env.pop("TMUX", None)
    env.pop("TMUX_PANE", None)
    # macOS lacks a C.UTF-8 locale; fall back to one that exists there so inner
    # programs don't warn on startup. Only override a missing/C/POSIX locale — a
    # real UTF-8 locale inherited from the user is kept as-is.
    utf8_fallback = "en_US.UTF-8" if sys.platform == "darwin" else "C.UTF-8"
    for key in ("LANG", "LC_CTYPE"):
        value = env.get(key, "")
        if not value or value in {"C", "POSIX"}:
            env[key] = utf8_fallback
    # LC_ALL overrides LANG/LC_CTYPE; an inherited C/POSIX LC_ALL would defeat the UTF-8
    # fallback set above, so drop it and let the UTF-8 LANG/LC_CTYPE take effect. A real
    # inherited locale is left untouched, mirroring the LANG/LC_CTYPE handling.
    if env.get("LC_ALL", "") in {"C", "POSIX"}:
        env.pop("LC_ALL", None)
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


async def _kill_tmux_server() -> None:
    # Kill the entire tmux server on our dedicated socket. Unlike kill-session (one tracked id),
    # this also removes sessions orphaned by a prior process that crashed/restarted — those are
    # still on the socket but absent from this process's registry, so they would otherwise
    # outlive `vibe stop` with no reaper to ever collect them. The socket is private to Avibe
    # terminals, so killing the server only affects our own sessions.
    tmux_binary = resolve_tmux_binary()
    if not tmux_binary:
        return
    try:
        process = await asyncio.create_subprocess_exec(
            tmux_binary,
            "-L",
            _tmux_socket_name(),
            "-f",
            "/dev/null",
            "kill-server",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        return
    await process.wait()


async def _tmux_has_session(session_id: str) -> bool:
    # `has-session` exits 0 when the named session still exists on our socket. Used to tell
    # a real detach (session alive) from a shell exit (session gone) before counting an id
    # as a live detached session.
    tmux_binary = resolve_tmux_binary()
    if not tmux_binary:
        return False
    try:
        process = await asyncio.create_subprocess_exec(
            tmux_binary,
            "-L",
            _tmux_socket_name(),
            "-f",
            "/dev/null",
            "has-session",
            "-t",
            session_id,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        return False
    return await process.wait() == 0


def _tmux_socket_name() -> str:
    runtime_dir = str(paths.get_runtime_dir())
    digest = hashlib.sha256(runtime_dir.encode("utf-8")).hexdigest()[:12]
    return f"avibe-{digest}"


def _close_fd(fd: int) -> None:
    try:
        os.close(fd)
    except OSError:
        pass
