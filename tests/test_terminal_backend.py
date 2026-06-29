from __future__ import annotations

import asyncio
import errno
import json
import os
import signal
import struct

import pytest

pytestmark = pytest.mark.skipif(os.name == "nt", reason="terminal PTY tests require POSIX")

fcntl = pytest.importorskip("fcntl")
termios = pytest.importorskip("termios")

from starlette.websockets import WebSocketDisconnect

from config.v2_config import (
    AgentsConfig,
    PlatformsConfig,
    RemoteAccessConfig,
    RuntimeConfig,
    SlackConfig,
    UiConfig,
    V2Config,
)
from core import terminal_service
from core.terminal_service import (
    TerminalService,
    _Phase,
    _Session,
    _tmux_launch_command,
    _tmux_socket_name,
    sanitize_session_id,
)
from tests.ui_server_test_helpers import csrf_headers
from vibe import remote_access
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


class _RecordingWebSocket:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int | None]] = []

    async def accept(self) -> None:
        self.calls.append(("accept", None))

    async def close(self, code: int = 1000) -> None:
        self.calls.append(("close", code))


def _save_remote_config(tmp_path) -> V2Config:
    config = V2Config(
        mode="self_host",
        version="v2",
        platform="slack",
        platforms=PlatformsConfig(enabled=["slack"], primary="slack"),
        slack=SlackConfig(bot_token=""),
        runtime=RuntimeConfig(default_cwd="."),
        agents=AgentsConfig(),
        ui=UiConfig(),
        remote_access=RemoteAccessConfig(),
    )
    cloud = config.remote_access.vibe_cloud
    cloud.enabled = True
    cloud.public_url = "https://alex.avibe.bot"
    cloud.client_id = "vr_client_123"
    cloud.instance_id = "inst_123"
    cloud.session_secret = "session-secret"
    config.save()
    return config


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


def test_terminal_websocket_cancels_siblings_on_pump_failure(monkeypatch, tmp_path):
    asyncio.run(_terminal_websocket_cancels_siblings_on_pump_failure(monkeypatch, tmp_path))


async def _terminal_websocket_cancels_siblings_on_pump_failure(monkeypatch, tmp_path):
    # If a pump dies with anything other than WebSocketDisconnect (e.g. EBADF when a fast
    # reconnect/shutdown closes the PTY fd), handle_websocket must still cancel the sibling
    # tasks instead of letting them outlive the torn-down connection.
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("SHELL", "/bin/sh")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(terminal_service, "resolve_tmux_binary", lambda: None)
    service = TerminalService(idle_timeout_seconds=60, max_sessions=2)

    input_cancelled = asyncio.Event()

    async def exploding_output(_websocket, _connection):
        raise OSError(errno.EBADF, "Bad file descriptor")

    async def blocking_input(_websocket, _connection):
        try:
            await asyncio.Event().wait()  # only a cancel ends this
        except asyncio.CancelledError:
            input_cancelled.set()
            raise

    monkeypatch.setattr(service, "_pump_output", exploding_output)
    monkeypatch.setattr(service, "_pump_input", blocking_input)

    try:
        with pytest.raises(OSError) as exc:
            await service.handle_websocket(_FakeWebSocket([]), "term_leak")
        assert exc.value.errno == errno.EBADF
        # The sibling input pump was cancelled (not leaked), and no terminal-* task is left
        # pending after the connection tore down.
        assert input_cancelled.is_set()
        leaked = [
            task
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task() and "terminal-" in task.get_name() and not task.done()
        ]
        assert leaked == []
    finally:
        await service.shutdown()


def test_shutdown_kills_whole_tmux_server_including_untracked(monkeypatch):
    asyncio.run(_shutdown_kills_whole_tmux_server_including_untracked(monkeypatch))


async def _shutdown_kills_whole_tmux_server_including_untracked(monkeypatch):
    # A persistent session orphaned by a prior crashed/restarted process is on our tmux socket
    # but absent from this process's registry. Shutdown must kill the whole server on our
    # private socket — not just tracked entries — so `vibe stop` leaves no detached shell.
    monkeypatch.setattr(terminal_service, "resolve_tmux_binary", lambda: "/usr/bin/tmux")
    commands: list[tuple] = []

    class _FakeProcess:
        async def wait(self) -> int:
            return 0

    async def fake_spawn(*args, **_kwargs):
        commands.append(tuple(args))
        return _FakeProcess()

    monkeypatch.setattr(terminal_service.asyncio, "create_subprocess_exec", fake_spawn)

    service = TerminalService(idle_timeout_seconds=60, max_sessions=2)
    # Empty registry: this process tracks nothing, so only a whole-server kill can reap orphans.
    await service.shutdown()

    socket = terminal_service._tmux_socket_name()
    kill_cmds = [cmd for cmd in commands if "kill-server" in cmd]
    assert kill_cmds, commands
    cmd = kill_cmds[0]
    assert "-L" in cmd and socket in cmd
    # kill-server must be the COMMAND (trailing token after the global flags), and /dev/null
    # must be the argument consumed by -f — not itself parsed as the command. This guards the
    # arg ORDER, not just membership: `tmux -L <sock> -f /dev/null kill-server`.
    assert cmd[-1] == "kill-server"
    assert cmd[cmd.index("/dev/null") - 1] == "-f"


def test_open_spawns_with_start_new_session_not_preexec(monkeypatch, tmp_path):
    asyncio.run(_open_spawns_with_start_new_session_not_preexec(monkeypatch, tmp_path))


