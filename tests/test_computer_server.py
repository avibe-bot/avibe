from __future__ import annotations

import asyncio
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import shutil
import time
from typing import Any, Callable, Mapping

import pytest

from core.computer_server import (
    ComputerServerError,
    ComputerUseServer,
    DesktopLeaseManager,
    JsonRpcUpstream,
    Upstream,
    UpstreamUnavailable,
    _fold_tool_result,
    _foreground_control_socket_path,
    _frontmost_window,
    _record_window_identity,
    _validate_foreground_input,
    _validate_input,
    _verify_window_identity,
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
        self.blocked_tools: set[str] = {"list_apps"}
        self.tool_errors: set[str] = set()
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
            tool_name = params.get("name")
            if self.block is not None and tool_name in self.blocked_tools:
                await self.block.wait()
            if self.fail_after_unblock:
                raise UpstreamUnavailable("transport failed after replacement")
            if tool_name in self.tool_errors:
                return {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {
                        "content": [{"type": "text", "text": "tool failed"}],
                        "isError": True,
                    },
                }
            structured: dict[str, Any] = {"tool": params.get("name")}
            if tool_name == "get_window_state":
                arguments = params.get("arguments") or {}
                pid = arguments.get("pid")
                window_id = arguments.get("window_id")
                structured.update(
                    {
                        "pid": pid,
                        "window_id": window_id,
                        "snapshot_id": f"snapshot-{pid}-{window_id}",
                        "capture_id": f"capture-{pid}-{window_id}",
                        "elements": [
                            {
                                "element_token": (
                                    f"snapshot-{pid}-{window_id}:element-0"
                                )
                            }
                        ],
                    }
                )
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

    async def notify(self, method: str, params: Mapping[str, Any]) -> None:
        return None

    async def close(self) -> None:
        self.close_calls += 1
        self.alive = False


class FakeForegroundControl:
    def __init__(self, *, allowed: bool = True) -> None:
        self.allowed = allowed
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.responses: dict[str, dict[str, Any]] = {}

    async def request(
        self,
        operation: str,
        *,
        session: str,
        title: str,
        target: Mapping[str, Any] | None = None,
        restoration: Mapping[str, Any] | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            (
                operation,
                {
                    "session": session,
                    "title": title,
                    "target": dict(target or {}),
                    "restoration": dict(restoration or {}),
                    "reason": reason,
                },
            )
        )
        if operation in self.responses:
            return self.responses[operation]
        if operation == "request_consent":
            return {"ok": True, "allowed": self.allowed}
        if operation == "restore":
            restored = json.loads(json.dumps(restoration or {}))
            components = restored.get("components", {})
            for name in ("prior_frontmost", "pointer", "active_space"):
                component = components.get(name)
                if isinstance(component, dict):
                    component["attempted"] = True
                    component["verified"] = False
                    component["failure_detail"] = None
            restored["restoration_ok"] = False
            return {"ok": True, "restoration": restored}
        return {"ok": True}


class ForegroundUpstream(FakeUpstream):
    def __init__(
        self,
        *,
        target: dict[str, Any] | None = None,
        after_restore: dict[str, Any] | None = None,
        pointer: tuple[float, float] = (10.0, 20.0),
        after_pointer: tuple[float, float] | None = None,
        click_effect: str = "confirmed",
        after_prior_title: str | None = None,
        after_prior_space_id: int | None = None,
        fail_restore_windows: bool = False,
        omit_target_after_restore: bool = False,
        extra_windows: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__()
        self.target = target or {
            "pid": 7,
            "window_id": 9,
            "app_name": "Target",
            "title": "Target Window",
            "is_on_screen": True,
            "minimized": False,
            "on_current_space": True,
            "current_space_id": 1,
            "z_index": 10,
        }
        self.after_restore = after_restore
        self.pointer = pointer
        self.after_pointer = after_pointer or pointer
        self.click_effect = click_effect
        self.after_prior_title = after_prior_title
        self.after_prior_space_id = after_prior_space_id
        self.fail_restore_windows = fail_restore_windows
        self.omit_target_after_restore = omit_target_after_restore
        self.extra_windows = extra_windows or []
        self.list_windows_calls = 0
        self.cursor_calls = 0

    async def request(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        tool_name = params.get("name")
        if tool_name == "list_windows":
            self.calls.append((method, json.loads(json.dumps(params))))
            self.list_windows_calls += 1
            if self.list_windows_calls > 1 and self.fail_restore_windows:
                return {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {
                        "isError": True,
                        "content": [],
                        "structuredContent": {
                            "code": "window_observation_failed"
                        },
                    },
                }
            target = (
                self.after_restore
                if self.list_windows_calls > 1 and self.after_restore is not None
                else self.target
            )
            prior = {
                "pid": 3,
                "window_id": 30,
                "app_name": "Prior",
                "title": (
                    self.after_prior_title
                    if self.list_windows_calls > 1
                    and self.after_prior_title is not None
                    else "Prior Window"
                ),
                "is_on_screen": True,
                "minimized": False,
                "on_current_space": True,
                "current_space_id": (
                    self.after_prior_space_id
                    if self.list_windows_calls > 1
                    and self.after_prior_space_id is not None
                    else 1
                ),
                "z_index": 20,
            }
            return {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "content": [],
                    "structuredContent": {
                        "current_space_id": 1,
                        "windows": [
                            *(
                                []
                                if self.list_windows_calls > 1
                                and self.omit_target_after_restore
                                else [target]
                            ),
                            prior,
                            *self.extra_windows,
                        ],
                    },
                },
            }
        if tool_name == "get_cursor_position":
            self.calls.append((method, json.loads(json.dumps(params))))
            self.cursor_calls += 1
            x, y = (
                self.after_pointer
                if self.cursor_calls > 1
                else self.pointer
            )
            return {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "content": [],
                    "structuredContent": {"available": True, "x": x, "y": y},
                },
            }
        if tool_name == "click":
            self.calls.append((method, json.loads(json.dumps(params))))
            return {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "content": [],
                    "structuredContent": {
                        "effect": self.click_effect,
                        "route": "trusted_input",
                        "delivery": {
                            "mode": "foreground",
                            "delivered_count": 1,
                        },
                    },
                },
            }
        if tool_name == "get_window_state":
            self.calls.append((method, json.loads(json.dumps(params))))
            return {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "content": [],
                    "structuredContent": {
                        "pid": 7,
                        "window_id": 9,
                        "snapshot_id": "snapshot-7-9",
                        "capture_id": "capture-7-9",
                        "elements": [
                            {"element_token": "snapshot-7-9:element-0"}
                        ],
                    },
                },
            }
        return await super().request(method, params)


