from __future__ import annotations

import asyncio
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import shutil
import time
from typing import Any, Mapping

import pytest

from core.computer_server import (
    ComputerServerError,
    ComputerUseServer,
    DesktopLeaseManager,
    JsonRpcUpstream,
    UpstreamUnavailable,
    _fold_tool_result,
    _validate_input,
)
from core.computer_use import (
    COMPUTER_USE_TOOL_NAMES,
    ComputerUseState,
    ComputerUseStatus,
    ToolSnapshot,
)


def _snapshot() -> dict[str, Any]:
    source = Path("desktop/cua-driver/tools-v0.31.0.json")
    return json.loads(source.read_text(encoding="utf-8"))


def _state(tmp_path: Path, *, generation: int = 1) -> tuple[Path, ComputerUseState]:
    snapshot_path = tmp_path / "tools.json"
    shutil.copyfile("desktop/cua-driver/tools-v0.31.0.json", snapshot_path)
    digest = hashlib.sha256(snapshot_path.read_bytes()).hexdigest()
    state = ComputerUseState(
        schema_version=1,
        enabled=True,
        state="ready",
        reason=None,
        shell_pid=123,
        instance_id="shell-a",
        generation=generation,
        driver_version="0.31.0",
        tool_snapshot=ToolSnapshot(snapshot_path, digest),
        socket_path=str(tmp_path / "driver.sock"),
        proxy_executable=str(tmp_path / "cua-driver"),
        host_bundle_id="bot.avibe.desktop.acceptance",
    )
    payload = {
        "schema_version": 1,
        "enabled": True,
        "state": "ready",
        "reason": None,
        "shell_pid": 123,
        "instance_id": state.instance_id,
        "generation": state.generation,
        "driver_version": state.driver_version,
        "tool_snapshot": {
            "path": str(snapshot_path),
            "sha256": digest,
        },
        "socket_path": state.socket_path,
        "proxy_executable": state.proxy_executable,
        "host_bundle_id": state.host_bundle_id,
    }
    state_path = tmp_path / "computer-use.json"
    state_path.write_text(json.dumps(payload), encoding="utf-8")
    return state_path, state


class FakeUpstream:
    _serial = 0

    def __init__(self) -> None:
        type(self)._serial += 1
        self.serial = type(self)._serial
        self.alive = True
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.inflight = 0
        self.max_inflight = 0
        self.block: asyncio.Event | None = None
        self.fail_transport = False
        self.fail_after_unblock = False
        self.close_calls = 0

    async def request(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        assert method == "tools/call"
        if self.fail_transport:
            raise UpstreamUnavailable("transport failed")
        copied = json.loads(json.dumps(params))
        self.calls.append((method, copied))
        self.inflight += 1
        self.max_inflight = max(self.max_inflight, self.inflight)
        try:
            if self.block is not None and params.get("name") == "list_apps":
                await self.block.wait()
            if self.fail_after_unblock:
                raise UpstreamUnavailable("transport failed after replacement")
            return {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "content": [{"type": "image", "data": "png", "mimeType": "image/png"}],
                    "structuredContent": {"tool": params.get("name")},
                },
            }
        finally:
            self.inflight -= 1

    async def notify(self, method: str, params: Mapping[str, Any]) -> None:
        return None

    async def close(self) -> None:
        self.close_calls += 1
        self.alive = False


class FakeLeaseManager:
    def __init__(self) -> None:
        self.epoch = 1
        self.holder: str | None = None
        self.daemon_key: tuple[str, int] | None = None
        self.refresh_count = 0

    def acquire(
        self,
        session: str,
        daemon_key: tuple[str, int],
        current_daemon_key,
    ):
        from core.computer_server import (
            ComputerServerError,
            Lease,
            LeaseAcquisition,
        )

        if current_daemon_key() != daemon_key:
            raise RuntimeError("stale fake daemon key")
        if self.daemon_key != daemon_key:
            self.holder = None
            self.daemon_key = daemon_key
        if self.holder is not None and self.holder != session:
            raise ComputerServerError("desktop_busy", f"held by {self.holder}")
        newly_claimed = self.holder != session
        self.holder = session
        return LeaseAcquisition(
            lease=Lease(session, 1.0, self.epoch, daemon_key[0], daemon_key[1]),
            newly_claimed=newly_claimed,
        )

    def refresh(self, lease) -> bool:
        self.refresh_count += 1
        return self.holder == lease.holder and self.daemon_key == lease.daemon_key

    def release(self, lease) -> None:
        if self.holder == lease.holder and self.daemon_key == lease.daemon_key:
            self.holder = None