async def _open_spawns_with_start_new_session_not_preexec(monkeypatch, tmp_path):
    # A Python preexec_fn can deadlock the forked child if another thread holds a lock at fork
    # time; the process group must be created via start_new_session (C-level setsid) instead.
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(terminal_service.Path, "home", lambda: tmp_path / "home")
    monkeypatch.setattr(terminal_service, "resolve_tmux_binary", lambda: None)

    captured: dict = {}

    class _FakeProcess:
        returncode = None
        pid = 4321

        async def wait(self) -> int:
            return 0

    async def fake_spawn(*_args, **kwargs):
        captured.update(kwargs)
        return _FakeProcess()

    opened_fds: list[int] = []

    def fake_openpty():
        master = os.open(os.devnull, os.O_RDWR)
        slave = os.open(os.devnull, os.O_RDWR)
        opened_fds.extend([master, slave])
        return master, slave

    monkeypatch.setattr(terminal_service.asyncio, "create_subprocess_exec", fake_spawn)
    monkeypatch.setattr(terminal_service.os, "openpty", fake_openpty)

    service = TerminalService(idle_timeout_seconds=60, max_sessions=1)
    try:
        await service.open("spawn")
        assert captured.get("start_new_session") is True
        assert "preexec_fn" not in captured
    finally:
        await service.shutdown()
        for fd in opened_fds:
            terminal_service._close_fd(fd)


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
        assert len(service._sessions) == 1
        assert service._sessions["dup"].connection is second
        assert first.process.returncode is not None
    finally:
        await service.shutdown()


def test_reconnect_clears_detached_bookkeeping(monkeypatch, tmp_path):
    asyncio.run(_reconnect_clears_detached_bookkeeping(monkeypatch, tmp_path))


async def _reconnect_clears_detached_bookkeeping(monkeypatch, tmp_path):
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(terminal_service.Path, "home", lambda: tmp_path / "home")
    monkeypatch.setattr(terminal_service, "resolve_tmux_binary", lambda: "/usr/bin/tmux")
    await _set_tmux_has_session(monkeypatch, True)

    class _FakeProcess:
        returncode = None
        pid = None

        async def wait(self) -> int:
            return 0

    async def fake_spawn(*_args, **_kwargs):
        return _FakeProcess()

    monkeypatch.setattr(terminal_service.asyncio, "create_subprocess_exec", fake_spawn)

    opened_fds: list[int] = []

    def fake_openpty():
        master = os.open(os.devnull, os.O_RDWR)
        slave = os.open(os.devnull, os.O_RDWR)
        opened_fds.extend([master, slave])
        return master, slave

    monkeypatch.setattr(terminal_service.os, "openpty", fake_openpty)

    service = TerminalService(idle_timeout_seconds=60, max_sessions=2)
    existing = _make_persistent_connection("id", process=_LiveProcess())
    service._sessions["id"] = _Session("id", _Phase.ATTACHED, persistent=True, connection=existing)

    try:
        spawned = await service.open("id")
        # One entry per id: the reconnect transitions the slot in place — ATTACHED to the new
        # client, never simultaneously DETACHED. The old three-collection desync (an id in
        # both _connections and _detached_tmux_sessions) is structurally impossible now.
        assert list(service._sessions) == ["id"]
        assert service._sessions["id"].phase is _Phase.ATTACHED
        assert service._sessions["id"].connection is spawned
    finally:
        await service.shutdown()
        for fd in opened_fds:
            terminal_service._close_fd(fd)


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
        assert service._sessions["first"].connection is connection
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

    assert "cancelled" not in service._sessions  # the OPENING placeholder was reconciled away
    # The PTY master opened for the cancelled spawn must be closed, not leaked.
    assert opened_masters, "openpty was not called"
    assert opened_masters[0] in closed_fds

    monkeypatch.setattr(terminal_service.asyncio, "create_subprocess_exec", original_spawn)
    connection = await service.open("next")
    try:
        assert service._sessions["next"].connection is connection
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
        assert service._sessions == {}
    finally:
        await service.shutdown()


def test_detached_session_tracked_when_client_exits(monkeypatch, tmp_path):
    asyncio.run(_detached_session_tracked_when_client_exits(monkeypatch, tmp_path))


class _ExitedProcess:
    returncode = 0
    pid = None

    async def wait(self) -> int:
        return 0


class _LiveProcess:
    returncode = None
    pid = None

    async def wait(self) -> int:
        return 0