class NonCancellableForegroundUpstream(ForegroundUpstream):
    def __init__(self) -> None:
        super().__init__()
        self.click_started = asyncio.Event()
        self.release_click = asyncio.Event()

    async def request(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        if params.get("name") == "click":
            self.calls.append((method, json.loads(json.dumps(params))))
            self.click_started.set()
            await self.release_click.wait()
            return await super().request(method, params)
        return await super().request(method, params)


class FailingRestoreControl(FakeForegroundControl):
    async def request(
        self,
        operation: str,
        *,
        session: str,
        title: str,
        target: Mapping[str, Any] | None = None,
        restoration: Mapping[str, Any] | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        result = await super().request(
            operation,
            session=session,
            title=title,
            target=target,
            restoration=restoration,
            reason=reason,
        )
        if operation == "restore":
            raise RuntimeError("unexpected native restore failure")
        return result


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
            "get_window_state",
            {"pid": 41, "window_id": 9, "session": "ses-script"},
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
            ["start_session", "list_apps", "list_windows"],
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
        lease_manager=FakeLeaseManager(),  # type: ignore[arg-type]
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
        lease_manager=FakeLeaseManager(),  # type: ignore[arg-type]
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
        "list_apps",
        "list_windows",
        "get_window_state",
    ]
    assert upstream.calls[-1][1]["arguments"] == {
        "pid": 41,
        "window_id": 9,
        "session": "ses-script",
    }


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
        lease_manager=FakeLeaseManager(),  # type: ignore[arg-type]
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
        lease_manager=FakeLeaseManager(),  # type: ignore[arg-type]
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
async def test_end_session_if_active_releases_without_starting_unknown_session(
    tmp_path: Path,
) -> None:
    state_path, state = _state(tmp_path)
    upstream = FakeUpstream()
    leases = FakeLeaseManager()
    server = ComputerUseServer(
        state_path=state_path,
        status_reader=lambda: ComputerUseStatus("ready", None, state),
        upstream_factory=lambda _state: asyncio.sleep(0, result=upstream),
        lease_manager=leases,  # type: ignore[arg-type]
    )

    idle = await server.end_session_if_active("ses-never-used")
    await server.call_tool("list_apps", {"session": "ses-a"})
    ended = await server.end_session_if_active("ses-a")

    assert idle["isError"] is False
    assert ended.get("isError") is not True
    assert leases.holder is None
    assert "ses-a" not in server._sessions
    assert "ses-a" not in server._session_locks
    names = [params["name"] for _method, params in upstream.calls]
    assert names == ["start_session", "list_apps", "end_session"]


@pytest.mark.asyncio
async def test_failed_explicit_end_keeps_session_observations_and_real_lease(
    tmp_path: Path,
) -> None:
    """A driver refusal to end cannot publish or lease an inactive session."""

    state_path, state = _state(tmp_path)
    leases = DesktopLeaseManager(tmp_path)
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
    session_state = server._sessions["ses-a"]
    expected_proxy = session_state.active_proxy_serial
    expected_key = session_state.active_daemon_key
    expected_epoch = session_state.last_epoch
    expected_observations = set(session_state.observed_windows)

    upstream.tool_errors.add("end_session")
    ended = await server.call_tool("end_session", {"session": "ses-a"})

    assert ended.get("isError")
    assert session_state.active_proxy_serial == expected_proxy
    assert session_state.active_daemon_key == expected_key
    assert session_state.last_epoch == expected_epoch
    assert session_state.observed_windows == expected_observations
    with pytest.raises(
        ComputerServerError,
        match="Desktop computer use is held by another Avibe Session",
    ):
        leases.acquire(
            "ses-b",
            state.daemon_key,
            lambda: state.daemon_key,
        )


@pytest.mark.asyncio
async def test_retire_session_releases_an_inactive_proxy_owned_lease_without_starting_driver(
    tmp_path: Path,
) -> None:
    state_path, state = _state(tmp_path)
    leases = DesktopLeaseManager(tmp_path)
    starts = 0

    async def upstream_factory(_state):
        nonlocal starts
        starts += 1
        return FakeUpstream()

    server = ComputerUseServer(
        state_path=state_path,
        status_reader=lambda: ComputerUseStatus("ready", None, state),
        upstream_factory=upstream_factory,
        lease_manager=leases,
    )

    leases.acquire("ses-a", state.daemon_key, lambda: state.daemon_key)
    retired = await server.retire_session("ses-a")

    assert retired["isError"] is False
    assert starts == 0
    assert "ses-a" not in server._sessions
    assert "ses-a" not in server._session_locks
    assert leases.acquire(
        "ses-b",
        state.daemon_key,
        lambda: state.daemon_key,
    ).lease.holder == "ses-b"


@pytest.mark.asyncio
async def test_retire_session_preserves_a_replacement_lease_holder(
    tmp_path: Path,
) -> None:
    state_path, first_state = _state(tmp_path)
    leases = DesktopLeaseManager(tmp_path)
    server = ComputerUseServer(
        state_path=state_path,
        status_reader=lambda: ComputerUseStatus("ready", None, first_state),
        upstream_factory=lambda _state: pytest.fail("retirement started a driver"),
        lease_manager=leases,
    )

    leases.acquire(
        "ses-retired",
        first_state.daemon_key,
        lambda: first_state.daemon_key,
    )
    replacement_key = (first_state.instance_id, first_state.generation + 1)
    replacement = leases.acquire(
        "ses-replacement",
        replacement_key,
        lambda: replacement_key,
    ).lease

    retired = await server.retire_session("ses-retired")

    assert retired["isError"] is False
    assert leases.refresh(replacement) is True
    with pytest.raises(
        ComputerServerError,
        match="Desktop computer use is held by another Avibe Session",
    ):
        leases.acquire(
            "ses-other",
            replacement_key,
            lambda: replacement_key,
        )


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
        lease_manager=FakeLeaseManager(),  # type: ignore[arg-type]
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
    assert session_state.last_epoch == 1


@pytest.mark.asyncio
async def test_failed_explicit_start_does_not_change_committed_epoch(
    tmp_path: Path,
) -> None:
    """A failed explicit restart cannot publish a new session generation."""

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
    session_state = server._sessions["ses-a"]
    expected_proxy = session_state.active_proxy_serial
    expected_key = session_state.active_daemon_key
    expected_epoch = session_state.last_epoch

    upstream.tool_errors.add("start_session")
    result = await server.call_tool(
        "start_session",
        {"session": "ses-a", "capture_scope": "desktop"},
    )

    assert result.get("isError")
    payload = json.loads(result["content"][0]["text"])
    assert payload == {
        "code": "upstream_tool_error",
        "detail": "tool failed",
        "params": {"tool": "start_session"},
    }
    assert [params["name"] for _method, params in upstream.calls][-1] == "start_session"
    assert session_state.active_proxy_serial == expected_proxy
    assert session_state.active_daemon_key == expected_key
    assert session_state.last_epoch == expected_epoch
    with pytest.raises(
        ComputerServerError,
        match="Desktop computer use is held by another Avibe Session",
    ):
        leases.acquire(
            "ses-b",
            state.daemon_key,
            lambda: state.daemon_key,
        )


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
        platform="darwin",
        process_start_stamp_reader=lambda _pid: None,
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
            "capture_id": "capture-7-9",
            "delivery_mode": "foreground",
        },
    )
    assert "target_missing" in foreground["content"][0]["text"]

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


