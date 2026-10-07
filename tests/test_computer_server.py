from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

import pytest

from core.computer_server import (
    ComputerUseServer,
    DesktopLeaseManager,
    _fold_tool_result,
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
    snapshot_path.write_text(json.dumps(_snapshot()), encoding="utf-8")
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

    async def request(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        assert method == "tools/call"
        copied = json.loads(json.dumps(params))
        self.calls.append((method, copied))
        self.inflight += 1
        self.max_inflight = max(self.max_inflight, self.inflight)
        try:
            if self.block is not None and params.get("name") == "list_apps":
                await self.block.wait()
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
        self.alive = False


class FakeLeaseManager:
    def __init__(self) -> None:
        self.epoch = 1
        self.holder: str | None = None

    def acquire(self, session: str, daemon_key: tuple[str, int]):
        from core.computer_server import ComputerServerError, Lease

        if self.holder is not None and self.holder != session:
            raise ComputerServerError("desktop_busy", f"held by {self.holder}")
        self.holder = session
        return Lease(session, 1.0, self.epoch, daemon_key[0], daemon_key[1])

    def refresh(self, lease) -> bool:
        return self.holder == lease.holder

    def release(self, lease) -> None:
        if self.holder == lease.holder:
            self.holder = None


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


@pytest.mark.asyncio
async def test_non_ready_call_never_spawns_upstream(tmp_path: Path) -> None:
    """Availability errors stay per-call and never create a daemon or proxy."""

    state_path, _state_value = _state(tmp_path)
    starts = 0

    async def factory(_state_value):
        nonlocal starts
        starts += 1
        return FakeUpstream()

    server = ComputerUseServer(
        state_path=state_path,
        status_reader=lambda: ComputerUseStatus("needs_permission", "accessibility"),
        upstream_factory=factory,
        lease_manager=FakeLeaseManager(),  # type: ignore[arg-type]
    )
    result = await server.call_tool("list_apps", {"session": "ses-a"})
    assert result["isError"] is True
    assert "needs_permission" in result["content"][0]["text"]
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

    current = ComputerUseState(
        **{
            **first_state.__dict__,
            "generation": 2,
        }
    )
    leases.epoch += 1
    await server.call_tool("list_apps", {"session": "ses-a"})
    assert len(upstreams) == 2

    upstreams[-1].alive = False
    await server.call_tool("list_apps", {"session": "ses-a"})
    assert len(upstreams) == 3


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


def test_desktop_lease_expires_and_daemon_generation_voids_it(
    tmp_path: Path,
) -> None:
    """A crash cannot hold the desktop past TTL, and native restart clears it."""

    now = 100.0
    manager = DesktopLeaseManager(tmp_path, now=lambda: now)
    first = manager.acquire("ses-a", ("shell-a", 1))
    with pytest.raises(Exception, match="ses-a"):
        manager.acquire("ses-b", ("shell-a", 1))

    now += 61.0
    second = manager.acquire("ses-b", ("shell-a", 1))
    assert second.epoch == first.epoch + 1

    third = manager.acquire("ses-c", ("shell-a", 2))
    assert third.epoch == second.epoch + 1