def _make_persistent_connection(
    session_id: str,
    *,
    process=None,
) -> "terminal_service.TerminalConnection":
    fd = os.open(os.devnull, os.O_RDWR)
    return terminal_service.TerminalConnection(
        session_id=session_id,
        process=process or _ExitedProcess(),
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
    # user hit tmux's detach key — must transition to DETACHED WHEN THE SESSION IS STILL
    # ALIVE, or it goes uncounted against max_sessions, unreaped, and unkilled.
    await _set_tmux_has_session(monkeypatch, True)
    service = TerminalService(idle_timeout_seconds=60, max_sessions=2)
    conn = _make_persistent_connection("detached", process=_ExitedProcess())
    service._sessions["detached"] = _Session("detached", _Phase.ATTACHED, persistent=True, connection=conn)

    await service.close(conn)

    assert service._sessions["detached"].phase is _Phase.DETACHED


def test_terminate_drops_only_target_session_and_frees_capacity(monkeypatch, tmp_path):
    asyncio.run(_terminate_drops_only_target_session_and_frees_capacity(monkeypatch, tmp_path))


async def _terminate_drops_only_target_session_and_frees_capacity(monkeypatch, tmp_path):
    killed: list[str] = []

    async def fake_kill(session_id: str) -> None:
        killed.append(session_id)

    monkeypatch.setattr(terminal_service, "_kill_tmux_session", fake_kill)
    monkeypatch.setattr(terminal_service, "resolve_tmux_binary", lambda: None)
    monkeypatch.setattr(terminal_service.Path, "home", lambda: tmp_path / "home")
    (tmp_path / "home").mkdir()

    opened_fds: list[int] = []

    def fake_openpty():
        master = os.open(os.devnull, os.O_RDWR)
        slave = os.open(os.devnull, os.O_RDWR)
        opened_fds.extend([master, slave])
        return master, slave

    class _FakeProcess:
        returncode = None
        pid = None

        def send_signal(self, _signum: int) -> None:
            pass

        async def wait(self) -> int:
            return 0

    async def fake_spawn(*_args, **_kwargs):
        return _FakeProcess()

    monkeypatch.setattr(terminal_service.os, "openpty", fake_openpty)
    monkeypatch.setattr(terminal_service.asyncio, "create_subprocess_exec", fake_spawn)

    service = TerminalService(idle_timeout_seconds=60, max_sessions=2)
    service._sessions["a"] = _Session("a", _Phase.DETACHED, persistent=True, detached_at=1.0)
    service._sessions["b"] = _Session("b", _Phase.DETACHED, persistent=True, detached_at=1.0)

    try:
        assert await service.terminate("a") is True

        assert killed == ["a"]
        assert "a" not in service._sessions
        assert service._sessions["b"].phase is _Phase.DETACHED

        # The target was removed from _sessions, so the cap has room for one new id.
        # If terminate only killed tmux but left "a" registered, this open fails with
        # too_many_sessions because "a" + "b" still fill max_sessions=2.
        conn = await service.open("c")
        assert conn.session_id == "c"
        assert set(service._sessions) == {"b", "c"}

        assert await service.terminate("missing") is False
    finally:
        await service.shutdown()
        for fd in opened_fds:
            terminal_service._close_fd(fd)


def test_open_rejects_same_id_while_terminate_is_in_progress(monkeypatch):
    asyncio.run(_open_rejects_same_id_while_terminate_is_in_progress(monkeypatch))


async def _open_rejects_same_id_while_terminate_is_in_progress(monkeypatch):
    gate = asyncio.Event()
    killed: list[str] = []

    async def gated_kill(session_id: str) -> None:
        killed.append(session_id)
        await gate.wait()

    monkeypatch.setattr(terminal_service, "_kill_tmux_session", gated_kill)

    service = TerminalService(idle_timeout_seconds=60, max_sessions=2)
    service._sessions["a"] = _Session("a", _Phase.DETACHED, persistent=True, detached_at=1.0)

    terminate_task = asyncio.create_task(service.terminate("a"))
    await asyncio.sleep(0.02)
    try:
        with pytest.raises(terminal_service.TerminalServiceError, match="session_opening"):
            await service.open("a")
    finally:
        gate.set()
        await terminate_task

    assert killed == ["a"]
    assert "a" not in service._sessions


def test_terminate_keeps_opening_session_reserved_until_abandon_settles(monkeypatch, tmp_path):
    asyncio.run(_terminate_keeps_opening_session_reserved_until_abandon_settles(monkeypatch, tmp_path))


async def _terminate_keeps_opening_session_reserved_until_abandon_settles(monkeypatch, tmp_path):
    # Windowed terminal ids come from a small reusable slot pool. If DELETE releases an id
    # while the original websocket open is still abandoning, the next window can reuse the id
    # and the stale abandon can tear down that newer terminal's tmux session.
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(terminal_service.Path, "home", lambda: tmp_path / "home")
    monkeypatch.setattr(terminal_service, "resolve_tmux_binary", lambda: "/usr/bin/tmux")

    killed: list[str] = []
    kill_started = asyncio.Event()

    async def fake_kill(session_id: str) -> None:
        killed.append(session_id)
        kill_started.set()

    monkeypatch.setattr(terminal_service, "_kill_tmux_session", fake_kill)

    opened_fds: list[int] = []

    def fake_openpty():
        master = os.open(os.devnull, os.O_RDWR)
        slave = os.open(os.devnull, os.O_RDWR)
        opened_fds.extend([master, slave])
        return master, slave

    monkeypatch.setattr(terminal_service.os, "openpty", fake_openpty)

    spawn_started = asyncio.Event()
    release_stale_spawn = asyncio.Event()
    spawn_calls = 0

    class _FakeProcess:
        returncode = None
        pid = None

        def send_signal(self, _signum: int) -> None:
            pass

        async def wait(self) -> int:
            return 0

    async def gated_spawn(*_args, **_kwargs):
        nonlocal spawn_calls
        spawn_calls += 1
        if spawn_calls == 1:
            spawn_started.set()
            await release_stale_spawn.wait()
        return _FakeProcess()

    monkeypatch.setattr(terminal_service.asyncio, "create_subprocess_exec", gated_spawn)

    service = TerminalService(idle_timeout_seconds=60, max_sessions=1)
    stale_open = asyncio.create_task(service.open("slot-1"))
    await spawn_started.wait()

    terminate_task = asyncio.create_task(service.terminate("slot-1"))
    kill_wait = asyncio.create_task(kill_started.wait())
    done, pending = await asyncio.wait({kill_wait, terminate_task}, timeout=1, return_when=asyncio.FIRST_COMPLETED)
    assert done
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)

    try:
        reused_before_stale_settled: terminal_service.TerminalConnection | None = None
        try:
            reused_before_stale_settled = await service.open("slot-1")
        except terminal_service.TerminalServiceError as exc:
            assert str(exc) == "session_opening"

        release_stale_spawn.set()
        with pytest.raises(terminal_service.TerminalServiceError, match="session_opening"):
            await stale_open
        assert await terminate_task is True

        replacement = reused_before_stale_settled or await service.open("slot-1")
        assert service._sessions["slot-1"].connection is replacement
        assert service._sessions["slot-1"].phase is _Phase.ATTACHED
        if reused_before_stale_settled is not None:
            # If the implementation ever allows same-id reuse before the stale open settles,
            # the stale abandon still must not kill that newer tmux session by id.
            assert killed == ["slot-1"]
    finally:
        release_stale_spawn.set()
        await asyncio.gather(stale_open, terminate_task, return_exceptions=True)
        await service.shutdown()
        for fd in opened_fds:
            terminal_service._close_fd(fd)