def _foreground_server(
    state_path: Path,
    state: ComputerUseState,
    upstream: Upstream,
    control: FakeForegroundControl,
    *,
    platform: str = "darwin",
    process_start_stamp_reader: Callable[[int], str | None] | None = None,
) -> ComputerUseServer:
    return ComputerUseServer(
        state_path=state_path,
        status_reader=lambda: ComputerUseStatus("ready", None, state),
        upstream_factory=lambda _state: asyncio.sleep(0, result=upstream),
        lease_manager=FakeLeaseManager(),  # type: ignore[arg-type]
        foreground_control=control,  # type: ignore[arg-type]
        platform=platform,
        process_start_stamp_reader=(
            process_start_stamp_reader or (lambda pid: f"start-{pid}")
        ),
    )


@pytest.mark.asyncio
async def test_foreground_requires_native_consent_and_reports_unattempted(
    tmp_path: Path,
) -> None:
    state_path, state = _state(tmp_path)
    upstream = ForegroundUpstream()
    control = FakeForegroundControl(allowed=False)
    server = _foreground_server(state_path, state, upstream, control)
    observed = await server.call_tool(
        "get_window_state",
        {"session": "ses-a", "pid": 7, "window_id": 9},
    )
    assert not observed.get("isError")

    result = await server.call_tool(
        "click",
        {
            "session": "ses-a",
            "pid": 7,
            "window_id": 9,
            "x": 1,
            "y": 2,
            "capture_id": "capture-7-9",
            "delivery_mode": "foreground",
        },
        session_title="QQ conversation",
    )

    payload = json.loads(result["content"][0]["text"])
    assert payload["code"] == "foreground_consent_required"
    assert payload["params"]["foreground_outcome"] == "unattempted"
    assert [name for name, _ in control.calls] == ["request_consent"]
    assert not any(
        params["arguments"].get("name") == "click"
        for _method, params in upstream.calls
    )


@pytest.mark.parametrize(
    ("refused_operation", "expected_control_calls"),
    [
        ("request_consent", ["request_consent"]),
        ("set_active", ["request_consent", "set_active"]),
    ],
)
@pytest.mark.asyncio
async def test_foreground_attention_pending_refuses_before_input(
    tmp_path: Path,
    refused_operation: str,
    expected_control_calls: list[str],
) -> None:
    state_path, state = _state(tmp_path)
    upstream = ForegroundUpstream()
    control = FakeForegroundControl()
    control.responses[refused_operation] = {
        "ok": False,
        "error": "foreground_attention_pending",
    }
    server = _foreground_server(state_path, state, upstream, control)
    observed = await server.call_tool(
        "get_window_state",
        {"session": "ses-a", "pid": 7, "window_id": 9},
    )
    assert not observed.get("isError")

    result = await server.call_tool(
        "click",
        {
            "session": "ses-a",
            "pid": 7,
            "window_id": 9,
            "x": 1,
            "y": 2,
            "capture_id": "capture-7-9",
            "delivery_mode": "foreground",
        },
    )

    payload = result["structuredContent"]
    assert payload["code"] == "foreground_attention_pending"
    assert payload["params"]["foreground_outcome"] == "unattempted"
    assert [name for name, _payload in control.calls] == expected_control_calls
    assert not any(
        params["arguments"].get("name") == "click"
        for _method, params in upstream.calls
    )

    background = await server.call_tool(
        "click",
        {
            "session": "ses-a",
            "pid": 7,
            "window_id": 9,
            "x": 1,
            "y": 2,
        },
    )
    assert not background.get("isError")
    background_click = next(
        params["arguments"]
        for _method, params in reversed(upstream.calls)
        if params.get("name") == "click"
    )
    assert "delivery_mode" not in background_click