class _TestStdin:
    def __init__(self) -> None:
        self.writes: list[bytes] = []

    def write(self, payload: bytes) -> None:
        self.writes.append(payload)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        return None

    async def wait_closed(self) -> None:
        return None


class _EofStream:
    async def readline(self) -> bytes:
        return b""


class _BlockedStream:
    def __init__(self) -> None:
        self.release = asyncio.Event()

    async def readline(self) -> bytes:
        await self.release.wait()
        return b""


class _TransportProcess:
    def __init__(self, stdout) -> None:
        self.returncode = None
        self.stdin = _TestStdin()
        self.stdout = stdout
        self.stderr = _EofStream()

    async def wait(self) -> int:
        self.returncode = 0
        return 0

    def kill(self) -> None:
        self.returncode = -9


def _acquire_lease_in_process(
    directory: str,
    session: str,
    start: multiprocessing.synchronize.Event,
    results: multiprocessing.queues.Queue,
) -> None:
    manager = DesktopLeaseManager(Path(directory), now=time.monotonic)
    start.wait()
    try:
        acquisition = manager.acquire(
            session,
            ("shell-a", 1),
            lambda: ("shell-a", 1),
        )
    except ComputerServerError as exc:
        results.put(("error", exc.code))
    else:
        results.put(("ok", acquisition.lease.holder))


def test_advertised_surface_requires_session_and_drops_output_schema(
    tmp_path: Path,
) -> None:
    """Schema-driven clients must identify every call, including list_sessions."""

    state_path, state = _state(tmp_path)
    server = ComputerUseServer(
        state_path=state_path,
        status_reader=lambda: ComputerUseStatus("ready", None, state),
        upstream_factory=lambda _state: None,  # type: ignore[arg-type]
        lease_manager=FakeLeaseManager(),  # type: ignore[arg-type]
    )
    assert len(server.tools) == 28
    assert {tool["name"] for tool in server.tools} == set(COMPUTER_USE_TOOL_NAMES)
    for tool in server.tools:
        assert "session" in tool["inputSchema"]["required"]
        assert tool["inputSchema"]["properties"]["session"]["minLength"] == 1
        assert "outputSchema" not in tool


def test_snapshot_loader_rejects_changed_or_unapproved_bytes(tmp_path: Path) -> None:
    """The server owns the final digest check before advertising tools."""

    state_path, _state_value = _state(tmp_path)
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    snapshot_path = Path(payload["tool_snapshot"]["path"])
    snapshot_path.write_text(snapshot_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="digest"):
        ComputerUseServer(state_path=state_path)


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        ("off", "never_enabled"),
        ("off", "toggle_off"),
        ("needs_permission", "accessibility"),
        ("needs_permission", "screen_recording"),
        ("starting", None),
        ("error", "spawn_failed"),
        ("stopped", None),
        ("needs_runtime", "runtime_unavailable"),
        ("unavailable", "shell_not_running"),
        ("unavailable", "daemon_unreachable"),
    ],
)
@pytest.mark.asyncio
async def test_non_ready_call_names_status_and_never_spawns_upstream(
    tmp_path: Path,
    status: str,
    reason: str | None,
) -> None:
    """Every non-ready state is returned per call without creating a proxy."""

    state_path, _state_value = _state(tmp_path)
    starts = 0

    async def factory(_state_value):
        nonlocal starts
        starts += 1
        return FakeUpstream()

    server = ComputerUseServer(
        state_path=state_path,
        status_reader=lambda: ComputerUseStatus(status, reason),
        upstream_factory=factory,
        lease_manager=FakeLeaseManager(),  # type: ignore[arg-type]
    )
    result = await server.call_tool("list_apps", {"session": "ses-a"})
    assert result["isError"] is True
    payload = json.loads(result["content"][0]["text"])
    assert payload == {
        "code": "computer_use_unavailable",
        "message": "Computer use is not ready.",
        "reason": reason,
        "status": status,
    }
    assert starts == 0


