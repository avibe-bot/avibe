from __future__ import annotations

import asyncio
import json
import os
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