@pytest.mark.asyncio
async def test_foreground_click_on_visible_target_verifies_restore(
    tmp_path: Path,
) -> None:
    state_path, state = _state(tmp_path)
    upstream = ForegroundUpstream(
        target={
            "pid": 7,
            "window_id": 9,
            "app_name": "Target",
            "title": "Target Window",
            "is_on_screen": True,
            "minimized": False,
            "on_current_space": True,
            "current_space_id": 1,
            "z_index": 10,
        },
    )
    control = FakeForegroundControl()
    server = _foreground_server(state_path, state, upstream, control)

    observed = await server.call_tool(
        "get_window_state",
        {
            "session": "ses-a",
            "pid": 7,
            "window_id": 9,
            "delivery_mode": "foreground",
        },
        session_title="QQ conversation",
    )
    assert not observed.get("isError")
    observed_call = next(
        params["arguments"]
        for _method, params in upstream.calls
        if params.get("name") == "get_window_state"
    )
    assert "delivery_mode" not in observed_call
    control.calls.clear()
    result = await server.call_tool(
        "click",
        {
            "session": "ses-a",
            "pid": 7,
            "window_id": 9,
            "x": 1,
            "y": 2,
            "capture_id": "capture-7-9",
            "delivery_mode": "foreground",
        },
        session_title="QQ conversation",
    )

    assert not result.get("isError")
    assert result["structuredContent"]["foreground_outcome"] == (
        "effect_occurred_restore_verified"
    )
    restoration = result["structuredContent"]["restoration"]
    assert restoration["restoration_ok"] is True
    assert all(
        component["attempted"]
        and component["verified"]
        and component["failure_detail"] is None
        for component in restoration["components"].values()
    )
    payloads = [
        json.loads(item["text"].split("\n", 1)[1])
        for item in result["content"]
        if item.get("type") == "text" and item["text"].startswith("Structured content:\n")
    ]
    assert payloads[-1]["foreground_outcome"] == "effect_occurred_restore_verified"
    assert payloads[-1]["restoration"] == restoration
    assert [name for name, _ in control.calls] == [
        "request_consent",
        "set_active",
        "restore",
        "terminal",
    ]
    click_calls = [
        params["arguments"]
        for _method, params in upstream.calls
        if params.get("name") == "click"
    ]
    assert click_calls == [
        {
            "session": "ses-a",
            "pid": 7,
            "window_id": 9,
            "x": 1,
            "y": 2,
            "capture_id": "capture-7-9",
            "delivery_mode": "foreground",
        }
    ]
    restore_request = next(
        payload
        for operation, payload in control.calls
        if operation == "restore"
    )
    assert restore_request["restoration"] == {
        "components": {
            "prior_frontmost": {
                "recorded": {
                    "pid": 3,
                    "process_start_stamp": "start-3",
                    "window_id": 30,
                },
                "attempted": False,
                "verified": False,
                "failure_detail": None,
            },
            "pointer": {
                "recorded": {"x": 10.0, "y": 20.0},
                "attempted": False,
                "verified": False,
                "failure_detail": None,
            },
            "active_space": {
                "recorded": {
                    "prior_frontmost_display": 1,
                    "target_display": 1,
                },
                "attempted": False,
                "verified": False,
                "failure_detail": None,
            },
        },
        "restoration_ok": False,
    }


@pytest.mark.asyncio
async def test_foreground_caller_cancellation_does_not_cancel_transaction(
    tmp_path: Path,
) -> None:
    state_path, state = _state(tmp_path)
    upstream = NonCancellableForegroundUpstream()
    control = FakeForegroundControl()
    server = _foreground_server(state_path, state, upstream, control)
    observed = await server.call_tool(
        "get_window_state",
        {"session": "ses-a", "pid": 7, "window_id": 9},
    )
    assert not observed.get("isError")

    call = asyncio.create_task(
        server.call_tool(
            "click",
            {
                "session": "ses-a",
                "pid": 7,
                "window_id": 9,
                "x": 1,
                "y": 2,
                "capture_id": "capture-7-9",
                "delivery_mode": "foreground",
            },
        )
    )
    await upstream.click_started.wait()
    call.cancel()

    with pytest.raises(asyncio.CancelledError):
        await call

    assert [name for name, _ in control.calls] == [
        "request_consent",
        "set_active",
    ]

    upstream.release_click.set()
    for _ in range(100):
        if server._foreground_transaction is None:
            break
        await asyncio.sleep(0)
    else:
        pytest.fail("foreground transaction did not reach terminal")

    assert [name for name, _ in control.calls] == [
        "request_consent",
        "set_active",
        "restore",
        "terminal",
    ]
    assert not any(
        name in {"cancel", "interrupt", "join"} for name, _ in control.calls
    )


@pytest.mark.asyncio
async def test_close_closes_admission_and_waits_for_exact_foreground_task(
    tmp_path: Path,
) -> None:
    state_path, state = _state(tmp_path)
    upstream = NonCancellableForegroundUpstream()
    control = FakeForegroundControl()
    server = _foreground_server(state_path, state, upstream, control)
    observed = await server.call_tool(
        "get_window_state",
        {"session": "ses-a", "pid": 7, "window_id": 9},
    )
    assert not observed.get("isError")

    call = asyncio.create_task(
        server.call_tool(
            "click",
            {
                "session": "ses-a",
                "pid": 7,
                "window_id": 9,
                "x": 1,
                "y": 2,
                "capture_id": "capture-7-9",
                "delivery_mode": "foreground",
            },
        )
    )
    await upstream.click_started.wait()
    foreground_task = server._foreground_transaction
    assert foreground_task is not None

    close = asyncio.create_task(server.close())
    await asyncio.sleep(0)
    assert not close.done()
    assert upstream.close_calls == 0
    assert server._foreground_admission_open is False

    refused = await server.call_tool(
        "click",
        {
            "session": "ses-b",
            "pid": 7,
            "window_id": 9,
            "x": 1,
            "y": 2,
            "capture_id": "capture-7-9",
            "delivery_mode": "foreground",
        },
    )
    assert refused["structuredContent"]["code"] == "computer_use_unavailable"
    assert server._foreground_transaction is foreground_task

    upstream.release_click.set()
    await asyncio.wait_for(close, timeout=1)
    result = await asyncio.wait_for(call, timeout=1)

    assert not result.get("isError")
    assert foreground_task.done()
    assert upstream.close_calls == 1
    assert [name for name, _payload in control.calls][-2:] == [
        "restore",
        "terminal",
    ]


