from __future__ import annotations

import asyncio
import json
import os
import signal
import struct

import pytest

pytestmark = pytest.mark.skipif(os.name == "nt", reason="terminal PTY tests require POSIX")

fcntl = pytest.importorskip("fcntl")
termios = pytest.importorskip("termios")

from starlette.websockets import WebSocketDisconnect

from core import terminal_service
from core.terminal_service import TerminalService, _tmux_launch_command, _tmux_socket_name, sanitize_session_id
from vibe import ui_server
from vibe.ui_server import app


class _FakeWebSocket:
    def __init__(self, messages: list[dict]) -> None:
        self._messages = list(messages)
        self.sent_bytes: list[bytes] = []
        self.sent_text: list[str] = []

    async def receive(self) -> dict:
        if not self._messages:
            await asyncio.sleep(0.05)
            return {"type": "websocket.disconnect", "code": 1000}
        message = self._messages.pop(0)
        if delay := message.pop("delay", None):
            await asyncio.sleep(delay)
        return message

    async def send_bytes(self, payload: bytes) -> None:
        self.sent_bytes.append(payload)

    async def send_text(self, payload: str) -> None:
        self.sent_text.append(payload)


def test_terminal_ephemeral_pty_round_trip(monkeypatch, tmp_path):
    asyncio.run(_terminal_ephemeral_pty_round_trip(monkeypatch, tmp_path))


async def _terminal_ephemeral_pty_round_trip(monkeypatch, tmp_path):
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("SHELL", "/bin/sh")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LANG", "C")
    monkeypatch.setattr(terminal_service, "resolve_tmux_binary", lambda: None)
    service = TerminalService(idle_timeout_seconds=60, max_sessions=2)
    websocket = _FakeWebSocket(
        [
            {"type": "websocket.receive", "bytes": b"printf READY\\\\n; exit 7\n", "delay": 0.1},
            {"type": "websocket.disconnect", "code": 1000, "delay": 0.5},
        ]
    )

    await service.handle_websocket(websocket, "term_1")
    await service.shutdown()

    assert json.loads(websocket.sent_text[0]) == {"type": "ready", "persistent": False}
    output = b"".join(websocket.sent_bytes)
    assert b"READY" in output
    assert json.loads(websocket.sent_text[-1]) == {"type": "exit", "code": 7}


def test_terminal_reconnect_replaces_session(monkeypatch, tmp_path):
    asyncio.run(_terminal_reconnect_replaces_session(monkeypatch, tmp_path))


async def _terminal_reconnect_replaces_session(monkeypatch, tmp_path):
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("SHELL", "/bin/sh")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(terminal_service, "resolve_tmux_binary", lambda: None)
    service = TerminalService(idle_timeout_seconds=60, max_sessions=2)
    try:
        first = await service.open("dup")
        second = await service.open("dup")
        # Reconnecting the same id reuses one slot; the old connection is
        # replaced (and its shell terminated), not orphaned past max_sessions.
        assert len(service._connections) == 1
        assert service._connections["dup"] is second
        assert first.process.returncode is not None
    finally:
        await service.shutdown()


def test_terminal_resize_applies_winsize(monkeypatch, tmp_path):
    asyncio.run(_terminal_resize_applies_winsize(monkeypatch, tmp_path))


async def _terminal_resize_applies_winsize(monkeypatch, tmp_path):
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("SHELL", "/bin/sh")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(terminal_service, "resolve_tmux_binary", lambda: None)
    service = TerminalService(idle_timeout_seconds=60, max_sessions=2)
    connection = await service.open("resize")
    try:
        await service.resize(connection, cols=100, rows=35)
        packed = fcntl.ioctl(connection.master_fd, termios.TIOCGWINSZ, struct.pack("HHHH", 0, 0, 0, 0))
        rows, cols, _, _ = struct.unpack("HHHH", packed)
        assert (rows, cols) == (35, 100)
    finally:
        await service.close(connection)
        await service.shutdown()


def test_terminal_tmux_launch_command_uses_safe_session():
    cmd = _tmux_launch_command("/tmp/tmux", sanitize_session_id("../bad session!"))

    assert cmd[0:3] == ["/tmp/tmux", "-L", _tmux_socket_name()]
    assert cmd[3:] == [
        "-f",
        "/dev/null",
        "new-session",
        "-A",
        "-s",
        "bad_session",
        ";",
        "set-option",
        "-g",
        "status",
        "off",
    ]
    assert _tmux_socket_name().startswith("avibe-")


def test_terminal_open_reserves_session_slot_during_spawn(monkeypatch, tmp_path):
    asyncio.run(_terminal_open_reserves_session_slot_during_spawn(monkeypatch, tmp_path))