@pytest.mark.asyncio
async def test_daemon_generation_and_child_exit_each_replace_proxy_once(
    tmp_path: Path,
) -> None:
    """The backend-facing server survives both independent lifecycle changes."""

    state_path, first_state = _state(tmp_path)
    current = first_state
    upstreams: list[FakeUpstream] = []

    async def factory(_state_value):
        upstream = FakeUpstream()
        upstreams.append(upstream)
        return upstream

    leases = FakeLeaseManager()
    server = ComputerUseServer(
        state_path=state_path,
        status_reader=lambda: ComputerUseStatus("ready", None, current),
        upstream_factory=factory,
        lease_manager=leases,  # type: ignore[arg-type]
    )
    await server.call_tool("list_apps", {"session": "ses-a"})
    await server.call_tool("list_apps", {"session": "ses-a"})
    assert len(upstreams) == 1
    assert [params["name"] for _method, params in upstreams[0].calls] == [
        "start_session",
        "list_apps",
        "list_apps",
    ]

    current = ComputerUseState(
        **{
            **first_state.__dict__,
            "generation": 2,
        }
    )
    leases.epoch += 1
    await server.call_tool("list_apps", {"session": "ses-a"})
    assert len(upstreams) == 2
    assert [params["name"] for _method, params in upstreams[1].calls] == [
        "start_session",
        "list_apps",
    ]

    upstreams[-1].alive = False
    await server.call_tool("list_apps", {"session": "ses-a"})
    assert len(upstreams) == 3
    assert [params["name"] for _method, params in upstreams[2].calls] == [
        "start_session",
        "list_apps",
    ]


def test_result_folding_preserves_images_and_moves_structured_content() -> None:
    """Codex must receive image blocks even when upstream also returns structure."""

    result = _fold_tool_result(
        {
            "result": {
                "content": [{"type": "image", "data": "png", "mimeType": "image/png"}],
                "structuredContent": {"answer": 42},
                "outputSchema": {"type": "object"},
            }
        }
    )
    assert result["content"][0]["type"] == "image"
    assert result["content"][-1]["type"] == "text"
    assert '"answer":42' in result["content"][-1]["text"]
    assert "structuredContent" not in result
    assert "outputSchema" not in result


@pytest.mark.asyncio
async def test_one_sessions_calls_are_serial_and_end_releases_after_queue(
    tmp_path: Path,
) -> None:
    """A queued end_session cannot overtake an in-flight call for that session."""

    state_path, state = _state(tmp_path)
    upstream = FakeUpstream()
    upstream.block = asyncio.Event()
    leases = FakeLeaseManager()
    server = ComputerUseServer(
        state_path=state_path,
        status_reader=lambda: ComputerUseStatus("ready", None, state),
        upstream_factory=lambda _state: asyncio.sleep(0, result=upstream),
        lease_manager=leases,  # type: ignore[arg-type]
    )
    first = asyncio.create_task(
        server.call_tool("list_apps", {"session": "ses-a"})
    )
    await asyncio.sleep(0)
    ending = asyncio.create_task(
        server.call_tool("end_session", {"session": "ses-a"})
    )
    await asyncio.sleep(0)
    assert not ending.done()
    upstream.block.set()
    await first
    await ending
    assert upstream.max_inflight == 1
    assert leases.holder is None
    names = [params["name"] for _method, params in upstream.calls]
    assert names == ["start_session", "list_apps", "end_session"]


@pytest.mark.asyncio
async def test_named_sessions_start_independently_and_revive_after_end(
    tmp_path: Path,
) -> None:
    """Each Avibe session receives its own Cua lifecycle and end is revivable."""

    state_path, state = _state(tmp_path)
    upstream = FakeUpstream()
    leases = FakeLeaseManager()
    server = ComputerUseServer(
        state_path=state_path,
        status_reader=lambda: ComputerUseStatus("ready", None, state),
        upstream_factory=lambda _state: asyncio.sleep(0, result=upstream),
        lease_manager=leases,  # type: ignore[arg-type]
    )

    await server.call_tool("list_apps", {"session": "ses-a"})
    await server.call_tool("end_session", {"session": "ses-a"})
    await server.call_tool("list_apps", {"session": "ses-b"})
    await server.call_tool("end_session", {"session": "ses-b"})
    await server.call_tool("list_apps", {"session": "ses-a"})

    calls = [
        (params["name"], params["arguments"].get("session"))
        for _method, params in upstream.calls
    ]
    assert calls == [
        ("start_session", "ses-a"),
        ("list_apps", "ses-a"),
        ("end_session", "ses-a"),
        ("start_session", "ses-b"),
        ("list_apps", "ses-b"),
        ("end_session", "ses-b"),
        ("start_session", "ses-a"),
        ("list_apps", "ses-a"),
    ]