@pytest.mark.asyncio
async def test_close_bounds_foreground_wait_before_closing_upstream(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("ERROR", logger="core.computer_server")
    monkeypatch.setattr(
        "core.computer_server._FOREGROUND_STOP_TIMEOUT_SECONDS",
        0.01,
    )
    state_path, state = _state(tmp_path)
    upstream = NonCancellableForegroundUpstream()
    control = FakeForegroundControl()
    server = _foreground_server(state_path, state, upstream, control)
    await server.call_tool(
        "get_window_state",
        {"session": "ses-a", "pid": 7, "window_id": 9},
    )
    call = asyncio.create_task(
        server.call_tool(
            "click",
            {
                "session": "ses-a",
                "pid": 7,
                "window_id": 9,
                "x": 1,
                "y": 2,
                "capture_id": "capture-7-9",
                "delivery_mode": "foreground",
            },
        )
    )
    await upstream.click_started.wait()
    foreground_task = server._foreground_transaction
    assert foreground_task is not None

    await asyncio.wait_for(server.close(), timeout=1)

    assert upstream.close_calls == 1
    assert not foreground_task.done()
    assert "foreground restoration incomplete" in caplog.text
    upstream.release_click.set()
    await asyncio.wait_for(call, timeout=1)


@pytest.mark.asyncio
async def test_foreground_restore_ignores_mutable_window_titles(
    tmp_path: Path,
) -> None:
    state_path, state = _state(tmp_path)
    upstream = ForegroundUpstream(after_prior_title="Renamed Prior Window")
    control = FakeForegroundControl()
    server = _foreground_server(state_path, state, upstream, control)
    await server.call_tool(
        "get_window_state",
        {"session": "ses-a", "pid": 7, "window_id": 9},
    )

    result = await server.call_tool(
        "click",
        {
            "session": "ses-a",
            "pid": 7,
            "window_id": 9,
            "x": 1,
            "y": 2,
            "capture_id": "capture-7-9",
            "delivery_mode": "foreground",
        },
    )

    assert not result.get("isError")
    assert result["structuredContent"]["restoration"]["restoration_ok"] is True


@pytest.mark.asyncio
async def test_foreground_restoration_records_each_involved_display_space(
    tmp_path: Path,
) -> None:
    state_path, state = _state(tmp_path)
    upstream = ForegroundUpstream(
        target={
            "pid": 7,
            "window_id": 9,
            "app_name": "Target",
            "title": "Target Window",
            "is_on_screen": True,
            "minimized": False,
            "on_current_space": True,
            "current_space_id": 2,
            "z_index": 10,
        }
    )
    control = FakeForegroundControl()
    server = _foreground_server(state_path, state, upstream, control)
    await server.call_tool(
        "get_window_state",
        {"session": "ses-a", "pid": 7, "window_id": 9},
    )

    result = await server.call_tool(
        "click",
        {
            "session": "ses-a",
            "pid": 7,
            "window_id": 9,
            "x": 1,
            "y": 2,
            "capture_id": "capture-7-9",
            "delivery_mode": "foreground",
        },
    )

    restoration = result["structuredContent"]["restoration"]
    assert restoration["restoration_ok"] is True
    assert restoration["components"]["active_space"]["recorded"] == {
        "prior_frontmost_display": 1,
        "target_display": 2,
    }


@pytest.mark.asyncio
async def test_foreground_restore_reports_missing_target_display_space(
    tmp_path: Path,
) -> None:
    state_path, state = _state(tmp_path)
    upstream = ForegroundUpstream(omit_target_after_restore=True)
    control = FakeForegroundControl()
    server = _foreground_server(state_path, state, upstream, control)
    await server.call_tool(
        "get_window_state",
        {"session": "ses-a", "pid": 7, "window_id": 9},
    )

    result = await server.call_tool(
        "click",
        {
            "session": "ses-a",
            "pid": 7,
            "window_id": 9,
            "x": 1,
            "y": 2,
            "capture_id": "capture-7-9",
            "delivery_mode": "foreground",
        },
    )

    assert result["structuredContent"]["code"] == "foreground_restore_failed"
    restoration = result["structuredContent"]["params"]["restoration"]
    assert restoration["restoration_ok"] is False
    assert restoration["components"]["active_space"]["verified"] is False


@pytest.mark.asyncio
async def test_foreground_refuses_reused_or_noncurrent_observation_identity(
    tmp_path: Path,
) -> None:
    state_path, state = _state(tmp_path)
    upstream = ForegroundUpstream()
    control = FakeForegroundControl()
    server = _foreground_server(state_path, state, upstream, control)
    await server.call_tool(
        "get_window_state",
        {"session": "ses-a", "pid": 7, "window_id": 9},
    )

    stale_capture = await server.call_tool(
        "click",
        {
            "session": "ses-a",
            "pid": 7,
            "window_id": 9,
            "x": 1,
            "y": 2,
            "capture_id": "capture-from-replaced-window",
            "delivery_mode": "foreground",
        },
    )
    stale_element = await server.call_tool(
        "click",
        {
            "session": "ses-a",
            "pid": 7,
            "window_id": 9,
            "element_token": "replaced-snapshot:element-0",
            "delivery_mode": "foreground",
        },
    )

    assert stale_capture["structuredContent"]["code"] == "target_not_admissible"
    assert stale_element["structuredContent"]["code"] == "target_not_admissible"
    assert control.calls == []


@pytest.mark.asyncio
async def test_foreground_admission_refuses_process_identity_reuse(
    tmp_path: Path,
) -> None:
    state_path, state = _state(tmp_path)
    upstream = ForegroundUpstream()
    control = FakeForegroundControl()
    reads = 0

    def process_start_stamp_reader(_pid: int) -> str:
        nonlocal reads
        reads += 1
        return "start-7" if reads <= 2 else "replacement-7"

    server = _foreground_server(
        state_path,
        state,
        upstream,
        control,
        process_start_stamp_reader=process_start_stamp_reader,
    )
    observed = await server.call_tool(
        "get_window_state",
        {"session": "ses-a", "pid": 7, "window_id": 9},
    )
    assert not observed.get("isError")

    result = await server.call_tool(
        "click",
        {
            "session": "ses-a",
            "pid": 7,
            "window_id": 9,
            "x": 1,
            "y": 2,
            "capture_id": "capture-7-9",
            "delivery_mode": "foreground",
        },
    )

    assert result["structuredContent"]["code"] == "target_not_admissible"
    assert result["structuredContent"]["params"]["foreground_outcome"] == "unattempted"
    assert control.calls == []


@pytest.mark.asyncio
async def test_foreground_restore_rejects_reused_prior_frontmost_process(
    tmp_path: Path,
) -> None:
    state_path, state = _state(tmp_path)
    upstream = ForegroundUpstream()
    control = FakeForegroundControl()
    reads: dict[int, int] = {}

    def process_start_stamp_reader(pid: int) -> str:
        reads[pid] = reads.get(pid, 0) + 1
        if pid == 3 and reads[pid] > 2:
            return "replacement-3"
        return f"start-{pid}"

    server = _foreground_server(
        state_path,
        state,
        upstream,
        control,
        process_start_stamp_reader=process_start_stamp_reader,
    )
    await server.call_tool(
        "get_window_state",
        {"session": "ses-a", "pid": 7, "window_id": 9},
    )

    result = await server.call_tool(
        "click",
        {
            "session": "ses-a",
            "pid": 7,
            "window_id": 9,
            "x": 1,
            "y": 2,
            "capture_id": "capture-7-9",
            "delivery_mode": "foreground",
        },
    )

    restoration = result["structuredContent"]["params"]["restoration"]
    assert result["structuredContent"]["code"] == "foreground_restore_failed"
    assert restoration["components"]["prior_frontmost"]["verified"] is False
    assert "identity" in restoration["components"]["prior_frontmost"]["failure_detail"]
    assert [name for name, _ in control.calls] == [
        "request_consent",
        "set_active",
        "restore",
        "terminal",
    ]


def test_foreground_click_shape_is_one_exact_press() -> None:
    base = {
        "pid": 7,
        "window_id": 9,
        "delivery_mode": "foreground",
    }
    _validate_foreground_input(
        "click",
        {
            **base,
            "element_token": "snapshot-7-9:element-0",
            "action": "press",
        },
        platform="darwin",
    )
    _validate_foreground_input(
        "click",
        {
            **base,
            "x": 1,
            "y": 2,
            "capture_id": "capture-7-9",
        },
        platform="darwin",
    )
    for arguments in (
        {**base, "element_token": "snapshot-7-9:element-0", "action": "open"},
        {**base, "x": 1, "y": 2},
        {
            **base,
            "x": 1,
            "y": 2,
            "capture_id": "capture-7-9",
            "element_token": "snapshot-7-9:element-0",
        },
    ):
        with pytest.raises(ComputerServerError):
            _validate_foreground_input("click", arguments, platform="darwin")


def test_prior_frontmost_requires_a_visible_current_space_window() -> None:
    frontmost = _frontmost_window(
        {
            "windows": [
                {
                    "pid": 8,
                    "window_id": 80,
                    "is_on_screen": False,
                    "minimized": True,
                    "on_current_space": True,
                    "z_index": 30,
                },
                {
                    "pid": 3,
                    "window_id": 30,
                    "is_on_screen": True,
                    "on_current_space": True,
                    "z_index": 20,
                },
            ]
        }
    )

    assert frontmost is not None
    assert (frontmost["pid"], frontmost["window_id"]) == (3, 30)


def test_foreground_control_socket_uses_the_private_short_directory(
    tmp_path: Path,
) -> None:
    state_directory = tmp_path / ("long-state-directory-" * 8)
    state_directory.mkdir()
    path = _foreground_control_socket_path(state_directory / "computer-use.json")

    assert path.name == "foreground-control.sock"
    assert path.parent.name.startswith(f"avibe-cua-{state_directory.stat().st_uid}-")
    assert len(os.fsencode(path)) <= 103


@pytest.mark.asyncio
async def test_foreground_restore_failure_reports_effect_without_retry(
    tmp_path: Path,
) -> None:
    state_path, state = _state(tmp_path)
    upstream = ForegroundUpstream(
        after_restore={
            "pid": 7,
            "window_id": 9,
            "app_name": "Target",
            "title": "Target Window",
            "is_on_screen": True,
            "minimized": False,
            "on_current_space": True,
            "current_space_id": 1,
            "z_index": 30,
        },
    )
    control = FakeForegroundControl()
    server = _foreground_server(state_path, state, upstream, control)
    observed = await server.call_tool(
        "get_window_state",
        {"session": "ses-a", "pid": 7, "window_id": 9},
    )
    assert not observed.get("isError")

    result = await server.call_tool(
        "click",
        {
            "session": "ses-a",
            "pid": 7,
            "window_id": 9,
            "x": 1,
            "y": 2,
            "capture_id": "capture-7-9",
            "delivery_mode": "foreground",
        },
        session_title="QQ conversation",
    )

    payload = json.loads(result["content"][0]["text"])
    assert payload["code"] == "foreground_restore_failed"
    assert payload["params"]["foreground_outcome"] == "effect_occurred_restore_failed"
    restoration = payload["params"]["restoration"]
    assert restoration["restoration_ok"] is False
    assert (
        restoration["components"]["prior_frontmost"]["failure_detail"]
        == "prior frontmost exact window identity was not restored"
    )
    assert restoration["components"]["pointer"]["verified"] is True
    assert restoration["components"]["active_space"]["verified"] is True
    assert [name for name, _ in control.calls] == [
        "request_consent",
        "set_active",
        "restore",
        "terminal",
    ]
    assert len(
        [
            params["arguments"]
            for _method, params in upstream.calls
            if params.get("name") == "click"
        ]
    ) == 1


@pytest.mark.asyncio
async def test_unexpected_restore_failure_leaves_native_attention(
    tmp_path: Path,
) -> None:
    state_path, state = _state(tmp_path)
    upstream = ForegroundUpstream()
    control = FailingRestoreControl()
    server = _foreground_server(state_path, state, upstream, control)
    await server.call_tool(
        "get_window_state",
        {"session": "ses-a", "pid": 7, "window_id": 9},
    )

    result = await server.call_tool(
        "click",
        {
            "session": "ses-a",
            "pid": 7,
            "window_id": 9,
            "x": 1,
            "y": 2,
            "capture_id": "capture-7-9",
            "delivery_mode": "foreground",
        },
    )

    assert result["structuredContent"]["code"] == "foreground_restore_failed"
    assert [name for name, _payload in control.calls] == [
        "request_consent",
        "set_active",
        "restore",
        "terminal",
    ]
    terminal = control.calls[-1][1]["restoration"]
    assert terminal == result["structuredContent"]["params"]["restoration"]
    assert terminal["restoration_ok"] is False
    assert all(
        component["failure_detail"] == "unexpected native restore failure"
        for component in terminal["components"].values()
    )


@pytest.mark.asyncio
async def test_foreground_restoration_verifies_every_component_after_window_failure(
    tmp_path: Path,
) -> None:
    state_path, state = _state(tmp_path)
    upstream = ForegroundUpstream(fail_restore_windows=True)
    control = FakeForegroundControl()
    server = _foreground_server(state_path, state, upstream, control)
    await server.call_tool(
        "get_window_state",
        {"session": "ses-a", "pid": 7, "window_id": 9},
    )

    result = await server.call_tool(
        "click",
        {
            "session": "ses-a",
            "pid": 7,
            "window_id": 9,
            "x": 1,
            "y": 2,
            "capture_id": "capture-7-9",
            "delivery_mode": "foreground",
        },
    )

    restoration = result["structuredContent"]["params"]["restoration"]
    assert result["structuredContent"]["code"] == "foreground_restore_failed"
    assert upstream.cursor_calls == 2
    assert restoration["components"]["prior_frontmost"]["verified"] is False
    assert restoration["components"]["active_space"]["verified"] is False
    assert restoration["components"]["pointer"]["verified"] is True


@pytest.mark.asyncio
async def test_foreground_restoration_verifies_space_via_prior_window(
    tmp_path: Path,
) -> None:
    state_path, state = _state(tmp_path)
    upstream = ForegroundUpstream(after_prior_space_id=2)
    control = FakeForegroundControl()
    server = _foreground_server(state_path, state, upstream, control)
    await server.call_tool(
        "get_window_state",
        {"session": "ses-a", "pid": 7, "window_id": 9},
    )

    result = await server.call_tool(
        "click",
        {
            "session": "ses-a",
            "pid": 7,
            "window_id": 9,
            "x": 1,
            "y": 2,
            "capture_id": "capture-7-9",
            "delivery_mode": "foreground",
        },
    )

    restoration = result["structuredContent"]["params"]["restoration"]
    assert restoration["restoration_ok"] is False
    assert restoration["components"]["prior_frontmost"]["verified"] is True
    assert restoration["components"]["pointer"]["verified"] is True
    assert restoration["components"]["active_space"] == {
        "recorded": {
            "prior_frontmost_display": 1,
            "target_display": 1,
        },
        "attempted": True,
        "verified": False,
        "failure_detail": "one or more involved display active Spaces were not restored",
    }


@pytest.mark.asyncio
async def test_foreground_admission_has_stable_missing_and_space_reasons(
    tmp_path: Path,
) -> None:
    for target, expected in (
        (
            {
                "pid": 7,
                "window_id": 8,
                "app_name": "Other",
                "title": "Other",
                "is_on_screen": True,
                "minimized": False,
                "on_current_space": True,
                "current_space_id": 1,
                "z_index": 10,
            },
            "target_missing",
        ),
        (
            {
                "pid": 7,
                "window_id": 9,
                "app_name": "Target",
                "title": "Target",
                "is_on_screen": True,
                "minimized": False,
                "on_current_space": False,
                "current_space_id": 2,
                "z_index": 10,
            },
            "target_off_active_space",
        ),
        (
            {
                "pid": 7,
                "window_id": 9,
                "app_name": "Target",
                "title": "Target",
                "is_on_screen": False,
                "minimized": False,
                "on_current_space": True,
                "current_space_id": 1,
                "z_index": 10,
            },
            "target_not_admissible",
        ),
    ):
        target_dir = tmp_path / expected
        target_dir.mkdir()
        state_path, state = _state(target_dir)
        upstream = ForegroundUpstream(target=target)
        control = FakeForegroundControl()
        server = _foreground_server(state_path, state, upstream, control)
        observed = await server.call_tool(
            "get_window_state",
            {"session": "ses-a", "pid": 7, "window_id": 9},
        )
        assert not observed.get("isError")
        result = await server.call_tool(
            "click",
            {
                "session": "ses-a",
                "pid": 7,
                "window_id": 9,
                "x": 1,
                "y": 2,
                "capture_id": "capture-7-9",
                "delivery_mode": "foreground",
            },
        )
        payload = json.loads(result["content"][0]["text"])
        assert payload["code"] == expected
        assert payload["params"]["foreground_outcome"] == "unattempted"


def test_foreground_rejects_other_operations_and_windows(
    tmp_path: Path,
) -> None:
    cases = (
        ("type_text", {"pid": 7, "window_id": 9, "text": "x"}, "darwin", "foreground_forbidden"),
        ("click", {"pid": 7, "window_id": 9}, "win32", "platform_unsupported"),
    )
    for index, (tool, arguments, platform, expected) in enumerate(cases):
        target_dir = tmp_path / str(index)
        target_dir.mkdir()
        state_path, state = _state(target_dir)
        server = _foreground_server(
            state_path,
            state,
            ForegroundUpstream(),
            FakeForegroundControl(),
            platform=platform,
        )
        result = asyncio.run(
            server.call_tool(
                tool,
                {"session": "ses-a", **arguments, "delivery_mode": "foreground"},
            )
        )
        payload = json.loads(result["content"][0]["text"])
        assert payload["code"] == expected


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
async def test_failed_internal_end_blocks_revival_and_element_token_input(
    tmp_path: Path,
) -> None:
    """A failed epoch handoff cannot preserve tokens from the previous holder."""

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

    first = await server.call_tool("list_apps", {"session": "ses-a"})
    assert not first.get("isError")
    now += 61.0
    handoff = await server.call_tool("list_apps", {"session": "ses-b"})
    assert not handoff.get("isError")
    ended = await server.call_tool("end_session", {"session": "ses-b"})
    assert not ended.get("isError")

    upstream.tool_errors.add("end_session")
    before = len(upstream.calls)
    stale = await server.call_tool(
        "click",
        {"session": "ses-a", "element_token": "old-element", "x": 1, "y": 2},
    )

    assert "session_unavailable" in stale["content"][0]["text"]
    assert [
        params["name"] for _method, params in upstream.calls[before:]
    ] == ["end_session"]
    assert leases.acquire(
        "ses-b",
        state.daemon_key,
        lambda: state.daemon_key,
    ).newly_claimed


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
    """A pre-forward transport failure must not strand a newly claimed lease."""

    state_path, state = _state(tmp_path)
    leases = DesktopLeaseManager(tmp_path)

    async def fail_upstream(_state: ComputerUseState) -> Any:
        raise UpstreamUnavailable("proxy failed before the first tool call")

    server = ComputerUseServer(
        state_path=state_path,
        status_reader=lambda: ComputerUseStatus("ready", None, state),
        upstream_factory=fail_upstream,
        lease_manager=leases,  # type: ignore[arg-type]
    )

    result = await server.call_tool("start_session", {"session": "ses-a"})

    assert result["isError"]
    assert "upstream_unavailable" in result["content"][0]["text"]
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

    with pytest.raises(
        ComputerServerError,
        match="Desktop computer use is held by another Avibe Session",
    ):
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
        lease_manager=DesktopLeaseManager(tmp_path),
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
        lease_manager=DesktopLeaseManager(tmp_path),
    )

    result = await server.call_tool("list_apps", {"session": "ses-a"})

    assert result["isError"]
    assert "daemon_generation_changed" in result["content"][0]["text"]
    assert len(upstreams) == 2
    assert all(upstream.calls == [] for upstream in upstreams)
    assert all(upstream.close_calls == 1 for upstream in upstreams)


