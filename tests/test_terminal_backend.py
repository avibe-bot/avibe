from __future__ import annotations

import asyncio
import fcntl
import json
import struct
import termios

import pytest
from starlette.websockets import WebSocketDisconnect

from core import terminal_service
from core.terminal_service import TerminalService, _tmux_launch_command, sanitize_session_id
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

    assert cmd == [
        "/tmp/tmux",
        "-L",
        "avibe",
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