@pytest.mark.asyncio
async def test_session_label_is_normalized_before_forwarding(
    tmp_path: Path,
) -> None:
    """Whitespace around one public label cannot create a second driver session."""

    state_path, state = _state(tmp_path)
    upstream = FakeUpstream()
    server = ComputerUseServer(
        state_path=state_path,
        status_reader=lambda: ComputerUseStatus("ready", None, state),
        upstream_factory=lambda _state: asyncio.sleep(0, result=upstream),
        lease_manager=FakeLeaseManager(),  # type: ignore[arg-type]
    )

    await server.call_tool("list_apps", {"session": "  ses-a  "})
    assert [
        params["arguments"].get("session")
        for _method, params in upstream.calls
    ] == ["ses-a", "ses-a"]


@pytest.mark.asyncio
async def test_window_input_requires_observation_and_rejects_focus_routes(
    tmp_path: Path,
) -> None:
    """macOS admits only exact background-window input after fresh observation."""

    state_path, state = _state(tmp_path)
    upstream = FakeUpstream()
    server = ComputerUseServer(
        state_path=state_path,
        status_reader=lambda: ComputerUseStatus("ready", None, state),
        upstream_factory=lambda _state: asyncio.sleep(0, result=upstream),
        lease_manager=FakeLeaseManager(),  # type: ignore[arg-type]
    )

    first = await server.call_tool(
        "click",
        {"session": "ses-a", "pid": 7, "window_id": 9, "x": 1, "y": 2},
    )
    assert "observe_first" in first["content"][0]["text"]

    observed = await server.call_tool(
        "get_window_state",
        {"session": "ses-a", "pid": 7, "window_id": 9},
    )
    assert not observed.get("isError")
    clicked = await server.call_tool(
        "click",
        {"session": "ses-a", "pid": 7, "window_id": 9, "x": 1, "y": 2},
    )
    assert not clicked.get("isError")

    foreground = await server.call_tool(
        "click",
        {
            "session": "ses-a",
            "pid": 7,
            "window_id": 9,
            "x": 1,
            "y": 2,
            "delivery_mode": "foreground",
        },
    )
    assert "foreground_forbidden" in foreground["content"][0]["text"]

    shortcut = await server.call_tool(
        "hotkey",
        {
            "session": "ses-a",
            "pid": 7,
            "window_id": 9,
            "keys": ["cmd", "shift", "["],
        },
    )
    assert "focus_shortcut_forbidden" in shortcut["content"][0]["text"]


@pytest.mark.asyncio
async def test_real_lease_epoch_survives_handoff_and_rejects_stale_observation(
    tmp_path: Path,
) -> None:
    """A released lease keeps its epoch so a later holder revives the session."""

    now = 100.0
    leases = DesktopLeaseManager(tmp_path, now=lambda: now, ttl_seconds=60.0)
    state_path, state = _state(tmp_path)
    upstream = FakeUpstream()
    server = ComputerUseServer(
        state_path=state_path,
        status_reader=lambda: ComputerUseStatus("ready", None, state),
        upstream_factory=lambda _state: asyncio.sleep(0, result=upstream),
        lease_manager=leases,
    )

    observed = await server.call_tool(
        "get_window_state",
        {"session": "ses-a", "pid": 7, "window_id": 9},
    )
    assert not observed.get("isError")

    now += 61.0
    observed_by_b = await server.call_tool(
        "get_window_state",
        {"session": "ses-b", "pid": 8, "window_id": 10},
    )
    assert not observed_by_b.get("isError")
    ended = await server.call_tool("end_session", {"session": "ses-b"})
    assert not ended.get("isError")

    stale = await server.call_tool(
        "click",
        {"session": "ses-a", "pid": 7, "window_id": 9, "x": 1, "y": 2},
    )
    assert "observe_first" in stale["content"][0]["text"]
    assert [params["name"] for _method, params in upstream.calls][-2:] == [
        "end_session",
        "start_session",
    ]


@pytest.mark.asyncio
async def test_proxy_replacement_clears_observations(tmp_path: Path) -> None:
    """A fresh upstream proxy cannot reuse observations from the old process."""

    state_path, state = _state(tmp_path)
    upstreams: list[FakeUpstream] = []

    async def factory(_state):
        upstream = FakeUpstream()
        upstreams.append(upstream)
        return upstream

    server = ComputerUseServer(
        state_path=state_path,
        status_reader=lambda: ComputerUseStatus("ready", None, state),
        upstream_factory=factory,
        lease_manager=FakeLeaseManager(),  # type: ignore[arg-type]
    )
    await server.call_tool(
        "get_window_state",
        {"session": "ses-a", "pid": 7, "window_id": 9},
    )
    upstreams[0].alive = False
    await server.call_tool("list_apps", {"session": "ses-a"})
    stale = await server.call_tool(
        "click",
        {"session": "ses-a", "pid": 7, "window_id": 9, "x": 1, "y": 2},
    )
    assert "observe_first" in stale["content"][0]["text"]