@pytest.mark.asyncio
async def test_new_claim_is_released_after_same_key_generation_guard(
    tmp_path: Path,
) -> None:
    """A newly claimed lease cannot survive a pre-forward readiness change."""

    state_path, state = _state(tmp_path)
    leases = DesktopLeaseManager(tmp_path)
    status_reads = 0

    def status_reader() -> ComputerUseStatus:
        nonlocal status_reads
        status_reads += 1
        if status_reads in {3, 4}:
            return ComputerUseStatus("unavailable", "daemon_unreachable")
        return ComputerUseStatus("ready", None, state)

    server = ComputerUseServer(
        state_path=state_path,
        status_reader=status_reader,
        upstream_factory=lambda _state: asyncio.sleep(0, result=FakeUpstream()),
        lease_manager=leases,
    )

    result = await server.call_tool("list_apps", {"session": "ses-a"})

    assert result["isError"]
    assert "computer_use_unavailable" in result["content"][0]["text"]
    assert leases.acquire(
        "ses-b",
        state.daemon_key,
        lambda: state.daemon_key,
    ).newly_claimed


@pytest.mark.asyncio
async def test_readiness_blip_keeps_a_renewed_lease_epoch(
    tmp_path: Path,
) -> None:
    """A same-key not-ready read must not release and reclaim an existing holder."""

    state_path, state = _state(tmp_path)
    leases = DesktopLeaseManager(tmp_path)
    blip = False
    blip_reads = 0

    def status_reader() -> ComputerUseStatus:
        nonlocal blip_reads
        if blip:
            blip_reads += 1
            if blip_reads == 3:
                return ComputerUseStatus("unavailable", "daemon_unreachable")
        return ComputerUseStatus("ready", None, state)

    server = ComputerUseServer(
        state_path=state_path,
        status_reader=status_reader,
        upstream_factory=lambda _state: asyncio.sleep(0, result=FakeUpstream()),
        lease_manager=leases,
    )

    first = await server.call_tool("list_apps", {"session": "ses-a"})
    assert not first.get("isError")
    assert leases.acquire(
        "ses-a",
        state.daemon_key,
        lambda: state.daemon_key,
    ).lease.epoch == 1

    blip = True
    second = await server.call_tool("list_apps", {"session": "ses-a"})

    assert not second.get("isError")
    assert blip_reads >= 6
    assert leases.acquire(
        "ses-a",
        state.daemon_key,
        lambda: state.daemon_key,
    ).lease.epoch == 1
    with pytest.raises(
        ComputerServerError,
        match="Desktop computer use is held by another Avibe Session",
    ):
        leases.acquire(
            "ses-b",
            state.daemon_key,
            lambda: state.daemon_key,
        )


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
    with pytest.raises(
        ComputerServerError,
        match="Desktop computer use is held by another Avibe Session",
    ):
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