def test_terminate_during_abandon_recheck_clears_terminating(monkeypatch, tmp_path):
    asyncio.run(_terminate_during_abandon_recheck_clears_terminating(monkeypatch, tmp_path))


async def _terminate_during_abandon_recheck_clears_terminating(monkeypatch, tmp_path):
    # DELETE can arrive while a cancelled open is already abandoning: _abandon_open has
    # sampled the slot as OPENING, released the lock, and is awaiting teardown/has-session.
    # It must re-read the slot before settling so the mid-abandon CLOSING transition clears
    # _terminating and frees the reusable window terminal id.
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(terminal_service.Path, "home", lambda: tmp_path / "home")
    monkeypatch.setattr(terminal_service, "resolve_tmux_binary", lambda: "/usr/bin/tmux")

    opened_fds: list[int] = []

    def fake_openpty():
        master = os.open(os.devnull, os.O_RDWR)
        slave = os.open(os.devnull, os.O_RDWR)
        opened_fds.extend([master, slave])
        return master, slave

    monkeypatch.setattr(terminal_service.os, "openpty", fake_openpty)

    class _FakeProcess:
        returncode = None
        pid = None

        def send_signal(self, signum: int) -> None:
            self.returncode = -signum

        def kill(self) -> None:
            self.returncode = -signal.SIGKILL

        async def wait(self) -> int:
            return self.returncode or 0

    spawn_started = asyncio.Event()
    release_spawn = asyncio.Event()

    async def gated_spawn(*_args, **_kwargs):
        spawn_started.set()
        await release_spawn.wait()
        return _FakeProcess()

    monkeypatch.setattr(terminal_service.asyncio, "create_subprocess_exec", gated_spawn)

    async def fake_has_session(_session_id: str) -> bool:
        return True

    monkeypatch.setattr(terminal_service, "_tmux_has_session", fake_has_session)

    killed: list[str] = []

    async def fake_kill(session_id: str) -> None:
        killed.append(session_id)

    monkeypatch.setattr(terminal_service, "_kill_tmux_session", fake_kill)

    service = TerminalService(idle_timeout_seconds=60, max_sessions=1)
    teardown_started = asyncio.Event()
    release_teardown = asyncio.Event()
    teardown_calls = 0

    async def gated_teardown(
        connection: terminal_service.TerminalConnection,
        *,
        kill_session: bool,
    ) -> None:
        nonlocal teardown_calls
        teardown_calls += 1
        if teardown_calls == 1:
            teardown_started.set()
            await release_teardown.wait()
        fd, connection.master_fd = connection.master_fd, -1
        if fd >= 0:
            terminal_service._close_fd(fd)
        if kill_session and connection.persistent:
            await fake_kill(connection.session_id)
        if connection.process.returncode is None:
            connection.process.send_signal(signal.SIGHUP if connection.persistent else signal.SIGTERM)

    monkeypatch.setattr(service, "_teardown_client", gated_teardown)

    stale_open = asyncio.create_task(service.open("slot-race"))
    await spawn_started.wait()
    await service._lock.acquire()
    try:
        release_spawn.set()
        await asyncio.sleep(0)
        stale_open.cancel()
    finally:
        service._lock.release()

    await teardown_started.wait()
    terminate_task = asyncio.create_task(service.terminate("slot-race"))
    assert await terminate_task is True
    assert service._sessions["slot-race"].phase is _Phase.CLOSING
    assert "slot-race" in service._terminating

    release_teardown.set()
    with pytest.raises(asyncio.CancelledError):
        await stale_open

    assert "slot-race" not in service._terminating
    assert "slot-race" not in service._sessions
    assert killed == ["slot-race"]

    replacement = await service.open("slot-race")
    try:
        assert service._sessions["slot-race"].connection is replacement
        assert service._sessions["slot-race"].phase is _Phase.ATTACHED
    finally:
        await service.shutdown()
        for fd in opened_fds:
            terminal_service._close_fd(fd)


def test_dead_session_not_tracked_when_shell_exits(monkeypatch, tmp_path):
    asyncio.run(_dead_session_not_tracked_when_shell_exits(monkeypatch, tmp_path))


async def _dead_session_not_tracked_when_shell_exits(monkeypatch, tmp_path):
    # When the shell inside tmux exits (rather than a detach), the client process exits AND
    # the tmux session is gone — it must NOT be left tracked, or a dead id counts against
    # max_sessions until the idle timeout.
    await _set_tmux_has_session(monkeypatch, False)
    service = TerminalService(idle_timeout_seconds=60, max_sessions=2)
    conn = _make_persistent_connection("ended", process=_ExitedProcess())
    service._sessions["ended"] = _Session("ended", _Phase.ATTACHED, persistent=True, connection=conn)

    await service.close(conn)

    assert "ended" not in service._sessions


def test_detach_not_recorded_when_session_gone_despite_live_client(monkeypatch, tmp_path):
    asyncio.run(_detach_not_recorded_when_session_gone_despite_live_client(monkeypatch, tmp_path))


async def _detach_not_recorded_when_session_gone_despite_live_client(monkeypatch, tmp_path):
    await _set_tmux_has_session(monkeypatch, False)
    service = TerminalService(idle_timeout_seconds=60, max_sessions=2)
    conn = _make_persistent_connection("raced", process=_LiveProcess())
    service._sessions["raced"] = _Session("raced", _Phase.ATTACHED, persistent=True, connection=conn)

    await service.close(conn)

    assert "raced" not in service._sessions


def test_superseded_close_leaves_live_session(monkeypatch, tmp_path):
    asyncio.run(_superseded_close_leaves_live_session(monkeypatch, tmp_path))