@pytest.mark.asyncio
async def test_transport_failure_discards_an_alive_proxy_and_its_observations(
    tmp_path: Path,
) -> None:
    """A broken reader cannot be reused merely because its process is alive."""

    state_path, state = _state(tmp_path)
    upstreams: list[FakeUpstream] = []

    async def factory(_state):
        upstream = FakeUpstream()
        upstreams.append(upstream)
        return upstream

    server = ComputerUseServer(
        state_path=state_path,
        status_reader=lambda: ComputerUseStatus("ready", None, state),
        upstream_factory=factory,
        lease_manager=FakeLeaseManager(),  # type: ignore[arg-type]
    )
    await server.call_tool(
        "get_window_state",
        {"session": "ses-a", "pid": 7, "window_id": 9},
    )
    upstreams[0].fail_transport = True
    failed = await server.call_tool("list_apps", {"session": "ses-a"})
    assert "upstream_unavailable" in failed["content"][0]["text"]
    assert upstreams[0].close_calls == 1
    assert not upstreams[0].alive

    upstreams[0].fail_transport = False
    recovered = await server.call_tool("list_apps", {"session": "ses-a"})
    assert not recovered.get("isError")
    assert len(upstreams) == 2
    stale = await server.call_tool(
        "click",
        {"session": "ses-a", "pid": 7, "window_id": 9, "x": 1, "y": 2},
    )
    assert "observe_first" in stale["content"][0]["text"]


@pytest.mark.asyncio
async def test_late_failure_from_old_proxy_preserves_new_proxy_observations(
    tmp_path: Path,
) -> None:
    """A late real call failure cannot invalidate a replacement it no longer owns."""

    state_path, first_state = _state(tmp_path)
    current = first_state
    upstreams: list[FakeUpstream] = []

    async def factory(_state):
        upstream = FakeUpstream()
        if not upstreams:
            upstream.block = asyncio.Event()
        upstreams.append(upstream)
        return upstream

    server = ComputerUseServer(
        state_path=state_path,
        status_reader=lambda: ComputerUseStatus("ready", None, current),
        upstream_factory=factory,
        lease_manager=FakeLeaseManager(),  # type: ignore[arg-type]
    )
    old_call = asyncio.create_task(
        server.call_tool("list_apps", {"session": "ses-a"})
    )
    for _attempt in range(100):
        if upstreams and upstreams[0].inflight:
            break
        await asyncio.sleep(0)
    old = upstreams[0]
    old.fail_after_unblock = True

    current = ComputerUseState(**{**first_state.__dict__, "generation": 2})
    server._lease_manager.epoch += 1  # type: ignore[attr-defined]
    observed = await server.call_tool(
        "get_window_state",
        {"session": "ses-b", "pid": 7, "window_id": 9},
    )
    assert not observed.get("isError")
    replacement = upstreams[1]

    assert old.block is not None
    old.block.set()
    failed = await old_call
    assert "upstream_unavailable" in failed["content"][0]["text"]

    assert server._upstream is replacement
    assert replacement.alive
    clicked = await server.call_tool(
        "click",
        {"session": "ses-b", "pid": 7, "window_id": 9, "x": 1, "y": 2},
    )
    assert not clicked.get("isError")


@pytest.mark.asyncio
async def test_epoch_change_restarts_session_and_requires_each_window_again(
    tmp_path: Path,
) -> None:
    """A reacquired desktop invalidates Cua state and each observed window."""

    state_path, state = _state(tmp_path)
    upstream = FakeUpstream()
    leases = FakeLeaseManager()
    server = ComputerUseServer(
        state_path=state_path,
        status_reader=lambda: ComputerUseStatus("ready", None, state),
        upstream_factory=lambda _state: asyncio.sleep(0, result=upstream),
        lease_manager=leases,  # type: ignore[arg-type]
    )

    for window_id in (9, 10):
        result = await server.call_tool(
            "get_window_state",
            {"session": "ses-a", "pid": 7, "window_id": window_id},
        )
        assert not result.get("isError")

    leases.epoch += 1
    stale = await server.call_tool(
        "click",
        {"session": "ses-a", "pid": 7, "window_id": 9, "x": 1, "y": 2},
    )
    assert "observe_first" in stale["content"][0]["text"]
    assert [params["name"] for _method, params in upstream.calls][-2:] == [
        "end_session",
        "start_session",
    ]

    await server.call_tool(
        "get_window_state",
        {"session": "ses-a", "pid": 7, "window_id": 9},
    )
    window_a = await server.call_tool(
        "click",
        {"session": "ses-a", "pid": 7, "window_id": 9, "x": 1, "y": 2},
    )
    window_b = await server.call_tool(
        "click",
        {"session": "ses-a", "pid": 7, "window_id": 10, "x": 1, "y": 2},
    )
    assert not window_a.get("isError")
    assert "observe_first" in window_b["content"][0]["text"]


