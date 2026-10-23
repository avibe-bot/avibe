from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import shutil
from typing import Any, Callable, Mapping

import pytest

from core.computer_script_handles import _WINDOW_HANDLE_TOOLS
from core.computer_server import (
    ComputerServerError,
    ComputerUseServer,
    JsonRpcUpstream,
    _SessionState,
    _TargetLockManager,
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


async def _wait_until(
    predicate: Callable[[], object],
    *,
    timeout: float = 5.0,
) -> None:
    """Wait for a condition on a wall-clock bound, never a count of loop turns.

    ``ComputerUseServer.call_tool`` crosses ``asyncio.to_thread`` before it
    reaches the upstream, so how many loop iterations that takes depends on
    how busy the machine is.
    """

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() >= deadline:
            raise AssertionError(f"condition not met within {timeout} seconds")
        await asyncio.sleep(0.001)


class _ObservedLock(asyncio.Lock):
    """An asyncio lock that records each acquire which has to wait."""

    def __init__(self) -> None:
        super().__init__()
        self.contended = 0

    async def acquire(self) -> bool:
        if self.locked():
            self.contended += 1
        return await super().acquire()


class _ObservedTargetLocks(_TargetLockManager):
    """A target lock manager that records each acquire refused by a held lock."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.refusals = 0

    def _available(self, keys: tuple[tuple[str, str], ...]) -> bool:
        available = super()._available(keys)
        if not available:
            self.refusals += 1
        return available


class FakeUpstream:
    _serial = 0

    def __init__(self) -> None:
        type(self)._serial += 1
        self.serial = type(self)._serial
        self.alive = True
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.inflight = 0
        self.max_inflight = 0
        self.inflight_by_tool: dict[str, int] = {}
        self.max_inflight_by_tool: dict[str, int] = {}
        self.block: asyncio.Event | None = None
        self.blocked_tools: set[str] = {"list_apps"}
        self.tool_errors: set[str] = set()
        self.stale_element_tokens: set[str] = set()
        self.resolved_pid = 7
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
        tool_name = params.get("name")
        arguments = params.get("arguments") or {}
        if isinstance(tool_name, str):
            self.inflight_by_tool[tool_name] = (
                self.inflight_by_tool.get(tool_name, 0) + 1
            )
            self.max_inflight_by_tool[tool_name] = max(
                self.max_inflight_by_tool.get(tool_name, 0),
                self.inflight_by_tool[tool_name],
            )
        try:
            if self.block is not None and tool_name in self.blocked_tools:
                await self.block.wait()
            if self.fail_after_unblock:
                raise UpstreamUnavailable("transport failed after replacement")
            element_token = arguments.get("element_token")
            if (
                isinstance(element_token, str)
                and element_token in self.stale_element_tokens
            ):
                return {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {
                        "content": [{"type": "text", "text": "stale token"}],
                        "structuredContent": {
                            "code": "stale_element_token",
                            "dispatched": False,
                        },
                        "isError": True,
                    },
                }
            if tool_name in self.tool_errors:
                return {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {
                        "content": [{"type": "text", "text": "tool failed"}],
                        "isError": True,
                    },
                }
            structured = {"tool": params.get("name")}
            if tool_name in {"get_window_state", "zoom"}:
                structured.update(
                    {
                        "pid": arguments.get("pid", self.resolved_pid),
                        "window_id": arguments.get("window_id"),
                    }
                )
            if tool_name == "get_window_state":
                session = arguments.get("session", "session")
                structured["elements"] = [
                    {"element_token": f"{session}-element-token"}
                ]
            return {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "content": [{"type": "image", "data": "png", "mimeType": "image/png"}],
                    "structuredContent": structured,
                },
            }
        finally:
            self.inflight -= 1
            if isinstance(tool_name, str):
                self.inflight_by_tool[tool_name] -= 1

    async def notify(self, method: str, params: Mapping[str, Any]) -> None:
        return None

    async def close(self) -> None:
        self.close_calls += 1
        self.alive = False


_DRIVER_LIFECYCLE_TOOLS = frozenset(
    {"start_session", "end_session", "get_session", "list_sessions"}
)


def driver_session_ended_refusal(tool: str, session: str) -> dict[str, Any]:
    """The exact wire result cua-driver 0.31.0 returns for an ended name.

    ``serve.rs`` refuses the call before dispatch, and the proxy wraps the
    daemon error through ``tool_error_result`` + ``conforming_error_envelope``.
    """

    return {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "content": [
                {
                    "type": "text",
                    "text": (
                        f"session has ended; tool call '{tool}' was rejected. "
                        f"Call start_session with session '{session}' to start "
                        "it again, or use a new session label."
                    ),
                }
            ],
            "isError": True,
            "structuredContent": {
                "exit_code": 1,
                "code": "tool_invocation_failed",
            },
        },
    }


class DriverSessionUpstream(FakeUpstream):
    """FakeUpstream with Cua's named-session tombstones.

    The real driver retires a named session after five idle minutes
    (``DEFAULT_SESSION_IDLE_TTL``) or on ``end_session``; afterwards only
    ``start_session`` revives the name and every other call is refused before
    it is dispatched.
    """

    def __init__(self, *, revivable: bool = True) -> None:
        super().__init__()
        self.blocked_tools = set()
        self.ended: set[str] = set()
        self.revivable = revivable

    def expire_idle(self, session: str) -> None:
        self.ended.add(session)

    async def request(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        session = (params.get("arguments") or {}).get("session")
        if name == "start_session" and self.revivable:
            self.ended.discard(session)
        elif name == "end_session":
            self.ended.add(session)
        elif name not in _DRIVER_LIFECYCLE_TOOLS and session in self.ended:
            self.calls.append((method, json.loads(json.dumps(params))))
            return driver_session_ended_refusal(str(name), str(session))
        return await super().request(method, params)


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


class _BlockingStdin(_TestStdin):
    def __init__(self) -> None:
        super().__init__()
        self.release = asyncio.Event()

    async def drain(self) -> None:
        await self.release.wait()


class _EofStream:
    async def readline(self) -> bytes:
        return b""


class _BlockedStream:
    def __init__(self) -> None:
        self.release = asyncio.Event()

    async def readline(self) -> bytes:
        await self.release.wait()
        return b""


class _LinesStream:
    def __init__(self, lines: list[bytes]) -> None:
        self.lines = iter([*lines, b""])

    async def readline(self) -> bytes:
        return next(self.lines)


class _RaisingStream:
    async def readline(self) -> bytes:
        raise RuntimeError("stream reader failed\nwith details")


class _TransportProcess:
    def __init__(self, stdout, *, stdin=None, stderr=None) -> None:
        self.returncode = None
        self.stdin = stdin or _TestStdin()
        self.stdout = stdout
        self.stderr = stderr or _EofStream()

    async def wait(self) -> int:
        self.returncode = 0
        return 0

    def kill(self) -> None:
        self.returncode = -9


def _write_fake_mcp_proxy(path: Path) -> None:
    path.write_text(
        """#!/usr/bin/env python3
import base64
import json
import sys


def send(request_id, result):
    print(
        json.dumps(
            {"jsonrpc": "2.0", "id": request_id, "result": result},
            separators=(",", ":"),
        ),
        flush=True,
    )


for line in sys.stdin:
    message = json.loads(line)
    request_id = message.get("id")
    if request_id is None:
        continue
    method = message.get("method")
    if method == "initialize":
        send(request_id, {"protocolVersion": "2025-06-18"})
        continue
    params = message.get("params") or {}
    if method != "tools/call":
        send(request_id, {})
        continue
    name = params.get("name")
    arguments = params.get("arguments") or {}
    if name == "start_session":
        send(
            request_id,
            {
                "content": [{"type": "text", "text": "session started"}],
                "structuredContent": {"active": True},
            },
        )
        continue
    if name == "get_window_state" and arguments.get("window_id") == 6285:
        send(
            request_id,
            {
                "content": [
                    {
                        "type": "text",
                        "text": "simulated owner lookup failure from the driver",
                    }
                ],
                "structuredContent": {
                    "exit_code": 1,
                    "code": "tool_invocation_failed",
                },
                "isError": True,
            },
        )
        continue
    if name == "get_window_state":
        label = "q" * 500
        elements = [
            {
                "element_index": index,
                "role": "group",
                "label": label,
                "depth": 1,
            }
            for index in range(330)
        ]
        tree_markdown = "\\n".join(
            f"- [element_index {index}] group {label}" for index in range(330)
        )
        send(
            request_id,
            {
                "content": [
                    {
                        "type": "image",
                        "data": base64.b64encode(b"p" * 900000).decode("ascii"),
                        "mimeType": "image/png",
                    }
                ],
                "structuredContent": {
                    "pid": arguments["pid"],
                    "window_id": arguments["window_id"],
                    "element_count": len(elements),
                    "elements": elements,
                    "tree_markdown": tree_markdown,
                    "screenshot_width": 1568,
                    "screenshot_height": 1442,
                    "screenshot_mime_type": "image/png",
                },
            },
        )
        continue
    send(
        request_id,
        {
            "content": [{"type": "text", "text": "unexpected tool"}],
            "isError": True,
        },
    )
""",
        encoding="utf-8",
    )
    path.chmod(0o755)


def test_advertised_surface_requires_session_and_drops_output_schema(
    tmp_path: Path,
) -> None:
    """Schema-driven clients must identify every call, including list_sessions."""

    state_path, state = _state(tmp_path)
    server = ComputerUseServer(
        state_path=state_path,
        status_reader=lambda: ComputerUseStatus("ready", None, state),
        upstream_factory=lambda _state: None,  # type: ignore[arg-type]
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
    )
    result = await server.call_tool("list_apps", {"session": "ses-a"})
    assert result["isError"] is True
    payload = json.loads(result["content"][0]["text"])
    assert payload == {
        "code": "computer_use_unavailable",
        "dispatched": False,
        "params": {
            "reason": reason,
            "status": status,
        },
    }
    assert starts == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "descriptor",
        "tool",
        "arguments",
        "apps",
        "windows",
        "expected_code",
        "expected_tools",
    ),
    [
        (
            {
                "kind": "app",
                "pid": 41,
                "bundle_id": "com.apple.mail",
                "name": None,
            },
            "kill_app",
            {"pid": 41, "session": "ses-script"},
            [
                {
                    "pid": 41,
                    "bundle_id": "com.apple.notes",
                    "name": "Notes",
                }
            ],
            [],
            "stale_app_handle",
            ["start_session", "list_apps"],
        ),
        (
            {
                "kind": "app",
                "pid": 41,
                "bundle_id": "com.apple.mail",
                "name": None,
            },
            "list_windows",
            {"pid": 41, "session": "ses-script"},
            [
                {
                    "pid": 57,
                    "bundle_id": "com.apple.mail",
                    "name": "Mail",
                }
            ],
            [],
            "stale_app_handle",
            ["start_session", "list_apps"],
        ),
        (
            {
                "kind": "window",
                "pid": 41,
                "window_id": 9,
                "app": {
                    "kind": "app",
                    "pid": 41,
                    "bundle_id": "com.apple.mail",
                    "name": None,
                },
            },
            "click",
            {"pid": 41, "window_id": 9, "x": 1, "y": 1, "session": "ses-script"},
            [
                {
                    "pid": 41,
                    "bundle_id": "com.apple.mail",
                    "name": "Mail",
                }
            ],
            [
                {
                    "pid": 57,
                    "window_id": 9,
                    "app_name": "Mail",
                }
            ],
            "stale_window_handle",
            ["start_session", "list_windows"],
        ),
        (
            {
                "kind": "window",
                "pid": 41,
                "window_id": 9,
                "app": {
                    "kind": "app",
                    "pid": 41,
                    "bundle_id": "com.apple.mail",
                    "name": None,
                },
            },
            "click",
            {"pid": 41, "window_id": 9, "x": 1, "y": 1, "session": "ses-script"},
            [],
            [],
            "stale_window_handle",
            ["start_session", "list_windows"],
        ),
    ],
)
async def test_script_handle_descriptors_are_validated_before_the_action(
    tmp_path: Path,
    descriptor: dict[str, Any],
    tool: str,
    arguments: dict[str, Any],
    apps: list[dict[str, Any]],
    windows: list[dict[str, Any]],
    expected_code: str,
    expected_tools: list[str],
) -> None:
    state_path, state = _state(tmp_path)

    class HandleUpstream(FakeUpstream):
        async def request(
            self,
            method: str,
            params: Mapping[str, Any],
        ) -> dict[str, Any]:
            assert method == "tools/call"
            copied = json.loads(json.dumps(params))
            self.calls.append((method, copied))
            name = params["name"]
            if name == "list_apps":
                structured = {"apps": apps}
            elif name == "list_windows":
                structured = {"windows": windows}
            else:
                structured = {"effect": "confirmed", "route": "system_api"}
            return {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "content": [],
                    "structuredContent": structured,
                },
            }

    upstream = HandleUpstream()
    server = ComputerUseServer(
        state_path=state_path,
        status_reader=lambda: ComputerUseStatus("ready", None, state),
        upstream_factory=lambda _state: asyncio.sleep(0, result=upstream),
    )

    result = await server.call_tool(
        tool,
        arguments,
        handle_descriptor=descriptor,
    )

    assert result["structuredContent"]["code"] == expected_code
    assert [params["name"] for _method, params in upstream.calls] == expected_tools


@pytest.mark.asyncio
async def test_current_script_window_descriptor_is_not_forwarded_to_driver(
    tmp_path: Path,
) -> None:
    state_path, state = _state(tmp_path)

    class HandleUpstream(FakeUpstream):
        async def request(
            self,
            method: str,
            params: Mapping[str, Any],
        ) -> dict[str, Any]:
            assert method == "tools/call"
            copied = json.loads(json.dumps(params))
            self.calls.append((method, copied))
            name = params["name"]
            if name == "list_apps":
                structured = {
                    "apps": [
                        {
                            "pid": 41,
                            "bundle_id": "com.apple.mail",
                            "name": "Mail",
                        }
                    ]
                }
            elif name == "list_windows":
                structured = {
                    "windows": [
                        {
                            "pid": 41,
                            "window_id": 9,
                            "app_name": "Mail",
                        }
                    ]
                }
            else:
                structured = {"pid": 41, "window_id": 9, "elements": []}
            return {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "content": [],
                    "structuredContent": structured,
                },
            }

    upstream = HandleUpstream()
    server = ComputerUseServer(
        state_path=state_path,
        status_reader=lambda: ComputerUseStatus("ready", None, state),
        upstream_factory=lambda _state: asyncio.sleep(0, result=upstream),
    )
    descriptor = {
        "kind": "window",
        "pid": 41,
        "window_id": 9,
        "app": {
            "kind": "app",
            "pid": 41,
            "bundle_id": "com.apple.mail",
            "name": None,
        },
    }

    result = await server.call_tool(
        "get_window_state",
        {"pid": 41, "window_id": 9, "session": "ses-script"},
        handle_descriptor=descriptor,
    )

    assert result.get("isError") is not True
    assert [params["name"] for _method, params in upstream.calls] == [
        "start_session",
        "get_window_state",
    ]
    assert upstream.calls[-1][1]["arguments"] == {
        "pid": 41,
        "window_id": 9,
        "session": "ses-script",
    }


_MAIL_WINDOW_DESCRIPTOR = {
    "kind": "window",
    "pid": 41,
    "window_id": 9,
    "app": {
        "kind": "app",
        "pid": 41,
        "bundle_id": "com.apple.mail",
        "name": None,
    },
}


class _WindowHandleUpstream(FakeUpstream):
    """Driver double for one live Mail window that may close or fail to read."""

    def __init__(self) -> None:
        super().__init__()
        self.window_open = True
        self.observation_error: dict[str, Any] | None = None

    async def request(
        self,
        method: str,
        params: Mapping[str, Any],
    ) -> dict[str, Any]:
        assert method == "tools/call"
        self.calls.append((method, json.loads(json.dumps(params))))
        name = params["name"]
        result: dict[str, Any]
        if name == "list_apps":
            result = {
                "content": [],
                "structuredContent": {
                    "apps": [
                        {"pid": 41, "bundle_id": "com.apple.mail", "name": "Mail"}
                    ]
                },
            }
        elif name == "list_windows":
            windows = (
                [{"pid": 41, "window_id": 9, "app_name": "Mail"}]
                if self.window_open
                else []
            )
            result = {"content": [], "structuredContent": {"windows": windows}}
        elif name == "get_window_state" and self.observation_error is not None:
            result = {
                "content": [],
                "structuredContent": self.observation_error,
                "isError": True,
            }
        elif name == "get_window_state":
            result = {
                "content": [],
                "structuredContent": {
                    "pid": 41,
                    "window_id": 9,
                    "elements": [{"element_token": "s1:2", "role": "AXButton"}],
                },
            }
        else:
            result = {
                "content": [],
                "structuredContent": {"effect": "unverifiable"},
            }
        return {"jsonrpc": "2.0", "id": 1, "result": result}


def _window_handle_server(
    tmp_path: Path,
) -> tuple[ComputerUseServer, _WindowHandleUpstream]:
    state_path, state = _state(tmp_path)
    upstream = _WindowHandleUpstream()
    server = ComputerUseServer(
        state_path=state_path,
        status_reader=lambda: ComputerUseStatus("ready", None, state),
        upstream_factory=lambda _state: asyncio.sleep(0, result=upstream),
    )
    return server, upstream


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", sorted(_WINDOW_HANDLE_TOOLS))
async def test_window_handle_calls_never_scan_applications(
    tmp_path: Path,
    tool: str,
) -> None:
    """A current window handle costs at most one pid-scoped window lookup."""

    server, upstream = _window_handle_server(tmp_path)
    observed = await server.call_tool(
        "get_window_state",
        {"pid": 41, "window_id": 9, "session": "ses-script"},
        handle_descriptor=_MAIL_WINDOW_DESCRIPTOR,
    )
    assert observed.get("isError") is not True
    upstream.calls.clear()

    result = await server.call_tool(
        tool,
        {"pid": 41, "window_id": 9, "session": "ses-script"},
        handle_descriptor=_MAIL_WINDOW_DESCRIPTOR,
    )

    assert result.get("isError") is not True, result
    names = [params["name"] for _method, params in upstream.calls]
    assert "list_apps" not in names
    assert names.count("list_windows") <= 1
    assert names[-1] == tool
    discovery = [params for _method, params in upstream.calls if params["name"] == "list_windows"]
    assert all(params["arguments"]["pid"] == 41 for params in discovery)


@pytest.mark.asyncio
async def test_element_click_hot_path_validates_its_window_once(
    tmp_path: Path,
) -> None:
    """The SDK's resolve-then-act pair performs exactly one discovery call."""

    server, upstream = _window_handle_server(tmp_path)

    observed = await server.call_tool(
        "get_window_state",
        {
            "pid": 41,
            "window_id": 9,
            "include_screenshot": False,
            "session": "ses-script",
        },
        handle_descriptor=_MAIL_WINDOW_DESCRIPTOR,
    )
    clicked = await server.call_tool(
        "click",
        {"pid": 41, "window_id": 9, "element_token": "s1:2", "session": "ses-script"},
        handle_descriptor=_MAIL_WINDOW_DESCRIPTOR,
    )

    assert observed.get("isError") is not True
    assert clicked.get("isError") is not True
    assert [params["name"] for _method, params in upstream.calls] == [
        "start_session",
        "get_window_state",
        "list_windows",
        "click",
    ]


@pytest.mark.asyncio
async def test_closed_window_observation_reports_a_stale_handle(
    tmp_path: Path,
) -> None:
    server, upstream = _window_handle_server(tmp_path)
    upstream.window_open = False
    upstream.observation_error = {
        "code": "window_id_not_found",
        "detail": "window_id 9 is not a live window",
    }

    result = await server.call_tool(
        "get_window_state",
        {"pid": 41, "window_id": 9, "session": "ses-script"},
        handle_descriptor=_MAIL_WINDOW_DESCRIPTOR,
    )

    assert result["isError"] is True
    assert result["structuredContent"] == {
        "code": "stale_window_handle",
        "dispatched": False,
        "params": {"pid": 41, "window_id": 9},
    }
    assert [params["name"] for _method, params in upstream.calls] == [
        "start_session",
        "get_window_state",
        "list_windows",
    ]
    # A refused observation never authorizes input on the window.
    clicked = await server.call_tool(
        "click",
        {"pid": 41, "window_id": 9, "x": 1, "y": 1, "session": "ses-script"},
        handle_descriptor=_MAIL_WINDOW_DESCRIPTOR,
    )
    assert clicked["structuredContent"]["code"] == "stale_window_handle"
    assert clicked["structuredContent"]["dispatched"] is False
    assert [params["name"] for _method, params in upstream.calls][-1] == "list_windows"


@pytest.mark.asyncio
async def test_failed_observation_of_a_live_window_keeps_the_driver_error(
    tmp_path: Path,
) -> None:
    server, upstream = _window_handle_server(tmp_path)
    upstream.observation_error = {
        "code": "ax_timeout",
        "detail": "the application did not answer",
    }

    result = await server.call_tool(
        "get_window_state",
        {"pid": 41, "window_id": 9, "session": "ses-script"},
        handle_descriptor=_MAIL_WINDOW_DESCRIPTOR,
    )

    assert result["isError"] is True
    assert result["structuredContent"]["code"] == "ax_timeout"
    assert [params["name"] for _method, params in upstream.calls] == [
        "start_session",
        "get_window_state",
        "list_windows",
    ]


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

    server = ComputerUseServer(
        state_path=state_path,
        status_reader=lambda: ComputerUseStatus("ready", None, current),
        upstream_factory=factory,
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
    assert result["structuredContent"] == {"answer": 42}
    assert "outputSchema" not in result


def test_result_folding_preserves_driver_error_code_params_and_detail() -> None:
    result = _fold_tool_result(
        {
            "result": {
                "content": [{"type": "text", "text": "fallback driver text"}],
                "structuredContent": {
                    "code": "capture_failed",
                    "params": {"scope": "window"},
                    "reason": "ScreenCaptureKit window was unavailable",
                },
                "isError": True,
            }
        }
    )

    payload = json.loads(result["content"][0]["text"])
    assert payload == {
        "code": "capture_failed",
        "detail": "ScreenCaptureKit window was unavailable",
        "params": {"scope": "window"},
    }
    assert result["structuredContent"] == payload


def test_result_folding_marks_consent_refusal_not_dispatched() -> None:
    result = _fold_tool_result(
        {
            "result": {
                "content": [],
                "structuredContent": {
                    "code": "foreground_consent_required",
                    "params": {},
                },
                "isError": True,
            }
        }
    )

    payload = json.loads(result["content"][0]["text"])
    assert payload == {
        "code": "foreground_consent_required",
        "dispatched": False,
        "params": {},
    }
    assert result["structuredContent"] == payload


def test_result_folding_preserves_bounded_detail_inside_driver_params() -> None:
    result = _fold_tool_result(
        {
            "result": {
                "content": [{"type": "text", "text": "fallback driver text"}],
                "structuredContent": {
                    "code": "capture_failed",
                    "params": {
                        "detail": "api_key=supersecretvalue123456\ncapture failed",
                        "scope": "window",
                    },
                },
                "isError": True,
            }
        }
    )

    payload = json.loads(result["content"][0]["text"])
    assert payload == {
        "code": "capture_failed",
        "detail": "api_key=[redacted] capture failed",
        "params": {
            "detail": "api_key=[redacted] capture failed",
            "scope": "window",
        },
    }
    assert "supersecretvalue123456" not in result["content"][0]["text"]
    assert result["structuredContent"] == payload


def test_result_folding_preserves_real_driver_exit_code_and_content_detail() -> None:
    result = _fold_tool_result(
        {
            "result": {
                "content": [
                    {
                        "type": "text",
                        "text": "window ownership lookup failed in the driver",
                    }
                ],
                "structuredContent": {
                    "exit_code": 1,
                    "code": "tool_invocation_failed",
                },
                "isError": True,
            }
        }
    )

    payload = json.loads(result["content"][0]["text"])
    assert payload == {
        "code": "tool_invocation_failed",
        "detail": "window ownership lookup failed in the driver",
        "params": {"exit_code": 1},
    }
    assert result["structuredContent"] == payload


def test_result_folding_uses_bounded_content_text_for_unstructured_driver_errors() -> None:
    result = _fold_tool_result(
        {
            "result": {
                "content": [
                    {"type": "text", "text": "driver detail\nwith \x00 controls"},
                ],
                "isError": True,
            },
        },
        tool_name="get_window_state",
    )

    payload = json.loads(result["content"][0]["text"])
    assert payload == {
        "code": "upstream_tool_error",
        "detail": "driver detail with controls",
        "params": {"tool": "get_window_state"},
    }
    assert result["structuredContent"] == payload


def test_result_folding_preserves_json_rpc_error_code_and_detail() -> None:
    result = _fold_tool_result(
        {
            "error": {
                "code": -32001,
                "message": "driver rejected request",
                "data": {"reason": "window no longer exists"},
            }
        }
    )

    payload = json.loads(result["content"][0]["text"])
    assert payload == {
        "code": "upstream_error",
        "detail": "driver rejected request",
        "params": {"rpc_code": -32001},
    }
    assert result["structuredContent"] == payload


def test_diagnostic_text_is_bounded_and_single_line() -> None:
    result = _fold_tool_result(
        {
            "result": {
                "content": [
                    {"type": "text", "text": "\x1b[31m" + ("x" * 3_000)},
                ],
                "isError": True,
            }
        }
    )

    payload = json.loads(result["content"][0]["text"])
    detail = payload["detail"]
    assert len(detail) == 2048
    assert "\n" not in detail
    assert "\x1b" not in detail
    assert detail.endswith("...")


def test_driver_error_detail_redacts_credential_material() -> None:
    result = _fold_tool_result(
        {
            "result": {
                "content": [
                    {
                        "type": "text",
                        "text": "capture failed with api_key=supersecretvalue123456",
                    },
                ],
                "structuredContent": {
                    "code": "capture_failed",
                    "params": {"scope": "window"},
                },
                "isError": True,
            }
        }
    )

    payload = json.loads(result["content"][0]["text"])
    assert payload == {
        "code": "capture_failed",
        "detail": "capture failed with api_key=[redacted]",
        "params": {"scope": "window"},
    }
    assert result["structuredContent"] == payload
    assert "supersecretvalue123456" not in result["content"][0]["text"]


def test_driver_error_detail_redacts_before_flattening_lines() -> None:
    result = _fold_tool_result(
        {
            "result": {
                "content": [
                    {
                        "type": "text",
                        "text": "api_key=supersecretvalue123456\ncapture failed",
                    },
                ],
                "isError": True,
            }
        }
    )

    payload = json.loads(result["content"][0]["text"])
    assert payload["detail"] == "api_key=[redacted] capture failed"
    assert "supersecretvalue123456" not in result["content"][0]["text"]


@pytest.mark.asyncio
async def test_upstream_stderr_and_malformed_json_are_logged(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("WARNING", logger="core.computer_server")
    upstream = JsonRpcUpstream(
        _TransportProcess(
            _LinesStream([b"{not-json\n"]),
            stdin=_TestStdin(),
            stderr=_LinesStream([b"driver stderr detail\n"]),
        )
    )

    await asyncio.gather(upstream._reader_task, upstream._stderr_task)
    assert "malformed JSON" in caplog.text
    assert "driver stderr detail" in caplog.text
    await upstream.close()


@pytest.mark.asyncio
async def test_upstream_diagnostic_logging_is_line_bounded(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("WARNING", logger="core.computer_server")
    malformed = [f"malformed-{index:02d}\\n".encode() for index in range(25)]
    stderr = [f"stderr-{index:02d}\\n".encode() for index in range(25)]
    upstream = JsonRpcUpstream(
        _TransportProcess(
            _LinesStream(malformed),
            stdin=_TestStdin(),
            stderr=_LinesStream(stderr),
        )
    )

    await asyncio.gather(upstream._reader_task, upstream._stderr_task)
    assert "malformed-19" in caplog.text
    assert "malformed-20" not in caplog.text
    assert "stderr-19" in caplog.text
    assert "stderr-20" not in caplog.text
    assert caplog.text.count("further lines suppressed") == 2
    await upstream.close()


@pytest.mark.asyncio
async def test_upstream_stderr_log_redacts_credential_material(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("WARNING", logger="core.computer_server")
    upstream = JsonRpcUpstream(
        _TransportProcess(
            _EofStream(),
            stdin=_TestStdin(),
            stderr=_LinesStream(
                [b"request failed with Authorization: Bearer tokenvalue1234\n"]
            ),
        )
    )

    await asyncio.gather(upstream._reader_task, upstream._stderr_task)
    assert "Authorization: Bearer [redacted]" in caplog.text
    assert "tokenvalue1234" not in caplog.text
    await upstream.close()


@pytest.mark.asyncio
async def test_upstream_reader_exception_is_logged_and_preserved_for_pending_request(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("WARNING", logger="core.computer_server")
    upstream = JsonRpcUpstream(
        _TransportProcess(_RaisingStream(), stdin=_TestStdin())
    )
    with pytest.raises(UpstreamUnavailable, match="stream reader failed"):
        await upstream.request("tools/call", {"name": "list_apps"})

    assert "computer-use upstream reader failed" in caplog.text
    assert "stream reader failed with details" in caplog.text
    await upstream.close()


@pytest.mark.asyncio
async def test_upstream_reader_exception_redacts_detail_from_result_and_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class _SecretRaisingStream:
        async def readline(self) -> bytes:
            raise RuntimeError("reader failed with api_key=supersecretvalue123456")

    caplog.set_level("WARNING", logger="core.computer_server")
    upstream = JsonRpcUpstream(
        _TransportProcess(_SecretRaisingStream(), stdin=_TestStdin())
    )
    with pytest.raises(UpstreamUnavailable) as caught:
        await upstream.request("tools/call", {"name": "list_apps"})

    assert "api_key=[redacted]" in str(caught.value)
    assert "supersecretvalue123456" not in str(caught.value)
    assert "api_key=[redacted]" in caplog.text
    assert "supersecretvalue123456" not in caplog.text
    await upstream.close()


@pytest.mark.asyncio
async def test_call_tool_preserves_upstream_unavailable_detail_and_logs(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("WARNING", logger="core.computer_server")
    state_path, state = _state(tmp_path)
    upstream = FakeUpstream()
    upstream.fail_transport = True
    server = ComputerUseServer(
        state_path=state_path,
        status_reader=lambda: ComputerUseStatus("ready", None, state),
        upstream_factory=lambda _state: asyncio.sleep(0, result=upstream),
    )

    result = await server.call_tool("list_apps", {"session": "ses-a"})
    payload = json.loads(result["content"][0]["text"])
    assert payload == {
        "code": "upstream_unavailable",
        "detail": "transport failed",
        "dispatched": False,
        "params": {},
    }
    assert "transport failed" in caplog.text


@pytest.mark.asyncio
async def test_post_forward_upstream_failure_does_not_claim_not_dispatched(
    tmp_path: Path,
) -> None:
    state_path, state = _state(tmp_path)
    upstream = FakeUpstream()
    server = ComputerUseServer(
        state_path=state_path,
        status_reader=lambda: ComputerUseStatus("ready", None, state),
        upstream_factory=lambda _state: asyncio.sleep(0, result=upstream),
    )

    started = await server.call_tool("start_session", {"session": "ses-a"})
    assert started.get("isError") is not True

    upstream.fail_transport = True
    result = await server.call_tool("list_apps", {"session": "ses-a"})
    payload = result["structuredContent"]

    assert payload["code"] == "upstream_unavailable"
    assert "dispatched" not in payload


@pytest.mark.asyncio
async def test_real_stdio_proxy_preserves_large_window_state_response(
    tmp_path: Path,
) -> None:
    state_path, state = _state(tmp_path)
    _write_fake_mcp_proxy(Path(state.proxy_executable))
    server = ComputerUseServer(
        state_path=state_path,
        status_reader=lambda: ComputerUseStatus("ready", None, state),
    )

    try:
        result = await server.call_tool(
            "get_window_state",
            {
                "session": "ses-large-window",
                "pid": 97470,
                "window_id": 6286,
            },
        )
    finally:
        await server.close()

    assert result.get("isError") is not True
    image = next(item for item in result["content"] if item.get("type") == "image")
    assert len(image["data"]) == 1_200_000
    payload = result["structuredContent"]
    assert payload["pid"] == 97470
    assert payload["window_id"] == 6286
    assert payload["element_count"] == 330
    assert len(payload["elements"]) == 330
    assert len(payload["tree_markdown"]) > 165_000
    assert payload["screenshot_width"] == 1568
    assert payload["screenshot_height"] == 1442


@pytest.mark.asyncio
async def test_real_stdio_proxy_preserves_driver_failure_detail(
    tmp_path: Path,
) -> None:
    state_path, state = _state(tmp_path)
    _write_fake_mcp_proxy(Path(state.proxy_executable))
    server = ComputerUseServer(
        state_path=state_path,
        status_reader=lambda: ComputerUseStatus("ready", None, state),
    )

    try:
        result = await server.call_tool(
            "get_window_state",
            {
                "session": "ses-driver-error",
                "pid": 97470,
                "window_id": 6285,
            },
        )
    finally:
        await server.close()

    payload = json.loads(result["content"][0]["text"])
    assert payload == {
        "code": "tool_invocation_failed",
        "detail": "simulated owner lookup failure from the driver",
        "params": {"exit_code": 1},
    }


@pytest.mark.asyncio
async def test_one_sessions_calls_are_serial_and_end_releases_after_queue(
    tmp_path: Path,
) -> None:
    """A queued end_session cannot overtake an in-flight call for that session."""

    state_path, state = _state(tmp_path)
    upstream = FakeUpstream()
    upstream.block = asyncio.Event()
    server = ComputerUseServer(
        state_path=state_path,
        status_reader=lambda: ComputerUseStatus("ready", None, state),
        upstream_factory=lambda _state: asyncio.sleep(0, result=upstream),
    )
    session_lock = _ObservedLock()
    server._session_locks["ses-a"] = session_lock
    first = asyncio.create_task(
        server.call_tool("list_apps", {"session": "ses-a"})
    )
    await _wait_until(lambda: upstream.inflight_by_tool.get("list_apps"))
    ending = asyncio.create_task(
        server.call_tool("end_session", {"session": "ses-a"})
    )
    await _wait_until(lambda: session_lock.contended)
    assert not ending.done()
    assert upstream.inflight == 1
    upstream.block.set()
    await first
    await ending
    assert upstream.max_inflight == 1
    names = [params["name"] for _method, params in upstream.calls]
    assert names == ["start_session", "list_apps", "end_session"]


@pytest.mark.asyncio
async def test_named_sessions_start_independently_and_revive_after_end(
    tmp_path: Path,
) -> None:
    """Each Avibe session receives its own Cua lifecycle and end is revivable."""

    state_path, state = _state(tmp_path)
    upstream = FakeUpstream()
    server = ComputerUseServer(
        state_path=state_path,
        status_reader=lambda: ComputerUseStatus("ready", None, state),
        upstream_factory=lambda _state: asyncio.sleep(0, result=upstream),
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
async def test_driver_idle_retired_session_is_started_again_once(
    tmp_path: Path,
) -> None:
    """A Session idle past the driver TTL keeps working instead of dying."""

    state_path, state = _state(tmp_path)
    upstream = DriverSessionUpstream()
    server = ComputerUseServer(
        state_path=state_path,
        status_reader=lambda: ComputerUseStatus("ready", None, state),
        upstream_factory=lambda _state: asyncio.sleep(0, result=upstream),
    )

    first = await server.call_tool("list_apps", {"session": "ses-a"})
    upstream.expire_idle("ses-a")
    second = await server.call_tool("list_apps", {"session": "ses-a"})
    third = await server.call_tool("list_apps", {"session": "ses-a"})

    assert first.get("isError") is not True
    assert second.get("isError") is not True
    assert third.get("isError") is not True
    names = [params["name"] for _method, params in upstream.calls]
    assert names == [
        "start_session",
        "list_apps",
        "list_apps",
        "start_session",
        "list_apps",
        "list_apps",
    ]


@pytest.mark.asyncio
async def test_driver_retired_session_drops_observations_before_input(
    tmp_path: Path,
) -> None:
    """The driver retires the ended episode's tokens, so input must re-observe."""

    state_path, state = _state(tmp_path)
    upstream = DriverSessionUpstream()
    server = ComputerUseServer(
        state_path=state_path,
        status_reader=lambda: ComputerUseStatus("ready", None, state),
        upstream_factory=lambda _state: asyncio.sleep(0, result=upstream),
    )
    target = {"session": "ses-a", "pid": 7, "window_id": 9}
    await server.call_tool("get_window_state", target)
    before = await server.call_tool("click", {**target, "x": 1, "y": 2})
    upstream.expire_idle("ses-a")
    refused = await server.call_tool("click", {**target, "x": 1, "y": 2})
    await server.call_tool("get_window_state", target)
    after = await server.call_tool("click", {**target, "x": 1, "y": 2})

    assert before.get("isError") is not True
    assert refused["structuredContent"]["code"] == "observe_first"
    assert refused["structuredContent"]["dispatched"] is False
    assert after.get("isError") is not True
    names = [params["name"] for _method, params in upstream.calls]
    assert names == [
        "start_session",
        "get_window_state",
        "click",
        "click",
        "start_session",
        "get_window_state",
        "click",
    ]


@pytest.mark.asyncio
async def test_driver_session_refused_after_fresh_start_is_not_dispatched(
    tmp_path: Path,
) -> None:
    """Revival is bounded to one start, and the final refusal is unambiguous."""

    state_path, state = _state(tmp_path)
    upstream = DriverSessionUpstream(revivable=False)
    server = ComputerUseServer(
        state_path=state_path,
        status_reader=lambda: ComputerUseStatus("ready", None, state),
        upstream_factory=lambda _state: asyncio.sleep(0, result=upstream),
    )

    await server.call_tool("list_apps", {"session": "ses-a"})
    upstream.expire_idle("ses-a")
    refused = await server.call_tool("list_apps", {"session": "ses-a"})

    assert refused["isError"] is True
    assert refused["structuredContent"]["code"] == "tool_invocation_failed"
    assert refused["structuredContent"]["dispatched"] is False
    assert refused["structuredContent"]["detail"].startswith("session has ended;")
    names = [params["name"] for _method, params in upstream.calls]
    assert names == [
        "start_session",
        "list_apps",
        "list_apps",
        "start_session",
        "list_apps",
    ]


@pytest.mark.asyncio
async def test_in_tool_session_ended_refusal_also_starts_the_session_again(
    tmp_path: Path,
) -> None:
    """The driver's in-tool admission refusal has a second, unstructured shape."""

    state_path, state = _state(tmp_path)
    upstream = DriverSessionUpstream()
    server = ComputerUseServer(
        state_path=state_path,
        status_reader=lambda: ComputerUseStatus("ready", None, state),
        upstream_factory=lambda _state: asyncio.sleep(0, result=upstream),
    )
    refusals = 0
    base_request = upstream.request

    async def request(method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        nonlocal refusals
        if params.get("name") == "list_windows" and refusals == 0:
            refusals += 1
            upstream.calls.append((method, json.loads(json.dumps(params))))
            return {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "content": [{"type": "text", "text": "session has ended"}],
                    "isError": True,
                    "structuredContent": {
                        "status": "refused",
                        "refusal": {
                            "code": "session_ended",
                            "message": "session has ended",
                        },
                    },
                },
            }
        return await base_request(method, params)

    upstream.request = request  # type: ignore[method-assign]
    await server.call_tool("list_apps", {"session": "ses-a"})
    result = await server.call_tool("list_windows", {"session": "ses-a"})

    assert result.get("isError") is not True
    names = [params["name"] for _method, params in upstream.calls]
    assert names == [
        "start_session",
        "list_apps",
        "list_windows",
        "start_session",
        "list_windows",
    ]


@pytest.mark.asyncio
async def test_end_session_if_active_releases_without_starting_unknown_session(
    tmp_path: Path,
) -> None:
    state_path, state = _state(tmp_path)
    upstream = FakeUpstream()
    server = ComputerUseServer(
        state_path=state_path,
        status_reader=lambda: ComputerUseStatus("ready", None, state),
        upstream_factory=lambda _state: asyncio.sleep(0, result=upstream),
    )

    idle = await server.end_session_if_active("ses-never-used")
    await server.call_tool("list_apps", {"session": "ses-a"})
    ended = await server.end_session_if_active("ses-a")

    assert idle["isError"] is False
    assert ended.get("isError") is not True
    assert "ses-a" not in server._sessions
    names = [params["name"] for _method, params in upstream.calls]
    assert names == ["start_session", "list_apps", "end_session"]


@pytest.mark.asyncio
async def test_retire_session_releases_session_state_and_lock(tmp_path: Path) -> None:
    """Terminal archive cleanup drops both registries without starting unknown sessions."""

    state_path, state = _state(tmp_path)
    upstream = FakeUpstream()
    server = ComputerUseServer(
        state_path=state_path,
        status_reader=lambda: ComputerUseStatus("ready", None, state),
        upstream_factory=lambda _state: asyncio.sleep(0, result=upstream),
    )

    unknown = await server.retire_session("ses-never-used")
    assert unknown.get("isError") is not True
    assert "ses-never-used" not in server._session_locks

    server._session_locks["ses-idle"] = asyncio.Lock()
    server._sessions["ses-idle"] = _SessionState()
    idle = await server.retire_session("ses-idle")
    assert idle.get("isError") is not True
    assert "ses-idle" not in server._sessions
    assert "ses-idle" not in server._session_locks

    await server.call_tool("list_apps", {"session": "ses-a"})
    assert "ses-a" in server._sessions
    assert "ses-a" in server._session_locks

    retired = await server.retire_session("ses-a")

    assert retired.get("isError") is not True
    assert "ses-a" not in server._sessions
    assert "ses-a" not in server._session_locks
    assert [params["name"] for _method, params in upstream.calls] == [
        "start_session",
        "list_apps",
        "end_session",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("replace_lock", [False, True])
async def test_late_retire_cleanup_preserves_newer_session_identity(
    tmp_path: Path,
    replace_lock: bool,
) -> None:
    """A delayed archive cleanup cannot remove a replacement state or lock."""

    state_path, state = _state(tmp_path)
    upstream = FakeUpstream()
    server = ComputerUseServer(
        state_path=state_path,
        status_reader=lambda: ComputerUseStatus("ready", None, state),
        upstream_factory=lambda _state: asyncio.sleep(0, result=upstream),
    )
    await server.call_tool("list_apps", {"session": "ses-a"})
    old_lock = server._session_locks["ses-a"]
    entered = asyncio.Event()
    release = asyncio.Event()

    async def delayed_end_session(_session: str) -> dict[str, Any]:
        entered.set()
        await release.wait()
        return {"isError": False, "content": []}

    server.end_session_if_active = delayed_end_session  # type: ignore[method-assign]
    cleanup = asyncio.create_task(server.retire_session("ses-a"))
    await entered.wait()

    new_lock = asyncio.Lock() if replace_lock else old_lock
    new_state = _SessionState()
    server._session_locks["ses-a"] = new_lock
    server._sessions["ses-a"] = new_state
    release.set()
    retired = await cleanup

    assert retired.get("isError") is not True
    assert server._session_locks["ses-a"] is new_lock
    assert server._sessions["ses-a"] is new_state


@pytest.mark.asyncio
async def test_explicit_start_forwards_options_and_normalized_session(
    tmp_path: Path,
) -> None:
    """The public start tool reaches the driver instead of fabricating success."""

    state_path, state = _state(tmp_path)
    upstream = FakeUpstream()
    server = ComputerUseServer(
        state_path=state_path,
        status_reader=lambda: ComputerUseStatus("ready", None, state),
        upstream_factory=lambda _state: asyncio.sleep(0, result=upstream),
    )
    result = await server.call_tool(
        "start_session",
        {
            "session": "  ses-a  ",
            "capture_scope": "window",
            "cursor_theme": {
                "theme_id": "high-contrast",
                "reduced_motion": "on",
            },
        },
    )

    assert not result.get("isError")
    assert [
        (params["name"], params["arguments"])
        for _method, params in upstream.calls
    ] == [
        (
            "start_session",
            {
                "session": "ses-a",
                "capture_scope": "window",
                "cursor_theme": {
                    "theme_id": "high-contrast",
                    "reduced_motion": "on",
                },
            },
        )
    ]
    session_state = server._sessions["ses-a"]
    assert session_state.active_proxy_serial == upstream.serial
    assert session_state.active_daemon_key == state.daemon_key


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
    )

    first = await server.call_tool(
        "click",
        {"session": "ses-a", "pid": 7, "window_id": 9, "x": 1, "y": 2},
    )
    assert "observe_first" in first["content"][0]["text"]

    raw_before_observation = await server.call_tool(
        "click",
        {
            "session": "ses-a",
            "pid": 7,
            "window_id": 9,
            "x": 1,
            "y": 2,
            "click_mode": "raw",
        },
    )
    assert "observe_first" in raw_before_observation["content"][0]["text"]
    assert not any(
        params["arguments"].get("click_mode") == "raw"
        for _method, params in upstream.calls
    )

    observed = await server.call_tool(
        "get_window_state",
        {"session": "ses-a", "pid": 7, "window_id": 9},
    )
    assert not observed.get("isError")
    token = observed["structuredContent"]["elements"][0]["element_token"]
    token_clicked = await server.call_tool(
        "click",
        {"session": "ses-a", "element_token": token, "x": 1, "y": 2},
    )
    assert not token_clicked.get("isError")
    unknown_token = await server.call_tool(
        "click",
        {"session": "ses-a", "element_token": "unknown-token", "x": 1, "y": 2},
    )
    unknown_payload = json.loads(unknown_token["content"][0]["text"])
    assert unknown_payload["code"] == "observe_first"
    assert unknown_payload["dispatched"] is False
    clicked = await server.call_tool(
        "click",
        {"session": "ses-a", "pid": 7, "window_id": 9, "x": 1, "y": 2},
    )
    assert not clicked.get("isError")

    raw_after_observation = await server.call_tool(
        "click",
        {
            "session": "ses-a",
            "pid": 7,
            "window_id": 9,
            "x": 1,
            "y": 2,
            "click_mode": "raw",
        },
    )
    assert not raw_after_observation.get("isError")
    assert upstream.calls[-1][1]["arguments"]["click_mode"] == "raw"

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
async def test_pidless_zoom_preserves_observation_for_exact_window_input(
    tmp_path: Path,
) -> None:
    """A zoom that resolves its pid from the driver keeps the legacy wildcard observation."""

    state_path, state = _state(tmp_path)
    upstream = FakeUpstream()
    server = ComputerUseServer(
        state_path=state_path,
        status_reader=lambda: ComputerUseStatus("ready", None, state),
        upstream_factory=lambda _state: asyncio.sleep(0, result=upstream),
    )

    zoomed = await server.call_tool(
        "zoom",
        {
            "session": "ses-a",
            "window_id": 9,
            "x1": 0,
            "y1": 0,
            "x2": 100,
            "y2": 100,
        },
    )
    assert not zoomed.get("isError")
    clicked = await server.call_tool(
        "click",
        {"session": "ses-a", "pid": 7, "window_id": 9, "x": 1, "y": 2},
    )
    assert not clicked.get("isError")


def test_raw_click_mode_is_admitted_only_for_exact_background_pixel_click() -> None:
    valid_raw_click = {
        "click_mode": "raw",
        "pid": 7,
        "window_id": 9,
        "x": 1,
        "y": 2,
    }

    _validate_input("click", valid_raw_click)
    _validate_input(
        "click",
        {**valid_raw_click, "delivery_mode": "background"},
    )
    with pytest.raises(ComputerServerError, match="only for the click tool"):
        _validate_input("get_window_state", {"click_mode": "raw"})
    with pytest.raises(ComputerServerError, match="exact pid"):
        _validate_input(
            "click",
            {
                **valid_raw_click,
                "modifier": ["cmd"],
            },
        )
    for delivery_mode in (None, "foreground", "future-mode"):
        with pytest.raises(ComputerServerError, match="background left-click"):
            _validate_input(
                "click",
                {**valid_raw_click, "delivery_mode": delivery_mode},
            )
    with pytest.raises(ComputerServerError, match="auto.*raw"):
        _validate_input("click", {"click_mode": "semantic"})


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
    )
    old_call = asyncio.create_task(
        server.call_tool("list_apps", {"session": "ses-a"})
    )
    await _wait_until(
        lambda: upstreams and upstreams[0].inflight_by_tool.get("list_apps")
    )
    old = upstreams[0]
    old.fail_after_unblock = True

    current = ComputerUseState(**{**first_state.__dict__, "generation": 2})
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
async def test_new_server_process_requires_observation_again(tmp_path: Path) -> None:
    """Observation state is intentionally process-local and starts empty."""

    state_path, state = _state(tmp_path)
    first_upstream = FakeUpstream()
    first = ComputerUseServer(
        state_path=state_path,
        status_reader=lambda: ComputerUseStatus("ready", None, state),
        upstream_factory=lambda _state: asyncio.sleep(0, result=first_upstream),
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
    )
    result = await second.call_tool(
        "click",
        {"session": "ses-a", "pid": 7, "window_id": 9, "x": 1, "y": 2},
    )
    assert "observe_first" in result["content"][0]["text"]


@pytest.mark.asyncio
async def test_post_proxy_creation_revalidates_generation_before_forwarding(
    tmp_path: Path,
) -> None:
    """A proxy created from stale D is discarded before any user tool reaches it."""

    state_path, initial = _state(tmp_path)
    current = initial
    upstreams: list[FakeUpstream] = []

    def status_reader() -> ComputerUseStatus:
        return ComputerUseStatus("ready", None, current)

    async def factory(_state: ComputerUseState) -> FakeUpstream:
        nonlocal current
        upstream = FakeUpstream()
        upstreams.append(upstream)
        if len(upstreams) == 1:
            current = ComputerUseState(
                **{**initial.__dict__, "generation": initial.generation + 1}
            )
        return upstream

    server = ComputerUseServer(
        state_path=state_path,
        status_reader=status_reader,
        upstream_factory=factory,
    )

    result = await server.call_tool("list_apps", {"session": "ses-a"})

    assert not result.get("isError")
    assert len(upstreams) == 2
    assert upstreams[0].calls == []
    assert upstreams[0].close_calls == 1
    assert [params["name"] for _method, params in upstreams[1].calls] == [
        "start_session",
        "list_apps",
    ]


@pytest.mark.asyncio
async def test_admission_retries_generation_change_exactly_once(
    tmp_path: Path,
) -> None:
    """Two consecutive generation changes stop after one bounded retry."""

    state_path, current = _state(tmp_path)
    upstreams: list[FakeUpstream] = []

    def status_reader() -> ComputerUseStatus:
        return ComputerUseStatus("ready", None, current)

    async def factory(_state: ComputerUseState) -> FakeUpstream:
        nonlocal current
        upstream = FakeUpstream()
        upstreams.append(upstream)
        current = ComputerUseState(
            **{**current.__dict__, "generation": current.generation + 1}
        )
        return upstream

    server = ComputerUseServer(
        state_path=state_path,
        status_reader=status_reader,
        upstream_factory=factory,
    )

    result = await server.call_tool("list_apps", {"session": "ses-a"})

    assert result["isError"]
    assert "daemon_generation_changed" in result["content"][0]["text"]
    assert len(upstreams) == 2
    assert all(upstream.calls == [] for upstream in upstreams)
    assert all(upstream.close_calls == 1 for upstream in upstreams)


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


@pytest.mark.asyncio
async def test_observations_are_not_serialized_across_sessions(tmp_path: Path) -> None:
    """Read-only window observations can run concurrently."""

    state_path, state = _state(tmp_path)
    upstream = FakeUpstream()
    upstream.block = asyncio.Event()
    upstream.blocked_tools = {"get_window_state"}
    server = ComputerUseServer(
        state_path=state_path,
        status_reader=lambda: ComputerUseStatus("ready", None, state),
        upstream_factory=lambda _state: asyncio.sleep(0, result=upstream),
    )

    first = asyncio.create_task(
        server.call_tool(
            "get_window_state",
            {"session": "ses-a", "pid": 7, "window_id": 9},
        )
    )
    second = asyncio.create_task(
        server.call_tool(
            "get_window_state",
            {"session": "ses-b", "pid": 7, "window_id": 9},
        )
    )
    await _wait_until(lambda: upstream.max_inflight >= 2)
    assert upstream.max_inflight == 2
    upstream.block.set()
    assert not (await first).get("isError")
    assert not (await second).get("isError")


@pytest.mark.asyncio
async def test_target_lock_wait_revalidates_generation_before_dispatch(
    tmp_path: Path,
) -> None:
    """A daemon restart while waiting cannot send the waiting action to the old proxy."""

    state_path, initial = _state(tmp_path)
    current = initial
    upstreams: list[FakeUpstream] = []
    target_locks = _ObservedTargetLocks(wait_seconds=5.0)

    async def factory(_state: ComputerUseState) -> FakeUpstream:
        upstream = FakeUpstream()
        if not upstreams:
            upstream.block = asyncio.Event()
            upstream.blocked_tools = {"clipboard_write"}
        upstreams.append(upstream)
        return upstream

    server = ComputerUseServer(
        state_path=state_path,
        status_reader=lambda: ComputerUseStatus("ready", None, current),
        upstream_factory=factory,
        target_lock_manager=target_locks,
    )
    first = asyncio.create_task(
        server.call_tool(
            "clipboard_write",
            {"session": "ses-a", "text": "first"},
        )
    )
    await _wait_until(
        lambda: upstreams and upstreams[0].inflight_by_tool.get("clipboard_write")
    )
    second = asyncio.create_task(
        server.call_tool(
            "clipboard_write",
            {"session": "ses-b", "text": "second"},
        )
    )
    await _wait_until(lambda: target_locks.refusals)

    current = ComputerUseState(
        **{**initial.__dict__, "generation": initial.generation + 1}
    )
    old = upstreams[0]
    assert old.block is not None
    old.block.set()
    first_result = await first
    second_result = await second

    assert first_result.get("isError") is not True
    assert second_result.get("isError") is not True, second_result
    assert len(upstreams) == 2
    assert old.close_calls == 1
    old_actions = [
        params["arguments"]["text"]
        for _method, params in old.calls
        if params["name"] == "clipboard_write"
    ]
    new_actions = [
        params["arguments"]["text"]
        for _method, params in upstreams[1].calls
        if params["name"] == "clipboard_write"
    ]
    assert old_actions == ["first"]
    assert new_actions == ["second"]


@pytest.mark.asyncio
async def test_different_window_mutations_run_concurrently(tmp_path: Path) -> None:
    """Different window targets do not wait on one another."""

    state_path, state = _state(tmp_path)
    upstream = FakeUpstream()
    server = ComputerUseServer(
        state_path=state_path,
        status_reader=lambda: ComputerUseStatus("ready", None, state),
        upstream_factory=lambda _state: asyncio.sleep(0, result=upstream),
    )
    for session, window_id in (("ses-a", 9), ("ses-b", 10)):
        observed = await server.call_tool(
            "get_window_state",
            {"session": session, "pid": 7, "window_id": window_id},
        )
        assert not observed.get("isError")

    upstream.block = asyncio.Event()
    upstream.blocked_tools = {"click"}
    first = asyncio.create_task(
        server.call_tool(
            "click",
            {"session": "ses-a", "pid": 7, "window_id": 9, "x": 1, "y": 2},
        )
    )
    second = asyncio.create_task(
        server.call_tool(
            "click",
            {"session": "ses-b", "pid": 7, "window_id": 10, "x": 1, "y": 2},
        )
    )
    await _wait_until(lambda: upstream.max_inflight >= 2)
    assert upstream.max_inflight == 2
    upstream.block.set()
    assert not (await first).get("isError")
    assert not (await second).get("isError")


@pytest.mark.asyncio
async def test_same_window_lock_waits_then_releases_to_the_next_session(
) -> None:
    """A target lock waits for release, then admits the next operation."""

    manager = _ObservedTargetLocks(wait_seconds=5.0)
    arguments = {"pid": 7, "window_id": 9}
    first = await manager.acquire("click", arguments)
    second_task = asyncio.create_task(manager.acquire("click", arguments))
    await _wait_until(lambda: manager.refusals)
    assert not second_task.done()
    await first.__aexit__(None, None, None)
    second = await second_task
    await second.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_target_lock_timeout_returns_target_busy_without_dispatch(
    tmp_path: Path,
) -> None:
    """A bounded lock wait returns a retryable, pre-dispatch error."""

    state_path, state = _state(tmp_path)
    upstream = FakeUpstream()
    upstream.block = asyncio.Event()
    upstream.blocked_tools = {"click"}
    server = ComputerUseServer(
        state_path=state_path,
        status_reader=lambda: ComputerUseStatus("ready", None, state),
        upstream_factory=lambda _state: asyncio.sleep(0, result=upstream),
        target_lock_manager=_TargetLockManager(wait_seconds=0.01),
    )
    for session in ("ses-a", "ses-b"):
        observed = await server.call_tool(
            "get_window_state",
            {"session": session, "pid": 7, "window_id": 9},
        )
        assert not observed.get("isError")

    first = asyncio.create_task(
        server.call_tool(
            "click",
            {"session": "ses-a", "pid": 7, "window_id": 9, "x": 1, "y": 2},
        )
    )
    await _wait_until(lambda: upstream.inflight_by_tool.get("click"))
    # Bounded: without the target lock this click would block on the upstream.
    second = await asyncio.wait_for(
        server.call_tool(
            "click",
            {"session": "ses-b", "pid": 7, "window_id": 9, "x": 3, "y": 4},
        ),
        timeout=5,
    )
    payload = json.loads(second["content"][0]["text"])
    assert payload["code"] == "target_busy"
    assert payload["dispatched"] is False
    assert sum(params["name"] == "click" for _method, params in upstream.calls) == 1
    upstream.block.set()
    assert not (await first).get("isError")


@pytest.mark.asyncio
async def test_force_quit_conflicts_with_window_lock_for_same_pid(
    tmp_path: Path,
) -> None:
    """Killing a process waits behind every mutation lock for its windows."""

    state_path, state = _state(tmp_path)
    upstream = FakeUpstream()
    upstream.block = asyncio.Event()
    upstream.blocked_tools = {"click"}
    server = ComputerUseServer(
        state_path=state_path,
        status_reader=lambda: ComputerUseStatus("ready", None, state),
        upstream_factory=lambda _state: asyncio.sleep(0, result=upstream),
        target_lock_manager=_TargetLockManager(wait_seconds=0.01),
    )
    for session in ("ses-a", "ses-b"):
        observed = await server.call_tool(
            "get_window_state",
            {"session": session, "pid": 7, "window_id": 9},
        )
        assert not observed.get("isError")

    click = asyncio.create_task(
        server.call_tool(
            "click",
            {"session": "ses-a", "pid": 7, "window_id": 9, "x": 1, "y": 2},
        )
    )
    await _wait_until(lambda: upstream.inflight_by_tool.get("click"))
    killed = await server.call_tool("kill_app", {"session": "ses-b", "pid": 7})
    payload = json.loads(killed["content"][0]["text"])
    assert payload["code"] == "target_busy"
    assert payload["dispatched"] is False
    assert not any(params["name"] == "kill_app" for _method, params in upstream.calls)
    upstream.block.set()
    assert not (await click).get("isError")


@pytest.mark.asyncio
async def test_clipboard_writes_are_serialized(tmp_path: Path) -> None:
    """Clipboard writes share one target lock across sessions."""

    state_path, state = _state(tmp_path)
    upstream = FakeUpstream()
    upstream.block = asyncio.Event()
    upstream.blocked_tools = {"clipboard_write"}
    target_locks = _ObservedTargetLocks()
    server = ComputerUseServer(
        state_path=state_path,
        status_reader=lambda: ComputerUseStatus("ready", None, state),
        upstream_factory=lambda _state: asyncio.sleep(0, result=upstream),
        target_lock_manager=target_locks,
    )
    first = asyncio.create_task(
        server.call_tool(
            "clipboard_write",
            {"session": "ses-a", "text": "first"},
        )
    )
    await _wait_until(lambda: upstream.inflight_by_tool.get("clipboard_write"))
    second = asyncio.create_task(
        server.call_tool(
            "clipboard_write",
            {"session": "ses-b", "text": "second"},
        )
    )
    await _wait_until(lambda: target_locks.refusals)
    assert sum(
        params["name"] == "clipboard_write"
        for _method, params in upstream.calls
    ) == 1
    upstream.block.set()
    assert not (await first).get("isError")
    assert not (await second).get("isError")
    assert upstream.max_inflight_by_tool["clipboard_write"] == 1


def test_legacy_lease_files_are_removed_on_server_start(tmp_path: Path) -> None:
    """Released lease artifacts are ignored and cleaned up best-effort."""

    state_path, _state_value = _state(tmp_path)
    (tmp_path / "computer-lease.json").write_text(
        '{"holder":"old-session","epoch":4}',
        encoding="utf-8",
    )
    (tmp_path / "computer-lease.lock").write_bytes(b"legacy")

    ComputerUseServer(state_path=state_path)

    assert not (tmp_path / "computer-lease.json").exists()
    assert not (tmp_path / "computer-lease.lock").exists()


@pytest.mark.asyncio
async def test_action_after_other_session_action_uses_driver_stale_token_result(
    tmp_path: Path,
) -> None:
    """The driver, rather than the Controller, rejects a token stale after another action."""

    state_path, state = _state(tmp_path)
    upstream = FakeUpstream()
    target_locks = _ObservedTargetLocks()
    server = ComputerUseServer(
        state_path=state_path,
        status_reader=lambda: ComputerUseStatus("ready", None, state),
        upstream_factory=lambda _state: asyncio.sleep(0, result=upstream),
        target_lock_manager=target_locks,
    )
    for session in ("ses-a", "ses-b"):
        observed = await server.call_tool(
            "get_window_state",
            {"session": session, "pid": 7, "window_id": 9},
        )
        assert not observed.get("isError")
        if session == "ses-a":
            token_a = observed["structuredContent"]["elements"][0]["element_token"]
        else:
            token_b = observed["structuredContent"]["elements"][0]["element_token"]

    upstream.block = asyncio.Event()
    upstream.blocked_tools = {"click"}
    upstream.stale_element_tokens.add(token_b)
    first_task = asyncio.create_task(
        server.call_tool(
            "click",
            {
                "session": "ses-a",
                "pid": 7,
                "window_id": 9,
                "element_token": token_a,
                "x": 1,
                "y": 2,
            },
        )
    )
    await _wait_until(lambda: upstream.inflight_by_tool.get("click"))
    stale_task = asyncio.create_task(
        server.call_tool(
            "click",
            {
                "session": "ses-b",
                "pid": 7,
                "window_id": 9,
                "element_token": token_b,
                "x": 3,
                "y": 4,
            },
        )
    )
    await _wait_until(lambda: target_locks.refusals)
    upstream.block.set()
    first = await first_task
    stale = await stale_task
    assert not first.get("isError")
    payload = json.loads(stale["content"][0]["text"])
    assert payload["code"] == "stale_element_token"
    assert payload["dispatched"] is False
    assert sum(
        params["name"] == "click"
        for _method, params in upstream.calls
    ) == 2


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
    """A silent but live child cannot hold a target lock indefinitely."""

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
async def test_upstream_request_deadline_covers_a_blocked_drain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The request bound starts before writing bytes to a stalled child."""

    monkeypatch.setattr(
        "core.computer_server._UPSTREAM_REQUEST_TIMEOUT_SECONDS",
        0.01,
    )
    stdin = _BlockingStdin()
    upstream = JsonRpcUpstream(  # type: ignore[arg-type]
        _TransportProcess(_BlockedStream(), stdin=stdin)
    )

    with pytest.raises(UpstreamUnavailable, match="180 second"):
        await upstream.request("tools/call", {"name": "list_apps"})

    assert len(stdin.writes) == 1
    assert upstream._pending == {}
    await upstream.close()


@pytest.mark.asyncio
async def test_upstream_request_deadline_covers_queued_writers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every caller waiting for the shared write lock has its own full deadline."""

    monkeypatch.setattr(
        "core.computer_server._UPSTREAM_REQUEST_TIMEOUT_SECONDS",
        0.02,
    )
    stdin = _BlockingStdin()
    upstream = JsonRpcUpstream(  # type: ignore[arg-type]
        _TransportProcess(_BlockedStream(), stdin=stdin)
    )

    loop = asyncio.get_running_loop()
    first_started = loop.time()
    first = asyncio.create_task(
        upstream.request("tools/call", {"name": "list_apps"})
    )
    await _wait_until(lambda: stdin.writes)
    assert len(stdin.writes) == 1
    second_started = loop.time()
    second = asyncio.create_task(
        upstream.request("tools/call", {"name": "list_windows"})
    )

    results = await asyncio.wait_for(
        asyncio.gather(first, second, return_exceptions=True),
        timeout=0.2,
    )

    assert all(
        isinstance(result, UpstreamUnavailable)
        and "180 second" in str(result)
        for result in results
    )
    finished = loop.time()
    assert finished - first_started < 0.2
    assert finished - second_started < 0.2
    assert 1 <= len(stdin.writes) <= 2
    assert upstream._pending == {}
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
    monkeypatch.setenv("ANTHROPIC_API_KEY", "provider-secret-sentinel")
    monkeypatch.setenv("AVIBE_CALLER_AUTHORIZATION", "caller-secret-sentinel")
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
    assert "ANTHROPIC_API_KEY" not in env
    assert "AVIBE_CALLER_AUTHORIZATION" not in env
    assert set(env).issubset(
        {
            "CUA_DRIVER_EMBEDDED",
            "CUA_DRIVER_RS_TELEMETRY_ENABLED",
            "CUA_DRIVER_RS_UPDATE_CHECK",
            "HOME",
            "LANG",
            "LC_ALL",
            "LC_CTYPE",
            "PATH",
            "TEMP",
            "TMP",
            "TMPDIR",
        }
    )
    await upstream.close()