async def _superseded_close_leaves_live_session(monkeypatch, tmp_path):
    # A fast reconnect already registered a new connection for this id; the OLD connection's
    # close must not touch the registry — no double-count, and the reaper must not later kill
    # the session out from under the live client.
    await _set_tmux_has_session(monkeypatch, True)
    service = TerminalService(idle_timeout_seconds=60, max_sessions=2)
    live = _make_persistent_connection("id", process=_LiveProcess())
    service._sessions["id"] = _Session("id", _Phase.ATTACHED, persistent=True, connection=live)
    superseded = _make_persistent_connection("id", process=_ExitedProcess())

    await service.close(superseded)

    assert service._sessions["id"].connection is live
    assert service._sessions["id"].phase is _Phase.ATTACHED
    await service.shutdown()


def test_open_failure_preserves_detached_session(monkeypatch, tmp_path):
    asyncio.run(_open_failure_preserves_detached_session(monkeypatch, tmp_path))


async def _open_failure_preserves_detached_session(monkeypatch, tmp_path):
    # Reconnecting to a DETACHED session whose spawn then fails must leave the (still-alive)
    # tmux session tracked as DETACHED — never drop it into an untracked orphan.
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(terminal_service.Path, "home", lambda: tmp_path / "home")
    monkeypatch.setattr(terminal_service, "resolve_tmux_binary", lambda: "/usr/bin/tmux")
    await _set_tmux_has_session(monkeypatch, True)

    async def failing_spawn(*_args, **_kwargs):
        raise OSError("spawn failed")

    monkeypatch.setattr(terminal_service.asyncio, "create_subprocess_exec", failing_spawn)

    service = TerminalService(idle_timeout_seconds=60, max_sessions=1)
    service._sessions["id"] = _Session("id", _Phase.DETACHED, persistent=True, detached_at=1.0)

    with pytest.raises(OSError, match="spawn failed"):
        await service.open("id")

    assert service._sessions["id"].phase is _Phase.DETACHED
    await service.shutdown()


def test_abandoned_open_kills_orphan_tmux_session(monkeypatch, tmp_path):
    asyncio.run(_abandoned_open_kills_orphan_tmux_session(monkeypatch, tmp_path))


async def _abandoned_open_kills_orphan_tmux_session(monkeypatch, tmp_path):
    # When the OPENING slot is gone (service shutting down) after a persistent child spawned,
    # _abandon_open must KILL the tmux session, not merely detach it — otherwise a terminal
    # opened during `vibe stop` leaves an orphaned tmux server.
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(terminal_service.Path, "home", lambda: tmp_path / "home")
    monkeypatch.setattr(terminal_service, "resolve_tmux_binary", lambda: "/usr/bin/tmux")

    killed: list[str] = []

    async def fake_kill(session_id: str) -> None:
        killed.append(session_id)

    monkeypatch.setattr(terminal_service, "_kill_tmux_session", fake_kill)

    opened_fds: list[int] = []

    def fake_openpty():
        master = os.open(os.devnull, os.O_RDWR)
        slave = os.open(os.devnull, os.O_RDWR)
        opened_fds.extend([master, slave])
        return master, slave

    monkeypatch.setattr(terminal_service.os, "openpty", fake_openpty)

    class _FakeProcess:
        returncode = None
        pid = None

        def send_signal(self, signum: int) -> None:
            pass

        async def wait(self) -> int:
            return 0

    service = TerminalService(idle_timeout_seconds=60, max_sessions=1)

    async def fake_spawn(*_args, **_kwargs):
        service._sessions.clear()  # simulate shutdown winning the race mid-spawn
        return _FakeProcess()

    monkeypatch.setattr(terminal_service.asyncio, "create_subprocess_exec", fake_spawn)

    try:
        with pytest.raises(terminal_service.TerminalServiceError, match="session_opening"):
            await service.open("orphan")
        assert "orphan" in killed  # the orphaned tmux session was killed, not just detached
    finally:
        for fd in opened_fds:
            terminal_service._close_fd(fd)


def test_reaper_retracks_dead_client_with_live_session(monkeypatch, tmp_path):
    asyncio.run(_reaper_retracks_dead_client_with_live_session(monkeypatch, tmp_path))


async def _reaper_retracks_dead_client_with_live_session(monkeypatch, tmp_path):
    # A tmux client can exit (returncode set) while its session is still alive (the user
    # detached). If the reaper sees it before close() runs, it must reconcile via has-session
    # and re-track it as DETACHED — not silently drop a live, uncounted session.
    await _set_tmux_has_session(monkeypatch, True)
    service = TerminalService(idle_timeout_seconds=60, max_sessions=2)
    conn = _make_persistent_connection("d", process=_ExitedProcess())
    service._sessions["d"] = _Session("d", _Phase.ATTACHED, persistent=True, connection=conn)

    await service.reap_idle()

    assert service._sessions["d"].phase is _Phase.DETACHED


def test_forget_finished_keeps_persistent_dead_session(monkeypatch, tmp_path):
    asyncio.run(_forget_finished_keeps_persistent_dead_session(monkeypatch, tmp_path))


async def _forget_finished_keeps_persistent_dead_session(monkeypatch, tmp_path):
    # open()'s synchronous GC must not drop a persistent (tmux) session whose client process
    # exited — its tmux session may still be alive (a prefix-key detach), so dropping it here
    # would untrack a live session. Ephemeral dead clients have no session and are dropped.
    service = TerminalService(idle_timeout_seconds=60, max_sessions=4)
    pconn = _make_persistent_connection("p", process=_ExitedProcess())
    service._sessions["p"] = _Session("p", _Phase.ATTACHED, persistent=True, connection=pconn)
    efd = os.open(os.devnull, os.O_RDWR)
    econn = terminal_service.TerminalConnection("e", _ExitedProcess(), efd, False, 0.0, 0.0)
    service._sessions["e"] = _Session("e", _Phase.ATTACHED, persistent=False, connection=econn)

    service._forget_finished_locked()

    assert "p" in service._sessions  # persistent dead kept (reaper finalize reconciles via has-session)
    assert "e" not in service._sessions  # ephemeral dead dropped (no tmux session to preserve)
    terminal_service._close_fd(pconn.master_fd)
    terminal_service._close_fd(efd)