async def _terminal_open_reserves_session_slot_during_spawn(monkeypatch, tmp_path):
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("SHELL", "/bin/sh")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(terminal_service.Path, "home", lambda: tmp_path / "home")
    monkeypatch.setattr(terminal_service, "resolve_tmux_binary", lambda: None)
    service = TerminalService(idle_timeout_seconds=60, max_sessions=1)
    spawn_started = asyncio.Event()
    release_spawn = asyncio.Event()
    original_spawn = asyncio.create_subprocess_exec

    async def delayed_spawn(*args, **kwargs):
        spawn_started.set()
        await release_spawn.wait()
        return await original_spawn(*args, **kwargs)

    monkeypatch.setattr(terminal_service.asyncio, "create_subprocess_exec", delayed_spawn)

    first_task = asyncio.create_task(service.open("first"))
    await spawn_started.wait()
    with pytest.raises(terminal_service.TerminalServiceError):
        await service.open("second")

    release_spawn.set()
    connection = await first_task
    try:
        assert service._connections["first"] is connection
    finally:
        await service.shutdown()


def test_open_releases_reserved_slot_on_cancel(monkeypatch, tmp_path):
    asyncio.run(_open_releases_reserved_slot_on_cancel(monkeypatch, tmp_path))


async def _open_releases_reserved_slot_on_cancel(monkeypatch, tmp_path):
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("SHELL", "/bin/sh")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(terminal_service.Path, "home", lambda: tmp_path / "home")
    monkeypatch.setattr(terminal_service, "resolve_tmux_binary", lambda: None)
    service = TerminalService(idle_timeout_seconds=60, max_sessions=1)
    spawn_started = asyncio.Event()
    original_spawn = asyncio.create_subprocess_exec

    # Track the PTY master fds so we can assert the cancelled spawn closed its own.
    opened_masters: list[int] = []
    real_openpty = terminal_service.os.openpty
    closed_fds: list[int] = []
    real_close_fd = terminal_service._close_fd

    def tracking_openpty():
        master, slave = real_openpty()
        opened_masters.append(master)
        return master, slave

    def tracking_close_fd(fd: int) -> None:
        closed_fds.append(fd)
        real_close_fd(fd)

    monkeypatch.setattr(terminal_service.os, "openpty", tracking_openpty)
    monkeypatch.setattr(terminal_service, "_close_fd", tracking_close_fd)

    async def delayed_spawn(*_args, **_kwargs):
        spawn_started.set()
        await asyncio.sleep(60)

    monkeypatch.setattr(terminal_service.asyncio, "create_subprocess_exec", delayed_spawn)

    open_task = asyncio.create_task(service.open("cancelled"))
    await spawn_started.wait()
    open_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await open_task

    assert service._reserved_sessions == set()
    # The PTY master opened for the cancelled spawn must be closed, not leaked.
    assert opened_masters, "openpty was not called"
    assert opened_masters[0] in closed_fds

    monkeypatch.setattr(terminal_service.asyncio, "create_subprocess_exec", original_spawn)
    connection = await service.open("next")
    try:
        assert service._connections["next"] is connection
    finally:
        await service.shutdown()


def test_ready_frame_failure_closes_connection(monkeypatch, tmp_path):
    asyncio.run(_ready_frame_failure_closes_connection(monkeypatch, tmp_path))


async def _ready_frame_failure_closes_connection(monkeypatch, tmp_path):
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("SHELL", "/bin/sh")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(terminal_service.Path, "home", lambda: tmp_path / "home")
    monkeypatch.setattr(terminal_service, "resolve_tmux_binary", lambda: None)
    service = TerminalService(idle_timeout_seconds=60, max_sessions=1)

    class _DisconnectingWebSocket(_FakeWebSocket):
        async def send_text(self, payload: str) -> None:
            raise RuntimeError("client dropped before ready")

    with pytest.raises(RuntimeError, match="client dropped"):
        await service.handle_websocket(_DisconnectingWebSocket([]), "drop")

    try:
        assert service._connections == {}
    finally:
        await service.shutdown()


def test_detached_session_tracked_when_client_exits(monkeypatch, tmp_path):
    asyncio.run(_detached_session_tracked_when_client_exits(monkeypatch, tmp_path))


class _ExitedProcess:
    returncode = 0
    pid = None

    async def wait(self) -> int:
        return 0


def _make_persistent_connection(session_id: str) -> "terminal_service.TerminalConnection":
    fd = os.open(os.devnull, os.O_RDWR)
    return terminal_service.TerminalConnection(
        session_id=session_id,
        process=_ExitedProcess(),
        master_fd=fd,
        persistent=True,
        attached_at=0.0,
        last_seen=0.0,
    )


async def _set_tmux_has_session(monkeypatch, exists: bool) -> None:
    async def _has_session(_session_id: str) -> bool:
        return exists

    monkeypatch.setattr(terminal_service, "_tmux_has_session", _has_session)