@pytest.mark.asyncio
async def test_new_server_process_requires_observation_again(tmp_path: Path) -> None:
    """Observation state is intentionally process-local and starts empty."""

    state_path, state = _state(tmp_path)
    leases = FakeLeaseManager()
    first_upstream = FakeUpstream()
    first = ComputerUseServer(
        state_path=state_path,
        status_reader=lambda: ComputerUseStatus("ready", None, state),
        upstream_factory=lambda _state: asyncio.sleep(0, result=first_upstream),
        lease_manager=leases,  # type: ignore[arg-type]
    )
    await first.call_tool(
        "get_window_state",
        {"session": "ses-a", "pid": 7, "window_id": 9},
    )
    await first.call_tool("end_session", {"session": "ses-a"})

    second_upstream = FakeUpstream()
    second = ComputerUseServer(
        state_path=state_path,
        status_reader=lambda: ComputerUseStatus("ready", None, state),
        upstream_factory=lambda _state: asyncio.sleep(0, result=second_upstream),
        lease_manager=leases,  # type: ignore[arg-type]
    )
    result = await second.call_tool(
        "click",
        {"session": "ses-a", "pid": 7, "window_id": 9, "x": 1, "y": 2},
    )
    assert "observe_first" in result["content"][0]["text"]


@pytest.mark.asyncio
async def test_setup_failure_releases_lease_before_returning(tmp_path: Path) -> None:
    """A proxy/session setup error must not strand the cross-process lease."""

    state_path, state = _state(tmp_path)
    leases = DesktopLeaseManager(tmp_path)

    async def fail_upstream(_state: ComputerUseState) -> Any:
        raise RuntimeError("proxy failed before the first tool call")

    server = ComputerUseServer(
        state_path=state_path,
        status_reader=lambda: ComputerUseStatus("ready", None, state),
        upstream_factory=fail_upstream,
        lease_manager=leases,  # type: ignore[arg-type]
    )

    result = await server.call_tool("start_session", {"session": "ses-a"})

    assert result["isError"]
    acquisition = leases.acquire(
        "ses-b",
        state.daemon_key,
        lambda: state.daemon_key,
    )
    assert acquisition.lease.holder == "ses-b"


@pytest.mark.asyncio
async def test_renewed_lease_survives_observe_first_guard(tmp_path: Path) -> None:
    """A guard failure must not release a lease this call merely renewed."""

    state_path, state = _state(tmp_path)
    leases = DesktopLeaseManager(tmp_path)
    upstream = FakeUpstream()
    server = ComputerUseServer(
        state_path=state_path,
        status_reader=lambda: ComputerUseStatus("ready", None, state),
        upstream_factory=lambda _state: asyncio.sleep(0, result=upstream),
        lease_manager=leases,
    )

    started = await server.call_tool("list_apps", {"session": "ses-a"})
    assert not started.get("isError")
    guarded = await server.call_tool(
        "click",
        {"session": "ses-a", "pid": 7, "window_id": 9, "x": 1, "y": 2},
    )
    assert "observe_first" in guarded["content"][0]["text"]

    with pytest.raises(ComputerServerError, match="ses-a"):
        leases.acquire(
            "ses-b",
            state.daemon_key,
            lambda: state.daemon_key,
        )


@pytest.mark.asyncio
async def test_stale_state_cannot_overwrite_current_generation_holder(
    tmp_path: Path,
) -> None:
    """Lease admission re-reads D under the lock before changing its holder."""

    state_path, stale_state = _state(tmp_path)
    current_state = ComputerUseState(
        **{**stale_state.__dict__, "generation": 2}
    )
    leases = DesktopLeaseManager(tmp_path)
    current_claim = leases.acquire(
        "ses-current",
        current_state.daemon_key,
        lambda: current_state.daemon_key,
    )
    status_reads = 0

    def status_reader() -> ComputerUseStatus:
        nonlocal status_reads
        status_reads += 1
        state = stale_state if status_reads == 1 else current_state
        return ComputerUseStatus("ready", None, state)

    starts = 0

    async def factory(_state: ComputerUseState) -> FakeUpstream:
        nonlocal starts
        starts += 1
        return FakeUpstream()

    server = ComputerUseServer(
        state_path=state_path,
        status_reader=status_reader,
        upstream_factory=factory,
        lease_manager=leases,
    )
    result = await server.call_tool("list_apps", {"session": "ses-stale"})

    assert "desktop_busy" in result["content"][0]["text"]
    assert starts == 0
    assert leases.refresh(current_claim.lease)