def test_reaper_keeps_detached_registered_until_kill_completes(monkeypatch, tmp_path):
    asyncio.run(_reaper_keeps_detached_registered_until_kill_completes(monkeypatch, tmp_path))


async def _reaper_keeps_detached_registered_until_kill_completes(monkeypatch, tmp_path):
    # An expired DETACHED session must stay registered (counted) while its async kill runs,
    # and only be removed once the kill completes — so a reconnect in that gap isn't dropped
    # from the cap or silently killed.
    gate = asyncio.Event()
    killed: list[str] = []

    async def gated_kill(session_id: str) -> None:
        killed.append(session_id)
        await gate.wait()

    monkeypatch.setattr(terminal_service, "_kill_tmux_session", gated_kill)
    service = TerminalService(idle_timeout_seconds=60, max_sessions=2)
    service._sessions["d"] = _Session("d", _Phase.DETACHED, persistent=True, detached_at=0.0)

    reap_task = asyncio.create_task(service.reap_idle())
    await asyncio.sleep(0.02)  # reaper marks CLOSING and parks in the gated kill

    assert killed == ["d"]
    assert "d" in service._sessions  # still registered during the kill
    assert service._sessions["d"].phase is _Phase.CLOSING

    gate.set()
    await reap_task
    assert "d" not in service._sessions  # removed once the kill completed


def test_reconnect_during_closing_is_rejected(monkeypatch, tmp_path):
    asyncio.run(_reconnect_during_closing_is_rejected(monkeypatch, tmp_path))


async def _reconnect_during_closing_is_rejected(monkeypatch, tmp_path):
    # While a session is tearing down (CLOSING) — e.g. the reaper is awaiting its kill — a
    # reconnect must be rejected with session_opening, not allowed to reattach to a session
    # that is about to be killed. The client retries once teardown finishes.
    monkeypatch.setattr(terminal_service, "resolve_tmux_binary", lambda: None)
    service = TerminalService(idle_timeout_seconds=60, max_sessions=2)
    conn = _make_persistent_connection("c", process=_ExitedProcess())
    service._sessions["c"] = _Session("c", _Phase.CLOSING, persistent=True, connection=conn)

    try:
        with pytest.raises(terminal_service.TerminalServiceError, match="session_opening"):
            await service.open("c")
    finally:
        service._sessions.clear()
        terminal_service._close_fd(conn.master_fd)


def test_reconnect_window_session_killed_on_shutdown(monkeypatch, tmp_path):
    asyncio.run(_reconnect_window_session_killed_on_shutdown(monkeypatch, tmp_path))


async def _reconnect_window_session_killed_on_shutdown(monkeypatch, tmp_path):
    # When a reconnect overwrites a persistent session's slot with an OPENING placeholder, the
    # placeholder must carry persistent=True so a shutdown landing in that window still kills
    # the replaced tmux session instead of leaking it after `vibe stop`.
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(terminal_service.Path, "home", lambda: tmp_path / "home")
    monkeypatch.setattr(terminal_service, "resolve_tmux_binary", lambda: "/usr/bin/tmux")

    killed: list[str] = []

    async def fake_kill(session_id: str) -> None:
        killed.append(session_id)

    monkeypatch.setattr(terminal_service, "_kill_tmux_session", fake_kill)

    opened_fds: list[int] = []

    def fake_openpty():
        master = os.open(os.devnull, os.O_RDWR)
        slave = os.open(os.devnull, os.O_RDWR)
        opened_fds.extend([master, slave])
        return master, slave

    monkeypatch.setattr(terminal_service.os, "openpty", fake_openpty)

    spawn_started = asyncio.Event()
    gate = asyncio.Event()

    class _FakeProcess:
        returncode = None
        pid = None

        def send_signal(self, signum: int) -> None:
            pass

        async def wait(self) -> int:
            return 0

    async def gated_spawn(*_args, **_kwargs):
        spawn_started.set()
        await gate.wait()
        return _FakeProcess()

    monkeypatch.setattr(terminal_service.asyncio, "create_subprocess_exec", gated_spawn)

    service = TerminalService(idle_timeout_seconds=60, max_sessions=2)
    old = _make_persistent_connection("s", process=_LiveProcess())
    service._sessions["s"] = _Session("s", _Phase.ATTACHED, persistent=True, connection=old)

    reopen = asyncio.create_task(service.open("s"))
    await spawn_started.wait()  # reconnect overwrote the slot with OPENING and parked mid-spawn

    assert service._sessions["s"].phase is _Phase.OPENING
    assert service._sessions["s"].persistent is True  # placeholder stays visible to shutdown

    # shutdown() now also kills the whole tmux server; stub that out so it doesn't block on the
    # gated open-spawn above. This test asserts the tracked per-session kill (_kill_tmux_session).
    async def fake_kill_server() -> None:
        pass

    monkeypatch.setattr(terminal_service, "_kill_tmux_server", fake_kill_server)
    await service.shutdown()
    assert "s" in killed  # shutdown killed the replaced tmux session rather than leaking it

    gate.set()
    with pytest.raises(terminal_service.TerminalServiceError):
        await reopen
    for fd in opened_fds:
        terminal_service._close_fd(fd)


def test_teardown_client_is_idempotent_on_fd(monkeypatch, tmp_path):
    asyncio.run(_teardown_client_is_idempotent_on_fd(monkeypatch, tmp_path))