def test_detached_session_tracked_when_client_exits(monkeypatch, tmp_path):
    asyncio.run(_detached_session_tracked_when_client_exits(monkeypatch, tmp_path))


async def _detached_session_tracked_when_client_exits(monkeypatch, tmp_path):
    # A persistent (tmux) connection whose client process has already exited — e.g. the
    # user hit tmux's detach key — must still be recorded as detached WHEN THE SESSION IS
    # STILL ALIVE, or it goes uncounted against max_sessions, unreaped, and unkilled.
    await _set_tmux_has_session(monkeypatch, True)
    service = TerminalService(idle_timeout_seconds=60, max_sessions=2)

    await service._cleanup_connection(_make_persistent_connection("detached"), detach=True)

    assert "detached" in service._detached_tmux_sessions


def test_dead_session_not_tracked_when_shell_exits(monkeypatch, tmp_path):
    asyncio.run(_dead_session_not_tracked_when_shell_exits(monkeypatch, tmp_path))


async def _dead_session_not_tracked_when_shell_exits(monkeypatch, tmp_path):
    # When the shell inside tmux exits (rather than a detach), the client process exits AND
    # the tmux session is gone — it must NOT be recorded as a live detached session, or a
    # dead id counts against max_sessions until the idle timeout.
    await _set_tmux_has_session(monkeypatch, False)
    service = TerminalService(idle_timeout_seconds=60, max_sessions=2)

    await service._cleanup_connection(_make_persistent_connection("ended"), detach=True)

    assert "ended" not in service._detached_tmux_sessions


def test_open_cleans_up_process_when_registration_fails(monkeypatch, tmp_path):
    asyncio.run(_open_cleans_up_process_when_registration_fails(monkeypatch, tmp_path))


async def _open_cleans_up_process_when_registration_fails(monkeypatch, tmp_path):
    # If open() is interrupted after the child has spawned but before it is registered in
    # _connections (a cancel while reacquiring the lock, modelled here by a failing
    # registration), the spawned process + reservation must be torn down — otherwise it
    # lives outside _connections where shutdown/reaping can never reach it.
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("SHELL", "/bin/sh")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(terminal_service.Path, "home", lambda: tmp_path / "home")
    monkeypatch.setattr(terminal_service, "resolve_tmux_binary", lambda: None)

    signals: list[int] = []

    class _FakeProcess:
        returncode = None
        pid = None

        def send_signal(self, signum: int) -> None:
            signals.append(signum)

        async def wait(self) -> int:
            return 0

    async def fake_spawn(*_args, **_kwargs):
        return _FakeProcess()

    monkeypatch.setattr(terminal_service.asyncio, "create_subprocess_exec", fake_spawn)

    service = TerminalService(idle_timeout_seconds=60, max_sessions=1)

    class _RaisingConnections(dict):
        def __setitem__(self, key, value):
            raise RuntimeError("registration boom")

    service._connections = _RaisingConnections()

    with pytest.raises(RuntimeError, match="registration boom"):
        await service.open("orphan")

    assert service._reserved_sessions == set()
    assert signal.SIGTERM in signals  # the spawned shell was terminated, not leaked


def test_concurrent_reconnect_is_rejected(monkeypatch, tmp_path):
    asyncio.run(_concurrent_reconnect_is_rejected(monkeypatch, tmp_path))


async def _concurrent_reconnect_is_rejected(monkeypatch, tmp_path):
    # Two opens for the same id must serialize: while the first is closing the old
    # connection and spawning the replacement, the second must be rejected with
    # session_opening rather than racing and overwriting the first's registration.
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("SHELL", "/bin/sh")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(terminal_service.Path, "home", lambda: tmp_path / "home")
    monkeypatch.setattr(terminal_service, "resolve_tmux_binary", lambda: None)

    service = TerminalService(idle_timeout_seconds=60, max_sessions=2)
    first = await service.open("dup")

    spawn_started = asyncio.Event()
    spawn_gate = asyncio.Event()
    original_spawn = asyncio.create_subprocess_exec

    async def gated_spawn(*args, **kwargs):
        spawn_started.set()
        await spawn_gate.wait()
        return await original_spawn(*args, **kwargs)

    monkeypatch.setattr(terminal_service.asyncio, "create_subprocess_exec", gated_spawn)

    reopen = asyncio.create_task(service.open("dup"))
    await spawn_started.wait()  # reopen has reserved "dup" and is parked mid-spawn
    try:
        with pytest.raises(terminal_service.TerminalServiceError, match="session_opening"):
            await service.open("dup")
    finally:
        spawn_gate.set()
        replacement = await reopen
        try:
            assert service._connections["dup"] is replacement
            assert first.session_id == "dup"
        finally:
            await service.shutdown()