@pytest.mark.parametrize(
    ("arguments", "code"),
    [
        ({"target": {"kind": "desktop"}}, "window_target_required"),
        ({"target": {"kind": "window", "pid": 7}}, "window_target_required"),
        (
            {"target": {"kind": "window", "pid": 7, "window_id": 9, "display_id": 2}},
            "window_target_required",
        ),
        ({"target": "window"}, "window_target_required"),
        ({"scope": "desktop"}, "window_target_required"),
        ({"scope": "other"}, "window_target_required"),
        ({"scope": None}, "window_target_required"),
        ({"pid": 7}, "window_target_required"),
        ({"element_token": None}, "window_target_required"),
        ({"element_token": ""}, "window_target_required"),
        ({"pid": True, "window_id": 9}, "window_target_required"),
        ({"pid": 7, "window_id": True}, "window_target_required"),
        (
            {"pid": 7, "window_id": 9, "delivery_mode": "foreground"},
            "foreground_forbidden",
        ),
    ],
)
def test_input_rejects_non_window_and_unknown_target_shapes(
    arguments: dict[str, Any],
    code: str,
) -> None:
    """The macOS input boundary accepts only an exact background window."""

    with pytest.raises(ComputerServerError) as raised:
        _validate_input("click", arguments)
    assert raised.value.code == code


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("hotkey", {"keys": ["cmd", "l"]}),
        ("hotkey", {"keys": ["command", "shift", "g"]}),
        ("hotkey", {"keys": "meta+1"}),
        ("hotkey", {"keys": ["⌘", "9"]}),
        ("hotkey", {"keys": ["cmd", "["]}),
        ("hotkey", {"keys": ["cmd", "]"]}),
        ("hotkey", {"keys": ["cmd", "shift", "["]}),
        ("hotkey", {"keys": ["cmd", "shift", "]"]}),
        ("press_key", {"key": "l", "modifiers": ["cmd"]}),
        ("press_key", {"key": "g", "modifiers": ["meta", "shift"]}),
    ],
)
def test_all_documented_focus_shortcuts_are_rejected(
    tool_name: str,
    arguments: dict[str, Any],
) -> None:
    """Every shortcut named by the pinned no-foreground guide is blocked."""

    with pytest.raises(ComputerServerError) as raised:
        _validate_input(
            tool_name,
            {
                "pid": 7,
                "window_id": 9,
                **arguments,
            },
        )
    assert raised.value.code == "focus_shortcut_forbidden"


def test_desktop_lease_expires_and_daemon_generation_voids_it(
    tmp_path: Path,
) -> None:
    """A crash cannot hold the desktop past TTL, and native restart clears it."""

    now = 100.0
    manager = DesktopLeaseManager(tmp_path, now=lambda: now)
    first = manager.acquire(
        "ses-a",
        ("shell-a", 1),
        lambda: ("shell-a", 1),
    ).lease
    with pytest.raises(Exception, match="ses-a"):
        manager.acquire(
            "ses-b",
            ("shell-a", 1),
            lambda: ("shell-a", 1),
        )

    now += 61.0
    second = manager.acquire(
        "ses-b",
        ("shell-a", 1),
        lambda: ("shell-a", 1),
    ).lease
    assert second.epoch == first.epoch + 1

    third = manager.acquire(
        "ses-c",
        ("shell-a", 2),
        lambda: ("shell-a", 2),
    ).lease
    assert third.epoch == second.epoch + 1


@pytest.mark.skipif(os.name == "nt", reason="Phase 1 cross-process lease is macOS-only")
def test_cross_process_first_calls_have_exactly_one_lease_holder(
    tmp_path: Path,
) -> None:
    """The filesystem lock serializes simultaneous first calls across servers."""

    context = multiprocessing.get_context("spawn")
    start = context.Event()
    results = context.Queue()
    processes = [
        context.Process(
            target=_acquire_lease_in_process,
            args=(str(tmp_path), session, start, results),
        )
        for session in ("ses-a", "ses-b")
    ]
    for process in processes:
        process.start()
    start.set()
    observed = [results.get(timeout=10) for _process in processes]
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0
    assert sorted(kind for kind, _detail in observed) == ["error", "ok"]
    assert {detail for kind, detail in observed if kind == "error"} == {
        "desktop_busy"
    }