async def _teardown_client_is_idempotent_on_fd(monkeypatch, tmp_path):
    # A superseded connection can be torn down twice (open() replaces it, then the old
    # websocket's close() runs). The second teardown must NOT close the fd again — the number
    # may have been reused by the replacement terminal's PTY, and closing it would kill the
    # new terminal. First teardown closes once + poisons master_fd to -1; repeat is a no-op.
    monkeypatch.setattr(terminal_service, "resolve_tmux_binary", lambda: None)
    service = TerminalService(idle_timeout_seconds=60, max_sessions=2)
    closed: list[int] = []
    real_close = terminal_service._close_fd
    monkeypatch.setattr(terminal_service, "_close_fd", lambda fd: (closed.append(fd), real_close(fd)) and None)

    fd = os.open(os.devnull, os.O_RDWR)
    conn = terminal_service.TerminalConnection("x", _ExitedProcess(), fd, False, 0.0, 0.0)

    await service._teardown_client(conn, kill_session=False)
    assert conn.master_fd == -1  # poisoned
    assert closed == [fd]  # real fd closed exactly once

    await service._teardown_client(conn, kill_session=False)  # stale repeat teardown
    assert closed == [fd]  # closed nothing the second time


def test_closing_session_still_counts_against_cap(monkeypatch, tmp_path):
    asyncio.run(_closing_session_still_counts_against_cap(monkeypatch, tmp_path))


async def _closing_session_still_counts_against_cap(monkeypatch, tmp_path):
    # During a real close(), the session must keep counting toward max_sessions across the
    # async teardown window — a concurrent open for a different id must not slip past the cap
    # while the closing session's tmux state is still being reconciled.
    monkeypatch.setattr(terminal_service, "resolve_tmux_binary", lambda: "/usr/bin/tmux")
    gate = asyncio.Event()

    async def gated_has_session(_session_id: str) -> bool:
        await gate.wait()
        return True

    monkeypatch.setattr(terminal_service, "_tmux_has_session", gated_has_session)

    service = TerminalService(idle_timeout_seconds=60, max_sessions=1)
    conn = _make_persistent_connection("a", process=_ExitedProcess())
    service._sessions["a"] = _Session("a", _Phase.ATTACHED, persistent=True, connection=conn)

    close_task = asyncio.create_task(service.close(conn))
    await asyncio.sleep(0.02)  # let close() reach CLOSING and park in the gated has-session
    try:
        with pytest.raises(terminal_service.TerminalServiceError, match="too_many_sessions"):
            await service.open("b")
    finally:
        gate.set()
        await close_task

    # The session was alive (has-session True), so it settled to DETACHED — still one slot.
    assert service._sessions["a"].phase is _Phase.DETACHED
    await service.shutdown()


def test_open_cleans_up_process_when_registration_fails(monkeypatch, tmp_path):
    asyncio.run(_open_cleans_up_process_when_registration_fails(monkeypatch, tmp_path))


async def _open_cleans_up_process_when_registration_fails(monkeypatch, tmp_path):
    # If the OPENING slot is taken away after the child has spawned but before it is
    # registered (e.g. the service shuts down, or a forced supersession), the spawned
    # process must still be torn down — otherwise it lives outside the registry where
    # shutdown/reaping can never reach it.
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

    service = TerminalService(idle_timeout_seconds=60, max_sessions=1)

    async def fake_spawn(*_args, **_kwargs):
        # Drop the OPENING placeholder mid-spawn so the child can never be registered.
        service._sessions.clear()
        return _FakeProcess()

    monkeypatch.setattr(terminal_service.asyncio, "create_subprocess_exec", fake_spawn)

    with pytest.raises(terminal_service.TerminalServiceError, match="session_opening"):
        await service.open("orphan")

    assert "orphan" not in service._sessions
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
            assert service._sessions["dup"].connection is replacement
            assert first.session_id == "dup"
        finally:
            await service.shutdown()


def test_spawn_env_drops_c_lc_all(monkeypatch):
    # LC_ALL overrides LANG/LC_CTYPE; an inherited C/POSIX LC_ALL must be dropped so the
    # UTF-8 fallback actually takes effect.
    monkeypatch.setenv("LC_ALL", "C")
    monkeypatch.delenv("LANG", raising=False)
    monkeypatch.delenv("LC_CTYPE", raising=False)

    env = terminal_service._spawn_env()

    assert "LC_ALL" not in env
    assert env["LANG"].endswith("UTF-8")
    assert env["LC_CTYPE"].endswith("UTF-8")


def test_spawn_env_keeps_real_lc_all(monkeypatch):
    monkeypatch.setenv("LC_ALL", "en_US.UTF-8")

    env = terminal_service._spawn_env()

    assert env["LC_ALL"] == "en_US.UTF-8"


def test_spawn_env_drops_inherited_tmux_context(monkeypatch):
    # The Avibe daemon may itself run inside tmux. Every spawned terminal (vendored tmux
    # client or plain fallback shell) must NOT inherit that pane's TMUX/TMUX_PANE, or it
    # targets the operator's tmux socket and nests/targets the wrong server.
    monkeypatch.setenv("TMUX", "/tmp/tmux-501/default,1234,0")
    monkeypatch.setenv("TMUX_PANE", "%3")

    env = terminal_service._spawn_env()

    # Use .get(...) is None rather than "x not in env": a failed membership assert would dump
    # the entire spawned environment (API keys included) into the test log.
    assert env.get("TMUX") is None
    assert env.get("TMUX_PANE") is None


def test_sanitize_session_id_allows_only_contract_chars():
    assert sanitize_session_id("../bad session!") == "bad_session"
    assert sanitize_session_id("abc-DEF_123") == "abc-DEF_123"