def test_desktop_busy_does_not_disclose_the_current_session(
    tmp_path: Path,
) -> None:
    manager = DesktopLeaseManager(tmp_path)
    manager.acquire(
        "secret-session-id",
        ("shell-a", 1),
        lambda: ("shell-a", 1),
    )

    with pytest.raises(ComputerServerError) as raised:
        manager.acquire(
            "other-session",
            ("shell-a", 1),
            lambda: ("shell-a", 1),
        )

    assert raised.value.code == "desktop_busy"
    assert str(raised.value) == "Desktop computer use is held by another Avibe Session."
    assert "secret-session-id" not in str(raised.value)


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
async def test_heartbeat_starts_before_proxy_setup_finishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Proxy setup cannot age a newly acquired lease past its TTL."""

    monkeypatch.setattr("core.computer_server._LEASE_HEARTBEAT_SECONDS", 0.01)
    now = 100.0
    leases = DesktopLeaseManager(tmp_path, now=lambda: now, ttl_seconds=60.0)
    state_path, state = _state(tmp_path)
    setup_started = asyncio.Event()
    finish_setup = asyncio.Event()

    async def factory(_state: ComputerUseState) -> FakeUpstream:
        setup_started.set()
        await finish_setup.wait()
        return FakeUpstream()

    server = ComputerUseServer(
        state_path=state_path,
        status_reader=lambda: ComputerUseStatus("ready", None, state),
        upstream_factory=factory,
        lease_manager=leases,
    )
    call = asyncio.create_task(
        server.call_tool("list_apps", {"session": "ses-a"})
    )
    await asyncio.wait_for(setup_started.wait(), timeout=1)
    now += 61.0
    await asyncio.sleep(0.03)

    with pytest.raises(
        ComputerServerError,
        match="Desktop computer use is held by another Avibe Session",
    ):
        leases.acquire(
            "ses-b",
            state.daemon_key,
            lambda: state.daemon_key,
        )

    finish_setup.set()
    result = await asyncio.wait_for(call, timeout=1)
    assert not result.get("isError")


@pytest.mark.asyncio
async def test_lease_loss_during_session_setup_aborts_before_user_forward(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 180-second-capable session call cannot outlive desktop ownership."""

    monkeypatch.setattr("core.computer_server._LEASE_HEARTBEAT_SECONDS", 0.01)
    now = 100.0
    leases = DesktopLeaseManager(tmp_path, now=lambda: now, ttl_seconds=60.0)
    state_path, state = _state(tmp_path)
    upstream = FakeUpstream()
    upstream.block = asyncio.Event()
    upstream.blocked_tools = {"start_session"}
    server = ComputerUseServer(
        state_path=state_path,
        status_reader=lambda: ComputerUseStatus("ready", None, state),
        upstream_factory=lambda _state: asyncio.sleep(0, result=upstream),
        lease_manager=leases,
    )
    call = asyncio.create_task(
        server.call_tool("list_apps", {"session": "ses-a"})
    )
    for _attempt in range(100):
        if upstream.calls:
            break
        await asyncio.sleep(0.005)
    assert [params["name"] for _method, params in upstream.calls] == [
        "start_session"
    ]

    now += 61.0
    assert leases.acquire(
        "ses-b",
        state.daemon_key,
        lambda: state.daemon_key,
    ).newly_claimed
    result = await asyncio.wait_for(call, timeout=1)

    assert "desktop_lease_lost" in result["content"][0]["text"]
    assert [params["name"] for _method, params in upstream.calls] == [
        "start_session"
    ]
    assert upstream.close_calls == 1


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
    for _attempt in range(100):
        if stdin.writes:
            break
        await asyncio.sleep(0)
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