def test_spawn_env_drops_c_lc_all(monkeypatch):
    # LC_ALL overrides LANG/LC_CTYPE; an inherited C/POSIX LC_ALL must be dropped so the
    # UTF-8 fallback actually takes effect.
    monkeypatch.setenv("LC_ALL", "C")
    monkeypatch.delenv("LANG", raising=False)
    monkeypatch.delenv("LC_CTYPE", raising=False)

    env = terminal_service._spawn_env(persistent=False)

    assert "LC_ALL" not in env
    assert env["LANG"].endswith("UTF-8")
    assert env["LC_CTYPE"].endswith("UTF-8")


def test_spawn_env_keeps_real_lc_all(monkeypatch):
    monkeypatch.setenv("LC_ALL", "en_US.UTF-8")

    env = terminal_service._spawn_env(persistent=False)

    assert env["LC_ALL"] == "en_US.UTF-8"


def test_sanitize_session_id_allows_only_contract_chars():
    assert sanitize_session_id("../bad session!") == "bad_session"
    assert sanitize_session_id("abc-DEF_123") == "abc-DEF_123"


def test_terminal_websocket_disabled_when_flag_off(monkeypatch, tmp_path):
    # The terminal is ON by default; an explicit VIBE_UI_ENABLE_TERMINAL=0 disables it.
    monkeypatch.setenv("VIBE_UI_ENABLE_TERMINAL", "0")
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    with pytest.raises(WebSocketDisconnect) as exc:
        with app.test_client().websocket_connect(
            "/api/terminal/test",
            headers={"host": "127.0.0.1", "origin": "http://127.0.0.1"},
        ):
            pass

    assert exc.value.code == 1008


def test_terminal_websocket_rejects_forwarded_request(monkeypatch, tmp_path):
    monkeypatch.setenv("VIBE_UI_ENABLE_TERMINAL", "1")
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    with pytest.raises(WebSocketDisconnect) as exc:
        with app.test_client().websocket_connect(
            "/api/terminal/test",
            headers={
                "host": "127.0.0.1",
                "origin": "http://127.0.0.1",
                "x-forwarded-for": "203.0.113.10",
            },
        ):
            pass

    assert exc.value.code == 1008


def test_terminal_websocket_rejects_unauthorized_remote_request(monkeypatch, tmp_path):
    monkeypatch.setenv("VIBE_UI_ENABLE_TERMINAL", "1")
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    with pytest.raises(WebSocketDisconnect) as exc:
        with app.test_client().websocket_connect(
            "/api/terminal/test",
            headers={
                "host": "127.0.0.1",
                "origin": "http://127.0.0.1",
                "x-vibe-test-remote-addr": "203.0.113.10",
            },
        ):
            pass

    assert exc.value.code == 1008


def test_terminal_websocket_rejects_cross_origin(monkeypatch, tmp_path):
    monkeypatch.setenv("VIBE_UI_ENABLE_TERMINAL", "1")
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    with pytest.raises(WebSocketDisconnect) as exc:
        with app.test_client().websocket_connect(
            "/api/terminal/test",
            headers={"host": "127.0.0.1", "origin": "http://evil.example"},
        ):
            pass

    assert exc.value.code == 1008


def test_terminal_websocket_rejects_local_origin_from_different_port(monkeypatch, tmp_path):
    monkeypatch.setenv("VIBE_UI_ENABLE_TERMINAL", "1")
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    with pytest.raises(WebSocketDisconnect) as exc:
        with app.test_client().websocket_connect(
            "/api/terminal/test",
            headers={"host": "127.0.0.1", "origin": "http://127.0.0.1:3000"},
        ):
            pass

    assert exc.value.code == 1008


def test_terminal_websocket_accepts_local_origin_from_same_port(monkeypatch, tmp_path):
    monkeypatch.setenv("VIBE_UI_ENABLE_TERMINAL", "1")
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    accepted = False

    async def fake_handle_websocket(websocket, session_id):
        nonlocal accepted
        accepted = True

    monkeypatch.setattr(ui_server.get_terminal_service(), "handle_websocket", fake_handle_websocket)

    with app.test_client().websocket_connect(
        "/api/terminal/test",
        headers={"host": "127.0.0.1:5123", "origin": "http://127.0.0.1:5123"},
    ):
        pass

    assert accepted is True


def test_terminal_service_ignores_invalid_limit_env(monkeypatch):
    monkeypatch.setattr(ui_server, "_terminal_service", None)
    monkeypatch.setenv(ui_server.TERMINAL_IDLE_TIMEOUT_ENV, "1h")
    monkeypatch.setenv(ui_server.TERMINAL_MAX_SESSIONS_ENV, "many")

    service = ui_server.get_terminal_service()

    try:
        assert service.idle_timeout_seconds == 3600
        assert service.max_sessions == 8
    finally:
        monkeypatch.setattr(ui_server, "_terminal_service", None)