def test_terminal_websocket_disabled_when_flag_off(monkeypatch, tmp_path):
    # The terminal is ON by default; an explicit VIBE_UI_ENABLE_TERMINAL=0 disables it.
    monkeypatch.setenv("VIBE_UI_ENABLE_TERMINAL", "0")
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    with app.test_client().websocket_connect(
        "/api/terminal/test",
        headers={"host": "127.0.0.1", "origin": "http://127.0.0.1"},
    ) as websocket:
        with pytest.raises(WebSocketDisconnect) as exc:
            websocket.receive_text()

    assert exc.value.code == 1008


def test_terminal_websocket_disabled_accepts_before_policy_close(monkeypatch):
    monkeypatch.setenv("VIBE_UI_ENABLE_TERMINAL", "0")
    websocket = _RecordingWebSocket()

    asyncio.run(ui_server.terminal_websocket(websocket, "test"))

    assert websocket.calls == [("accept", None), ("close", 1008)]


def test_terminal_websocket_unsupported_accepts_before_policy_close(monkeypatch):
    monkeypatch.setenv("VIBE_UI_ENABLE_TERMINAL", "1")
    monkeypatch.setattr(ui_server, "TERMINAL_SUPPORTED", False)
    websocket = _RecordingWebSocket()

    asyncio.run(ui_server.terminal_websocket(websocket, "test"))

    assert websocket.calls == [("accept", None), ("close", 1008)]


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


def test_terminal_delete_terminates_scoped_local_session(monkeypatch, tmp_path):
    monkeypatch.setenv("VIBE_UI_ENABLE_TERMINAL", "1")
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.setattr(ui_server, "TERMINAL_SUPPORTED", True)
    terminated: list[str] = []

    class _FakeService:
        async def terminate(self, session_id: str) -> bool:
            terminated.append(session_id)
            return True

    monkeypatch.setattr(ui_server, "get_terminal_service", lambda: _FakeService())

    client = app.test_client()
    response = client.delete(
        "/api/terminal/local-session",
        headers=csrf_headers(client, "http://127.0.0.1:5123"),
    )

    assert response.status_code == 204
    assert terminated == ["local-session"]


def test_terminal_delete_rejects_disallowed_origin_without_terminating(monkeypatch, tmp_path):
    monkeypatch.setenv("VIBE_UI_ENABLE_TERMINAL", "1")
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.setattr(ui_server, "TERMINAL_SUPPORTED", True)
    terminated: list[str] = []

    class _FakeService:
        async def terminate(self, session_id: str) -> bool:
            terminated.append(session_id)
            return True

    monkeypatch.setattr(ui_server, "get_terminal_service", lambda: _FakeService())

    client = app.test_client()
    headers = csrf_headers(client, "http://127.0.0.1:5123")
    headers["Origin"] = "http://127.0.0.1:3000"
    response = client.delete("/api/terminal/local-session", base_url="http://127.0.0.1:5123", headers=headers)

    assert response.status_code == 403
    assert terminated == []


def test_terminal_delete_rejects_forwarded_origin_proxy_without_terminating(monkeypatch, tmp_path):
    monkeypatch.setenv("VIBE_UI_ENABLE_TERMINAL", "1")
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.setattr(ui_server, "TERMINAL_SUPPORTED", True)
    _save_remote_config(tmp_path)
    terminated: list[str] = []

    class _FakeService:
        async def terminate(self, session_id: str) -> bool:
            terminated.append(session_id)
            return True

    monkeypatch.setattr(ui_server, "get_terminal_service", lambda: _FakeService())

    client = app.test_client()
    headers = csrf_headers(client, "http://127.0.0.1:5123")
    # The generic HTTP CSRF guard accepts this loopback-origin-proxy shape because
    # _current_origin honors the forwarded host/proto. Terminal disposal still must
    # apply the stricter terminal Origin guard, which rejects forwarded metadata.
    headers.update(
        {
            "Origin": "https://vibe.example",
            "X-Forwarded-Proto": "https",
            "X-Forwarded-Host": "vibe.example",
        }
    )
    response = client.delete("/api/terminal/local-session", base_url="http://127.0.0.1:5123", headers=headers)

    assert response.status_code == 403
    assert terminated == []


def test_terminal_delete_scopes_remote_subject_and_rejects_cross_subject(monkeypatch, tmp_path):
    monkeypatch.setenv("VIBE_UI_ENABLE_TERMINAL", "1")
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.setattr(ui_server, "TERMINAL_SUPPORTED", True)
    config = _save_remote_config(tmp_path)
    user_one_effective = ui_server._terminal_effective_session_id("shared-session", "user-1")
    terminated: list[str] = []

    class _FakeService:
        async def terminate(self, session_id: str) -> bool:
            terminated.append(session_id)
            return session_id == user_one_effective

    monkeypatch.setattr(ui_server, "get_terminal_service", lambda: _FakeService())

    user_two_client = app.test_client()
    user_two_client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        remote_access.make_session_cookie(config, "user-2@example.com", "user-2"),
        domain="alex.avibe.bot",
    )
    headers = csrf_headers(user_two_client, "https://alex.avibe.bot")
    response = user_two_client.delete(
        "/api/terminal/shared-session",
        base_url="https://alex.avibe.bot",
        headers=headers,
        environ_base={"REMOTE_ADDR": "203.0.113.10"},
    )
    malicious_response = user_two_client.delete(
        f"/api/terminal/{user_one_effective}",
        base_url="https://alex.avibe.bot",
        headers=headers,
        environ_base={"REMOTE_ADDR": "203.0.113.10"},
    )

    assert response.status_code == 404
    assert malicious_response.status_code == 404
    assert terminated == [
        ui_server._terminal_effective_session_id("shared-session", "user-2"),
        ui_server._terminal_effective_session_id(user_one_effective, "user-2"),
    ]
    assert user_one_effective not in terminated


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