@pytest.mark.asyncio
async def test_long_call_refreshes_lease_until_it_finishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The heartbeat keeps a lease alive while one upstream call is blocked."""

    monkeypatch.setattr("core.computer_server._LEASE_HEARTBEAT_SECONDS", 0.01)
    state_path, state = _state(tmp_path)
    upstream = FakeUpstream()
    upstream.block = asyncio.Event()
    leases = FakeLeaseManager()
    server = ComputerUseServer(
        state_path=state_path,
        status_reader=lambda: ComputerUseStatus("ready", None, state),
        upstream_factory=lambda _state: asyncio.sleep(0, result=upstream),
        lease_manager=leases,  # type: ignore[arg-type]
    )
    call = asyncio.create_task(
        server.call_tool("list_apps", {"session": "ses-a"})
    )
    for _attempt in range(100):
        if leases.refresh_count >= 2:
            break
        await asyncio.sleep(0.005)
    assert leases.refresh_count >= 2
    upstream.block.set()
    await call
    refreshes_at_completion = leases.refresh_count
    await asyncio.sleep(0.03)
    assert leases.refresh_count == refreshes_at_completion


@pytest.mark.asyncio
async def test_upstream_eof_before_request_registration_fails_promptly() -> None:
    """A completed reader is never treated as a live transport."""

    upstream = JsonRpcUpstream(_TransportProcess(_EofStream()))  # type: ignore[arg-type]
    await asyncio.sleep(0)

    assert not upstream.alive
    with pytest.raises(UpstreamUnavailable, match="reader"):
        await upstream.request("tools/call", {"name": "list_apps"})

    await upstream.close()


@pytest.mark.asyncio
async def test_upstream_request_has_a_bounded_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A silent but live child cannot hold the desktop lease indefinitely."""

    monkeypatch.setattr(
        "core.computer_server._UPSTREAM_REQUEST_TIMEOUT_SECONDS",
        0.01,
    )
    upstream = JsonRpcUpstream(  # type: ignore[arg-type]
        _TransportProcess(_BlockedStream())
    )

    with pytest.raises(UpstreamUnavailable, match="180 second"):
        await upstream.request("tools/call", {"name": "list_apps"})

    await upstream.close()


@pytest.mark.asyncio
async def test_proxy_command_disables_telemetry_updates_and_window_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every proxy child receives the fixed embedded environment contract."""

    _state_path, state = _state(tmp_path)
    captured: dict[str, Any] = {}

    class FakeStream:
        async def readline(self) -> bytes:
            return b""

    class FakeStdin:
        def close(self) -> None:
            return None

        async def wait_closed(self) -> None:
            return None

    class FakeProcess:
        returncode = None
        stdin = FakeStdin()
        stdout = FakeStream()
        stderr = FakeStream()

        async def wait(self) -> int:
            self.returncode = 0
            return 0

        def kill(self) -> None:
            self.returncode = -9

    async def fake_exec(*args: Any, **kwargs: Any) -> FakeProcess:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return FakeProcess()

    async def fake_request(
        self: JsonRpcUpstream,
        method: str,
        params: Mapping[str, Any],
    ) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": 1, "result": {}}

    async def fake_notify(
        self: JsonRpcUpstream,
        method: str,
        params: Mapping[str, Any],
    ) -> None:
        return None

    monkeypatch.setenv("CUA_DRIVER_WINDOW_CHANGE_TIMEOUT_MS", "1")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(JsonRpcUpstream, "request", fake_request)
    monkeypatch.setattr(JsonRpcUpstream, "notify", fake_notify)

    upstream = await JsonRpcUpstream.start(state)
    env = captured["kwargs"]["env"]
    assert captured["args"] == (
        state.proxy_executable,
        "mcp",
        "--embedded",
        "--socket",
        state.socket_path,
        "--host-bundle-id",
        state.host_bundle_id,
    )
    assert env["CUA_DRIVER_EMBEDDED"] == "1"
    assert env["CUA_DRIVER_RS_TELEMETRY_ENABLED"] == "0"
    assert env["CUA_DRIVER_RS_UPDATE_CHECK"] == "0"
    assert "CUA_DRIVER_WINDOW_CHANGE_TIMEOUT_MS" not in env
    await upstream.close()
